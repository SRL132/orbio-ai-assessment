from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest

from app.adapters.calendar.sql_calendar import SqlCalendar
from app.adapters.clock.clocks import FakeClock
from app.adapters.llm.fake import ScriptedExtractor, TemplateResponder
from app.adapters.storage.sql import REDACTED, SqlRepository, make_engine
from app.domain.config import ClientConfig
from app.domain.models import (
    ConversationState,
    DomainEvent,
    EventType,
    ExtractionResult,
    Stage,
    Status,
    StoredMessage,
)
from app.domain.ports import Ports
from app.worker.service import RECENT_MESSAGE_CHARS, RECENT_MESSAGES, ScreeningService
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


class ContextSpy(ScriptedExtractor):
    """Records the conversation context each extraction was given."""

    def __init__(self) -> None:
        super().__init__()
        self.contexts: list[list[tuple[str, str]]] = []

    def extract(self, state, text, config, today):  # type: ignore[no-untyped-def]
        self.contexts.append(list(state.recent_messages))
        return super().extract(state, text, config, today)


def test_llm_gets_earlier_messages_as_context_but_not_the_one_being_read(
    config: ClientConfig, ports: Ports, clock: FakeClock
) -> None:
    spy = ContextSpy()
    service = make_service(config, replace(ports, extractor=spy), clock)
    cid = service.start_conversation()
    service.receive(cid, "sí")
    spy.push(ex(yes_no=True))
    service.process_pending(cid)
    service.receive(cid, "Ana López")
    spy.push(ex(full_name="Ana López"))
    service.process_pending(cid)

    assert [d for d, _ in spy.contexts[0]] == ["out"]  # only the greeting
    assert ("in", "sí") in spy.contexts[1]
    assert not any("Ana López" in text for _, text in spy.contexts[1])


def test_context_is_short_and_recent(config: ClientConfig, ports: Ports, clock: FakeClock) -> None:
    spy = ContextSpy()
    service = make_service(config, replace(ports, extractor=spy), clock)
    cid = service.start_conversation()
    spy.push(ex(yes_no=True))  # before consent, messages aren't kept, so they aren't context either
    for text in ("primero", "x" * 1000, "tercero", "cuarto", "quinto"):
        service.receive(cid, text)
        service.process_pending(cid)

    last = spy.contexts[-1]
    assert len(last) == RECENT_MESSAGES
    assert all(len(text) <= RECENT_MESSAGE_CHARS for _, text in last)
    assert ("in", "x" * RECENT_MESSAGE_CHARS) in spy.contexts[2]


def test_only_a_consent_answer_is_stored_before_consent(
    config: ClientConfig, ports: Ports, clock: FakeClock
) -> None:
    spy = ContextSpy()
    service = make_service(config, replace(ports, extractor=spy), clock)
    cid = service.start_conversation()
    service.receive(cid, "soy Ana López y tengo moto")
    service.process_pending(cid)
    service.receive(cid, "Sí")
    spy.push(ex(yes_no=True))
    service.process_pending(cid)
    service.receive(cid, "Ana López")
    service.process_pending(cid)
    assert ("in", REDACTED) in spy.contexts[1]
    assert not any("soy Ana" in text for context in spy.contexts[1:] for _, text in context)
    assert ("in", "Sí") in spy.contexts[2]


def test_context_never_brings_redacted_data_back(
    config: ClientConfig, ports: Ports, clock: FakeClock
) -> None:
    spy = ContextSpy()
    service = make_service(config, replace(ports, extractor=spy), clock)
    cid = service.start_conversation()
    service.receive(cid, "mi DNI es 12345678Z")
    spy.push(ex(sensitive_data=True))
    service.process_pending(cid)
    service.receive(cid, "sí")
    service.process_pending(cid)

    assert not any("12345678Z" in text for _, text in spy.contexts[1])
    assert ("in", REDACTED) in spy.contexts[1]


