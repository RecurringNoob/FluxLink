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
  GET    /repos                   — indexed repositories (+ allow_local, busy)
  POST   /repos                   — add a repository and index it in the background
                                    {kind: "local"|"github", location, ref?, max_commits?}
  GET    /repos/{id}              — one repository (poll while status == "indexing")
  POST   /repos/{id}/reindex      — pick up new commits
  DELETE /repos/{id}              — remove a repository and its index
  GET    /health                  — liveness probe + indexed document count

Both chat endpoints accept an optional session_id and return one; send it
back on the next request to continue the same conversation.  They also take
an optional repo_id (see /repos); without it the command-line index is used.
"""

import os
import threading
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

load_dotenv()

from fluxdiff.rag import providers
from fluxdiff.rag.chat.chat_engine import ChatEngine
from fluxdiff.rag.embedding.vector_store import EmbeddingMismatch
from fluxdiff.rag.llm.llm_client import LLMError
from fluxdiff.rag.repos import (DEFAULT_REPO_ID, RepoBusy, RepoError, RepoManager,
                                local_repos_allowed, readonly)
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

# Built on first use, not at import: constructing it creates the OpenAI
# client, which raises without an API key.  Creating it at import time
# made `uvicorn fluxdiff.rag.api:app` crash on startup (and the module
# impossible to import in tests) instead of answering with a clear error.
chat_engine: Optional[ChatEngine] = None
_engine_lock = threading.Lock()

# Needs no API key: it only manages folders and starts indexing jobs.
repo_manager = RepoManager()


def _store_for(repo_id: str):
    from fluxdiff.rag.embedding.vector_store import VectorStore
    return VectorStore(db_path=repo_manager.store_path(repo_id))


def get_chat_engine() -> ChatEngine:
    global chat_engine
    if chat_engine is None:
        with _engine_lock:
            if chat_engine is None:
                try:
                    chat_engine = ChatEngine(store_for=_store_for)
                except Exception as e:
                    p = providers.llm_provider()
                    raise HTTPException(
                        status_code=503,
                        detail=(f"Chat engine unavailable ({type(e).__name__}: {e}). "
                                f"Check {providers.KEY_HINT.get(p, 'the API key')} "
                                f"(provider: {p}) and that the [rag] extras are installed."),
                    )
    return chat_engine


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
    repo_id: Optional[str] = None    # from /repos; omitted = command-line index


class FilteredChatRequest(ChatRequest):
    filters: Optional[Filters] = None


class AddRepoRequest(BaseModel):
    kind: str                        # "local" | "github"
    location: str                    # folder path, or owner/repo / GitHub URL
    ref: str = ""                    # GitHub branch or tag (default branch if empty)
    max_commits: int = 50


def _check_repo(repo_id: Optional[str]):
    if not repo_id or repo_id == DEFAULT_REPO_ID:
        return
    try:
        repo = repo_manager.get(repo_id)
    except RepoError as e:
        raise HTTPException(status_code=404, detail=str(e))
    if repo["status"] == "indexing":
        raise HTTPException(status_code=409, detail=f"{repo['label']} is still being indexed.")
    if repo["status"] != "ready":
        raise HTTPException(status_code=409,
                            detail=f"{repo['label']} has no usable index: {repo['message']}")


def _respond(rag_query: RAGQuery, session_id: Optional[str], repo_id: Optional[str] = None):
    if not rag_query.query.strip():
        raise HTTPException(status_code=422, detail="query must not be empty")
    _check_repo(repo_id)
    try:
        response = get_chat_engine().ask_with_filters(rag_query, session_id=session_id,
                                                      repo_id=repo_id)
    except LLMError as e:
        raise HTTPException(status_code=502, detail=f"LLM request failed: {e}")
    except EmbeddingMismatch as e:
        raise HTTPException(status_code=409, detail=str(e))
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
    return _respond(RAGQuery(query=req.query), req.session_id, req.repo_id)


@app.post("/chat/filtered")
def chat_filtered(req: FilteredChatRequest):
    """RAG query restricted to documents matching every given filter."""
    filters = {}
    if req.filters:
        dump = getattr(req.filters, "model_dump", None) or req.filters.dict  # pydantic v2 / v1
        filters = dump(exclude_none=True)
    return _respond(RAGQuery(query=req.query, filters=filters), req.session_id, req.repo_id)


@app.delete("/chat/session/{session_id}")
def reset_session(session_id: str):
    get_chat_engine().reset_session(session_id)
    return {"status": "ok"}


@app.get("/repos")
def list_repos():
    return {"repos": repo_manager.list(), "allow_local": local_repos_allowed(),
            "readonly": readonly(), "busy": repo_manager.is_busy(), **providers.describe()}


def _repo_call(fn, *args):
    try:
        return fn(*args)
    except PermissionError as e:          # local folders disabled, or read-only server
        raise HTTPException(status_code=403, detail=str(e))
    except RepoBusy as e:
        raise HTTPException(status_code=409, detail=str(e))
    except RepoError as e:
        raise HTTPException(status_code=404 if str(e).startswith("Unknown") else 400,
                            detail=str(e))


@app.post("/repos", status_code=202)
def add_repo(req: AddRepoRequest):
    """Register a repository and start indexing it; poll GET /repos/{id}."""
    return _repo_call(repo_manager.add, req.kind, req.location, req.ref, req.max_commits)


@app.get("/repos/{repo_id}")
def get_repo(repo_id: str):
    return _repo_call(repo_manager.get, repo_id)


@app.post("/repos/{repo_id}/reindex", status_code=202)
def reindex_repo(repo_id: str):
    return _repo_call(repo_manager.reindex, repo_id)


@app.delete("/repos/{repo_id}")
def remove_repo(repo_id: str):
    _repo_call(repo_manager.remove, repo_id)
    if chat_engine is not None:
        chat_engine.forget_repo(repo_id)
    return {"status": "ok"}


@app.get("/health")
def health():
    """Liveness probe — reports the documents the chat engine is serving.
    Returns 503 when the engine cannot start (e.g. no API key)."""
    return {
        "status":            "ok",
        "documents_indexed": len(get_chat_engine().retriever.store),
        **providers.describe(),
    }
