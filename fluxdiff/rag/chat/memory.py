# fluxdiff/rag/chat/memory.py

from typing import List, Dict

from fluxdiff.rag.config import RAG_CONFIG


class ChatMemory:
    """
    In-process rolling window of recent conversation turns.

    max_history is sourced from RAG_CONFIG["memory_window"] so it can be
    tuned via the FLUXDIFF_MEMORY_WINDOW environment variable without
    touching code.

    Note: this is intentionally in-process only. Memory resets on server
    restart. For persistent cross-session memory, replace this class with
    a Redis- or DB-backed implementation that has the same add() /
    get_context() / clear() interface.
    """

    def __init__(self, max_history: int = None):
        self.max_history = max_history or RAG_CONFIG["memory_window"]
        self.history: List[Dict[str, str]] = []

    def add(self, user_query: str, assistant_response: str):
        self.history.append({
            "user":      user_query,
            "assistant": assistant_response,
        })
        if len(self.history) > self.max_history:
            self.history.pop(0)

    def get_context(self) -> str:
        if not self.history:
            return ""
        lines = ["Previous conversation:"]
        for turn in self.history:
            lines.append(f"User: {turn['user']}")
            lines.append(f"Assistant: {turn['assistant']}")
        return "\n".join(lines)

    def clear(self):
        self.history = []