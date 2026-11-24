# fluxdiff/rag/embedding/vector_store.py
"""
FAISS-backed vector store.

  - add_documents() deduplicates by content hash; has_document() lets the
    ingester skip embedding documents that are already stored.
  - similarity_search() applies metadata filters BEFORE taking the top k, so
    a commit / type / file filter returns the best matches inside that subset
    instead of whatever survived from the global top k (usually nothing).
  - The store reloads itself when the files on disk change, so a running chat
    server sees a fresh ingestion without a restart.
  - clear() wipes the index and removes persisted files.
  - _load() tolerates a missing or corrupt pickle file.
"""

import hashlib
import json
import os
import pickle
import threading
from typing import Dict, List, Optional

import faiss
import numpy as np

from fluxdiff.rag.config import RAG_CONFIG
from fluxdiff.rag.schemas import RAGDocument

class EmbeddingMismatch(RuntimeError):
    """The index was built with a different embedding model than the query uses."""


# Filter keys understood by similarity_search().
FILTER_KEYS = ("commit", "type", "file")
_MIN_COMMIT_PREFIX = 4


def matches_filters(metadata: Dict, filters: Optional[Dict]) -> bool:
    """
    commit — matches a full hash or any prefix of ≥ 4 characters
    type   — exact document type (summary, component, net, ...)
    file   — exact repository-relative path
    Unknown keys are ignored; None / empty values mean "no filter".
    """
    for key in FILTER_KEYS:
        want = (filters or {}).get(key)
        if not want:
            continue
        have = str(metadata.get(key, ""))
        if key == "commit":
            want = str(want).lower()
            if len(want) < _MIN_COMMIT_PREFIX or not have.lower().startswith(want):
                return False
        elif have != str(want):
            return False
    return True


