"""Repository registry (rag/repos.py) and per-repository chat retrieval."""
import json
import threading
from pathlib import Path

import pytest

import test_rag                                   # installs the faiss stand-in
from test_rag import (RecordingLLM, board_repo, doc, fake_embedder,  # noqa: F401  (fixture)
                      _git)
from fake_github import FakeGitHub
from fluxdiff.rag.chat.chat_engine import ChatEngine
from fluxdiff.rag.embedding.vector_store import VectorStore
from fluxdiff.rag.repos import (DEFAULT_REPO_ID, LocalReposDisabled, RepoBusy, RepoError,
                                RepoManager)
from fluxdiff.rag.retrieval.retriever import Retriever


@pytest.fixture
def manager(tmp_path, board_repo, monkeypatch):
    monkeypatch.delenv("FLUXLINK_ERP_DEMO", raising=False)
    monkeypatch.delenv("FLUXLINK_ALLOW_LOCAL_REPOS", raising=False)
    return RepoManager(root=str(tmp_path / "db"), embedder_factory=fake_embedder,
                       github_http=FakeGitHub(board_repo))


def test_local_repo_is_indexed(manager, board_repo):
    repo = manager.add("local", str(board_repo), background=False)
    assert repo["status"] == "ready" and repo["documents"] > 0, repo
    assert repo["id"].startswith("local-repo-")
    store = VectorStore(db_path=manager.store_path(repo["id"]))
    assert {d.metadata["type"] for d in store.documents} >= {"summary", "net", "repo_file"}
    assert [r["id"] for r in manager.list()] == [repo["id"]]


def test_github_repo_is_indexed_without_clone(manager, board_repo):
    repo = manager.add("github", "https://github.com/acme/board", background=False)
    assert repo["status"] == "ready", repo
    assert repo["id"] == "github-acme-board" and repo["label"] == "acme/board"
    assert repo["ref"] == ""
    assert repo["branch"] == _git(board_repo, "rev-parse", "--abbrev-ref", "HEAD")


def test_reindex_reuses_the_same_index(manager, board_repo):
    first = manager.add("github", "acme/board", background=False)
    again = manager.reindex(first["id"], background=False)
    assert again["id"] == first["id"] and again["documents"] == first["documents"]
    assert len(manager.list()) == 1


@pytest.mark.parametrize("kind,location,error", [
    ("local", "/definitely/not/here", "Folder not found"),
    ("github", "not a repo", "expected owner/repo"),
    ("svn", "x", "Unknown kind"),
    ("github", "   ", "Enter a folder path"),
])
def test_invalid_locations(manager, kind, location, error):
    with pytest.raises(RepoError, match=error):
        manager.add(kind, location, background=False)


def test_plain_folder_is_not_a_git_repo(manager, tmp_path):
    folder = tmp_path / "plain"
    folder.mkdir()
    with pytest.raises(RepoError, match="Not a git repository"):
        manager.add("local", str(folder), background=False)


def test_local_repos_can_be_disabled(manager, board_repo, monkeypatch):
    monkeypatch.setenv("FLUXLINK_ALLOW_LOCAL_REPOS", "0")
    with pytest.raises(LocalReposDisabled):
        manager.add("local", str(board_repo), background=False)
    assert manager.add("github", "acme/board", background=False)["status"] == "ready"


def test_one_indexing_job_at_a_time(tmp_path, board_repo):
    release = threading.Event()

    def slow_embedder():
        release.wait(10)
        return fake_embedder()

    m = RepoManager(root=str(tmp_path / "db"), embedder_factory=slow_embedder,
                    github_http=FakeGitHub(board_repo))
    started = m.add("github", "acme/board")
    assert started["status"] == "indexing" and m.is_busy()
    with pytest.raises(RepoBusy):
        m.add("local", str(board_repo))
    release.set()
    m._job.join(10)
    assert m.get(started["id"])["status"] == "ready"


