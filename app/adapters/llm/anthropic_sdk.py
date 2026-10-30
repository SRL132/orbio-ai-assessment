"""Anthropic adapter: the intended production path (docs/architecture.md, "LLM provider").

Uses the official SDK: structured outputs via `messages.parse` with our Pydantic schema, prompt
caching on the stable system prompt, low effort for latency, and server-side refusal fallbacks.
"""

from __future__ import annotations

import logging
import time
from datetime import date
from typing import Literal

import anthropic
from anthropic.types.beta import BetaMessage, BetaOutputConfigParam, BetaTextBlockParam

from app.adapters.llm.prompting import (
    extractor_system,
    extractor_user,
    responder_system,
    responder_user,
)
from app.domain.config import ClientConfig
from app.domain.models import (
    AnswerQuestion,
    AskField,
    ConversationState,
    ExtractionResult,
    ReAsk,
)

logger = logging.getLogger("app.llm")

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class RefusalError(RuntimeError):
    """The model (and its fallback) declined; the engine falls back to a template."""


class AnthropicLLM:
    def __init__(
        self,
        client: anthropic.Anthropic,
        model: str = "claude-opus-5-5",
        effort: Literal["low", "medium", "high", "xhigh", "max"] = "low",
    ) -> None:
        self._client = client
        self._model = model
        self._output_config: BetaOutputConfigParam = {"effort": effort}

    def extract(
        self, state: ConversationState, text: str, config: ClientConfig, today: date
    ) -> ExtractionResult:
        started = time.monotonic()
        response = self._client.beta.messages.parse(
            model=self._model,
            max_tokens=8000,
            system=_cached_system(extractor_system(config)),
            messages=[{"role": "user", "content": extractor_user(state, text, today)}],
            output_format=ExtractionResult,
            output_config=self._output_config,
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )
        self._log("extract", response, started)
        if response.stop_reason == "refusal" or response.parsed_output is None:
            raise RefusalError(str(response.stop_reason))
        result: ExtractionResult = response.parsed_output
        return result

    def compose(
        self,
        action: AskField | ReAsk | AnswerQuestion,
        state: ConversationState,
        config: ClientConfig,
    ) -> str:
        started = time.monotonic()
        response = self._client.beta.messages.create(
            model=self._model,
            max_tokens=4000,
            system=_cached_system(responder_system(config)),
            messages=[{"role": "user", "content": responder_user(action, state, config)}],
            output_config=self._output_config,
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )
        self._log("compose", response, started)
        if response.stop_reason == "refusal":
            raise RefusalError("refusal")
        return "".join(block.text for block in response.content if block.type == "text").strip()

    def _log(self, kind: str, response: BetaMessage, started: float) -> None:
        usage = response.usage
        logger.info(
            "llm_call provider=anthropic kind=%s model=%s input_tokens=%s output_tokens=%s "
            "cache_read_tokens=%s stop_reason=%s latency_ms=%d request_id=%s",
            kind,
            response.model,
            usage.input_tokens,
            usage.output_tokens,
            getattr(usage, "cache_read_input_tokens", None),
            response.stop_reason,
            (time.monotonic() - started) * 1000,
            getattr(response, "_request_id", None),
        )


def _cached_system(text: str) -> list[BetaTextBlockParam]:
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]
