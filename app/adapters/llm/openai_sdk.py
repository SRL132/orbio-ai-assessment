"""OpenAI adapter: GPT-5.6 Terra won the eval comparison (docs/architecture.md, "LLM provider").

Uses the official SDK with a strict schema for extraction. OpenAI caches long prompt prefixes by
itself, and our system prompts are byte-stable, so there's nothing to mark. Refusals raise
`RefusalError`, and the engine falls back to a template.
"""

from __future__ import annotations

import logging
import time
from datetime import date
from typing import Any, Literal

import openai
from openai.types.chat import ChatCompletion

from app import capture, metrics
from app.adapters.llm.anthropic_sdk import RefusalError
from app.adapters.llm.prompting import (
    extractor_system,
    extractor_user,
    json_object_text,
    responder_system,
    responder_user,
    strict_schema,
)
from app.adapters.llm.usage import UsageMeter
from app.domain.config import ClientConfig
from app.domain.models import AskField, ConversationState, ExtractionResult, ReAsk

logger = logging.getLogger("app.llm")

_EXTRACTION_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "extraction",
        "strict": True,
        "schema": strict_schema(ExtractionResult),
    },
}


class OpenAILLM:
    def __init__(
        self,
        client: openai.OpenAI,
        model: str = "gpt-5.6-terra",
        effort: Literal["none", "minimal", "low", "medium", "high"] = "low",
        meter: UsageMeter | None = None,
        usd_per_mtok: tuple[float, float] | None = None,
    ) -> None:
        """`usd_per_mtok` is (input, output). Cached input is priced as input, which overestimates:
        the safe side for a budget cap."""
        self._client = client
        self._model = model
        self._effort = effort
        self.meter = meter
        self._prices = usd_per_mtok

    def extract(
        self, state: ConversationState, text: str, config: ClientConfig, today: date
    ) -> ExtractionResult:
        content = self._chat(
            "extract",
            extractor_system(config),
            extractor_user(state, text, today),
            2000,
            _EXTRACTION_FORMAT,
        )
        return ExtractionResult.model_validate_json(json_object_text(content))

    def compose(
        self,
        action: AskField | ReAsk,
        state: ConversationState,
        config: ClientConfig,
    ) -> str:
        user = responder_user(action, state, config)
        return self._chat("compose", responder_system(config), user, 400).strip()

    def complete(self, system: str, user: str, max_tokens: int = 400) -> str:
        """Free-text completion (used by the eval candidate simulator)."""
        return self._chat("complete", system, user, max_tokens)

    def _chat(
        self,
        kind: str,
        system: str,
        user: str,
        max_tokens: int,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        started = time.monotonic()
        extra: dict[str, Any] = {"response_format": response_format} if response_format else {}
        response = self._client.chat.completions.create(
            model=self._model,
            max_completion_tokens=max_tokens,
            reasoning_effort=self._effort,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            **extra,
        )
        self._log(kind, response, started)
        message = response.choices[0].message
        capture.report(kind, "openai", self._model, str(message.content or message.refusal or ""))
        if message.refusal:
            raise RefusalError("refusal")
        if not message.content:
            raise ValueError("empty completion")
        return str(message.content)

    def _log(self, kind: str, response: ChatCompletion, started: float) -> None:
        usage = response.usage
        prompt = usage.prompt_tokens if usage else 0
        completion = usage.completion_tokens if usage else 0
        cost = 0.0
        if self._prices is not None:
            cost = (prompt * self._prices[0] + completion * self._prices[1]) / 1e6
        if self.meter is not None:
            self.meter.record(prompt, completion, cost)
        metrics.record_llm_call(
            "openai", kind, time.monotonic() - started, prompt, completion, cost
        )
        logger.info(
            "llm_call provider=openai kind=%s model=%s input_tokens=%s output_tokens=%s "
            "finish_reason=%s latency_ms=%d",
            kind,
            response.model,
            prompt,
            completion,
            response.choices[0].finish_reason,
            (time.monotonic() - started) * 1000,
        )
