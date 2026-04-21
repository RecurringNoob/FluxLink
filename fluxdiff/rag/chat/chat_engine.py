# fluxdiff/rag/chat/chat_engine.py

from fluxdiff.rag.chat.memory import ChatMemory
from fluxdiff.rag.llm.llm_client import LLMClient
from fluxdiff.rag.llm.prompt_templates import build_rag_prompt, format_documents
from fluxdiff.rag.retrieval.retriever import Retriever
from fluxdiff.rag.schemas import ChatResponse, RAGQuery


class ChatEngine:
    def __init__(self):
        self.retriever = Retriever()
        self.llm       = LLMClient()
        self.memory    = ChatMemory()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ask(self, query: str) -> ChatResponse:
        """
        Basic RAG pipeline.

        Retrieval is always attempted; the LLM system prompt instructs the
        model to handle greetings and off-topic chat gracefully without
        forcing repo context into the answer. This removes the hardcoded
        greeting-word list that would miss "good morning", non-English
        greetings, etc.
        """
        retrieval_result = self.retriever.retrieve(query)

        # Drop empty or trivially uninformative documents
        documents = [
            d for d in retrieval_result.documents
            if d.content and "no changes" not in d.content.lower()
        ]

        context        = format_documents(documents)
        memory_context = self.memory.get_context()
        prompt         = build_rag_prompt(context, query, memory_context)

        answer = self.llm.generate_response(prompt)
        self.memory.add(query, answer)

        return ChatResponse(
            answer=answer,
            sources=[doc.metadata for doc in documents],
        )

    def ask_with_filters(self, rag_query: RAGQuery) -> ChatResponse:
        """Supports metadata filtering (commit, type, file)."""
        retrieval_result = self.retriever.retrieve_with_query(rag_query)
        documents        = retrieval_result.documents

        context = format_documents(documents)
        prompt  = build_rag_prompt(context, rag_query.query)

        answer = self.llm.generate_response(prompt)

        return ChatResponse(
            answer=answer,
            sources=[doc.metadata for doc in documents],
        )