def test_context_is_not_saved_with_the_state(
    config: ClientConfig, ports: Ports, clock: FakeClock
) -> None:
    service = make_service(config, ports, clock)
    cid = service.start_conversation()
    service.receive(cid, "sí")
    result = service.process_pending(cid)
    assert result is not None
    assert result.state.recent_messages  # the turn had it
    assert "recent_messages" not in result.state.model_dump()
    stored = service.repo.get(cid)
    assert stored is not None
    assert stored.recent_messages == []


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


def test_start_records_when_the_candidate_applied(
    config: ClientConfig, ports: Ports, clock: FakeClock
) -> None:
    service = make_service(config, ports, clock)
    applied = clock.now() - timedelta(hours=5)
    state = service.repo.get(service.start_conversation(applied_at=applied))
    assert state is not None
    assert state.applied_at == applied
    with pytest.raises(ValueError, match="future"):
        service.start_conversation(applied_at=clock.now() + timedelta(minutes=1))


def test_opening_an_invite_that_no_longer_exists_does_nothing(
    config: ClientConfig, ports: Ports, clock: FakeClock
) -> None:
    service = make_service(config, ports, clock)
    service.recruiter_wait_seconds = 0
    service.open_invite(uuid4())  # erased long ago: the chat page answers 404 from here


def test_opening_an_invite_while_a_turn_runs_keeps_it_pending(
    config: ClientConfig, ports: Ports, clock: FakeClock
) -> None:
    service = make_service(config, ports, clock)
    service.recruiter_wait_seconds = 0
    cid = service.start_conversation(invited_by="ats")
    assert service.repo.try_lease(cid, "other-worker", timedelta(minutes=1))
    service.open_invite(cid)
    state = service.repo.get(cid)
    assert state is not None
    assert state.invite_pending is True


def test_every_kind_of_write_can_be_replayed(
    config: ClientConfig, ports: Ports, clock: FakeClock
) -> None:
    """Override, notes and reminders all go through the log, not just candidate turns."""
    service = make_service(config, ports, clock)
    repo = service.repo
    assert isinstance(repo, SqlRepository)
    cid = service.start_conversation()
    clock.advance(hours=config.reminder_hours[0] + 1)
    assert service.send_due_reminders() == 1
    assert repo.replay(cid) == repo.get(cid)
    service.add_note(cid, "called them", "recruiter@example.com")
    service.record_view(cid, "recruiter@example.com")
    assert repo.replay(cid) == repo.get(cid)
    service.override(cid, Status.QUALIFIED, "checked by phone", "recruiter@example.com")
    assert repo.replay(cid) == repo.get(cid)
    assert repo.rebuild_projections(check_only=True) == []


def test_a_turn_records_what_it_ran_on_once_per_version(
    config: ClientConfig, ports: Ports, clock: FakeClock, extractor: ScriptedExtractor
) -> None:
    service = make_service(config, ports, clock)
    service.agent_version = "v1 fake/keyword"
    cid = service.start_conversation()

    def versions() -> list[tuple[object, object]]:
        return [
            (e.data["from"], e.data["to"])
            for e in service.repo.events(cid)
            if e.type == EventType.AGENT_VERSION_CHANGED
        ]

    for _ in range(2):
        service.receive(cid, "hola")
        service.process_pending(cid)
    assert versions() == [(None, "v1 fake/keyword")]

    service.agent_version = "v2 fake/keyword"
    service.receive(cid, "hola")
    service.process_pending(cid)

    assert versions()[-1] == ("v1 fake/keyword", "v2 fake/keyword")
    state = service.repo.get(cid)
    assert state is not None
    assert state.agent_version == "v2 fake/keyword"
    assert service.repo.rebuild_projections() == []  # the event log gives the same state


def test_no_version_records_nothing(
    config: ClientConfig, ports: Ports, clock: FakeClock, extractor: ScriptedExtractor
) -> None:
    service = make_service(config, ports, clock)
    cid = service.start_conversation()
    service.receive(cid, "hola")
    service.process_pending(cid)

    state = service.repo.get(cid)
    assert state is not None
    assert state.agent_version is None
