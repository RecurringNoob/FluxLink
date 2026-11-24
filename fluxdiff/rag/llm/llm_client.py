# fluxdiff/rag/llm/llm_client.py

import os

from dotenv import load_dotenv

from fluxdiff.rag.config import RAG_CONFIG
from fluxdiff.rag.llm.prompt_templates import SYSTEM_PROMPT

load_dotenv()


class LLMError(RuntimeError):
    """The completion request failed; the API maps this to HTTP 502."""


class LLMClient:
    def __init__(self, client=None):
        self.model = RAG_CONFIG["llm_model"]
        if client is None:
            from openai import OpenAI
            client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        self.client = client

    def generate_response(self, prompt: str) -> str:
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": prompt},
                ],
                temperature=0.2,
            )
        except Exception as e:
            # Previously this returned "Error generating response." as if it
            # were an answer (HTTP 200) and stored it in the chat memory.
            raise LLMError(f"{type(e).__name__}: {e}") from e
        return (response.choices[0].message.content or "").strip()
