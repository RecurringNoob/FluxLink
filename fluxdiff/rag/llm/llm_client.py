# fluxdiff/rag/llm/llm_client.py
"""Chat completions from OpenAI or Gemini (see rag/providers.py)."""

import os
from typing import Optional

from dotenv import load_dotenv

from fluxdiff.rag.llm.prompt_templates import SYSTEM_PROMPT, TOOLS_PROMPT
from fluxdiff.rag.providers import (call_with_retries, gemini_api_key, is_transient,
                                    llm_fallback_models, llm_model, llm_provider, require_key,
                                    short_error)

load_dotenv()


class LLMError(RuntimeError):
    """The completion request failed; the API maps this to HTTP 502."""


class LLMBusy(LLMError):
    """Every model (main and fallbacks) was busy / rate-limited."""


# Tool-calling rounds per answer: enough to look up several things, few
# enough to keep answers quick.
MAX_TOOL_ROUNDS = 4


class LLMClient:
    def __init__(self, client=None, provider: Optional[str] = None,
                 model: Optional[str] = None, gemini_types=None):
        """
        client / gemini_types let tests inject stand-ins for the SDKs.
        A bare `client` without `provider` is treated as an OpenAI client.
        """
        self.provider = provider or ("openai" if client is not None else llm_provider())
        self.model = model or llm_model(self.provider)
        # Tried in order when the model is busy / rate-limited (see providers.py).
        self.fallback_models = llm_fallback_models(self.provider, self.model)
        self.last_model = self.model
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
        """
        Ask the model; on a busy / rate-limited response retry briefly, then
        try each fallback model.  Other errors fail at once.  Failures raise
        LLMError (HTTP 502) with a one-line reason.
        """
        call = self._gemini if self.provider == "gemini" else self._openai
        return self._with_fallbacks(lambda model: call(prompt, model))

    def generate_with_tools(self, prompt: str, tools) -> str:
        """
        Answer with tool calling: the model may call `tools` (chat/tools.py,
        via tools.specs / tools.call) for up to MAX_TOOL_ROUNDS rounds.
        Same retry / fallback-model behaviour as generate_response.
        """
        loop = self._gemini_tools if self.provider == "gemini" else self._openai_tools

        def attempt(model):
            tools.used.clear()                  # a retried attempt starts afresh
            return loop(prompt, tools, model)

        return self._with_fallbacks(attempt)

    def _with_fallbacks(self, run):
        busy = []
        for model in [self.model] + self.fallback_models:
            try:
                answer = call_with_retries(lambda: run(model))
                if model != self.model:
                    print(f"[LLMClient] {self.model} unavailable; answered with {model}")
                self.last_model = model
                return answer
            except LLMError:
                raise
            except Exception as e:
                if not is_transient(e):
                    raise LLMError(f"{model}: {short_error(e)}") from e
                busy.append(f"{model} ({short_error(e)})")
        raise LLMBusy("The model is busy right now — " + "; ".join(busy)
                      + ". Please try again in a moment.")

    def _openai(self, prompt: str, model: str) -> str:
        response = self.client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": prompt},
            ],
            temperature=0.2,
        )
        return (response.choices[0].message.content or "").strip()

    def _gemini(self, prompt: str, model: str) -> str:
        types = self._types
        if types is None:
            from google.genai import types
        # Temperature is left at the model default: Google advises against
        # lowering it for Gemini 3 models.
        response = self.client.models.generate_content(
            model=model,
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

    # ------------------------------------------------------------------
    # Tool-calling loops
    # ------------------------------------------------------------------

    def _openai_tools(self, prompt: str, tools, model: str) -> str:
        from fluxdiff.rag.chat.tools import as_json_args
        specs = [{"type": "function", "function": spec} for spec in tools.specs]
        messages = [{"role": "system", "content": f"{SYSTEM_PROMPT}\n\n{TOOLS_PROMPT}"},
                    {"role": "user", "content": prompt}]
        for round_ in range(MAX_TOOL_ROUNDS + 1):
            extra = {"tool_choice": "none"} if round_ == MAX_TOOL_ROUNDS else {}
            response = self.client.chat.completions.create(
                model=model, messages=messages, tools=specs, temperature=0.2, **extra)
            message = response.choices[0].message
            calls = getattr(message, "tool_calls", None) or []
            if not calls:
                return (message.content or "").strip()
            messages.append({
                "role": "assistant", "content": message.content or "",
                "tool_calls": [{"id": c.id, "type": "function",
                                "function": {"name": c.function.name,
                                             "arguments": c.function.arguments or "{}"}}
                               for c in calls],
            })
            for c in calls:
                result = tools.call(c.function.name, as_json_args(c.function.arguments))
                messages.append({"role": "tool", "tool_call_id": c.id, "content": result})
        return ""

    def _gemini_tools(self, prompt: str, tools, model: str) -> str:
        """
        Manual function-calling loop.  The model's turns are appended exactly
        as received: Gemini 3 requires its thought signatures to be passed back.
        """
        from fluxdiff.rag.chat.tools import as_json_args
        t = self._types
        if t is None:
            from google.genai import types as t
        declarations = [t.FunctionDeclaration(name=s["name"], description=s["description"],
                                              parameters_json_schema=s["parameters"])
                        for s in tools.specs]

        def config(allow_tools: bool):
            return t.GenerateContentConfig(
                system_instruction=f"{SYSTEM_PROMPT}\n\n{TOOLS_PROMPT}",
                tools=[t.Tool(function_declarations=declarations)],
                tool_config=t.ToolConfig(function_calling_config=t.FunctionCallingConfig(
                    mode="AUTO" if allow_tools else "NONE")),
                automatic_function_calling=t.AutomaticFunctionCallingConfig(disable=True),
            )

        contents = [t.Content(role="user", parts=[t.Part.from_text(text=prompt)])]
        for round_ in range(MAX_TOOL_ROUNDS + 1):
            response = self.client.models.generate_content(
                model=model, contents=contents, config=config(round_ < MAX_TOOL_ROUNDS))
            calls = getattr(response, "function_calls", None) or []
            if not calls:
                text = getattr(response, "text", None)
                if not text:
                    raise LLMError("Gemini returned no text")
                return text.strip()
            contents.append(response.candidates[0].content)      # unchanged (signatures)
            parts = [t.Part(function_response=t.FunctionResponse(
                        name=c.name, id=getattr(c, "id", None),
                        response={"result": tools.call(c.name, as_json_args(getattr(c, "args", None)))}))
                     for c in calls]
            contents.append(t.Content(role="user", parts=parts))
        return ""
