# fluxdiff/rag/ingest/git_loader.py

import subprocess
from typing import List, Optional

from fluxdiff.rag.config import RAG_CONFIG
from fluxdiff.rag.schemas import CommitInfo

PCB_PATHSPEC = "*.kicad_pcb"


class GitLoader:
    def __init__(self, repo_path: str = None):
        self.repo_path = repo_path or RAG_CONFIG["repo_path"]

    def _run_git_command(self, command: List[str]) -> str:
        try:
            result = subprocess.check_output(
                command,
                cwd=self.repo_path,
                stderr=subprocess.STDOUT,
            )
            return result.decode("utf-8").strip()
        except subprocess.CalledProcessError as e:
            print(f"[GitLoader] Git command failed: {e.output.decode(errors='replace')}")
            return ""

    def get_commits(self, max_count: int = 20, pcb_only: bool = False) -> List[CommitInfo]:
        """
        Most recent commits, newest first.  With pcb_only=True, only commits
        that changed a .kicad_pcb file are returned.
        """
        command = [
            "git", "log",
            f"--max-count={max_count}",
            # ASCII unit/record separators cannot appear in commit subjects.
            "--pretty=format:%H%x1f%s%x1f%an%x1f%aI%x1e",   # %aI: ISO 8601, as GitHub reports it
        ]
        if pcb_only:
            command += ["--", PCB_PATHSPEC]
        output = self._run_git_command(command)
        commits = []
        for record in output.split("\x1e"):
            parts = record.strip().split("\x1f")
            if len(parts) < 4:
                continue
            commits.append(CommitInfo(
                commit_hash=parts[0],
                message=parts[1],
                author=parts[2],
                date=parts[3],
            ))
        return commits

    def get_first_parent(self, commit_hash: str) -> Optional[str]:
        """The commit's first parent, or None for a root commit."""
        output = self._run_git_command(["git", "rev-list", "--parents", "-n", "1", commit_hash])
        parts = output.split()
        return parts[1] if len(parts) > 1 else None

    def get_changed_pcb_files(self, parent: str, commit_hash: str) -> List[str]:
        """
        .kicad_pcb files that differ between parent and commit.  Uses
        `git diff` rather than `diff-tree` so merge commits are compared
        against their first parent instead of reporting nothing.
        """
        output = self._run_git_command([
            "git", "diff", "--name-only", parent, commit_hash, "--", PCB_PATHSPEC,
        ])
        return [f for f in output.split("\n") if f]

    def get_changed_files(self, commit_hash: str) -> List[str]:
        output = self._run_git_command([
            "git", "diff-tree", "--no-commit-id", "--name-only", "-r", commit_hash,
        ])
        return output.split("\n") if output else []

    def find_pcb_files(self) -> List[str]:
        """Return all .kicad_pcb files tracked by git."""
        output = self._run_git_command(["git", "ls-files", PCB_PATHSPEC])
        return [f for f in output.split("\n") if f] if output else []
