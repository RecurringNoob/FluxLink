# fluxdiff/rag/ingest/diff_generator.py
"""
Generates a DiffSummary for a pair of commits by:
  1. Extracting the .kicad_pcb file content at each commit via git-show
  2. Writing the contents to temp files
  3. Running the FluxDiff core pipeline in-process (compare_pcbs)
  4. Converting the resulting DiffResult → DiffSummary

Running in-process (rather than shelling out to the CLI) means:
  - No dependency on the output/ directory or diff_report.txt
  - All 8 analysis sections are captured (component, net, routing, power
    tree, diff pairs, grounding, impedance, BOM) — not just the 3 the old
    text parser happened to read
  - stackup_config can be passed through if configured
  - No subprocess overhead or fragile stdout parsing
"""

import os
import tempfile
import subprocess
from typing import Optional

from fluxdiff.rag.config import RAG_CONFIG
from fluxdiff.rag.schemas import DiffSummary


class DiffGenerator:
    def __init__(self, repo_path: str = None, stackup_config: str = None):
        self.repo_path = repo_path or RAG_CONFIG["repo_path"]
        # Optional path to a YAML/JSON stackup config forwarded to analyse_impedance
        self.stackup_config = stackup_config

    # ------------------------------------------------------------------
    # Git helpers
    # ------------------------------------------------------------------

    def _get_file_at_commit(self, commit_hash: str, file_path: str) -> Optional[str]:
        """Return the UTF-8 content of file_path at commit_hash, or None."""
        try:
            result = subprocess.check_output(
                ["git", "show", f"{commit_hash}:{file_path}"],
                cwd=self.repo_path,
                stderr=subprocess.STDOUT,
            )
            return result.decode("utf-8")
        except subprocess.CalledProcessError:
            return None

    def _write_temp_file(self, content: str) -> str:
        """Write content to a NamedTemporaryFile and return its path."""
        tmp = tempfile.NamedTemporaryFile(
            delete=False, suffix=".kicad_pcb", mode="w", encoding="utf-8"
        )
        tmp.write(content)
        tmp.close()
        return tmp.name

    # ------------------------------------------------------------------
    # In-process diff
    # ------------------------------------------------------------------

    def _run_diff_in_process(self, before_file: str, after_file: str) -> DiffSummary:
        """
        Run the FluxDiff core pipeline in-process and return a DiffSummary.

        Imports are deferred so that the RAG layer can be imported without
        the full analysis stack being loaded (useful for unit-testing the
        RAG modules in isolation).
        """
        try:
            from fluxdiff.parser.pcb_parser import parse_pcb
            from fluxdiff.diff.diff_engine import compare_pcbs
        except ImportError as e:
            print(f"[DiffGenerator] Could not import FluxDiff core: {e}")
            return DiffSummary()

        try:
            before_pcb = parse_pcb(before_file)
            after_pcb  = parse_pcb(after_file)
            diff       = compare_pcbs(
                before_pcb, after_pcb,
                stackup_config=self.stackup_config,
            )
        except Exception as e:
            print(f"[DiffGenerator] FluxDiff pipeline error: {e}")
            return DiffSummary()

        return DiffSummary(
            component_changes=diff.component_changes,
            net_changes=diff.net_changes,
            routing_changes=diff.routing_changes,
            power_tree=diff.power_tree_changes,
            diff_pairs=diff.diff_pair_changes,
            grounding=diff.ground_changes,
            impedance=diff.impedance_changes,
            bom=diff.bom_changes,
            summary=diff.summary,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate_diff(
        self,
        commit_a: str,
        commit_b: str,
        pcb_file_path: str,
    ) -> DiffSummary:
        """
        Generate a DiffSummary by diffing pcb_file_path between commit_a
        (before) and commit_b (after).

        Returns an empty DiffSummary if the file is absent in either commit.
        Temp files are always cleaned up, even on error.
        """
        before_content = self._get_file_at_commit(commit_a, pcb_file_path)
        after_content  = self._get_file_at_commit(commit_b, pcb_file_path)

        if not before_content or not after_content:
            print(
                f"[DiffGenerator] '{pcb_file_path}' not found in "
                f"{commit_a[:7]} or {commit_b[:7]} — skipping"
            )
            return DiffSummary()

        before_file = self._write_temp_file(before_content)
        after_file  = self._write_temp_file(after_content)

        try:
            return self._run_diff_in_process(before_file, after_file)
        finally:
            for path in (before_file, after_file):
                try:
                    os.remove(path)
                except OSError:
                    pass