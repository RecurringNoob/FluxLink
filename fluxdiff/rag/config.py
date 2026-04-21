# fluxdiff/rag/config.py

import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))

RAG_CONFIG = {
    # =========================
    # REPO SETTINGS
    # repo_path MUST be set via environment variable FLUXDIFF_REPO_PATH.
    # Fallback to cwd so tests/CI that set cwd to the repo still work.
    # Never hardcode a local machine path here.
    # =========================
    "repo_path": os.getenv("FLUXDIFF_REPO_PATH", os.getcwd()),

    # =========================
    # EMBEDDING SETTINGS
    # Requires OPENAI_API_KEY in environment.
    # =========================
    "embedding_model": os.getenv("FLUXDIFF_EMBEDDING_MODEL", "text-embedding-3-small"),

    # =========================
    # VECTOR DB SETTINGS
    # Override with FLUXDIFF_VECTOR_DB_PATH for non-default locations.
    # =========================
    "vector_db_path": os.getenv(
        "FLUXDIFF_VECTOR_DB_PATH",
        os.path.join(BASE_DIR, "rag_db"),
    ),

    # =========================
    # RETRIEVAL SETTINGS
    # =========================
    "top_k": int(os.getenv("FLUXDIFF_TOP_K", "5")),

    # =========================
    # LLM SETTINGS
    # =========================
    "llm_model": os.getenv("FLUXDIFF_LLM_MODEL", "gpt-4o-mini"),

    # =========================
    # CHAT MEMORY
    # Number of conversation turns retained in-process.
    # =========================
    "memory_window": int(os.getenv("FLUXDIFF_MEMORY_WINDOW", "5")),
}