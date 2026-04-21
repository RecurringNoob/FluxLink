# fluxdiff/rag/retrieval/retriever.py

from typing import List

from fluxdiff.rag.config import RAG_CONFIG
from fluxdiff.rag.embedding.embedder import Embedder
from fluxdiff.rag.embedding.vector_store import VectorStore
from fluxdiff.rag.schemas import RAGDocument, RAGQuery, RetrievalResult


class Retriever:
    def __init__(self):
        self.embedder = Embedder()
        self.store    = VectorStore()

    def retrieve(self, query: str) -> RetrievalResult:
        """Basic retrieval without filters."""
        embedding = self.embedder.embed_query(query)
        documents = self.store.similarity_search(embedding, top_k=RAG_CONFIG["top_k"])
        return RetrievalResult(documents=documents)

    def retrieve_with_query(self, rag_query: RAGQuery) -> RetrievalResult:
        """Retrieval with optional metadata filters."""
        embedding = self.embedder.embed_query(rag_query.query)
        documents = self.store.similarity_search(embedding, top_k=RAG_CONFIG["top_k"])
        if rag_query.filters:
            documents = self._apply_filters(documents, rag_query.filters)
        return RetrievalResult(documents=documents)

    def _apply_filters(
        self,
        documents: List[RAGDocument],
        filters: dict,
    ) -> List[RAGDocument]:
        result = documents
        for key in ("commit", "type", "file"):
            if key in filters:
                result = [d for d in result if d.metadata.get(key) == filters[key]]
        return result