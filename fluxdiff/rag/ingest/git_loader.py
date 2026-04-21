# fluxdiff/rag/ingest/git_loader.py

import subprocess
from typing import List

from fluxdiff.rag.config import RAG_CONFIG
from fluxdiff.rag.schemas import CommitInfo


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
            print(f"[GitLoader] Git command failed: {e.output.decode()}")
            return ""

    def get_commits(self, max_count: int = 20) -> List[CommitInfo]:
        output = self._run_git_command([
            "git", "log",
            f"--max-count={max_count}",
            "--pretty=format:%H|%s|%an|%ad",
        ])
        commits = []
        for line in output.split("\n"):
            parts = line.split("|")
            if len(parts) < 4:
                continue
            commits.append(CommitInfo(
                commit_hash=parts[0],
                message=parts[1],
                author=parts[2],
                date=parts[3],
            ))
        return commits

    def get_changed_files(self, commit_hash: str) -> List[str]:
        output = self._run_git_command([
            "git", "diff-tree", "--no-commit-id", "--name-only", "-r", commit_hash,
        ])
        return output.split("\n") if output else []

    def find_pcb_files(self) -> List[str]:
        """Return all .kicad_pcb files tracked by git."""
        output = self._run_git_command(["git", "ls-files", "*.kicad_pcb"])
        return [f for f in output.split("\n") if f] if output else []