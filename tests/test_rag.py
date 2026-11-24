"""
RAG tests.  faiss and openai are replaced by small in-process stand-ins when
they are not installed, so these run without network access or API keys.
"""
import pickle
import shutil
import subprocess
import sys
import types
from pathlib import Path

import numpy as np
import pytest


class _FakeIndexFlatL2:
    """Exhaustive L2 index with the slice of the faiss API VectorStore uses."""

    def __init__(self, d):
        self.d, self.vectors = d, np.zeros((0, d), dtype="float32")

    @property
    def ntotal(self):
        return len(self.vectors)

    def add(self, x):
        self.vectors = np.vstack([self.vectors, x])

    def search(self, q, k):
        dist = ((self.vectors - q[0]) ** 2).sum(axis=1)
        order = list(np.argsort(dist)[:k])
        dists = [float(dist[i]) for i in order]
        pad = k - len(order)
        return np.array([dists + [0.0] * pad]), np.array([order + [-1] * pad])


def _install_fake_faiss():
    try:
        import faiss  # noqa: F401
        return
    except ImportError:
        pass

    def write_index(index, path):
        with open(path, "wb") as f:
            pickle.dump(index, f)

    def read_index(path):
        with open(path, "rb") as f:
            return pickle.load(f)

    fake = types.ModuleType("faiss")
    fake.IndexFlatL2 = _FakeIndexFlatL2
    fake.write_index = write_index
    fake.read_index = read_index
    sys.modules["faiss"] = fake


_install_fake_faiss()

from fluxdiff.rag.chat.chat_engine import ChatEngine                      # noqa: E402
from fluxdiff.rag.embedding.embedder import (                              # noqa: E402
    MAX_CHARS_PER_REQUEST, MAX_INPUTS_PER_REQUEST, Embedder,
)
from fluxdiff.rag.embedding.vector_store import VectorStore              # noqa: E402
from fluxdiff.rag.ingest.document_builder import chunk_documents         # noqa: E402
from fluxdiff.rag.ingest.run_ingest import run_ingestion                 # noqa: E402
from fluxdiff.rag.llm.llm_client import LLMClient, LLMError              # noqa: E402
from fluxdiff.rag.retrieval.retriever import Retriever                   # noqa: E402
from fluxdiff.rag.schemas import RAGDocument, RAGQuery                   # noqa: E402

BOARDS = Path(__file__).resolve().parent.parent / "test_boards"


# --------------------------------------------------------------------------
# Stand-ins
# --------------------------------------------------------------------------

class FakeEmbeddingsAPI:
    def __init__(self):
        self.calls = []

    def create(self, model, input):
        texts = [input] if isinstance(input, str) else list(input)
        self.calls.append(texts)
        item = lambda t: types.SimpleNamespace(embedding=_vec(t))
        return types.SimpleNamespace(data=[item(t) for t in texts])


def _vec(text):
    # Deterministic 8-d embedding: similar texts share leading characters.
    v = np.zeros(8, dtype="float32")
    for i, ch in enumerate(text[:64]):
        v[i % 8] += ord(ch) / 1000.0
    return v.tolist()


def fake_embedder():
    return Embedder(client=types.SimpleNamespace(embeddings=FakeEmbeddingsAPI()))


class RecordingLLM:
    def __init__(self, fail=False):
        self.prompts, self.fail = [], fail

    def generate_response(self, prompt):
        self.prompts.append(prompt)
        if self.fail:
            raise LLMError("boom")
        return f"answer {len(self.prompts)}"


def doc(text, **meta):
    return RAGDocument(content=text, metadata=meta)


# --------------------------------------------------------------------------
# Vector store
# --------------------------------------------------------------------------

def _store_with(tmp_path, docs):
    store = VectorStore(db_path=str(tmp_path / "db"))
    emb = fake_embedder()
    store.add_documents(docs, emb.embed_documents(docs))
    return store, emb


