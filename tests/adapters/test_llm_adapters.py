"""Contract tests for the LLM adapters, with recorded-style responses and no network."""

import json
from datetime import date
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from prometheus_client import REGISTRY

from app.adapters.llm.anthropic_sdk import FALLBACK_BETA, AnthropicLLM, RefusalError
from app.adapters.llm.openrouter import OpenRouterLLM
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
    FieldName,
    ReAsk,
    Vehicle,
)

TODAY = date(2026, 11, 2)

RECORDED_EXTRACTION = {
    "fields": {
        "has_license": {"value": True, "quote": "tengo carnet de moto", "confidence": 0.95},
        "vehicle": {"value": "moto", "quote": "de moto", "confidence": 0.9},
    },
    "language": "es",
    "intent": "answer",
    "sentiment": "neutral",
    "confused": False,
    "yes_no": None,
    "sensitive_data": False,
    "question": None,
}


def completion(content: str) -> dict[str, Any]:
    return {
        "model": "test/model",
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 900, "completion_tokens": 60},
    }


class TestPrompts:
    def test_system_prompts_are_stable_across_turns(
        self, config: ClientConfig, state: ConversationState
    ) -> None:
        # Byte-identical system prompts are what make prompt caching work.
        assert extractor_system(config) == extractor_system(config)
        assert "Madrid" in extractor_system(config)
        assert "Glovo" in extractor_system(config)
        assert "Lucía" in responder_system(config)

    def test_per_turn_context_goes_in_the_user_message(self, state: ConversationState) -> None:
        state.awaiting = FieldName.START_DATE
        user = extractor_user(state, "el lunes", TODAY)
        assert "2026-11-02 (Monday)" in user
        assert "start_date" in user
        assert "<<<\nel lunes\n>>>" in user


class TestOpenRouter:
    def make(self, handler: Any) -> tuple[OpenRouterLLM, list[dict[str, Any]]]:
        sent: list[dict[str, Any]] = []

        def record(request: httpx.Request) -> httpx.Response:
            sent.append(json.loads(request.content))
            return handler(request, len(sent))

        llm = OpenRouterLLM("key", "test/model", transport=httpx.MockTransport(record))
        return llm, sent

    def test_extract_parses_schema_output(
        self, config: ClientConfig, state: ConversationState
    ) -> None:
        llm, sent = self.make(
            lambda _r, _n: httpx.Response(200, json=completion(json.dumps(RECORDED_EXTRACTION)))
        )
        result = llm.extract(state, "tengo carnet de moto", config, TODAY)
        assert isinstance(result, ExtractionResult)
        assert result.fields.vehicle is not None
        assert result.fields.vehicle.value == Vehicle.MOTO
        assert sent[0]["response_format"]["type"] == "json_schema"
        assert sent[0]["model"] == "test/model"

    def test_code_fenced_json_is_accepted(
        self, config: ClientConfig, state: ConversationState
    ) -> None:
        fenced = "```json\n" + json.dumps(RECORDED_EXTRACTION) + "\n```"
        llm, _ = self.make(lambda _r, _n: httpx.Response(200, json=completion(fenced)))
        assert llm.extract(state, "moto", config, TODAY).fields.has_license is not None

    def test_retries_once_on_rate_limit(
        self, config: ClientConfig, state: ConversationState
    ) -> None:
        def handler(_r: httpx.Request, n: int) -> httpx.Response:
            if n == 1:
                return httpx.Response(429, json={"error": "rate limited"})
            return httpx.Response(200, json=completion("¿Cómo te llamas?"))

        llm, sent = self.make(handler)
        reply = llm.compose(AskField(field=FieldName.FULL_NAME), state, config)
        assert reply == "¿Cómo te llamas?"
        assert len(sent) == 2

    def test_invalid_json_raises_so_the_engine_can_recover(
        self, config: ClientConfig, state: ConversationState
    ) -> None:
        llm, _ = self.make(lambda _r, _n: httpx.Response(200, json=completion("not json")))
        with pytest.raises(ValueError, match="Invalid JSON"):
            llm.extract(state, "hola", config, TODAY)

    def test_model_is_required(self) -> None:
        with pytest.raises(ValueError, match="OPENROUTER_MODEL"):
            OpenRouterLLM("key", "")


class _StubMessages:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.response

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.response


def _response(stop_reason: str = "end_turn", parsed: Any = None, text: str = "") -> Any:
    return SimpleNamespace(
        stop_reason=stop_reason,
        parsed_output=parsed,
        content=[SimpleNamespace(type="text", text=text)],
        model="claude-opus-5-5",
        usage=SimpleNamespace(
            input_tokens=50,
            output_tokens=80,
            cache_read_input_tokens=850,
            cache_creation_input_tokens=100,
        ),
        _request_id="req_test",
    )