def test_failures_are_reported_on_the_repo(tmp_path, board_repo):
    def no_key():
        raise RuntimeError("OPENAI_API_KEY is not set")
    m = RepoManager(root=str(tmp_path / "db"), embedder_factory=no_key,
                    github_http=FakeGitHub(board_repo))
    repo = m.add("github", "acme/board", background=False)
    assert repo["status"] == "error" and "OPENAI_API_KEY" in repo["message"]


def test_registry_survives_restart_and_interrupted_jobs_become_errors(manager, tmp_path):
    repo = manager.add("github", "acme/board", background=False)
    registry = Path(manager.root) / "repos.json"
    data = json.loads(registry.read_text())
    data[0]["status"] = "indexing"                  # as if the server died mid-job
    registry.write_text(json.dumps(data))
    reloaded = RepoManager(root=manager.root, embedder_factory=fake_embedder)
    again = reloaded.get(repo["id"])
    assert again["status"] == "error" and "interrupted" in again["message"]


def test_remove_deletes_the_index(manager):
    repo = manager.add("github", "acme/board", background=False)
    path = Path(manager.store_path(repo["id"]))
    assert path.exists()
    manager.remove(repo["id"])
    assert not path.exists() and manager.list() == []
    with pytest.raises(RepoError, match="Unknown repository"):
        manager.get(repo["id"])


def test_command_line_index_is_listed_as_default(manager):
    store = VectorStore(db_path=manager.root)
    store.add_documents([doc("x")], fake_embedder().embed_documents([doc("x")]))
    listed = manager.list()
    assert listed[0]["id"] == DEFAULT_REPO_ID and listed[0]["kind"] == "cli"
    assert manager.store_path(DEFAULT_REPO_ID) == manager.root


def test_chat_uses_the_selected_repository(tmp_path):
    emb = fake_embedder()
    stores = {}
    for name in ("alpha", "beta"):
        s = VectorStore(db_path=str(tmp_path / name))
        d = [doc(f"Commit: c\\nType: Net\\nChanges: only-in-{name}", commit=name * 5, type="net")]
        s.add_documents(d, emb.embed_documents(d))
        stores[name] = s
    default = VectorStore(db_path=str(tmp_path / "default"))
    llm = RecordingLLM()
    engine = ChatEngine(retriever=Retriever(embedder=emb, store=default), llm=llm,
                        store_for=lambda rid: stores[rid])

    engine.ask("what changed?", repo_id="alpha")
    assert "only-in-alpha" in llm.prompts[-1] and "only-in-beta" not in llm.prompts[-1]
    reply = engine.ask("what changed?", repo_id="beta")
    assert "only-in-beta" in llm.prompts[-1] and reply.sources[0]["commit"] == "beta" * 5
    engine.ask("what changed?")                      # no repo: default index (empty)
    assert "only-in" not in llm.prompts[-1]


def test_api_repo_endpoints(tmp_path, board_repo, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    import fluxdiff.rag.api as api

    monkeypatch.delenv("FLUXLINK_ERP_DEMO", raising=False)
    m = RepoManager(root=str(tmp_path / "db"), embedder_factory=fake_embedder,
                    github_http=FakeGitHub(board_repo))
    monkeypatch.setattr(api, "repo_manager", m)
    client = TestClient(api.app)

    assert client.get("/repos").json()["repos"] == []
    started = client.post("/repos", json={"kind": "github", "location": "acme/board"})
    assert started.status_code == 202
    m._job.join(10)
    repo = client.get(f"/repos/{started.json()['id']}").json()
    assert repo["status"] == "ready"
    assert client.post("/repos", json={"kind": "github", "location": "nope"}).status_code == 400
    assert client.get("/repos/unknown").status_code == 404

    engine, _ = test_rag._engine(tmp_path)
    engine._store_for = lambda rid: VectorStore(db_path=m.store_path(rid))
    monkeypatch.setattr(api, "chat_engine", engine)
    answer = client.post("/chat", json={"query": "what changed?", "repo_id": repo["id"]})
    assert answer.status_code == 200 and answer.json()["sources"]
    assert client.post("/chat", json={"query": "x", "repo_id": "unknown"}).status_code == 404
    assert client.delete(f"/repos/{repo['id']}").json() == {"status": "ok"}
