# fluxdiff/rag/embedding/embedder.py
"""
Text embeddings from OpenAI or Gemini (see rag/providers.py).

Embedder.signature ("provider:model") is stored with each index so an index
is never queried with a different embedding model (see vector_store.py).
"""

import math
import os
from typing import List, Optional

from dotenv import load_dotenv

from fluxdiff.rag.providers import (call_with_retries, embedding_model, embedding_provider,
                                    gemini_api_key, require_key, short_error)
from fluxdiff.rag.schemas import RAGDocument

load_dotenv()

class EmbeddingError(RuntimeError):
    """The embedding request failed (after retries); the API maps it to HTTP 502."""


# Request sizes stay well inside every provider's limits.  Per-input limits
# are handled by chunking documents to Embedder.max_input_chars.
MAX_INPUTS_PER_REQUEST = 100
MAX_CHARS_PER_REQUEST  = 200_000


def _batches(texts: List[str]):
    batch, size = [], 0
    for text in texts:
        if batch and (len(batch) >= MAX_INPUTS_PER_REQUEST
                      or size + len(text) > MAX_CHARS_PER_REQUEST):
            yield batch
            batch, size = [], 0
        batch.append(text)
        size += len(text)
    if batch:
        yield batch


class _OpenAIBackend:
    # text-embedding-3-*: 8,191 tokens per input; ~3 chars/token for diffs/code.
    max_input_chars = 16_000

    def __init__(self, model: str, client=None):
        if client is None:
            require_key("openai")
            from openai import OpenAI
            client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        self.client, self.model = client, model

    def embed(self, texts: List[str], is_query: bool) -> List[List[float]]:
        response = self.client.embeddings.create(model=self.model, input=texts)
        return [item.embedding for item in response.data]


class _GeminiBackend:
    """
    gemini-embedding-001 marks queries/documents with task_type and accepts
    a list of strings.  gemini-embedding-2 (and later) has no task_type: the
    task goes into the text, and each text must be its own Content object —
    a plain list of strings is embedded as ONE aggregated vector.
    """

    def __init__(self, model: str, client=None, types=None):
        if client is None:
            require_key("gemini")
            from google import genai
            client = genai.Client(api_key=gemini_api_key())
        if types is None:
            from google.genai import types
        self.client, self.model, self.types = client, model, types
        self.uses_task_type = "embedding-001" in model
        # gemini-embedding-001: 2,048 input tokens; gemini-embedding-2: 8,192.
        self.max_input_chars = 5_000 if self.uses_task_type else 16_000

    def embed(self, texts: List[str], is_query: bool) -> List[List[float]]:
        t = self.types
        if self.uses_task_type:
            config = t.EmbedContentConfig(task_type="RETRIEVAL_QUERY" if is_query
                                          else "RETRIEVAL_DOCUMENT")
            result = self.client.models.embed_content(model=self.model, contents=texts,
                                                      config=config)
        else:
            fmt = ("task: search result | query: {}" if is_query
                   else "title: none | text: {}")
            contents = [t.Content(parts=[t.Part.from_text(text=fmt.format(x))]) for x in texts]
            result = self.client.models.embed_content(model=self.model, contents=contents)
        vectors = [list(e.values) for e in result.embeddings]
        if len(vectors) != len(texts):
            raise RuntimeError(f"Gemini returned {len(vectors)} embeddings for {len(texts)} texts")
        if self.uses_task_type:
            # Reduced-dimension gemini-embedding-001 vectors are not normalised.
            vectors = [_normalise(v) for v in vectors]
        return vectors


def _normalise(v: List[float]) -> List[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class Embedder:
    def __init__(self, client=None, provider: Optional[str] = None,
                 model: Optional[str] = None, gemini_types=None):
        """
        client / gemini_types let tests inject stand-ins for the SDKs.
        A bare `client` without `provider` is treated as an OpenAI client.
        """
        self.provider = provider or ("openai" if client is not None else embedding_provider())
        self.model = model or embedding_model(self.provider)
        if self.provider == "gemini":
            self._backend = _GeminiBackend(self.model, client, gemini_types)
        else:
            self._backend = _OpenAIBackend(self.model, client)
        self.client = self._backend.client

    @property
    def signature(self) -> str:
        return f"{self.provider}:{self.model}"

    @property
    def max_input_chars(self) -> int:
        return self._backend.max_input_chars

    def _embed(self, texts: List[str], is_query: bool, delays) -> List[List[float]]:
        try:
            return call_with_retries(lambda: self._backend.embed(texts, is_query), delays=delays)
        except Exception as e:
            raise EmbeddingError(f"{self.signature}: {short_error(e)}") from e

    def embed_documents(self, documents: List[RAGDocument]) -> List[List[float]]:
        # Indexing is not interactive: wait longer for a busy provider.
        vectors: List[List[float]] = []
        for batch in _batches([doc.content for doc in documents]):
            vectors.extend(self._embed(batch, is_query=False, delays=(2, 5, 10)))
        return vectors

    def embed_query(self, query: str) -> List[float]:
        return self._embed([query], is_query=True, delays=(1.5,))[0]
