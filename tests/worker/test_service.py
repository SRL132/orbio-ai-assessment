from datetime import timedelta

import pytest

from app.adapters.calendar.sql_calendar import SqlCalendar
from app.adapters.clock.clocks import FakeClock
from app.adapters.llm.fake import ScriptedExtractor, TemplateResponder
from app.adapters.storage.sql import SqlRepository, make_engine
from app.domain.config import ClientConfig
from app.domain.models import (
    ConversationState,
    DomainEvent,
    ExtractionResult,
    Stage,
    Status,
    StoredMessage,
)
from app.domain.ports import Ports
from app.worker.service import ScreeningService
from tests.conftest import ex

_ENGINES = []


@pytest.fixture(autouse=True)
def _dispose_engines():
    yield
    while _ENGINES:
        _ENGINES.pop().dispose()


def make_service(config: ClientConfig, ports: Ports, clock: FakeClock) -> ScreeningService:
    engine = make_engine("sqlite:///:memory:")
    _ENGINES.append(engine)
    return ScreeningService(SqlRepository(engine, clock, config.client_id), config, ports)


def test_burst_of_messages_is_one_turn(
    config: ClientConfig, ports: Ports, clock: FakeClock, extractor: ScriptedExtractor
) -> None:
    service = make_service(config, ports, clock)
    cid = service.start_conversation()
    service.receive(cid, "sí")
    service.receive(cid, "  soy Ana López ")
    extractor.push(ex(yes_no=True, full_name="Ana López"))

    result = service.process_pending(cid)
    assert result is not None
    assert extractor.seen == ["sí\nsoy Ana López"]
    assert service.process_pending(cid) is None  # nothing left
    texts = [m.text for m in service.repo.messages(cid)]
    assert texts[0].startswith("¡Hola! Soy Lucía")
    assert texts[-1] == config.template("es", "ask_license")


def test_reminders_are_sent_then_abandoned(
    config: ClientConfig, ports: Ports, clock: FakeClock
) -> None:
    service = make_service(config, ports, clock)
    cid = service.start_conversation()
    assert service.send_due_reminders() == 0
    clock.advance(hours=2)
    assert service.send_due_reminders() == 1
    clock.advance(hours=22)
    assert service.send_due_reminders() == 1
    clock.advance(hours=24)
    assert service.send_due_reminders() == 1
    state = service.repo.get(cid)
    assert state is not None
    assert state.status == Status.ABANDONED
    clock.advance(hours=timedelta(days=7).total_seconds() / 3600)
    assert service.send_due_reminders() == 0


class FlakyRepository(SqlRepository):
    """Fails the next `save_turn`, like a database error after the turn already ran."""

    fail_next_save = False

    def save_turn(
        self,
        state: ConversationState,
        events: list[DomainEvent],
        replies: list[str],
        processed: list[StoredMessage],
        redact: bool = False,
        lease_owner: str | None = None,
    ) -> None:
        if self.fail_next_save:
            self.fail_next_save = False
            raise RuntimeError("database unavailable")
        super().save_turn(state, events, replies, processed, redact, lease_owner)


def at_scheduling(
    config: ClientConfig, clock: FakeClock, extractor: ScriptedExtractor
) -> tuple[ScreeningService, FlakyRepository, SqlCalendar, ConversationState]:
    """A qualified candidate who has been offered one slot and has just answered "la 1"."""
    engine = make_engine("sqlite:///:memory:")
    _ENGINES.append(engine)
    calendar = SqlCalendar(engine, config, clock)
    repo = FlakyRepository(engine, clock, config.client_id)
    service = ScreeningService(repo, config, Ports(extractor, TemplateResponder(), clock, calendar))
    state = ConversationState(
        client_id=config.client_id,
        status=Status.QUALIFIED,
        stage=Stage.SCHEDULING,
        offered_slots=calendar.available_slots("es-madrid", clock.now(), 1),
    )
    state.candidate.location_id = "es-madrid"
    repo.create(state, [], [])
    service.receive(state.conversation_id, "la 1")
    return service, repo, calendar, state


def test_booking_survives_a_failed_save(
    config: ClientConfig, clock: FakeClock, extractor: ScriptedExtractor
) -> None:
    service, repo, _, state = at_scheduling(config, clock, extractor)
    extractor.push(ExtractionResult(slot_choice=1), ExtractionResult(slot_choice=1))

    repo.fail_next_save = True
    with pytest.raises(RuntimeError):
        service.process_pending(state.conversation_id)  # booked, but the turn was not saved
    result = service.process_pending(state.conversation_id)  # the pending message is retried

    assert result is not None
    assert result.state.interview_at == state.offered_slots[0]
    assert "slot_taken" not in [e.type for e in result.events]


def test_orphaned_booking_is_released_if_the_turn_never_retries(
    config: ClientConfig, clock: FakeClock, extractor: ScriptedExtractor
) -> None:
    service, repo, calendar, state = at_scheduling(config, clock, extractor)
    slot = state.offered_slots[0]
    extractor.push(ExtractionResult(slot_choice=1))
    repo.fail_next_save = True
    with pytest.raises(RuntimeError):
        service.process_pending(state.conversation_id)
    assert slot not in calendar.available_slots("es-madrid", clock.now(), 3)

    assert calendar.release_orphans() == 0  # within the grace period: a retry may still come
    clock.advance(minutes=11)
    assert calendar.release_orphans() == 1
    assert slot in calendar.available_slots("es-madrid", clock.now(), 3)


def test_a_saved_booking_is_never_released(
    config: ClientConfig, clock: FakeClock, extractor: ScriptedExtractor
) -> None:
    service, _, calendar, state = at_scheduling(config, clock, extractor)
    extractor.push(ExtractionResult(slot_choice=1))
    service.process_pending(state.conversation_id)
    clock.advance(hours=1)
    assert calendar.release_orphans() == 0
