# fluxdiff/rag/schemas.py

from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional


# =========================
# CORE DOCUMENT
# =========================

@dataclass
class RAGDocument:
    """
    Represents a single chunk of knowledge stored in the vector DB.
    """
    content: str
    metadata: Dict[str, Any] = field(default_factory=dict)


# =========================
# COMMIT INFO
# =========================

@dataclass
class CommitInfo:
    """
    Represents a single Git commit.
    """
    commit_hash: str
    message: str
    author: Optional[str] = None
    date: Optional[str] = None


# =========================
# DIFF SUMMARY (RAG-FACING)
# =========================

@dataclass
class DiffSummary:
    """
    Simplified diff summary used by the RAG layer.

    Mirrors the section structure of diff_report.txt exactly so that
    _parse_diff_report() can populate every field and DocumentBuilder
    can emit a document per non-empty section.

    NOT the same as DiffResult in pcb_models.py — this is intentionally
    lightweight (plain strings only, no Finding objects) so the RAG layer
    has no import dependency on the core analysis engine.
    """
    component_changes: List[str] = field(default_factory=list)
    net_changes:       List[str] = field(default_factory=list)
    routing_changes:   List[str] = field(default_factory=list)
    power_tree:        List[str] = field(default_factory=list)
    diff_pairs:        List[str] = field(default_factory=list)
    grounding:         List[str] = field(default_factory=list)
    impedance:         List[str] = field(default_factory=list)
    bom:               List[str] = field(default_factory=list)
    summary:           str = ""


# =========================
# QUERY OBJECT
# =========================

@dataclass
class RAGQuery:
    """
    Represents a structured user query (used by ask_with_filters).
    """
    query: str
    filters: Dict[str, Any] = field(default_factory=dict)


# =========================
# RETRIEVAL RESULT
# =========================

@dataclass
class RetrievalResult:
    """
    Output of the retriever before sending to LLM.
    """
    documents: List[RAGDocument]
    scores: Optional[List[float]] = None


# =========================
# CHAT RESPONSE
# =========================

@dataclass
class ChatResponse:
    """
    Final response returned to the caller / API.
    """
    answer: str
    sources: List[Dict[str, Any]] = field(default_factory=list)