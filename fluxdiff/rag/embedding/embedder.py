# fluxdiff/rag/embedding/embedder.py

import os
from typing import List

from dotenv import load_dotenv

from fluxdiff.rag.config import RAG_CONFIG
from fluxdiff.rag.schemas import RAGDocument

load_dotenv()

# OpenAI embedding limits: 2,048 inputs and ~300k tokens per request, 8,191
# tokens per input.  Batches stay well inside both; document_builder.chunk_documents
# keeps every single input under the per-input limit.
MAX_INPUTS_PER_REQUEST = 256
MAX_CHARS_PER_REQUEST  = 400_000   # ≈ 100–130k tokens


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


class Embedder:
    def __init__(self, client=None):
        self.model = RAG_CONFIG["embedding_model"]
        if client is None:
            from openai import OpenAI
            client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        self.client = client

    def embed_documents(self, documents: List[RAGDocument]) -> List[List[float]]:
        vectors: List[List[float]] = []
        for batch in _batches([doc.content for doc in documents]):
            response = self.client.embeddings.create(model=self.model, input=batch)
            vectors.extend(item.embedding for item in response.data)
        return vectors

    def embed_query(self, query: str) -> List[float]:
        response = self.client.embeddings.create(model=self.model, input=query)
        return response.data[0].embedding
