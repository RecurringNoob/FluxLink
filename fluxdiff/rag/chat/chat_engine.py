# fluxdiff/rag/chat/chat_engine.py

import threading
import uuid
from collections import OrderedDict
from typing import List, Optional

from fluxdiff.rag.chat.memory import ChatMemory
from fluxdiff.rag.llm.llm_client import LLMClient
from fluxdiff.rag.llm.prompt_templates import build_rag_prompt, format_documents
from fluxdiff.rag.retrieval.retriever import Retriever
from fluxdiff.rag.schemas import ChatResponse, RAGDocument, RAGQuery

# Oldest conversations are dropped once this many are held in memory.
MAX_SESSIONS = 500
SOURCE_KEYS = ("commit", "file", "type", "date", "author")


def _sources(documents: List[RAGDocument]) -> List[dict]:
    """One entry per (commit, file, type), keeping retrieval order."""
    seen, out = set(), []
    for doc in documents:
        meta = doc.metadata or {}
        src = {k: meta[k] for k in SOURCE_KEYS if meta.get(k)}
        ident = (src.get("commit"), src.get("file"), src.get("type"))
        if ident not in seen:
            seen.add(ident)
            out.append(src)
    return out


class ChatEngine:
    """
    RAG chat with one rolling memory per session.

    The engine used to hold a single ChatMemory shared by every client of the
    API, so one user's conversation leaked into another's prompts, and "New
    chat" in the UI could not reset it.  Callers now pass a session_id
    (one is created when absent) and get it back in the response.
    """

    def __init__(self, retriever: Retriever = None, llm: LLMClient = None):
        self.retriever = retriever or Retriever()
        self.llm       = llm or LLMClient()
        self._sessions: "OrderedDict[str, ChatMemory]" = OrderedDict()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def _memory(self, session_id: str) -> ChatMemory:
        with self._lock:
            memory = self._sessions.get(session_id)
            if memory is None:
                memory = self._sessions[session_id] = ChatMemory()
                while len(self._sessions) > MAX_SESSIONS:
                    self._sessions.popitem(last=False)
            else:
                self._sessions.move_to_end(session_id)
            return memory

    def reset_session(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ask(self, query: str, session_id: Optional[str] = None) -> ChatResponse:
        return self.ask_with_filters(RAGQuery(query=query), session_id=session_id)

    def ask_with_filters(self, rag_query: RAGQuery,
                         session_id: Optional[str] = None) -> ChatResponse:
        """
        Retrieval is always attempted; the system prompt tells the model to
        handle greetings and off-topic chat without forcing repo context.
        """
        session_id = session_id or uuid.uuid4().hex
        memory = self._memory(session_id)

        documents = [
            d for d in self.retriever.retrieve_with_query(rag_query).documents
            if d.content and "no changes" not in d.content.lower()
        ]

        prompt = build_rag_prompt(format_documents(documents), rag_query.query,
                                  memory.get_context())
        answer = self.llm.generate_response(prompt)   # raises LLMError
        memory.add(rag_query.query, answer)

        return ChatResponse(answer=answer, sources=_sources(documents),
                            session_id=session_id)
