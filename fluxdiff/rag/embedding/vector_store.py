# fluxdiff/rag/embedding/vector_store.py
"""
FAISS-backed vector store.

Changes vs original:
  - add_documents() deduplicates by content hash before inserting, so
    running the ingestion pipeline twice does not double-index the same
    commits.
  - clear() wipes the index and document list and removes persisted files,
    making it safe to re-ingest from scratch.
  - _load() is tolerant of a missing or corrupt pickle file (logs a warning
    and starts fresh rather than crashing the ingestion run).
"""

import hashlib
import os
import pickle
from typing import List

import faiss
import numpy as np

from fluxdiff.rag.config import RAG_CONFIG
from fluxdiff.rag.schemas import RAGDocument


class VectorStore:
    def __init__(self):
        self.db_path   = RAG_CONFIG["vector_db_path"]
        self.index     = None
        self.documents: List[RAGDocument] = []
        self._seen_hashes: set = set()

        os.makedirs(self.db_path, exist_ok=True)
        self.index_file = os.path.join(self.db_path, "faiss.index")
        self.doc_file   = os.path.join(self.db_path, "documents.pkl")

        self._load()

    # ------------------------------------------------------------------
    # Index init
    # ------------------------------------------------------------------

    def _init_index(self, dim: int):
        self.index = faiss.IndexFlatL2(dim)

    # ------------------------------------------------------------------
    # Content-hash deduplication
    # ------------------------------------------------------------------

    @staticmethod
    def _content_hash(doc: RAGDocument) -> str:
        return hashlib.sha256(doc.content.encode("utf-8")).hexdigest()

    def _rebuild_seen_hashes(self):
        self._seen_hashes = {self._content_hash(d) for d in self.documents}

    # ------------------------------------------------------------------
    # Add documents
    # ------------------------------------------------------------------

    def add_documents(
        self,
        documents: List[RAGDocument],
        embeddings: List[List[float]],
    ):
        if not embeddings:
            return

        # Filter to only genuinely new documents
        new_docs, new_vecs = [], []
        for doc, vec in zip(documents, embeddings):
            h = self._content_hash(doc)
            if h not in self._seen_hashes:
                self._seen_hashes.add(h)
                new_docs.append(doc)
                new_vecs.append(vec)

        if not new_docs:
            print("[VectorStore] All documents already indexed — nothing to add.")
            return

        vectors = np.array(new_vecs, dtype="float32")
        if self.index is None:
            self._init_index(vectors.shape[1])

        self.index.add(vectors)
        self.documents.extend(new_docs)
        self._save()
        print(f"[VectorStore] Added {len(new_docs)} documents ({len(documents) - len(new_docs)} duplicates skipped).")

    # ------------------------------------------------------------------
    # Similarity search
    # ------------------------------------------------------------------

    def similarity_search(
        self,
        query_embedding: List[float],
        top_k: int = None,
    ) -> List[RAGDocument]:
        if self.index is None or not self.documents:
            return []

        top_k        = top_k or RAG_CONFIG["top_k"]
        query_vector = np.array([query_embedding], dtype="float32")
        _, indices   = self.index.search(query_vector, top_k)

        return [
            self.documents[idx]
            for idx in indices[0]
            if 0 <= idx < len(self.documents)
        ]

    # ------------------------------------------------------------------
    # Clear (wipe and reset)
    # ------------------------------------------------------------------

    def clear(self):
        """Remove all indexed documents and reset the FAISS index."""
        self.index     = None
        self.documents = []
        self._seen_hashes = set()

        for path in (self.index_file, self.doc_file):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass

        print("[VectorStore] Index cleared.")

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save(self):
        if self.index is not None:
            faiss.write_index(self.index, self.index_file)
        with open(self.doc_file, "wb") as f:
            pickle.dump(self.documents, f)

    def _load(self):
        if os.path.exists(self.index_file):
            try:
                self.index = faiss.read_index(self.index_file)
            except Exception as e:
                print(f"[VectorStore] Could not load FAISS index: {e} — starting fresh.")
                self.index = None

        if os.path.exists(self.doc_file):
            try:
                with open(self.doc_file, "rb") as f:
                    self.documents = pickle.load(f)
                self._rebuild_seen_hashes()
            except Exception as e:
                print(f"[VectorStore] Could not load documents pickle: {e} — starting fresh.")
                self.documents = []