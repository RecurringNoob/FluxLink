# fluxdiff/rag/chat/chat_engine.py

import threading
import uuid
from collections import OrderedDict
from typing import List, Optional

from fluxdiff.rag.chat.memory import ChatMemory
from fluxdiff.rag.chat.tools import HistoryTools, tools_enabled
from fluxdiff.rag.llm.llm_client import LLMBusy, LLMClient, LLMError
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

    def __init__(self, retriever: Retriever = None, llm: LLMClient = None,
                 store_for=None, repo_info=None):
        """
        store_for(repo_id) -> VectorStore selects a repository's index
        (see rag/repos.py).  Without a repo_id, or without store_for, the
        default index behind `retriever` is used.
        """
        self.retriever = retriever or Retriever()
        self.llm       = llm or LLMClient()
        self._store_for = store_for
        # repo_info(repo_id) -> dict (label, kind, location, branch) for tools.
        self._repo_info = repo_info
        self._retrievers: dict = {}
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

    def _retriever(self, repo_id: Optional[str]) -> Retriever:
        if not repo_id or repo_id == "default" or self._store_for is None:
            return self.retriever
        with self._lock:
            retriever = self._retrievers.get(repo_id)
            if retriever is None:
                # One embedder for all repositories; each store reloads
                # itself when its repository is re-indexed.
                retriever = Retriever(embedder=self.retriever.embedder,
                                      store=self._store_for(repo_id))
                self._retrievers[repo_id] = retriever
            return retriever

    def forget_repo(self, repo_id: str) -> None:
        with self._lock:
            self._retrievers.pop(repo_id, None)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ask(self, query: str, session_id: Optional[str] = None,
            repo_id: Optional[str] = None) -> ChatResponse:
        return self.ask_with_filters(RAGQuery(query=query), session_id=session_id,
                                     repo_id=repo_id)

    def ask_with_filters(self, rag_query: RAGQuery,
                         session_id: Optional[str] = None,
                         repo_id: Optional[str] = None) -> ChatResponse:
        """
        Retrieval is always attempted; the system prompt tells the model to
        handle greetings and off-topic chat without forcing repo context.
        """
        session_id = session_id or uuid.uuid4().hex
        memory = self._memory(session_id)

        documents = [
            d for d in self._retriever(repo_id).retrieve_with_query(rag_query).documents
            if d.content and "no changes" not in d.content.lower()
        ]

        prompt = build_rag_prompt(format_documents(documents), rag_query.query,
                                  memory.get_context())
        answer, tools = self._answer(prompt, repo_id)   # raises LLMError
        memory.add(rag_query.query, answer)

        cited = list(documents)
        if tools is not None:
            cited += self._summaries_for(repo_id, tools.touched)
        return ChatResponse(answer=answer, sources=_sources(cited), session_id=session_id,
                            tools_used=list(tools.used) if tools else [])

    # ------------------------------------------------------------------
    # Tool calling, with a safe fallback
    # ------------------------------------------------------------------

    def _tools_for(self, repo_id: Optional[str]) -> Optional[HistoryTools]:
        if not tools_enabled() or not hasattr(self.llm, "generate_with_tools"):
            return None
        retriever = self._retriever(repo_id)
        info = None
        if self._repo_info and repo_id and repo_id != "default":
            try:
                info = self._repo_info(repo_id)
            except Exception:
                info = None
        search = lambda q: retriever.retrieve_with_query(RAGQuery(query=q)).documents
        return HistoryTools(retriever.store.documents, repo=info, search=search)

    def _answer(self, prompt: str, repo_id: Optional[str]):
        tools = self._tools_for(repo_id)
        if tools is not None:
            try:
                answer = self.llm.generate_with_tools(prompt, tools)
                if answer:
                    return answer, tools
            except LLMBusy:
                raise                        # plain mode would hit the same busy models
            except Exception as e:           # e.g. a model without tool support
                print(f"[ChatEngine] Tool calling failed ({type(e).__name__}: {e}); "
                      f"answering without tools.")
        return self.llm.generate_response(prompt), None

    def _summaries_for(self, repo_id: Optional[str], commits: List[str]) -> List[RAGDocument]:
        if not commits:
            return []
        wanted = set(commits[:8])           # cite the first few commits tools looked at
        docs = [d for d in self._retriever(repo_id).store.documents
                if d.metadata.get("type") == "summary" and d.metadata.get("commit") in wanted]
        order = {c: i for i, c in enumerate(commits)}
        return sorted(docs, key=lambda d: order.get(d.metadata.get("commit"), 99))
