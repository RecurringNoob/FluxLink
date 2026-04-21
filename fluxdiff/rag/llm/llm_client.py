# fluxdiff/rag/llm/llm_client.py

import os

from dotenv import load_dotenv
from openai import OpenAI

from fluxdiff.rag.config import RAG_CONFIG
from fluxdiff.rag.llm.prompt_templates import SYSTEM_PROMPT

load_dotenv()


class LLMClient:
    def __init__(self):
        self.model  = RAG_CONFIG["llm_model"]
        self.client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

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
            return response.choices[0].message.content.strip()
        except Exception as e:
            print(f"[LLMClient] Error: {e}")
            return "Error generating response."