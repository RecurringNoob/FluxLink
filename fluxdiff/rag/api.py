# fluxdiff/rag/api.py
"""
FastAPI chat server for FluxDiff RAG.

Runs independently of the Flask viewer (viewer/server.py).
  Flask viewer → port 5000  (board diff viewer)
  This server  → port 8000  (RAG chat API)

Endpoints:
  POST   /chat                    — RAG query
  POST   /chat/filtered           — RAG query with metadata filters
  DELETE /chat/session/{id}       — forget a conversation ("New chat")
  GET    /health                  — liveness probe + indexed document count

Both chat endpoints accept an optional session_id and return one; send it
back on the next request to continue the same conversation.
"""

import os
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

load_dotenv()

from fluxdiff.rag.chat.chat_engine import ChatEngine
from fluxdiff.rag.llm.llm_client import LLMError
from fluxdiff.rag.schemas import RAGQuery

# ------------------------------------------------------------------
# App setup
# ------------------------------------------------------------------

app = FastAPI(title="FluxDiff RAG API", version="1.1.0")

# Restrict origins in production via FLUXDIFF_CORS_ORIGINS (comma-separated).
_raw_origins = os.getenv(
    "FLUXDIFF_CORS_ORIGINS",
    "http://localhost:5173,http://localhost:3000,http://localhost:5000",
)
ALLOWED_ORIGINS = [o.strip() for o in _raw_origins.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

chat_engine = ChatEngine()


# ------------------------------------------------------------------
# Request models
# ------------------------------------------------------------------

class Filters(BaseModel):
    commit: Optional[str] = None   # full hash or prefix (≥ 4 chars)
    type:   Optional[str] = None   # summary / component / net / routing / ...
    file:   Optional[str] = None   # repository-relative .kicad_pcb path


class ChatRequest(BaseModel):
    query: str
    session_id: Optional[str] = None


class FilteredChatRequest(ChatRequest):
    filters: Optional[Filters] = None


def _respond(rag_query: RAGQuery, session_id: Optional[str]):
    if not rag_query.query.strip():
        raise HTTPException(status_code=422, detail="query must not be empty")
    try:
        response = chat_engine.ask_with_filters(rag_query, session_id=session_id)
    except LLMError as e:
        raise HTTPException(status_code=502, detail=f"LLM request failed: {e}")
    return {
        "answer":     response.answer,
        "sources":    response.sources,
        "session_id": response.session_id,
    }


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------

@app.post("/chat")
def chat(req: ChatRequest):
    """RAG query — retrieves relevant documents and generates an answer."""
    return _respond(RAGQuery(query=req.query), req.session_id)


@app.post("/chat/filtered")
def chat_filtered(req: FilteredChatRequest):
    """RAG query restricted to documents matching every given filter."""
    filters = {}
    if req.filters:
        dump = getattr(req.filters, "model_dump", None) or req.filters.dict  # pydantic v2 / v1
        filters = dump(exclude_none=True)
    return _respond(RAGQuery(query=req.query, filters=filters), req.session_id)


@app.delete("/chat/session/{session_id}")
def reset_session(session_id: str):
    chat_engine.reset_session(session_id)
    return {"status": "ok"}


@app.get("/health")
def health():
    """Liveness probe — reports the documents the chat engine is serving."""
    return {
        "status":            "ok",
        "documents_indexed": len(chat_engine.retriever.store),
    }
