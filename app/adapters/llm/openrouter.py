"""OpenRouter adapter: development and cross-provider evals (synthetic data only).

Production uses the provider's own SDK (see docs/architecture.md, "LLM provider"); this adapter
exists because one key reaches many models, which makes model comparison cheap.
"""

from __future__ import annotations

import logging
import time
from datetime import date
from typing import Any

import httpx

from app import metrics
from app.adapters.llm.prompting import (
    extractor_system,
    extractor_user,
    responder_system,
    responder_user,
)
from app.adapters.llm.usage import UsageMeter
from app.domain.config import ClientConfig
from app.domain.models import (
    AnswerQuestion,
    AskField,
    ConversationState,
    ExtractionResult,
    ReAsk,
)

logger = logging.getLogger("app.llm")

BASE_URL = "https://openrouter.ai/api/v1"
_RETRYABLE = {408, 409, 429, 500, 502, 503, 504}


class OpenRouterLLM:
    def __init__(
        self,
        api_key: str,
        model: str,
        timeout: float = 20.0,
        transport: httpx.BaseTransport | None = None,
        meter: UsageMeter | None = None,
        reasoning_effort: str | None = "low",
    ) -> None:
        if not model:
            raise ValueError("OPENROUTER_MODEL is not set (see .env.example)")
        self._model = model
        self.meter = meter
        # Screening turns are simple: little or no hidden reasoning keeps latency and cost down,
        # and avoids reasoning eating the whole max_tokens budget (empty completion).
        self.reasoning_effort = reasoning_effort
        self._client = httpx.Client(
            base_url=BASE_URL,
            timeout=timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}", "X-Title": "Sazon screening agent"},
        )

    def extract(
        self, state: ConversationState, text: str, config: ClientConfig, today: date
    ) -> ExtractionResult:
        content = self._chat(
            kind="extract",
            system=extractor_system(config),
            user=extractor_user(state, text, today),
            max_tokens=2000,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "extraction",
                    "schema": ExtractionResult.model_json_schema(),
                },
            },
        )
        return ExtractionResult.model_validate_json(_strip_code_fence(content))

    def compose(
        self,
        action: AskField | ReAsk | AnswerQuestion,
        state: ConversationState,
        config: ClientConfig,
    ) -> str:
        return self._chat(
            kind="compose",
            system=responder_system(config),
            user=responder_user(action, state, config),
            max_tokens=400,
        ).strip()

    def complete(self, system: str, user: str, max_tokens: int = 400) -> str:
        """Free-text completion (used by the eval candidate simulator)."""
        return self._chat(kind="complete", system=system, user=user, max_tokens=max_tokens)

    def _chat(
        self,
        kind: str,
        system: str,
        user: str,
        max_tokens: int,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        body: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if response_format:
            body["response_format"] = response_format
        if self.reasoning_effort:
            body["reasoning"] = {"effort": self.reasoning_effort, "exclude": True}
        started = time.monotonic()
        response = self._post(body)
        data = response.json()
        usage = data.get("usage") or {}
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        cost = float(usage.get("cost") or 0.0)  # OpenRouter always reports usage and cost
        if self.meter is not None:
            self.meter.record(prompt, completion, cost)
        metrics.record_llm_call(
            "openrouter", kind, time.monotonic() - started, prompt, completion, cost
        )
        logger.info(
            "llm_call provider=openrouter kind=%s model=%s input_tokens=%s output_tokens=%s "
            "cost=%s latency_ms=%d",
            kind,
            data.get("model", self._model),
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
            usage.get("cost"),
            (time.monotonic() - started) * 1000,
        )
        content = data["choices"][0]["message"].get("content")
        if not content:
            raise ValueError("empty completion")
        return str(content)

    def _post(self, body: dict[str, Any]) -> httpx.Response:
        for attempt in range(2):
            try:
                response = self._client.post("/chat/completions", json=body)
            except httpx.TransportError:
                if attempt == 1:
                    raise
                continue
            if response.status_code in _RETRYABLE and attempt == 0:
                time.sleep(0.5)
                continue
            response.raise_for_status()
            return response
        raise RuntimeError("unreachable")


def _strip_code_fence(text: str) -> str:
    """Some models wrap JSON in ```json fences even when asked for a schema."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        stripped = stripped.rsplit("```", 1)[0]
    return stripped.strip()
