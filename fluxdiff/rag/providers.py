# fluxdiff/rag/providers.py
"""
Which LLM / embedding provider and model the chat uses.

Providers: "openai" and "gemini".  OpenAI-compatible servers (Ollama, Groq,
OpenRouter, Azure OpenAI, vLLM, ...) work through "openai" with
OPENAI_BASE_URL (read by the OpenAI SDK) and their own model names.

Resolution (at client creation, so .env values are always picked up):
  FLUXDIFF_LLM_PROVIDER        openai | gemini.  Unset: "openai" if
                               OPENAI_API_KEY is set, else "gemini" if
                               GEMINI_API_KEY / GOOGLE_API_KEY is set, else "openai".
  FLUXDIFF_EMBEDDING_PROVIDER  defaults to the LLM provider.
  FLUXDIFF_LLM_MODEL           defaults per provider (below).
  FLUXDIFF_EMBEDDING_MODEL     defaults per provider (below).

Changing the embedding provider or model makes existing indexes unusable
(different vector spaces); they are rebuilt on the next (re-)index, and the
chat asks for a re-index until then.
"""

import os
from typing import Optional

PROVIDERS = ("openai", "gemini")

DEFAULT_LLM_MODEL = {
    "openai": "gpt-4o-mini",
    "gemini": "gemini-3.8-flash",          # latest stable Flash (Nov 2026)
}
DEFAULT_EMBEDDING_MODEL = {
    "openai": "text-embedding-3-small",
    "gemini": "gemini-embedding-2",
}

KEY_HINT = {
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY (or GOOGLE_API_KEY)",
}


class ProviderError(RuntimeError):
    pass


def gemini_api_key() -> Optional[str]:
    return os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")


def _check(provider: str, variable: str) -> str:
    provider = provider.strip().lower()
    if provider not in PROVIDERS:
        raise ProviderError(f"{variable}={provider!r} is not supported; use one of {', '.join(PROVIDERS)}.")
    return provider


def llm_provider() -> str:
    explicit = os.getenv("FLUXDIFF_LLM_PROVIDER")
    if explicit:
        return _check(explicit, "FLUXDIFF_LLM_PROVIDER")
    if os.getenv("OPENAI_API_KEY"):
        return "openai"
    if gemini_api_key():
        return "gemini"
    return "openai"


def embedding_provider() -> str:
    explicit = os.getenv("FLUXDIFF_EMBEDDING_PROVIDER")
    if explicit:
        return _check(explicit, "FLUXDIFF_EMBEDDING_PROVIDER")
    return llm_provider()


def llm_model(provider: str) -> str:
    return os.getenv("FLUXDIFF_LLM_MODEL") or DEFAULT_LLM_MODEL[provider]


def embedding_model(provider: str) -> str:
    return os.getenv("FLUXDIFF_EMBEDDING_MODEL") or DEFAULT_EMBEDDING_MODEL[provider]


def require_key(provider: str):
    """Fail early with a clear message instead of an SDK stack trace."""
    if provider == "openai":
        # OpenAI-compatible local servers may not need a key, but the SDK
        # still requires one to be set (any value works for them).
        if not os.getenv("OPENAI_API_KEY"):
            raise ProviderError("OPENAI_API_KEY is not set (for a local OpenAI-compatible "
                                "server, set it to any value and set OPENAI_BASE_URL).")
    elif provider == "gemini":
        if not gemini_api_key():
            raise ProviderError("GEMINI_API_KEY is not set (create one at https://aistudio.google.com/apikey).")


def describe() -> dict:
    """Provider/model summary for /health and the UI."""
    lp, ep = llm_provider(), embedding_provider()
    return {"llm": f"{lp}:{llm_model(lp)}", "embedding": f"{ep}:{embedding_model(ep)}"}
