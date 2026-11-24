# fluxdiff/rag/repos.py
"""
Repository registry for the chat: index several repositories side by side
and choose one per question.

Each repository is either
  local  — a git clone on this machine (a folder path), or
  github — a GitHub repository read over the API (owner/repo), no clone.

Every repository gets its own vector store under
    <vector_db_path>/repos/<repo_id>/
and is listed in <vector_db_path>/repos.json.  An index built with the
command-line ingester (directly in <vector_db_path>) appears as the
repository "default", so existing setups keep working.

Indexing runs in a background thread, one repository at a time; its status
("indexing" → "ready" | "error") and latest progress message are stored on
the repository record for the UI to poll.

Local folders can be indexed from the browser only while
FLUXLINK_ALLOW_LOCAL_REPOS is true (the default).  Set it to 0 when the chat
API is reachable by other people, so they cannot make it read folders on
this machine.

FLUXLINK_READONLY=1 (set by the Dockerfile for hosted demos) serves the
indexes that exist and refuses to add, re-index or remove repositories, so
visitors cannot spend the API quota or change the demo.

Command line (build or ship indexes without the UI):
    python -m fluxdiff.rag.repos list
    python -m fluxdiff.rag.repos add github esp-rs/esp-rust-board [--ref BRANCH]
    python -m fluxdiff.rag.repos add local D:/boards/my-board
    python -m fluxdiff.rag.repos export deploy/index [REPO_ID ...]
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from fluxdiff.rag.config import RAG_CONFIG

DEFAULT_REPO_ID = "default"
KINDS = ("local", "github")


class RepoError(ValueError):
    """Bad request (unknown repo, invalid location, ...)."""


class RepoBusy(RuntimeError):
    """Another repository is being indexed."""


class LocalReposDisabled(PermissionError):
    pass


class RepoReadOnly(PermissionError):
    pass


def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() not in ("0", "false", "no", "")


def local_repos_allowed() -> bool:
    return _flag("FLUXLINK_ALLOW_LOCAL_REPOS", "1") and not readonly()


def readonly() -> bool:
    return _flag("FLUXLINK_READONLY", "0")


def _check_writable():
    if readonly():
        raise RepoReadOnly("This FluxLink server is read-only (FLUXLINK_READONLY=1); "
                           "repositories cannot be added, re-indexed or removed here.")


@dataclass
class Repo:
    id: str
    kind: str              # "local" | "github" | "cli"
    location: str          # folder path, or owner/repo
    label: str
    ref: str = ""          # GitHub branch/tag as requested ("" = default branch)
    branch: str = ""       # the branch/tag actually read (ref, or the default branch)
    status: str = "new"    # "new" | "indexing" | "ready" | "error"
    message: str = ""
    documents: int = 0
    max_commits: int = 50
    embedding: str = ""    # "provider:model" the index was built with
    updated: str = ""


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "repo"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RepoManager:
    def __init__(self, root: str = None,
                 embedder_factory: Callable = None,
                 github_http=None):
        self.root = root or RAG_CONFIG["vector_db_path"]
        self._embedder_factory = embedder_factory
        self._github_http = github_http        # tests inject a fake GitHub
        self._lock = threading.RLock()
        self._job: Optional[threading.Thread] = None
        os.makedirs(self.root, exist_ok=True)
        self._registry = os.path.join(self.root, "repos.json")
        self._repos: Dict[str, Repo] = self._load()

    # ------------------------------------------------------------------
    # Registry
    # ------------------------------------------------------------------

    def _load(self) -> Dict[str, Repo]:
        try:
            with open(self._registry, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return {}
        repos = {}
        for item in raw:
            try:
                repo = Repo(**item)
            except TypeError:
                continue
            if repo.status == "indexing":       # server stopped mid-job
                repo.status, repo.message = "error", "Indexing was interrupted; index again."
            repos[repo.id] = repo
        return repos

    def _save(self):
        tmp = self._registry + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump([asdict(r) for r in self._repos.values()], f, indent=2)
        os.replace(tmp, self._registry)

    def store_path(self, repo_id: str) -> str:
        if repo_id == DEFAULT_REPO_ID:
            return self.root
        if repo_id not in self._repos:
            raise RepoError(f"Unknown repository: {repo_id}")
        return os.path.join(self.root, "repos", repo_id)

    def _default_repo(self) -> Optional[Repo]:
        if not os.path.exists(os.path.join(self.root, "documents.pkl")):
            return None
        return Repo(id=DEFAULT_REPO_ID, kind="cli", location=RAG_CONFIG["repo_path"],
                    label="Command-line index", status="ready")

    def list(self) -> List[dict]:
        with self._lock:
            repos = [asdict(r) for r in self._repos.values()]
        default = self._default_repo()
        return ([asdict(default)] if default else []) + sorted(repos, key=lambda r: r["label"].lower())

    def get(self, repo_id: str) -> dict:
        if repo_id == DEFAULT_REPO_ID and self._default_repo():
            return asdict(self._default_repo())
        with self._lock:
            if repo_id not in self._repos:
                raise RepoError(f"Unknown repository: {repo_id}")
            return asdict(self._repos[repo_id])

    def is_busy(self) -> bool:
        return self._job is not None and self._job.is_alive()

    # ------------------------------------------------------------------
    # Add / re-index / remove
    # ------------------------------------------------------------------

    def _describe(self, kind: str, location: str, ref: str):
        location = (location or "").strip()
        if not location:
            raise RepoError("Enter a folder path or a GitHub repository.")
        if kind == "local":
            if not local_repos_allowed():
                raise LocalReposDisabled(
                    "Indexing local folders is disabled on this server (FLUXLINK_ALLOW_LOCAL_REPOS=0).")
            path = os.path.abspath(os.path.expanduser(location))
            if not os.path.isdir(path):
                raise RepoError(f"Folder not found: {path}")
            try:
                subprocess.check_output(["git", "-C", path, "rev-parse", "--git-dir"],
                                        stderr=subprocess.STDOUT)
            except (subprocess.CalledProcessError, OSError):
                raise RepoError(f"Not a git repository: {path}")
            digest = hashlib.sha1(os.path.normcase(path).encode("utf-8")).hexdigest()[:8]
            name = os.path.basename(path.rstrip("\\/")) or path
            return f"local-{_slug(name)}-{digest}", path, name
        if kind == "github":
            from fluxdiff.rag.ingest.github_source import GitHubError, parse_repo
            try:
                owner, name = parse_repo(location)
            except GitHubError as e:
                raise RepoError(str(e))
            spec = f"{owner}/{name}"
            repo_id = "github-" + _slug(spec + (f"-{ref}" if ref else ""))
            return repo_id, spec, spec + (f" @ {ref}" if ref else "")
        raise RepoError(f"Unknown kind {kind!r}; use 'local' or 'github'.")

    def add(self, kind: str, location: str, ref: str = "", max_commits: int = 50,
            background: bool = True) -> dict:
        """Register (or re-index) a repository and start indexing it."""
        _check_writable()
        ref = (ref or "").strip()
        max_commits = max(1, min(int(max_commits or 50), 500))
        repo_id, normalized, label = self._describe(kind, location, ref)
        with self._lock:
            if self.is_busy():
                raise RepoBusy("Another repository is being indexed; try again when it finishes.")
            repo = self._repos.get(repo_id) or Repo(id=repo_id, kind=kind,
                                                    location=normalized, label=label, ref=ref)
            repo.max_commits = max_commits
            repo.status, repo.message, repo.updated = "indexing", "Starting…", _now()
            self._repos[repo_id] = repo
            self._save()
            if background:
                self._job = threading.Thread(target=self._index, args=(repo_id,), daemon=True)
                self._job.start()
        if not background:
            self._index(repo_id)
        return self.get(repo_id)

    def reindex(self, repo_id: str, background: bool = True) -> dict:
        repo = self.get(repo_id)
        if repo["kind"] not in KINDS:
            raise RepoError("The command-line index is updated with run_ingest.")
        return self.add(repo["kind"], repo["location"], repo["ref"], repo["max_commits"],
                        background=background)

    def remove(self, repo_id: str):
        _check_writable()
        with self._lock:
            if repo_id == DEFAULT_REPO_ID:
                raise RepoError("The command-line index is managed with run_ingest --clear.")
            if repo_id not in self._repos:
                raise RepoError(f"Unknown repository: {repo_id}")
            if self.is_busy() and self._repos[repo_id].status == "indexing":
                raise RepoBusy("This repository is being indexed.")
            path = self.store_path(repo_id)
            del self._repos[repo_id]
            self._save()
        shutil.rmtree(path, ignore_errors=True)

    # ------------------------------------------------------------------
    # Indexing job
    # ------------------------------------------------------------------

    def _update(self, repo_id: str, **fields):
        with self._lock:
            repo = self._repos.get(repo_id)
            if repo is None:
                return
            for k, v in fields.items():
                setattr(repo, k, v)
            repo.updated = _now()
            self._save()

    def _index(self, repo_id: str):
        from fluxdiff.rag.embedding.vector_store import VectorStore
        from fluxdiff.rag.ingest.run_ingest import run_ingestion

        repo = self._repos[repo_id]

        def log(message: str):
            message = str(message).strip()
            if message:
                self._update(repo_id, message=message[:300])

        try:
            store = VectorStore(db_path=self.store_path(repo_id))
            if self._embedder_factory:
                embedder = self._embedder_factory()
            else:
                from fluxdiff.rag.embedding.embedder import Embedder
                embedder = Embedder()
            kwargs = dict(max_commits=repo.max_commits, embedder=embedder, store=store, log=log)
            if repo.kind == "github":
                from fluxdiff.rag.ingest.github_source import GitHubSource
                source = GitHubSource(repo.location, ref=repo.ref or None, http=self._github_http)
                self._update(repo_id, branch=source.ref)
                run_ingestion(github_source=source, **kwargs)
            else:
                run_ingestion(repo_path=repo.location, **kwargs)
            count = len(store)
            done = (f"Indexed {count} document(s)." if count
                    else "No board history found (no commits changed a .kicad_pcb file).")
            self._update(repo_id, status="ready", documents=count, message=done,
                         embedding=store.embedding or getattr(embedder, "signature", ""))
        except Exception as e:                       # reported to the UI, not raised
            self._update(repo_id, status="error", message=f"{type(e).__name__}: {e}"[:300])


    # ------------------------------------------------------------------
    # Export (ship prebuilt indexes with a deployment)
    # ------------------------------------------------------------------

    def export(self, target: str, repo_ids: Optional[List[str]] = None) -> List[str]:
        """
        Copy the indexes of ready repositories (all, or repo_ids) into target,
        with a repos.json listing only them.  Point FLUXDIFF_VECTOR_DB_PATH at
        target to serve them (the Dockerfile ships deploy/index this way).
        """
        with self._lock:
            chosen = [r for r in self._repos.values()
                      if r.status == "ready" and (not repo_ids or r.id in repo_ids)]
        missing = set(repo_ids or []) - {r.id for r in chosen}
        if missing:
            raise RepoError(f"Not ready or unknown: {', '.join(sorted(missing))}")
        if not chosen:
            raise RepoError("No indexed repositories to export.")
        target = os.path.abspath(target)
        if target == os.path.abspath(self.root):
            raise RepoError("Export target must differ from the live index folder.")
        os.makedirs(os.path.join(target, "repos"), exist_ok=True)
        for repo in chosen:
            dest = os.path.join(target, "repos", repo.id)
            shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(self.store_path(repo.id), dest)
        with open(os.path.join(target, "repos.json"), "w", encoding="utf-8") as f:
            json.dump([asdict(r) for r in chosen], f, indent=2)
        return [r.id for r in chosen]


def _main(argv=None):
    import argparse
    from dotenv import load_dotenv
    load_dotenv()

    parser = argparse.ArgumentParser(prog="python -m fluxdiff.rag.repos",
                                     description="Manage FluxLink chat repositories.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="show indexed repositories")
    add = sub.add_parser("add", help="add (or re-index) a repository, in the foreground")
    add.add_argument("kind", choices=KINDS)
    add.add_argument("location", help="owner/repo or GitHub URL, or a local folder")
    add.add_argument("--ref", default="", help="GitHub branch or tag")
    add.add_argument("--max-commits", type=int, default=50)
    exp = sub.add_parser("export", help="copy indexes to a folder to ship with a deployment")
    exp.add_argument("target")
    exp.add_argument("repo_ids", nargs="*")
    args = parser.parse_args(argv)

    manager = RepoManager()
    if args.cmd == "list":
        for r in manager.list():
            print(f"{r['id']:45s} {r['status']:9s} {r['documents']:5d} docs  "
                  f"{r.get('embedding') or '':35s} {r['label']}")
    elif args.cmd == "add":
        repo = manager.add(args.kind, args.location, args.ref, args.max_commits, background=False)
        print(f"{repo['id']}: {repo['status']} — {repo['message']}")
        if repo["status"] != "ready":
            raise SystemExit(1)
    elif args.cmd == "export":
        ids = manager.export(args.target, args.repo_ids or None)
        print(f"Exported {len(ids)} index(es) to {os.path.abspath(args.target)}: {', '.join(ids)}")


if __name__ == "__main__":
    _main()