def test_filters_apply_before_top_k(tmp_path, monkeypatch):
    monkeypatch.setitem(__import__("fluxdiff.rag.config", fromlist=["x"]).RAG_CONFIG, "top_k", 2)
    docs = [doc(f"aaaa routing change {i}", commit="1111" + "0" * 36, type="routing")
            for i in range(10)]
    docs.append(doc("zzzz impedance finding", commit="abcdef" + "0" * 34, type="impedance"))
    store, emb = _store_with(tmp_path, docs)
    query = emb.embed_query("aaaa routing")

    assert all(d.metadata["commit"].startswith("1111") for d in store.similarity_search(query))
    hits = store.similarity_search(query, filters={"commit": "abcdef"})
    assert [d.content for d in hits] == ["zzzz impedance finding"]


def test_commit_filter_needs_four_characters(tmp_path):
    store, emb = _store_with(tmp_path, [doc("x", commit="abcdef")])
    assert store.similarity_search(emb.embed_query("x"), filters={"commit": "abc"}) == []
    assert len(store.similarity_search(emb.embed_query("x"), filters={"commit": "ABCD"})) == 1


def test_store_reloads_after_another_process_writes(tmp_path):
    reader = VectorStore(db_path=str(tmp_path / "db"))
    assert len(reader) == 0
    writer, _ = _store_with(tmp_path, [doc("one"), doc("two")])
    assert len(reader) == 2


# --------------------------------------------------------------------------
# Chat engine
# --------------------------------------------------------------------------

def _engine(tmp_path, llm=None):
    store, emb = _store_with(tmp_path, [
        doc("Commit: c1\nType: Routing Changes", commit="c1" * 20, file="a.kicad_pcb", type="routing"),
        doc("Commit: c1\nType: Net / ERC Changes", commit="c1" * 20, file="a.kicad_pcb", type="net"),
    ])
    llm = llm or RecordingLLM()
    return ChatEngine(retriever=Retriever(embedder=emb, store=store), llm=llm), llm


def test_sessions_do_not_share_memory(tmp_path):
    engine, llm = _engine(tmp_path)
    a = engine.ask("secret question from alice")
    engine.ask("question from bob", session_id="bob")
    assert "secret question from alice" not in llm.prompts[-1]

    engine.ask("follow-up", session_id=a.session_id)
    assert "secret question from alice" in llm.prompts[-1]

    engine.reset_session(a.session_id)
    engine.ask("after reset", session_id=a.session_id)
    assert "secret question from alice" not in llm.prompts[-1]


def test_sources_use_backend_keys(tmp_path):
    engine, _ = _engine(tmp_path)
    sources = engine.ask("routing").sources
    assert {s["type"] for s in sources} == {"routing", "net"}
    assert all(s["commit"] == "c1" * 20 and s["file"] == "a.kicad_pcb" for s in sources)


def test_failed_llm_call_raises_and_is_not_remembered(tmp_path):
    engine, llm = _engine(tmp_path, llm=RecordingLLM(fail=True))
    with pytest.raises(LLMError):
        engine.ask("hello", session_id="s")
    llm.fail = False
    engine.ask("again", session_id="s")
    assert "hello" not in llm.prompts[-1].split("## REPOSITORY CONTEXT")[0]


def test_llm_client_wraps_api_errors():
    def boom(**_):
        raise TimeoutError("timed out")
    client = types.SimpleNamespace(chat=types.SimpleNamespace(
        completions=types.SimpleNamespace(create=boom)))
    with pytest.raises(LLMError, match="timed out"):
        LLMClient(client=client).generate_response("hi")


# --------------------------------------------------------------------------
# Chunking and batching
# --------------------------------------------------------------------------

def test_long_documents_are_chunked_with_header():
    body = "\n".join(f"Trace added: net N{i}, layer F.Cu" for i in range(2000))
    long_doc = doc(f"Commit: abc\nType: Routing Changes\nFile: b.kicad_pcb\nChanges:\n{body}",
                   commit="abc", type="routing")
    chunks = chunk_documents([long_doc], max_chars=5000)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c.content) <= 5000
        assert c.content.startswith("Commit: abc\nType: Routing Changes")
        assert c.metadata["chunks"] == len(chunks)
    assert sum(c.content.count("Trace added") for c in chunks) == 2000


