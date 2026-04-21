# fluxdiff/rag/api.py
"""
FastAPI chat server for FluxDiff RAG.

Runs independently of the Flask viewer (viewer/server.py).
  Flask viewer → port 5000  (board diff viewer)
  This server  → port 8000  (RAG chat API)

Endpoints:
  POST /chat              — basic RAG query
  POST /chat/filtered     — RAG query with metadata filters
  GET  /health            — liveness probe
"""

import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Dict, Any, Optional

load_dotenv()

from fluxdiff.rag.chat.chat_engine import ChatEngine
from fluxdiff.rag.embedding.vector_store import VectorStore
from fluxdiff.rag.schemas import RAGQuery

# ------------------------------------------------------------------
# App setup
# ------------------------------------------------------------------

app = FastAPI(title="FluxDiff RAG API", version="1.0.0")

# Allow the Vite dev server and the Flask viewer to call this API.
# Restrict origins in production via the FLUXDIFF_CORS_ORIGINS env var
# (comma-separated list of allowed origins).
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
# Request / response models (Pydantic)
# ------------------------------------------------------------------

class ChatRequest(BaseModel):
    query: str


class FilteredChatRequest(BaseModel):
    query: str
    filters: Optional[Dict[str, Any]] = None


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------

@app.post("/chat")
def chat(req: ChatRequest):
    """Basic RAG query — retrieves relevant documents and generates an answer."""
    response = chat_engine.ask(req.query)
    return {
        "answer":  response.answer,
        "sources": response.sources,
    }


@app.post("/chat/filtered")
def chat_filtered(req: FilteredChatRequest):
    """
    RAG query with optional metadata filters.

    Supported filter keys:
      commit — restrict to a specific commit hash
      type   — restrict to a document type (summary / component / net /
                routing / power_tree / diff_pair / grounding / impedance / bom)
      file   — restrict to a specific PCB file path
    """
    rag_query = RAGQuery(query=req.query, filters=req.filters or {})
    response  = chat_engine.ask_with_filters(rag_query)
    return {
        "answer":  response.answer,
        "sources": response.sources,
    }


@app.get("/health")
def health():
    """Liveness probe — also reports how many documents are indexed."""
    store = VectorStore()
    return {
        "status":           "ok",
        "documents_indexed": len(store.documents),
    }