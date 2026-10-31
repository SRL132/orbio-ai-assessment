"""The turn pool: one worker runs several conversations at once, never one twice."""

import threading
import time
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from app import metrics
from app.adapters.clock.clocks import FakeClock
from app.adapters.llm.fake import ScriptedExtractor
from app.adapters.storage.sql import SqlRepository, make_engine
from app.domain.config import ClientConfig
from app.domain.models import ConversationState, ExtractionResult, Status
from app.domain.ports import Ports
from app.worker.pool import TurnPool
from app.worker.service import ScreeningService
from tests.conftest import ex
from tests.worker.test_service import make_service

NO_DEBOUNCE = timedelta(0)


class Gate(ScriptedExtractor):
    """Blocks every extraction until released, counting how many run at once."""

    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.running = 0
        self.peak = 0

    def extract(
        self, state: ConversationState, text: str, config: ClientConfig, today: date
    ) -> ExtractionResult:
        with self.lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
        try:
            self.release.wait(5)
            return super().extract(state, text, config, today)
        finally:
            with self.lock:
                self.running -= 1


@pytest.fixture
def gated(
    config: ClientConfig, ports: Ports, clock: FakeClock, tmp_path: Path
) -> Iterator[tuple[ScreeningService, Gate]]:
    """A file database: the pool's threads each open their own connection to it."""
    gate = Gate()
    db = make_engine(f"sqlite:///{tmp_path / 'pool.db'}")
    repo = SqlRepository(db, clock, config.client_id)
    gated_ports = Ports(extractor=gate, responder=ports.responder, clock=clock)
    yield ScreeningService(repo, config, gated_ports), gate
    gate.release.set()
    db.dispose()


def waiting_conversations(service: ScreeningService, n: int) -> list[UUID]:
    ids = [service.start_conversation() for _ in range(n)]
    for cid in ids:
        service.receive(cid, "sí")
    return ids


def test_turns_run_concurrently_up_to_the_pool_size(
    gated: tuple[ScreeningService, Gate],
) -> None:
    service, gate = gated
    waiting_conversations(service, 5)
    pool = TurnPool(service, size=3, debounce=NO_DEBOUNCE)
    assert pool.fill() == 3
    assert pool.fill() == 0  # full: nothing more starts
    deadline = time.monotonic() + 5
    while gate.running < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert gate.running == 3  # all three turns are waiting on the LLM at the same time
    gate.release.set()
    pool.shutdown()
    assert gate.peak == 3


def test_a_conversation_in_flight_is_not_started_twice(
    gated: tuple[ScreeningService, Gate],
) -> None:
    service, gate = gated
    waiting_conversations(service, 1)
    pool = TurnPool(service, size=4, debounce=NO_DEBOUNCE)
    assert pool.fill() == 1
    assert pool.fill() == 0  # still waiting in the database, but already running here
    assert pool.in_flight == 1
    gate.release.set()
    pool.shutdown()
    assert pool.in_flight == 0
    assert service.waiting(NO_DEBOUNCE) == []


def test_the_debounce_still_holds_back_a_burst(
    gated: tuple[ScreeningService, Gate], clock: FakeClock
) -> None:
    service, gate = gated
    gate.release.set()
    waiting_conversations(service, 1)
    pool = TurnPool(service, size=2, debounce=timedelta(seconds=2))
    assert pool.fill() == 0
    clock.advance(seconds=2)
    assert pool.fill() == 1
    pool.shutdown()


def test_a_failing_turn_frees_its_slot(
    config: ClientConfig, ports: Ports, clock: FakeClock
) -> None:
    class Broken(ScreeningService):
        def process_pending(self, conversation_id: UUID) -> None:  # type: ignore[override]
            raise RuntimeError("database down")

    base = make_service(config, ports, clock)
    service = Broken(base.repo, config, ports)
    waiting_conversations(service, 1)
    pool = TurnPool(service, size=1, debounce=NO_DEBOUNCE)
    assert pool.fill() == 1
    pool.shutdown()
    assert pool.in_flight == 0


def test_pool_needs_a_thread(config: ClientConfig, ports: Ports, clock: FakeClock) -> None:
    with pytest.raises(ValueError, match="at least one"):
        TurnPool(make_service(config, ports, clock), size=0, debounce=NO_DEBOUNCE)


# --- Funnel metrics -------------------------------------------------------------------------


def test_cost_and_turns_are_recorded_when_a_conversation_finishes(
    config: ClientConfig, ports: Ports, clock: FakeClock, extractor: ScriptedExtractor
) -> None:
    service = make_service(config, ports, clock)
    cid = service.start_conversation()
    service.receive(cid, "no acepto")
    extractor.push(ex(yes_no=False))
    turns = metrics.CONVERSATION_TURNS.labels(Status.ABANDONED.value)
    count_before = _count(turns)
    service.process_pending(cid)
    state = service.get(cid)
    assert state is not None
    assert state.status == Status.ABANDONED
    assert state.turns == 1
    assert _count(turns) == count_before + 1


def test_cost_per_turn_adds_up_the_calls_inside_it() -> None:
    with metrics.turn_cost() as cost:
        metrics.record_llm_call("test", "extract", 0.1, 10, 5, 0.002)
        metrics.record_llm_call("test", "compose", 0.1, 10, 5, 0.001)
    metrics.record_llm_call("test", "compose", 0.1, 10, 5, 0.5)  # outside: not this turn
    assert cost.usd == pytest.approx(0.003)


def test_drop_offs_are_counted_by_stage(
    config: ClientConfig, ports: Ports, clock: FakeClock
) -> None:
    service = make_service(config, ports, clock)
    service.start_conversation()
    before = metrics.DROP_OFFS.labels("consent")._value.get()
    for hours in (2, 22, 24):
        clock.advance(hours=hours)
        service.send_due_reminders()
    assert metrics.DROP_OFFS.labels("consent")._value.get() == before + 1


def _count(histogram: object) -> float:
    for metric in histogram.collect():  # type: ignore[attr-defined]
        for sample in metric.samples:
            if sample.name.endswith("_count"):
                return float(sample.value)
    return 0.0