def test_single_huge_line_is_split():
    chunks = chunk_documents([doc("File: x\n\n" + "y" * 50_000)], max_chars=10_000)
    assert all(len(c.content) <= 10_000 for c in chunks)
    assert sum(c.content.count("y") for c in chunks) == 50_000


def test_embedding_requests_are_batched():
    emb = fake_embedder()
    docs = [doc("x" * 3000) for _ in range(600)]
    assert len(emb.embed_documents(docs)) == 600
    for call in emb.client.embeddings.calls:
        assert len(call) <= MAX_INPUTS_PER_REQUEST
        assert sum(map(len, call)) <= MAX_CHARS_PER_REQUEST


# --------------------------------------------------------------------------
# Ingestion against a real git repository
# --------------------------------------------------------------------------

def _git(repo, *args):
    return subprocess.check_output(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo, text=True,
    ).strip()


@pytest.fixture
def board_repo(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    shutil.copy(BOARDS / "before3.kicad_pcb", repo / "main.kicad_pcb")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "initial board")
    (repo / "notes.md").write_text("# notes\n")
    (repo / "package-lock.json").write_text("{" + '"x": 1,' * 50_000 + '"y": 2}')
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "docs only")
    shutil.copy(BOARDS / "after3.kicad_pcb", repo / "main.kicad_pcb")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "rework crystal")
    return repo


def test_ingestion_diffs_board_commits_against_parent(board_repo, tmp_path, monkeypatch):
    monkeypatch.delenv("FLUXLINK_ERP_DEMO", raising=False)
    store = VectorStore(db_path=str(tmp_path / "db"))
    emb = fake_embedder()
    added = run_ingestion(repo_path=str(board_repo), embedder=emb, store=store)
    assert added > 0

    head = _git(board_repo, "rev-parse", "HEAD")
    board_docs = [d for d in store.documents if d.metadata.get("type") != "repo_file"]
    # Only the commit that changed the board produces board documents; the
    # root commit and the docs-only commit do not.
    assert {d.metadata["commit"] for d in board_docs} == {head}
    net_doc = next(d for d in board_docs if d.metadata["type"] == "net")
    assert "Net-(C3-Pad1)" in net_doc.content

    files = {d.metadata["file"] for d in store.documents if d.metadata["type"] == "repo_file"}
    assert files == {"notes.md"}          # package-lock.json is skipped

    calls_before = len(emb.client.embeddings.calls)
    assert run_ingestion(repo_path=str(board_repo), embedder=emb, store=store) == 0
    assert len(emb.client.embeddings.calls) == calls_before   # nothing re-embedded


def test_api_module_imports_and_validates(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    import fluxdiff.rag.api as api

    engine, _ = _engine(tmp_path)
    monkeypatch.setattr(api, "chat_engine", engine)
    client = TestClient(api.app)
    assert client.post("/chat", json={"query": "  "}).status_code == 422
    first = client.post("/chat", json={"query": "routing"}).json()
    assert first["session_id"] and first["sources"]
    filtered = client.post("/chat/filtered",
                           json={"query": "x", "filters": {"type": "net"}}).json()
    assert {s["type"] for s in filtered["sources"]} == {"net"}
    assert client.delete(f"/chat/session/{first['session_id']}").json() == {"status": "ok"}
    assert client.get("/health").json()["documents_indexed"] == 2


def test_api_starts_without_openai_key(monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    import fluxdiff.rag.api as api

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(api, "chat_engine", None)

    def no_key():
        raise RuntimeError("The api_key client option must be set")
    monkeypatch.setattr(api, "ChatEngine", no_key)

    client = TestClient(api.app)
    resp = client.post("/chat", json={"query": "hello"})
    assert resp.status_code == 503
    assert "OPENAI_API_KEY" in resp.json()["detail"]
    assert client.get("/health").status_code == 503
