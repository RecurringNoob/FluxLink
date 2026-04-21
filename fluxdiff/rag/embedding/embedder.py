# fluxdiff/rag/embedding/embedder.py

import os
from typing import List

from dotenv import load_dotenv
from openai import OpenAI

from fluxdiff.rag.config import RAG_CONFIG
from fluxdiff.rag.schemas import RAGDocument

load_dotenv()


class Embedder:
    def __init__(self):
        self.model  = RAG_CONFIG["embedding_model"]
        self.client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    def embed_documents(self, documents: List[RAGDocument]) -> List[List[float]]:
        texts    = [doc.content for doc in documents]
        response = self.client.embeddings.create(model=self.model, input=texts)
        return [item.embedding for item in response.data]

    def embed_query(self, query: str) -> List[float]:
        response = self.client.embeddings.create(model=self.model, input=query)
        return response.data[0].embedding