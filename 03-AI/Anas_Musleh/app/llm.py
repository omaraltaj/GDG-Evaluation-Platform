from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import httpx
from google import genai
from google.genai import errors, types

from . import config
from .prompts import build_system_prompt
from .tools import TOOL_SPECS, ToolExecutor

logger = logging.getLogger("gdg.chatbot")

# Failures that another model may not share: busy, rate-limited, retired, or server-side errors.
TRY_NEXT_MODEL = {404, 429, 500, 503, 504}


def model_chain() -> list[str]:
    return list(dict.fromkeys([config.GEMINI_MODEL, *config.GEMINI_FALLBACK_MODELS]))


def describe(error: Exception | None) -> str:
    code = getattr(error, "code", None)
    return f"{code} {getattr(error, 'status', '')}".strip() if code else type(error).__name__


def build_declarations() -> list[types.FunctionDeclaration]:
    decls = []
    for spec in TOOL_SPECS:
        props = {k: {**v, "type": v["type"].upper()} for k, v in spec["properties"].items()}
        schema = {"type": "OBJECT", "properties": props}
        if spec["required"]:
            schema["required"] = spec["required"]
        decls.append(types.FunctionDeclaration(
            name=spec["name"], description=spec["description"],
            parameters=schema if props else None))
    return decls


@dataclass
class Turn:
    """What happened in one question: used by the CLI and by the evals."""
    answer: str
    tool_calls: list[dict] = field(default_factory=list)   # [{name, args, result}]


class Assistant:
    def __init__(self, executor: ToolExecutor, api_key: str | None = None, model: str | None = None):
        me = executor.me()
        self.executor = executor
        self.model = model or config.GEMINI_MODEL
        self.client = genai.Client(api_key=api_key or config.GEMINI_API_KEY)
        self.config = types.GenerateContentConfig(
            system_instruction=build_system_prompt(executor.today, me["name"], me["role"]),
            tools=[types.Tool(function_declarations=build_declarations())],
            # We run the tools ourselves (more control + easier to learn and to audit).
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            temperature=config.GEMINI_TEMPERATURE,   # low = consistent, factual answers
            max_output_tokens=config.GEMINI_MAX_OUTPUT_TOKENS,
            thinking_config=(types.ThinkingConfig(thinking_level=config.GEMINI_THINKING_LEVEL)
                             if config.GEMINI_THINKING_LEVEL else None),
            http_options=types.HttpOptions(timeout=config.GEMINI_TIMEOUT_MS),
        )
        self.history: list[types.Content] = []

    def _candidates(self) -> list[str]:
        """The model that answered last goes first, so one question stays on one model when possible."""
        ordered = [self.model, *(m for m in model_chain() if m != self.model)]
        return list(dict.fromkeys(ordered))

    def _generate(self):
        """Call Gemini, moving to the next model when one is busy (503), rate-limited (429), retired (404)
        or too slow; after a full pass with no answer, wait briefly and try the list again."""
        wait = 5
        last_error: Exception | None = None
        for attempt in range(max(1, config.LLM_RETRIES)):
            for model in self._candidates():
                try:
                    response = self.client.models.generate_content(model=model, contents=self.history, config=self.config)
                    self.model = model
                    return response
                except errors.APIError as e:
                    if self._drop_thinking_if_unsupported(e):
                        return self._generate()
                    if getattr(e, "code", None) not in TRY_NEXT_MODEL:
                        raise
                    last_error = e
                except httpx.TimeoutException as e:
                    last_error = e
                logger.warning("Gemini model %s failed (%s); trying the next one", model, describe(last_error))
            if attempt < config.LLM_RETRIES - 1:
                time.sleep(wait)
                wait *= 2
        raise last_error

    def _drop_thinking_if_unsupported(self, error: errors.APIError) -> bool:
        """A model that rejects the thinking setting (400) is retried once with its default instead of failing."""
        if (getattr(error, "code", None) == 400 and self.config.thinking_config is not None
                and "think" in str(getattr(error, "message", "") or "").lower()):
            logger.warning("Model %s rejected thinking_level=%s; using its default", self.model, config.GEMINI_THINKING_LEVEL)
            self.config.thinking_config = None
            return True
        return False

    def ask(self, text: str) -> Turn:
        """One question. If ANYTHING fails (Gemini down, quota, network...), the history is rolled back to
        how it was before this question, so a failed question never pollutes the next one."""
        start = len(self.history)
        self.history.append(types.Content(role="user", parts=[types.Part(text=text)]))
        trace: list[dict] = []
        try:
            for _ in range(config.MAX_TOOL_ROUNDS):
                resp = self._generate()
                calls = resp.function_calls or []
                if not calls:
                    answer = (resp.text or "").strip() or "عذراً، ما قدرت أجهّز إجابة. جرّب تعيد صياغة السؤال."
                    self.history.append(types.Content(role="model", parts=[types.Part(text=answer)]))
                    return Turn(answer, trace)
                self.history.append(resp.candidates[0].content)          # the model's tool request
                parts = []
                for call in calls:
                    args = dict(call.args or {})
                    result = self.executor.run(call.name, args)          # OUR code runs the tool
                    trace.append({"name": call.name, "args": args, "result": result})
                    parts.append(types.Part.from_function_response(name=call.name, response=result))
                self.history.append(types.Content(role="user", parts=parts))
            msg = "الطلب احتاج خطوات أكثر من المسموح. جرّب سؤالاً أبسط أو أكثر تحديداً."
            self.history.append(types.Content(role="model", parts=[types.Part(text=msg)]))
            return Turn(msg, trace)
        except BaseException:
            del self.history[start:]          # drop the unanswered question and any half-finished tool turns
            raise
