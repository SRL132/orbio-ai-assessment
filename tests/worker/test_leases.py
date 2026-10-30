"""Leases: one conversation is processed by one worker at a time, across processes."""

import threading
from datetime import date
from pathlib import Path

import pytest

from app.adapters.clock.clocks import FakeClock
from app.adapters.llm.fake import ScriptedExtractor, TemplateResponder
from app.adapters.storage.sql import SqlRepository, make_engine
from app.domain.config import ClientConfig
from app.domain.models import ConversationState, ExtractionResult, Status
from app.domain.ports import LeaseLost, Ports
from app.worker.service import LEASE, ConversationBusy, ScreeningService
from tests.conftest import ex


class GatedExtractor(ScriptedExtractor):
    """Blocks inside the LLM call until released, so a test can act while a turn is running."""

    def __init__(self) -> None:
        super().__init__()
        self.inside = threading.Event()
        self.release = threading.Event()

    def extract(
        self, state: ConversationState, text: str, config: ClientConfig, today: date
    ) -> ExtractionResult:
        self.inside.set()
        assert self.release.wait(5)
        return super().extract(state, text, config, today)


@pytest.fixture
def db(tmp_path: Path):
    engine = make_engine(f"sqlite:///{tmp_path / 'leases.db'}")  # shared by the "workers"
    yield engine
    engine.dispose()


def worker(db, config: ClientConfig, clock: FakeClock, extractor: ScriptedExtractor):
    """One worker process: its own repository and service over the shared database."""
    repo = SqlRepository(db, clock, config.client_id)
    ports = Ports(extractor=extractor, responder=TemplateResponder(), clock=clock)
    return ScreeningService(repo, config, ports, recruiter_wait_seconds=0)


def test_second_worker_backs_off_and_the_first_picks_up_new_messages(
    db, config: ClientConfig, clock: FakeClock
) -> None:
    gated = GatedExtractor()
    a = worker(db, config, clock, gated)
    b = worker(db, config, clock, ScriptedExtractor())
    cid = a.start_conversation()
    a.receive(cid, "sí")
    gated.push(ex(yes_no=True), ex(full_name="Ana López"))

    running = threading.Thread(target=a.process_pending, args=(cid,))
    running.start()
    assert gated.inside.wait(5)
    b.receive(cid, "soy Ana López")  # arrives while worker A is mid-turn
    assert b.process_pending(cid) is None  # B can't take the conversation, and doesn't wait
    gated.release.set()
    running.join(5)

    assert gated.seen == ["sí", "soy Ana López"]  # A ran a second turn for the new message
    assert a.repo.pending_inbound(cid) == []
    state = a.get(cid)
    assert state is not None
    assert state.candidate.full_name == "Ana López"


def test_a_crashed_workers_lease_expires(
    db, config: ClientConfig, clock: FakeClock, extractor: ScriptedExtractor
) -> None:
    service = worker(db, config, clock, extractor)
    cid = service.start_conversation()
    assert service.repo.try_lease(cid, "crashed", LEASE)
    service.receive(cid, "sí")
    extractor.push(ex(yes_no=True))
    assert service.process_pending(cid) is None
    clock.advance(seconds=LEASE.total_seconds() + 1)
    assert service.process_pending(cid) is not None


def test_a_worker_that_lost_its_lease_cannot_save(
    db, config: ClientConfig, clock: FakeClock, extractor: ScriptedExtractor
) -> None:
    service = worker(db, config, clock, extractor)
    cid = service.start_conversation()
    state = service.get(cid)
    assert state is not None
    assert service.repo.try_lease(cid, "slow", LEASE)
    clock.advance(seconds=LEASE.total_seconds() + 1)  # the slow worker's lease ran out
    assert service.repo.try_lease(cid, "new", LEASE)
    with pytest.raises(LeaseLost):
        service.repo.save_turn(state, [], ["stale reply"], [], lease_owner="slow")
    service.repo.release_lease(cid, "slow")  # releasing a lost lease changes nothing
    assert not service.repo.try_lease(cid, "third", LEASE)  # "new" still holds it


def test_recruiter_override_on_a_busy_conversation_is_refused(
    db, config: ClientConfig, clock: FakeClock, extractor: ScriptedExtractor
) -> None:
    service = worker(db, config, clock, extractor)
    cid = service.start_conversation()
    assert service.repo.try_lease(cid, "turn", LEASE)
    with pytest.raises(ConversationBusy):
        service.override(cid, Status.QUALIFIED, "checked by phone", "r@sazon.es")
    service.repo.release_lease(cid, "turn")
    service.override(cid, Status.QUALIFIED, "checked by phone", "r@sazon.es")
    state = service.get(cid)
    assert state is not None
    assert state.status == Status.QUALIFIED


def test_reminders_skip_a_busy_conversation(
    db, config: ClientConfig, clock: FakeClock, extractor: ScriptedExtractor
) -> None:
    service = worker(db, config, clock, extractor)
    cid = service.start_conversation()
    clock.advance(hours=2)
    assert service.repo.try_lease(cid, "turn", LEASE)
    assert service.send_due_reminders() == 0
    service.repo.release_lease(cid, "turn")
    assert service.send_due_reminders() == 1


def test_fast_forwarding_the_demo_clock_does_not_expire_a_lease(
    db, config: ClientConfig, clock: FakeClock
) -> None:
    lease_clock = FakeClock(clock.now())  # stands in for the system clock
    repo = SqlRepository(db, clock, config.client_id, lease_clock=lease_clock)
    state = ConversationState(client_id=config.client_id)
    repo.create(state, [], [])
    assert repo.try_lease(state.conversation_id, "turn", LEASE)
    clock.advance(hours=24)  # the demo's "skip ahead" button
    assert not repo.try_lease(state.conversation_id, "other", LEASE)
    lease_clock.advance(seconds=LEASE.total_seconds() + 1)  # real time passes
    assert repo.try_lease(state.conversation_id, "other", LEASE)
