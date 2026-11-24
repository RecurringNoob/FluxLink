# fluxdiff/rag/retrieval/retriever.py

from fluxdiff.rag.config import RAG_CONFIG
from fluxdiff.rag.embedding.embedder import Embedder
from fluxdiff.rag.embedding.vector_store import VectorStore
from fluxdiff.rag.schemas import RAGQuery, RetrievalResult


class Retriever:
    def __init__(self, embedder: Embedder = None, store: VectorStore = None):
        self.embedder = embedder or Embedder()
        self.store    = store or VectorStore()

    def retrieve(self, query: str) -> RetrievalResult:
        """Basic retrieval without filters."""
        return self.retrieve_with_query(RAGQuery(query=query))

    def retrieve_with_query(self, rag_query: RAGQuery) -> RetrievalResult:
        """
        Retrieval with optional metadata filters (commit / type / file).
        Filters are applied inside the vector search, before the top-k cut.
        """
        embedding = self.embedder.embed_query(rag_query.query)
        documents = self.store.similarity_search(
            embedding, top_k=RAG_CONFIG["top_k"], filters=rag_query.filters,
        )
        return RetrievalResult(documents=documents)
