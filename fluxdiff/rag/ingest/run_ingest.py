# fluxdiff/rag/ingest/run_ingest.py
"""
Canonical ingestion entry point.

Usage:
    python -m fluxdiff.rag.ingest.run_ingest                 # RAG_CONFIG defaults
    python -m fluxdiff.rag.ingest.run_ingest --max-commits 50
    python -m fluxdiff.rag.ingest.run_ingest --clear         # wipe DB first
    python -m fluxdiff.rag.ingest.run_ingest --stackup path/to/stackup.yaml
    python -m fluxdiff.rag.ingest.run_ingest --no-repo-files # board history only

Each commit that changed a .kicad_pcb file is diffed against its first
parent, one board file at a time.  (Previously every pair of neighbouring
`git log` entries was diffed for every board, which re-parsed unchanged
boards and, across merges, compared commits that are not parent and child.)

Environment variables (see config.py):
    FLUXDIFF_REPO_PATH       — path to the KiCad git repository
    FLUXDIFF_VECTOR_DB_PATH  — override storage location
    OPENAI_API_KEY           — required for embeddings
"""

import argparse
import os

from dotenv import load_dotenv

load_dotenv()

from fluxdiff.rag.config import RAG_CONFIG
from fluxdiff.rag.schemas import RAGDocument
from fluxdiff.rag.ingest.git_loader import GitLoader
from fluxdiff.rag.ingest.diff_generator import DiffGenerator
from fluxdiff.rag.ingest.document_builder import DocumentBuilder, chunk_documents

TEXT_EXTENSIONS = (".md", ".txt", ".py", ".json", ".yaml", ".yml")
SKIP_DIRS = {".git", "__pycache__", "node_modules", "rag_db", "output", "dist",
             "build", ".venv", "venv"}
# Generated files that add tokens but no knowledge.
SKIP_FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
              "Pipfile.lock", "uv.lock"}
MAX_REPO_FILE_BYTES = 256 * 1024


def collect_board_documents(loader: GitLoader, diff_gen: DiffGenerator,
                            builder: DocumentBuilder, max_commits: int):
    commits = loader.get_commits(max_count=max_commits, pcb_only=True)
    if not commits:
        print("No commits touching .kicad_pcb files — no board history to ingest.")
        return []

    print(f"Processing {len(commits)} commit(s) that changed a board file...")
    docs = []
    for i, commit in enumerate(commits, start=1):
        parent = loader.get_first_parent(commit.commit_hash)
        if parent is None:
            print(f"  [{i}/{len(commits)}] {commit.commit_hash[:7]} is a root commit — skipped")
            continue
        changed = loader.get_changed_pcb_files(parent, commit.commit_hash)
        print(f"  [{i}/{len(commits)}] {parent[:7]} → {commit.commit_hash[:7]} "
              f"({commit.message[:60]}) — {len(changed)} board file(s)")
        for pcb_file in changed:
            diff = diff_gen.generate_diff(parent, commit.commit_hash, pcb_file)
            docs.extend(builder.build_documents(commit, diff, pcb_file))
    return docs


def collect_repo_file_documents(repo_path: str):
    """Index the repository's text files so the chat can answer about them.

    Note: these files are sent to the embedding API.  Keep secrets out of
    tracked .json/.yaml files, or run with --no-repo-files.
    """
    docs = []
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for file in files:
            if not file.endswith(TEXT_EXTENSIONS) or file in SKIP_FILES:
                continue
            full_path = os.path.join(root, file)
            try:
                if os.path.getsize(full_path) > MAX_REPO_FILE_BYTES:
                    print(f"    Skipping large file {os.path.relpath(full_path, repo_path)}")
                    continue
                with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
            except OSError as e:
                print(f"    Warning: could not read {full_path}: {e}")
                continue
            if not content.strip():
                continue
            rel_path = os.path.relpath(full_path, repo_path)
            docs.append(RAGDocument(
                content=f"File: {rel_path}\n\n{content}",
                metadata={"type": "repo_file", "file": rel_path},
            ))
    return docs


def run_ingestion(
    max_commits: int = 20,
    clear_first: bool = False,
    stackup_config: str = None,
    include_repo_files: bool = True,
    repo_path: str = None,
    embedder=None,
    store=None,
):
    repo_path = repo_path or RAG_CONFIG["repo_path"]
    print(f"Starting ingestion for repo: {repo_path}")

    if embedder is None:
        from fluxdiff.rag.embedding.embedder import Embedder
        embedder = Embedder()
    if store is None:
        from fluxdiff.rag.embedding.vector_store import VectorStore
        store = VectorStore()

    if clear_first:
        store.clear()

    loader   = GitLoader(repo_path)
    diff_gen = DiffGenerator(repo_path, stackup_config=stackup_config)
    builder  = DocumentBuilder()

    all_docs = collect_board_documents(loader, diff_gen, builder, max_commits)
    if include_repo_files:
        print("Indexing repo text files...")
        all_docs.extend(collect_repo_file_documents(repo_path))

    all_docs = chunk_documents(all_docs)
    # Embed only what the store does not already hold, so re-running
    # ingestion costs nothing for unchanged history.
    new_docs = [d for d in all_docs if not store.has_document(d)]
    # A document can occur twice in one run (identical content); embed once.
    unique = {}
    for d in new_docs:
        unique.setdefault(store.content_hash(d), d)
    new_docs = list(unique.values())

    print(f"{len(all_docs)} document(s) built, {len(new_docs)} new.")
    if not new_docs:
        print("Nothing new to embed.")
        return 0

    print(f"Embedding {len(new_docs)} document(s)...")
    embeddings = embedder.embed_documents(new_docs)

    print("Storing in vector DB...")
    store.add_documents(new_docs, embeddings)
    print("Ingestion complete.")
    return len(new_docs)


# ------------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------------

def _parse_args():
    parser = argparse.ArgumentParser(description="FluxDiff RAG ingestion pipeline")
    parser.add_argument(
        "--max-commits", type=int, default=20,
        help="Maximum number of recent board-changing commits to process (default: 20)",
    )
    parser.add_argument("--clear", action="store_true",
                        help="Wipe the vector DB before ingesting")
    parser.add_argument("--stackup", default=None, metavar="PATH",
                        help="Path to YAML/JSON stackup config for impedance analysis")
    parser.add_argument("--no-repo-files", action="store_true",
                        help="Index board history only, not the repository's text files")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run_ingestion(
        max_commits=args.max_commits,
        clear_first=args.clear,
        stackup_config=args.stackup,
        include_repo_files=not args.no_repo_files,
    )
