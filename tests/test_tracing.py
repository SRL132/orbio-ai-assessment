"""Tracing: request → turn → LLM spans, request ids in responses and logs, no personal data."""

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import tracing
from app.adapters.llm.anthropic_sdk import AnthropicLLM
from app.api.main import create_app
from app.bootstrap import build
from app.settings import Settings

SPANS = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(SPANS))
trace.set_tracer_provider(_provider)  # once per process; the app's tracer is a proxy to it


@pytest.fixture(autouse=True)
def _fresh_spans() -> None:
    SPANS.clear()


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 't.db'}",
        debounce_seconds=0,  # turns run inside the request, so they nest under its span
        demo_mode=True,
    )
    with TestClient(create_app(build(settings))) as client:
        yield client


def test_a_message_traces_request_then_turn(client: TestClient) -> None:
    cid = client.post("/api/conversations").json()["id"]
    SPANS.clear()
    client.post(f"/api/conversations/{cid}/messages", json={"text": "sí, soy Ana López"})
    spans = SPANS.get_finished_spans()
    by_id = {s.context.span_id: s for s in spans}
    turn = next(s for s in spans if s.name == "turn")
    chain = [turn]  # walk up from the turn to the request's root span
    while chain[-1].parent is not None:
        chain.append(by_id[chain[-1].parent.span_id])
    root = chain[-1]
    assert root.name == "POST /api/conversations/{conversation_id}/messages"
    assert root.attributes["request.id"]  # our middleware tagged FastAPI's span
    assert chain[1].name == "fastapi.endpoint"  # the turn ran inside the endpoint
    assert turn.attributes["conversation.id"] == cid
    assert turn.attributes["stage"] == "collecting"
    every_value = [str(v) for s in spans for v in (s.attributes or {}).values()]
    assert not any("Ana" in v for v in every_value)  # no message text or names in traces


def test_llm_calls_nest_inside_the_turn() -> None:
    response = SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text="hola")],
        model="claude-opus-5-5",
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
        _request_id="req_x",
    )
    messages = SimpleNamespace(create=lambda **_kw: response)
    llm = AnthropicLLM(SimpleNamespace(beta=SimpleNamespace(messages=messages)))  # type: ignore[arg-type]
    with tracing.tracer.start_as_current_span("turn") as turn:
        llm.complete("system", "user")
    call = next(s for s in SPANS.get_finished_spans() if s.name == "llm.complete")
    assert call.parent is not None
    assert call.parent.span_id == turn.get_span_context().span_id
    assert (call.attributes["llm.provider"], call.attributes["llm.prompt_tokens"]) == (
        "anthropic",
        10,
    )


def test_request_ids_reach_the_response_and_the_logs(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.handler.addFilter(tracing.RequestIdFilter())
    with caplog.at_level(logging.INFO):
        response = client.post("/api/conversations", headers={"X-Request-ID": "abc-12345678"})
    assert response.headers["X-Request-ID"] == "abc-12345678"
    started = [r for r in caplog.records if r.getMessage().startswith("conversation_started")]
    assert started[0].request_id == "abc-12345678"  # type: ignore[attr-defined]
    made_up = client.get("/health", headers={"X-Request-ID": "<script>"}).headers["X-Request-ID"]
    assert made_up != "<script>"
    assert len(made_up) == 16  # unsafe ids are replaced by our own


def test_tracing_is_off_without_an_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert tracing.configure_tracing("sazon-test") is False
    installed: list[Any] = []
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    monkeypatch.setattr(tracing.trace, "set_tracer_provider", installed.append)
    assert tracing.configure_tracing("sazon-test") is True
    assert installed[0].resource.attributes["service.name"] == "sazon-test"
