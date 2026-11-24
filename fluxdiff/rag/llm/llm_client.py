# fluxdiff/rag/llm/llm_client.py
"""Chat completions from OpenAI or Gemini (see rag/providers.py)."""

import os
from typing import Optional

from dotenv import load_dotenv

from fluxdiff.rag.llm.prompt_templates import SYSTEM_PROMPT
from fluxdiff.rag.providers import gemini_api_key, llm_model, llm_provider, require_key

load_dotenv()


class LLMError(RuntimeError):
    """The completion request failed; the API maps this to HTTP 502."""


class LLMClient:
    def __init__(self, client=None, provider: Optional[str] = None,
                 model: Optional[str] = None, gemini_types=None):
        """
        client / gemini_types let tests inject stand-ins for the SDKs.
        A bare `client` without `provider` is treated as an OpenAI client.
        """
        self.provider = provider or ("openai" if client is not None else llm_provider())
        self.model = model or llm_model(self.provider)
        self._types = gemini_types
        if client is None:
            require_key(self.provider)
            if self.provider == "gemini":
                from google import genai
                client = genai.Client(api_key=gemini_api_key())
            else:
                from openai import OpenAI
                client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        self.client = client

    @property
    def signature(self) -> str:
        return f"{self.provider}:{self.model}"

    def generate_response(self, prompt: str) -> str:
        try:
            if self.provider == "gemini":
                return self._gemini(prompt)
            return self._openai(prompt)
        except LLMError:
            raise
        except Exception as e:
            # Previously failures came back as "Error generating response."
            # with HTTP 200 and were stored in the chat memory.
            raise LLMError(f"{type(e).__name__}: {e}") from e

    def _openai(self, prompt: str) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": prompt},
            ],
            temperature=0.2,
        )
        return (response.choices[0].message.content or "").strip()

    def _gemini(self, prompt: str) -> str:
        types = self._types
        if types is None:
            from google.genai import types
        # Temperature is left at the model default: Google advises against
        # lowering it for Gemini 3 models.
        response = self.client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT),
        )
        text = getattr(response, "text", None)
        if not text:
            reason = ""
            candidates = getattr(response, "candidates", None) or []
            if candidates:
                reason = f" (finish reason: {getattr(candidates[0], 'finish_reason', 'unknown')})"
            raise LLMError(f"Gemini returned no text{reason}")
        return text.strip()