class VectorStore:
    def __init__(self, db_path: str = None):
        self.db_path   = db_path or RAG_CONFIG["vector_db_path"]
        self.index     = None
        self.documents: List[RAGDocument] = []
        self._seen_hashes: set = set()
        self._loaded_mtime = None
        self._lock = threading.RLock()
        # "provider:model" of the embeddings in this index (None for indexes
        # built before this was recorded).
        self.embedding: Optional[str] = None

        os.makedirs(self.db_path, exist_ok=True)
        self.index_file = os.path.join(self.db_path, "faiss.index")
        self.doc_file   = os.path.join(self.db_path, "documents.pkl")
        self.meta_file  = os.path.join(self.db_path, "index_meta.json")

        self._load()

    # ------------------------------------------------------------------
    # Content-hash deduplication
    # ------------------------------------------------------------------

    @staticmethod
    def content_hash(doc: RAGDocument) -> str:
        return hashlib.sha256(doc.content.encode("utf-8")).hexdigest()

    def has_document(self, doc: RAGDocument) -> bool:
        self.reload_if_changed()
        return self.content_hash(doc) in self._seen_hashes

    def __len__(self) -> int:
        self.reload_if_changed()
        return len(self.documents)

    # ------------------------------------------------------------------
    # Add documents
    # ------------------------------------------------------------------

    def check_embedding(self, signature: Optional[str], dim: Optional[int] = None):
        """Raise EmbeddingMismatch if this index was built with another model."""
        self.reload_if_changed()
        if not self.documents:
            return
        if signature and self.embedding and signature != self.embedding:
            raise EmbeddingMismatch(
                f"This index was built with {self.embedding}, but the server now uses "
                f"{signature}. Re-index the repository (or switch the embedding model back).")
        if dim and self.index is not None and self.index.d != dim:
            raise EmbeddingMismatch(
                f"This index has {self.index.d}-dimensional embeddings, but the current "
                f"model ({signature or 'unknown'}) produces {dim}. Re-index the repository.")

    def add_documents(self, documents: List[RAGDocument], embeddings: List[List[float]],
                      embedding: Optional[str] = None):
        if not embeddings:
            return
        self.check_embedding(embedding, len(embeddings[0]))
        with self._lock:
            new_docs, new_vecs = [], []
            for doc, vec in zip(documents, embeddings):
                h = self.content_hash(doc)
                if h not in self._seen_hashes:
                    self._seen_hashes.add(h)
                    new_docs.append(doc)
                    new_vecs.append(vec)

            if not new_docs:
                print("[VectorStore] All documents already indexed — nothing to add.")
                return

            vectors = np.array(new_vecs, dtype="float32")
            if self.index is None:
                self.index = faiss.IndexFlatL2(vectors.shape[1])
            elif self.index.d != vectors.shape[1]:
                raise ValueError(
                    f"Embedding dimension {vectors.shape[1]} does not match the "
                    f"stored index ({self.index.d}); re-ingest with --clear."
                )

            self.index.add(vectors)
            self.documents.extend(new_docs)
            if embedding:
                self.embedding = embedding
            self._save()
            print(f"[VectorStore] Added {len(new_docs)} documents "
                  f"({len(documents) - len(new_docs)} duplicates skipped).")

    # ------------------------------------------------------------------
    # Similarity search
    # ------------------------------------------------------------------

    def similarity_search(
        self,
        query_embedding: List[float],
        top_k: int = None,
        filters: Optional[Dict] = None,
    ) -> List[RAGDocument]:
        self.reload_if_changed()
        with self._lock:
            if self.index is None or not self.documents:
                return []

            top_k = top_k or RAG_CONFIG["top_k"]
            active = {k: v for k, v in (filters or {}).items() if k in FILTER_KEYS and v}
            # With filters, rank every document and keep the best matching
            # ones.  IndexFlatL2 is exhaustive anyway, so this costs no more
            # than the unfiltered search for the index sizes FluxLink produces.
            if self.index.d != len(query_embedding):
                raise EmbeddingMismatch(
                    f"This index has {self.index.d}-dimensional embeddings but the query has "
                    f"{len(query_embedding)}; it was built with another embedding model. "
                    f"Re-index the repository.")
            k = self.index.ntotal if active else min(top_k, self.index.ntotal)
            query_vector = np.array([query_embedding], dtype="float32")
            _, indices = self.index.search(query_vector, k)

            results = []
            for idx in indices[0]:
                if not 0 <= idx < len(self.documents):
                    continue
                doc = self.documents[idx]
                if active and not matches_filters(doc.metadata, active):
                    continue
                results.append(doc)
                if len(results) == top_k:
                    break
            return results

    # ------------------------------------------------------------------
    # Clear (wipe and reset)
    # ------------------------------------------------------------------

    def clear(self):
        """Remove all indexed documents and reset the FAISS index."""
        with self._lock:
            self.index = None
            self.documents = []
            self._seen_hashes = set()
            self.embedding = None
            for path in (self.index_file, self.doc_file, self.meta_file):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
            self._loaded_mtime = None
        print("[VectorStore] Index cleared.")

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _disk_mtime(self):
        try:
            return os.path.getmtime(self.doc_file)
        except OSError:
            return None

    def reload_if_changed(self):
        """Reload from disk if another process (the ingester) rewrote it."""
        mtime = self._disk_mtime()
        if mtime != self._loaded_mtime:
            with self._lock:
                self._load()

    def _save(self):
        if self.index is not None:
            faiss.write_index(self.index, self.index_file)
        with open(self.meta_file, "w", encoding="utf-8") as f:
            json.dump({"embedding": self.embedding,
                       "dimension": self.index.d if self.index is not None else None}, f)
        with open(self.doc_file, "wb") as f:
            pickle.dump(self.documents, f)
        self._loaded_mtime = self._disk_mtime()

    def _load(self):
        self.index, self.documents, self.embedding = None, [], None
        try:
            with open(self.meta_file, "r", encoding="utf-8") as f:
                self.embedding = json.load(f).get("embedding")
        except (OSError, ValueError, AttributeError):
            pass
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
            except Exception as e:
                print(f"[VectorStore] Could not load documents pickle: {e} — starting fresh.")
                self.documents = []

        if self.index is not None and self.index.ntotal != len(self.documents):
            print("[VectorStore] Index and document list are out of sync — starting fresh. "
                  "Re-run ingestion with --clear.")
            self.index, self.documents = None, []

        self._rebuild_seen_hashes()
        self._loaded_mtime = self._disk_mtime()

    def _rebuild_seen_hashes(self):
        self._seen_hashes = {self.content_hash(d) for d in self.documents}
