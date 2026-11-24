# fluxdiff/rag/ingest/github_source.py
"""
Read a repository's board history from GitHub, without cloning it.

GitHubSource offers the same methods run_ingest uses on GitLoader
(get_commits, get_first_parent, get_changed_pcb_files, find_pcb_files) plus
read_file(), which DiffGenerator uses instead of `git show`.

Requests used (no third-party packages):
  GET /repos/{owner}/{repo}                         default branch (only if no ref given)
  GET /repos/{owner}/{repo}/git/trees/{ref}?recursive=1   find *.kicad_pcb files
  GET /repos/{owner}/{repo}/commits?sha=&path=      commits that changed each board file
  raw.githubusercontent.com/{owner}/{repo}/{sha}/{path}   file contents at a commit

A typical run makes 3–5 API calls, well inside GitHub's limit of 60 per hour
without a token.  Set GITHUB_TOKEN for private repositories or a higher limit.
Board files that were renamed or deleted before `ref` are not found, because
board files are discovered from the tree at `ref`.
"""

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from fluxdiff.rag.schemas import CommitInfo

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
PCB_SUFFIX = ".kicad_pcb"

# http(url, headers) -> (status_code, body_bytes)
Http = Callable[[str, Dict[str, str]], Tuple[int, bytes]]


class GitHubError(RuntimeError):
    pass


def parse_repo(spec: str) -> Tuple[str, str]:
    """'owner/repo', 'https://github.com/owner/repo(.git)' or 'github.com/owner/repo' -> (owner, repo)."""
    s = spec.strip()
    s = re.sub(r"^(?:https?://)?(?:www\.)?github\.com/", "", s)
    s = re.sub(r"\.git$", "", s.rstrip("/"))
    parts = s.split("/")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise GitHubError(f"Not a GitHub repository: {spec!r} (expected owner/repo)")
    return parts[0], parts[1]


def _urllib_http(url: str, headers: Dict[str, str]) -> Tuple[int, bytes]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() or b""


class GitHubSource:
    def __init__(self, repo: str, ref: Optional[str] = None,
                 token: Optional[str] = None, http: Optional[Http] = None):
        self.owner, self.name = parse_repo(repo)
        self.token = token if token is not None else os.getenv("GITHUB_TOKEN")
        self._http = http or _urllib_http
        self.ref = ref or self._default_branch()
        self._parents: Dict[str, List[str]] = {}
        self._pcb_paths_by_commit: Dict[str, List[str]] = {}
        self._pcb_files: Optional[List[str]] = None

    @property
    def label(self) -> str:
        return f"github.com/{self.owner}/{self.name}@{self.ref}"

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _headers(self, raw: bool) -> Dict[str, str]:
        h = {"User-Agent": "fluxlink-ingest",
             "Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _api(self, endpoint: str, **params):
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        url = f"{API}/repos/{self.owner}/{self.name}{endpoint}" + (f"?{query}" if query else "")
        status, body = self._http(url, self._headers(raw=False))
        if status == 200:
            return json.loads(body.decode("utf-8"))
        message = ""
        try:
            message = json.loads(body.decode("utf-8")).get("message", "")
        except (ValueError, AttributeError):
            pass
        if status in (403, 429) and "rate limit" in message.lower():
            raise GitHubError("GitHub API rate limit reached; set GITHUB_TOKEN and retry.")
        if status == 404:
            raise GitHubError(f"{self.owner}/{self.name} not found on GitHub "
                              f"(private repositories need GITHUB_TOKEN).")
        raise GitHubError(f"GitHub API {endpoint} returned HTTP {status}: {message}")

    def _default_branch(self) -> str:
        return self._api("")["default_branch"]

    # ------------------------------------------------------------------
    # GitLoader-compatible interface
    # ------------------------------------------------------------------

    def find_pcb_files(self) -> List[str]:
        if self._pcb_files is None:
            tree = self._api(f"/git/trees/{urllib.parse.quote(self.ref, safe='')}", recursive=1)
            if tree.get("truncated"):
                print("[GitHub] Repository tree is truncated; some board files may be missed.")
            self._pcb_files = sorted(e["path"] for e in tree.get("tree", [])
                                     if e.get("type") == "blob" and e["path"].endswith(PCB_SUFFIX))
        return self._pcb_files

    def get_commits(self, max_count: int = 20, pcb_only: bool = True) -> List[CommitInfo]:
        """Most recent commits that changed a board file, newest first."""
        found: Dict[str, Tuple[datetime, CommitInfo]] = {}
        for path in self.find_pcb_files():
            page, seen_for_path = 1, 0
            while True:
                batch = self._api("/commits", sha=self.ref, path=path, per_page=100, page=page)
                seen_for_path += len(batch)
                for item in batch:
                    sha = item["sha"]
                    self._parents[sha] = [p["sha"] for p in item.get("parents", [])]
                    self._pcb_paths_by_commit.setdefault(sha, [])
                    if path not in self._pcb_paths_by_commit[sha]:
                        self._pcb_paths_by_commit[sha].append(path)
                    if sha not in found:
                        c = item.get("commit", {})
                        author = c.get("author") or {}
                        date = author.get("date") or ""
                        try:
                            when = datetime.fromisoformat(date.replace("Z", "+00:00"))
                        except ValueError:
                            when = datetime.min
                        found[sha] = (when, CommitInfo(
                            commit_hash=sha,
                            message=(c.get("message") or "").split("\n", 1)[0],
                            author=author.get("name"),
                            date=date,
                        ))
                if len(batch) < 100 or seen_for_path >= max_count:
                    break
                page += 1
        ordered = sorted(found.values(), key=lambda t: t[0], reverse=True)
        return [info for _, info in ordered[:max_count]]

    def get_first_parent(self, commit_hash: str) -> Optional[str]:
        parents = self._parents.get(commit_hash)
        return parents[0] if parents else None

    def get_changed_pcb_files(self, parent: str, commit_hash: str) -> List[str]:
        return list(self._pcb_paths_by_commit.get(commit_hash, []))

    def read_file(self, commit_hash: str, path: str) -> Optional[str]:
        url = f"{RAW}/{self.owner}/{self.name}/{commit_hash}/{urllib.parse.quote(path)}"
        status, body = self._http(url, self._headers(raw=True))
        if status == 404:
            return None
        if status != 200:
            raise GitHubError(f"Downloading {path} at {commit_hash[:7]} returned HTTP {status}")
        return body.decode("utf-8")
