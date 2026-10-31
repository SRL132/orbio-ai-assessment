"""The LLM wrapper (fallback, circuit breaker, concurrency cap) and OpenRouter's backoff."""

import threading
from datetime import date
from typing import Any

import httpx
import pytest

from app import metrics
from app.adapters.llm.anthropic_sdk import RefusalError
from app.adapters.llm.fake import TemplateResponder
from app.adapters.llm.openrouter import MAX_BACKOFF_SECONDS, OpenRouterLLM, _backoff
from app.adapters.llm.resilient import LLMBusy, Pair, ResilientLLM
from app.domain.config import ClientConfig
from app.domain.models import (
    AnswerQuestion,
    AskField,
    ConversationState,
    ExtractionResult,
    FieldName,
    ReAsk,
)
from app.domain.ports import LLMUnavailable

TODAY = date(2026, 11, 2)


class Stub:
    """An LLM that raises the queued errors, then answers."""

    def __init__(self, name: str, *errors: Exception) -> None:
        self.name = name
        self.errors = list(errors)
        self.calls = 0

    def extract(
        self, state: ConversationState, text: str, config: ClientConfig, today: date
    ) -> ExtractionResult:
        self._maybe_fail()
        return ExtractionResult(language="es")

    def compose(
        self,
        action: AskField | ReAsk | AnswerQuestion,
        state: ConversationState,
        config: ClientConfig,
    ) -> str:
        self._maybe_fail()
        return self.name

    def _maybe_fail(self) -> None:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


ASK = AskField(field=FieldName.FULL_NAME)


def test_primary_answers_when_it_is_up(config: ClientConfig, state: ConversationState) -> None:
    primary, fallback = Stub("primary"), Stub("fallback")
    llm = ResilientLLM(primary, fallback)
    assert llm.compose(ASK, state, config) == "primary"
    assert fallback.calls == 0


def test_fallback_answers_when_the_primary_fails(
    config: ClientConfig, state: ConversationState
) -> None:
    primary = Stub("primary", httpx.ConnectError("down"))
    fallback = Stub("fallback")
    before = metrics.LLM_FALLBACKS.labels("compose")._value.get()
    llm = ResilientLLM(primary, fallback)
    assert llm.compose(ASK, state, config) == "fallback"
    assert metrics.LLM_FALLBACKS.labels("compose")._value.get() == before + 1


def test_breaker_skips_the_primary_while_it_cools_down(
    config: ClientConfig, state: ConversationState
) -> None:
    clock = Clock()
    primary = Stub("primary", httpx.ConnectError("down"))
    fallback = Stub("fallback")
    llm = ResilientLLM(primary, fallback, cooldown_seconds=30, monotonic=clock)
    llm.compose(ASK, state, config)  # primary fails, fallback answers
    assert llm.compose(ASK, state, config) == "fallback"
    assert primary.calls == 1  # not tried again during the cooldown
    clock.now = 31
    assert llm.compose(ASK, state, config) == "primary"  # tried again after it


def test_outage_with_no_fallback_is_unavailable(
    config: ClientConfig, state: ConversationState
) -> None:
    llm = ResilientLLM(Stub("primary", httpx.ReadTimeout("slow")))
    with pytest.raises(LLMUnavailable) as raised:
        llm.extract(state, "hola", config, TODAY)
    assert isinstance(raised.value.__cause__, httpx.ReadTimeout)


def test_both_providers_down_is_unavailable(config: ClientConfig, state: ConversationState) -> None:
    request = httpx.Request("POST", "https://example.test")
    error = httpx.HTTPStatusError("503", request=request, response=httpx.Response(503))
    llm = ResilientLLM(Stub("p", error), Stub("f", httpx.ConnectError("down")))
    with pytest.raises(LLMUnavailable):
        llm.extract(state, "hola", config, TODAY)


@pytest.mark.parametrize("error", [ValueError("Invalid JSON"), RefusalError("refusal")])
def test_a_bad_answer_is_not_an_outage(
    config: ClientConfig, state: ConversationState, error: Exception
) -> None:
    """Unparseable or refused answers keep their type: the engine asks again at once instead of
    holding the message for a retry that would get the same answer."""
    llm = ResilientLLM(Stub("primary", error))
    with pytest.raises(type(error)):
        llm.extract(state, "hola", config, TODAY)


def test_concurrency_cap_refuses_past_the_limit(
    config: ClientConfig, state: ConversationState
) -> None:
    release = threading.Event()
    entered = threading.Event()

    class Slow(Stub):
        def compose(self, *args: Any) -> str:
            entered.set()
            release.wait(5)
            return "slow"

    llm = ResilientLLM(Slow("slow"), max_concurrency=1, acquire_timeout=0.01)
    worker = threading.Thread(target=llm.compose, args=(ASK, state, config))
    worker.start()
    assert entered.wait(5)
    with pytest.raises(LLMBusy):  # an LLMUnavailable: the engine holds the message
        llm.compose(ASK, state, config)
    release.set()
    worker.join()
    assert llm.compose(ASK, state, config) == "slow"  # the slot is back


def test_pair_joins_separate_fakes(config: ClientConfig, state: ConversationState) -> None:
    llm = ResilientLLM(Pair(Stub("x"), TemplateResponder()))
    assert llm.extract(state, "hola", config, TODAY).language == "es"
    assert llm.compose(ASK, state, config) == config.template("es", "ask_full_name")


# --- OpenRouter backoff ---------------------------------------------------------------------


def completion(content: str) -> dict[str, Any]:
    return {"choices": [{"message": {"content": content}}], "usage": {}}


def openrouter(handler: Any, max_retries: int = 1) -> tuple[OpenRouterLLM, list[float]]:
    sleeps: list[float] = []
    llm = OpenRouterLLM(
        "key",
        "m",
        transport=httpx.MockTransport(handler),
        max_retries=max_retries,
        sleep=sleeps.append,
    )
    return llm, sleeps


def test_retry_after_is_honoured(config: ClientConfig, state: ConversationState) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "3"}, json={})
        return httpx.Response(200, json=completion("ok"))

    llm, sleeps = openrouter(handler)
    assert llm.compose(ASK, state, config) == "ok"
    assert sleeps == [3.0]


def test_backoff_is_jittered_exponential_and_capped() -> None:
    for attempt in range(6):
        assert 0 <= _backoff(attempt) <= min(0.5 * 2**attempt, MAX_BACKOFF_SECONDS)
    assert _backoff(0, "600") == MAX_BACKOFF_SECONDS  # a huge Retry-After is capped
    assert 0 <= _backoff(0, "Wed, 21 Oct 2026 07:28:00 GMT") <= 0.5  # date form: jitter


def test_retries_are_configurable(config: ClientConfig, state: ConversationState) -> None:
    llm, sleeps = openrouter(lambda _r: httpx.Response(503, json={}), max_retries=3)
    with pytest.raises(httpx.HTTPStatusError):
        llm.compose(ASK, state, config)
    assert len(sleeps) == 3


def test_a_read_timeout_is_not_retried(config: ClientConfig, state: ConversationState) -> None:
    """It already cost the whole timeout: the fallback provider gets the call instead."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadTimeout("slow")

    llm, sleeps = openrouter(handler, max_retries=3)
    with pytest.raises(httpx.ReadTimeout):
        llm.compose(ASK, state, config)
    assert len(calls) == 1
    assert sleeps == []
