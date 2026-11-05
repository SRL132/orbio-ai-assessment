"""Anthropic adapter: the intended production path (docs/architecture.md, "LLM provider").

Uses the official SDK: the schema in the prompt with Pydantic checking the answer (see
`json_instruction`), prompt caching on the stable system prompt, low effort for latency, and
server-side refusal fallbacks where the model has them.
"""

from __future__ import annotations

import logging
import time
from datetime import date
from typing import Any, Literal

import anthropic
from anthropic.types.beta import BetaMessage, BetaOutputConfigParam, BetaTextBlockParam

from app import capture, metrics
from app.adapters.llm.prompting import (
    extractor_system,
    extractor_user,
    json_instruction,
    json_object_text,
    responder_system,
    responder_user,
)
from app.adapters.llm.usage import UsageMeter
from app.domain.config import ClientConfig
from app.domain.models import (
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
        meter: UsageMeter | None = None,
        usd_per_mtok: tuple[float, float] | None = None,
    ) -> None:
        """`usd_per_mtok` is (input, output). Cache reads and writes are priced as input, which
        overestimates: the safe side for a budget cap."""
        self._client = client
        self._model = model
        self._output_config: BetaOutputConfigParam = {"effort": effort}
        # Haiku has no server-side refusal fallback: a refusal there reaches our own handling.
        self._refusal_fallback: dict[str, Any] = (
            {} if "haiku" in model else {"betas": [FALLBACK_BETA], "fallbacks": "default"}
        )
        self.meter = meter
        self._prices = usd_per_mtok

    def extract(
        self, state: ConversationState, message: str, config: ClientConfig, today: date
    ) -> ExtractionResult:
        started = time.monotonic()
        response = self._client.beta.messages.create(
            model=self._model,
            max_tokens=8000,
            system=_cached_system(extractor_system(config) + json_instruction(ExtractionResult)),
            messages=[{"role": "user", "content": extractor_user(state, message, today)}],
            output_config=self._output_config,
            **self._refusal_fallback,
        )
        self._log("extract", response, started)
        text = "".join(block.text for block in response.content if block.type == "text")
        capture.report("extract", "anthropic", self._model, text)
        if response.stop_reason == "refusal":
            raise RefusalError("refusal")
        return ExtractionResult.model_validate_json(json_object_text(text))

    def compose(
        self,
        action: AskField | ReAsk,
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
            **self._refusal_fallback,
        )
        self._log("compose", response, started)
        text = "".join(block.text for block in response.content if block.type == "text")
        capture.report("compose", "anthropic", self._model, text)
        if response.stop_reason == "refusal":
            raise RefusalError("refusal")
        return text.strip()

    def complete(self, system: str, user: str, max_tokens: int = 4000) -> str:
        """Free-text completion (used by the eval candidate simulator)."""
        started = time.monotonic()
        response = self._client.beta.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            system=_cached_system(system),
            messages=[{"role": "user", "content": user}],
            output_config=self._output_config,
            **self._refusal_fallback,
        )
        self._log("complete", response, started)
        return "".join(block.text for block in response.content if block.type == "text").strip()

    def _log(self, kind: str, response: BetaMessage, started: float) -> None:
        usage = response.usage
        prompt = (
            usage.input_tokens
            + (usage.cache_read_input_tokens or 0)
            + (usage.cache_creation_input_tokens or 0)
        )
        cost = 0.0
        if self._prices is not None:
            cost = (prompt * self._prices[0] + usage.output_tokens * self._prices[1]) / 1e6
        if self.meter is not None:
            self.meter.record(prompt, usage.output_tokens, cost)
        metrics.record_llm_call(
            "anthropic", kind, time.monotonic() - started, prompt, usage.output_tokens, cost
        )
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
