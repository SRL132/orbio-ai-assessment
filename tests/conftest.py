import os
import re
import threading
from datetime import UTC, datetime
from typing import Any

import pytest
from hypothesis import HealthCheck, settings

from app.adapters.clock.clocks import FakeClock
from app.adapters.config.yaml_loader import load_client_config
from app.adapters.llm.fake import ScriptedExtractor, TemplateResponder
from app.domain.config import ClientConfig
from app.domain.models import ConversationState, ExtractedFields, ExtractionResult
from app.domain.ports import Ports

START = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)  # a Monday

# Under mutmut (`uv run mutmut run`) each test runs once per mutant: fewer examples keep that
# fast, and mutmut's runner trips a health check meant for class-based tests run normally.
settings.register_profile(
    "mutmut", max_examples=25, suppress_health_check=[HealthCheck.differing_executors]
)
if "MUTANT_UNDER_TEST" in os.environ:
    settings.load_profile("mutmut")


@pytest.fixture(scope="session")
def config() -> ClientConfig:
    return load_client_config("grupo_sazon")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(START)


@pytest.fixture
def extractor() -> ScriptedExtractor:
    return ScriptedExtractor()


@pytest.fixture
def ports(extractor: ScriptedExtractor, clock: FakeClock) -> Ports:
    return Ports(extractor=extractor, responder=TemplateResponder(), clock=clock)


@pytest.fixture
def state(config: ClientConfig) -> ConversationState:
    return ConversationState(client_id=config.client_id)


def ex(
    *,
    yes_no: bool | None = None,
    language: str = "es",
    intent: str = "answer",
    sentiment: str = "neutral",
    confused: bool = False,
    sensitive_data: bool = False,
    question: str | None = None,
    **fields: Any,
) -> ExtractionResult:
    """Build an extraction from plain values: ex(full_name="Ana López", yes_no=True)."""
    extracted = {
        name: {"value": value, "quote": str(value), "confidence": 0.9}
        for name, value in fields.items()
    }
    return ExtractionResult(
        fields=ExtractedFields.model_validate(extracted),
        language=language,
        intent=intent,
        sentiment=sentiment,
        confused=confused,
        yes_no=yes_no,
        sensitive_data=sensitive_data,
        question=question,
    )


def write_concurrently(repo: Any, state: ConversationState, threads: int = 16) -> list[int]:
    """Inbound messages and agent replies for one conversation from many threads at once, as
    when a WhatsApp webhook arrives while the worker saves a turn. Returns the message seqs."""
    errors: list[str] = []
    barrier = threading.Barrier(threads)

    def write(n: int) -> None:
        barrier.wait()
        for i in range(10):
            try:
                if n % 2:
                    repo.add_inbound(state.conversation_id, f"in {n}.{i}")
                else:
                    repo.save_turn(state, [], [f"out {n}.{i}"], [])
            except Exception as exc:
                errors.append(type(exc).__name__)

    workers = [threading.Thread(target=write, args=(n,)) for n in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert errors == []
    return [m.seq for m in repo.messages(state.conversation_id)]


def submit(client: Any, path: str, data: dict[str, str], **kwargs: Any) -> Any:
    """Post a dashboard form like a browser would: with the token the page embeds."""
    found = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/login").text)
    assert found is not None
    return client.post(path, data={**data, "csrf_token": found.group(1)}, **kwargs)
