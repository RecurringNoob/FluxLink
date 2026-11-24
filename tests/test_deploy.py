"""Hosted deployment: single-URL server, read-only mode, index export."""
import http.client
import json
import socket
import threading
import time

import pytest

import test_rag                                    # installs the faiss stand-in
from test_rag import board_repo, fake_embedder     # noqa: F401  (fixture)
from fake_github import FakeGitHub
from fluxdiff.rag.embedding.vector_store import VectorStore
from fluxdiff.rag.repos import RepoError, RepoManager, RepoReadOnly, _main, local_repos_allowed
from fluxdiff.rag.server import create_app

uvicorn = pytest.importorskip("uvicorn")


# --------------------------------------------------------------------------
# server.py — run for real on a local port, with a stand-in for the chat API
# --------------------------------------------------------------------------

async def stub_api(scope, receive, send):
    """Minimal ASGI app echoing the path it received (as the mounted API)."""
    if scope["type"] != "http":
        return
    body = json.dumps({"path": scope["path"], "root_path": scope.get("root_path", "")}).encode()
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": body})


@pytest.fixture
def serve(tmp_path):
    servers = []

    def start(dist):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        config = uvicorn.Config(create_app(api_app=stub_api, dist=str(dist)),
                                host="127.0.0.1", port=port, log_level="error")
        server = uvicorn.Server(config)
        threading.Thread(target=server.run, daemon=True).start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        servers.append(server)
        return port

    yield start
    for s in servers:
        s.should_exit = True


def get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    return resp.status, dict(resp.getheaders()), resp.read().decode()


@pytest.fixture
def dist(tmp_path):
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text("<html>FluxLink SPA</html>")
    (d / "assets" / "app.js").write_text("console.log('app')")
    (tmp_path / "secret.txt").write_text("do not serve")
    return d


def test_root_redirects_to_chat(serve, dist):
    status, headers, _ = get(serve(dist), "/")
    assert status in (302, 307) and headers["location"] == "/chat"


def test_spa_routes_and_assets(serve, dist):
    port = serve(dist)
    status, _, body = get(port, "/chat")
    assert status == 200 and body == "<html>FluxLink SPA</html>"
    assert get(port, "/assets/app.js")[2] == "console.log('app')"
    assert get(port, "/some/client/route")[2] == "<html>FluxLink SPA</html>"


def test_files_outside_dist_are_not_served(serve, dist):
    status, _, body = get(serve(dist), "/..%2Fsecret.txt")
    assert "do not serve" not in body


def test_api_is_mounted_under_api(serve, dist):
    status, _, body = get(serve(dist), "/api/repos")
    data = json.loads(body)
    assert status == 200 and data["path"].endswith("/repos") and data["root_path"].endswith("/api")


def test_missing_frontend_build_is_explained(serve, tmp_path):
    status, _, body = get(serve(tmp_path / "nope"), "/chat")
    assert status == 404 and "npm run build" in body


# --------------------------------------------------------------------------
# Read-only mode and export
# --------------------------------------------------------------------------

@pytest.fixture
def manager(tmp_path, board_repo, monkeypatch):
    for k in ("FLUXLINK_READONLY", "FLUXLINK_ALLOW_LOCAL_REPOS", "FLUXLINK_ERP_DEMO"):
        monkeypatch.delenv(k, raising=False)
    return RepoManager(root=str(tmp_path / "db"), embedder_factory=fake_embedder,
                       github_http=FakeGitHub(board_repo))


def test_readonly_blocks_changes_but_serves_indexes(manager, monkeypatch):
    repo = manager.add("github", "acme/board", background=False)
    monkeypatch.setenv("FLUXLINK_READONLY", "1")
    assert not local_repos_allowed()
    for action in (lambda: manager.add("github", "acme/other", background=False),
                   lambda: manager.reindex(repo["id"], background=False),
                   lambda: manager.remove(repo["id"])):
        with pytest.raises(RepoReadOnly):
            action()
    assert manager.get(repo["id"])["status"] == "ready"
    assert len(VectorStore(db_path=manager.store_path(repo["id"]))) == repo["documents"]


def test_export_ships_ready_indexes(manager, tmp_path, board_repo):
    ready = manager.add("github", "acme/board", background=False)
    manager.add("local", str(board_repo), background=False)
    target = tmp_path / "deploy_index"
    assert manager.export(str(target), [ready["id"]]) == [ready["id"]]

    shipped = RepoManager(root=str(target), embedder_factory=fake_embedder)
    assert [r["id"] for r in shipped.list()] == [ready["id"]]
    store = VectorStore(db_path=shipped.store_path(ready["id"]))
    assert len(store) == ready["documents"] and store.embedding == ready["embedding"]


def test_export_errors(manager, tmp_path):
    with pytest.raises(RepoError, match="No indexed repositories"):
        manager.export(str(tmp_path / "x"))
    manager.add("github", "acme/board", background=False)
    with pytest.raises(RepoError, match="Not ready or unknown"):
        manager.export(str(tmp_path / "x"), ["nope"])
    with pytest.raises(RepoError, match="must differ"):
        manager.export(manager.root)


def test_cli_list_and_export(manager, tmp_path, monkeypatch, capsys):
    manager.add("github", "acme/board", background=False)
    monkeypatch.setenv("FLUXDIFF_VECTOR_DB_PATH", manager.root)
    monkeypatch.setitem(__import__("fluxdiff.rag.config", fromlist=["x"]).RAG_CONFIG,
                        "vector_db_path", manager.root)
    _main(["list"])
    assert "github-acme-board" in capsys.readouterr().out
    _main(["export", str(tmp_path / "out")])
    assert "Exported 1 index(es)" in capsys.readouterr().out
    assert (tmp_path / "out" / "repos.json").exists()
