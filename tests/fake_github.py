"""
A stand-in for the GitHub REST API and raw.githubusercontent.com, answered
from a local git repository.  It implements only the requests GitHubSource
makes, with GitHub's response shapes, so ingestion can be tested offline.
"""
import json
import subprocess
import urllib.parse


class FakeGitHub:
    def __init__(self, repo_dir, owner="acme", name="board", branch=None):
        self.dir, self.owner, self.name = str(repo_dir), owner, name
        self.branch = branch or self._git("rev-parse", "--abbrev-ref", "HEAD")
        self.calls = []
        self.fail_with = None     # (status, message) to simulate errors

    def _git(self, *args, raw=False):
        out = subprocess.check_output(["git", *args], cwd=self.dir)
        return out if raw else out.decode("utf-8").strip()

    def _json(self, obj, status=200):
        return status, json.dumps(obj).encode("utf-8")

    def __call__(self, url, headers):
        self.calls.append(url)
        if self.fail_with:
            status, message = self.fail_with
            return self._json({"message": message}, status)
        parts = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parts.query))
        prefix = f"/repos/{self.owner}/{self.name}"

        if parts.netloc == "raw.githubusercontent.com":
            _, owner, name, sha, path = parts.path.split("/", 4)
            try:
                return 200, self._git("show", f"{sha}:{urllib.parse.unquote(path)}", raw=True)
            except subprocess.CalledProcessError:
                return 404, b"404: Not Found"

        if not parts.path.startswith(prefix):
            return self._json({"message": "Not Found"}, 404)
        rest = parts.path[len(prefix):]
        if rest == "":
            return self._json({"default_branch": self.branch})
        if rest.startswith("/git/trees/"):
            ref = urllib.parse.unquote(rest[len("/git/trees/"):])
            files = self._git("ls-tree", "-r", "--name-only", ref).splitlines()
            return self._json({"tree": [{"path": f, "type": "blob"} for f in files],
                               "truncated": False})
        if rest == "/commits":
            per_page, page = int(query.get("per_page", 30)), int(query.get("page", 1))
            fmt = "%H%x1f%P%x1f%an%x1f%aI%x1f%B%x1e"
            out = self._git("log", query.get("sha", self.branch), f"--format={fmt}",
                            "--", query["path"])
            items = []
            for rec in filter(None, (r.strip() for r in out.split("\x1e"))):
                sha, parents, author, date, message = rec.split("\x1f")
                items.append({"sha": sha,
                              "parents": [{"sha": p} for p in parents.split()],
                              "commit": {"message": message,
                                         "author": {"name": author, "date": date}}})
            return self._json(items[(page - 1) * per_page: page * per_page])
        return self._json({"message": "Not Found"}, 404)
