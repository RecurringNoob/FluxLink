# fluxdiff/rag/ingest/document_builder.py
"""
Builds RAGDocument chunks from a CommitInfo + DiffSummary pair.

One document is produced per non-empty analysis section so that vector
search can retrieve, for example, only the grounding findings for a
specific commit without pulling in unrelated routing noise.

Document types emitted:
  summary       — always emitted; commit metadata + change counts
  component     — if diff.component_changes is non-empty
  net           — if diff.net_changes is non-empty
  routing       — if diff.routing_changes is non-empty
  power_tree    — if diff.power_tree is non-empty
  diff_pair     — if diff.diff_pairs is non-empty
  grounding     — if diff.grounding is non-empty
  impedance     — if diff.impedance is non-empty
  bom           — if diff.bom is non-empty
"""

from dataclasses import replace
from typing import List

from fluxdiff.rag.schemas import RAGDocument, CommitInfo, DiffSummary

# text-embedding-3-* accept 8,191 tokens per input.  Code and diff lines run
# at roughly 3 characters per token, so 16k characters leaves a safe margin.
MAX_CHUNK_CHARS = 16_000


def chunk_documents(documents: List[RAGDocument],
                    max_chars: int = MAX_CHUNK_CHARS) -> List[RAGDocument]:
    """
    Split documents longer than max_chars on line boundaries.  Each chunk
    repeats the document's header lines (Commit / Type / File, up to the
    first blank line or "Changes:") so it can still be cited on its own, and
    carries chunk / chunks metadata.
    """
    out: List[RAGDocument] = []
    for doc in documents:
        if len(doc.content) <= max_chars:
            out.append(doc)
            continue

        lines = doc.content.split("\n")
        header_len = 0
        for i, line in enumerate(lines[:8]):
            if not line.strip() or line.startswith("Changes:"):
                header_len = i + 1
                break
        header = "\n".join(lines[:header_len])
        budget = max(max_chars - len(header) - 1, max_chars // 2)

        bodies, cur, size = [], [], 0
        for line in lines[header_len:]:
            # A single over-long line is hard-split.
            while len(line) > budget:
                if cur:
                    bodies.append(cur)
                    cur, size = [], 0
                bodies.append([line[:budget]])
                line = line[budget:]
            if cur and size + len(line) + 1 > budget:
                bodies.append(cur)
                cur, size = [], 0
            cur.append(line)
            size += len(line) + 1
        if cur:
            bodies.append(cur)

        for n, body in enumerate(bodies, start=1):
            text = (header + "\n" if header else "") + "\n".join(body)
            meta = dict(doc.metadata, chunk=n, chunks=len(bodies))
            out.append(replace(doc, content=text, metadata=meta))
    return out


class DocumentBuilder:

    def build_documents(
        self,
        commit: CommitInfo,
        diff: DiffSummary,
        file_path: str,
    ) -> List[RAGDocument]:
        docs = []

        docs.append(self._build_summary(commit, diff, file_path))

        section_map = {
            "component":  diff.component_changes,
            "net":        diff.net_changes,
            "routing":    diff.routing_changes,
            "power_tree": diff.power_tree,
            "diff_pair":  diff.diff_pairs,
            "grounding":  diff.grounding,
            "impedance":  diff.impedance,
            "bom":        diff.bom,
        }

        for doc_type, changes in section_map.items():
            if changes:
                docs.append(
                    self._build_section_doc(commit, file_path, doc_type, changes)
                )

        return docs

    # ------------------------------------------------------------------
    # Internal builders
    # ------------------------------------------------------------------

    def _build_summary(
        self,
        commit: CommitInfo,
        diff: DiffSummary,
        file_path: str,
    ) -> RAGDocument:
        content = (
            f"Commit: {commit.commit_hash}\n"
            f"Message: {commit.message}\n"
            f"Author: {commit.author}\n"
            f"Date: {commit.date}\n"
            f"File: {file_path}\n"
            f"Summary:\n"
            f"  component changes : {len(diff.component_changes)}\n"
            f"  net changes       : {len(diff.net_changes)}\n"
            f"  routing changes   : {len(diff.routing_changes)}\n"
            f"  power tree issues : {len(diff.power_tree)}\n"
            f"  diff pair issues  : {len(diff.diff_pairs)}\n"
            f"  grounding issues  : {len(diff.grounding)}\n"
            f"  impedance issues  : {len(diff.impedance)}\n"
            f"  BOM / supply chain: {len(diff.bom)}\n"
        )
        if diff.summary:
            content += f"\nEngine summary:\n{diff.summary}\n"

        return RAGDocument(
            content=content.strip(),
            metadata={
                "commit": commit.commit_hash,
                "type":   "summary",
                "file":   file_path,
                "author": commit.author or "",
                "date":   commit.date or "",
            },
        )

    def _build_section_doc(
        self,
        commit: CommitInfo,
        file_path: str,
        doc_type: str,
        changes: List[str],
    ) -> RAGDocument:
        # Friendly section headers for readability inside the LLM context window
        header_map = {
            "component":  "Component Changes",
            "net":        "Net / ERC Changes",
            "routing":    "Routing Changes",
            "power_tree": "Power Tree Analysis",
            "diff_pair":  "Differential Pair Analysis",
            "grounding":  "Grounding Analysis",
            "impedance":  "Impedance Analysis",
            "bom":        "BOM / Supply Chain",
        }
        header = header_map.get(doc_type, doc_type.replace("_", " ").title())

        changes_text = "\n".join(changes)
        content = (
            f"Commit: {commit.commit_hash}\n"
            f"Type: {header}\n"
            f"File: {file_path}\n"
            f"Changes:\n{changes_text}"
        )

        return RAGDocument(
            content=content.strip(),
            metadata={
                "commit": commit.commit_hash,
                "type":   doc_type,
                "file":   file_path,
            },
        )