class TestAnthropic:
    def make(self, response: Any) -> tuple[AnthropicLLM, _StubMessages]:
        messages = _StubMessages(response)
        client = SimpleNamespace(beta=SimpleNamespace(messages=messages))
        return AnthropicLLM(client), messages  # type: ignore[arg-type]

    def test_extract_uses_schema_cache_and_fallbacks(
        self, config: ClientConfig, state: ConversationState
    ) -> None:
        parsed = ExtractionResult.model_validate(RECORDED_EXTRACTION)
        llm, messages = self.make(_response(parsed=parsed))
        assert llm.extract(state, "tengo carnet de moto", config, TODAY) == parsed
        call = messages.calls[0]
        assert call["output_format"] is ExtractionResult
        assert call["output_config"] == {"effort": "low"}
        assert call["fallbacks"] == "default"
        assert call["betas"] == [FALLBACK_BETA]
        assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
        assert call["system"][0]["text"] == extractor_system(config)

    def test_refusal_raises(self, config: ClientConfig, state: ConversationState) -> None:
        llm, _ = self.make(_response(stop_reason="refusal"))
        with pytest.raises(RefusalError):
            llm.extract(state, "hola", config, TODAY)

    def test_compose_returns_text(self, config: ClientConfig, state: ConversationState) -> None:
        llm, _ = self.make(_response(text=" ¿Qué día podrías empezar? "))
        reply = llm.compose(AskField(field=FieldName.START_DATE), state, config)
        assert reply == "¿Qué día podrías empezar?"

    def test_calls_are_metered_and_priced(self) -> None:
        calls = REGISTRY.get_sample_value(
            "sazon_llm_calls_total", {"provider": "anthropic", "kind": "complete"}
        )
        meter = UsageMeter()
        messages = _StubMessages(_response(text="hola"))
        client = SimpleNamespace(beta=SimpleNamespace(messages=messages))
        llm = AnthropicLLM(client, meter=meter, usd_per_mtok=(1.0, 5.0))  # type: ignore[arg-type]
        assert llm.complete("system", "user") == "hola"
        # 1,000 prompt tokens (cache included) at $1/M plus 80 output tokens at $5/M.
        assert (meter.calls, meter.prompt_tokens, meter.completion_tokens) == (1, 1000, 80)
        assert meter.cost_usd == pytest.approx(0.0014)
        after = REGISTRY.get_sample_value(
            "sazon_llm_calls_total", {"provider": "anthropic", "kind": "complete"}
        )
        assert after == (calls or 0) + 1  # also exported to Prometheus

    def test_unpriced_calls_count_tokens_only(self) -> None:
        meter = UsageMeter()
        messages = _StubMessages(_response(text="hola"))
        client = SimpleNamespace(beta=SimpleNamespace(messages=messages))
        AnthropicLLM(client, meter=meter).complete("system", "user")  # type: ignore[arg-type]
        assert (meter.prompt_tokens, meter.cost_usd) == (1000, 0.0)


class TestResponderPrompt:
    def test_reask_explains_the_reason(
        self, config: ClientConfig, state: ConversationState
    ) -> None:
        user = responder_user(ReAsk(field=FieldName.START_DATE, reason="in_past"), state, config)
        assert "in_past" in user
        assert config.template("es", "reask_start_date") in user

    def test_answer_question_uses_faq_only(
        self, config: ClientConfig, state: ConversationState
    ) -> None:
        user = responder_user(AnswerQuestion(question="¿Cuánto pagan?"), state, config)
        assert "Use only the FAQ" in user

    def test_confused_candidate_gets_options(
        self, config: ClientConfig, state: ConversationState
    ) -> None:
        user = responder_user(AskField(field=FieldName.SCHEDULE, offer_options=True), state, config)
        assert "offer the options" in user


def test_openrouter_retries_network_errors_once(
    config: ClientConfig, state: ConversationState
) -> None:
    calls = []

    def flaky(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json=completion("¿Cómo te llamas?"))

    llm = OpenRouterLLM("key", "m", transport=httpx.MockTransport(flaky))
    assert llm.compose(AskField(field=FieldName.FULL_NAME), state, config) == "¿Cómo te llamas?"


def test_openrouter_server_error_raises_after_retry(
    config: ClientConfig, state: ConversationState
) -> None:
    llm = OpenRouterLLM(
        "key", "m", transport=httpx.MockTransport(lambda _r: httpx.Response(503, json={}))
    )
    with pytest.raises(httpx.HTTPStatusError):
        llm.compose(AskField(field=FieldName.FULL_NAME), state, config)
