# fluxdiff/rag/ingest/run_ingest.py
"""
Canonical ingestion entry point.

Replaces the two separate scripts that existed previously
(ingest_cli.py and run_ingest.py) which duplicated the same logic.

Usage:
    python -m fluxdiff.rag.ingest.run_ingest               # uses RAG_CONFIG defaults
    python -m fluxdiff.rag.ingest.run_ingest --max-commits 50
    python -m fluxdiff.rag.ingest.run_ingest --clear       # wipe DB before ingesting
    python -m fluxdiff.rag.ingest.run_ingest --stackup path/to/stackup.yaml

Environment variables (see config.py):
    FLUXDIFF_REPO_PATH       — path to the KiCad git repository
    FLUXDIFF_VECTOR_DB_PATH  — override storage location
    OPENAI_API_KEY           — required for embeddings
"""

import argparse
import os
import sys

from dotenv import load_dotenv

load_dotenv()

# Ensure project root is importable when run directly
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

from fluxdiff.rag.config import RAG_CONFIG
from fluxdiff.rag.schemas import RAGDocument
from fluxdiff.rag.ingest.git_loader import GitLoader
from fluxdiff.rag.ingest.diff_generator import DiffGenerator
from fluxdiff.rag.ingest.document_builder import DocumentBuilder
from fluxdiff.rag.embedding.embedder import Embedder
from fluxdiff.rag.embedding.vector_store import VectorStore


def run_ingestion(
    max_commits: int = 20,
    clear_first: bool = False,
    stackup_config: str = None,
):
    repo_path = RAG_CONFIG["repo_path"]
    print(f"Starting ingestion for repo: {repo_path}")

    loader  = GitLoader(repo_path)
    diff_gen = DiffGenerator(repo_path, stackup_config=stackup_config)
    builder  = DocumentBuilder()
    embedder = Embedder()
    store    = VectorStore()

    if clear_first:
        store.clear()

    # ------------------------------------------------------------------
    # Step 1: discover PCB files
    # ------------------------------------------------------------------
    pcb_files = loader.find_pcb_files()
    if not pcb_files:
        print("No .kicad_pcb files found in repository — nothing to ingest.")
        return

    print(f"Tracking {len(pcb_files)} PCB file(s): {pcb_files}")

    # ------------------------------------------------------------------
    # Step 2: get commits
    # ------------------------------------------------------------------
    commits = loader.get_commits(max_count=max_commits)
    if len(commits) < 2:
        print("Need at least 2 commits to produce a diff — nothing to ingest.")
        return

    print(f"Processing {len(commits) - 1} commit pair(s)...")

    # ------------------------------------------------------------------
    # Step 3: diff each consecutive commit pair across all PCB files
    # ------------------------------------------------------------------
    all_docs = []

    for i in range(len(commits) - 1):
        after_commit  = commits[i]
        before_commit = commits[i + 1]
        print(
            f"  [{i + 1}/{len(commits) - 1}] "
            f"{before_commit.commit_hash[:7]} → {after_commit.commit_hash[:7]} "
            f"({after_commit.message[:60]})"
        )

        for pcb_file in pcb_files:
            diff = diff_gen.generate_diff(
                before_commit.commit_hash,
                after_commit.commit_hash,
                pcb_file,
            )
            docs = builder.build_documents(after_commit, diff, pcb_file)
            all_docs.extend(docs)

    # ------------------------------------------------------------------
    # Step 4: index repo text files (markdown, Python, JSON, etc.)
    #         so the chat engine can answer questions about the codebase
    # ------------------------------------------------------------------
    print("Indexing repo text files...")
    text_extensions = (".md", ".txt", ".py", ".json", ".yaml", ".yml")
    skip_dirs = {".git", "__pycache__", "node_modules", "rag_db", "output"}

    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for file in files:
            if not file.endswith(text_extensions):
                continue
            full_path = os.path.join(root, file)
            try:
                with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                if not content.strip():
                    continue
                rel_path = os.path.relpath(full_path, repo_path)
                all_docs.append(RAGDocument(
                    content=f"File: {rel_path}\n\n{content}",
                    metadata={"type": "repo_file", "file": rel_path},
                ))
            except Exception as e:
                print(f"    Warning: could not read {full_path}: {e}")

    # ------------------------------------------------------------------
    # Step 5: embed and store
    # ------------------------------------------------------------------
    if not all_docs:
        print("No documents generated — nothing to store.")
        return

    print(f"Embedding {len(all_docs)} document(s)...")
    embeddings = embedder.embed_documents(all_docs)

    print("Storing in vector DB...")
    store.add_documents(all_docs, embeddings)

    print("Ingestion complete.")


# ------------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------------

def _parse_args():
    parser = argparse.ArgumentParser(
        description="FluxDiff RAG ingestion pipeline"
    )
    parser.add_argument(
        "--max-commits", type=int, default=20,
        help="Maximum number of recent commits to process (default: 20)",
    )
    parser.add_argument(
        "--clear", action="store_true",
        help="Wipe the vector DB before ingesting",
    )
    parser.add_argument(
        "--stackup", default=None, metavar="PATH",
        help="Path to YAML/JSON stackup config for impedance analysis",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run_ingestion(
        max_commits=args.max_commits,
        clear_first=args.clear,
        stackup_config=args.stackup,
    )