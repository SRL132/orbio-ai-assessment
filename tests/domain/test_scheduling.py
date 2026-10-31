from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from app.adapters.clock.clocks import FakeClock
from app.adapters.llm.fake import ScriptedExtractor, TemplateResponder
from app.domain import engine
from app.domain.config import ClientConfig
from app.domain.models import (
    Availability,
    ConversationState,
    InboundMessage,
    Schedule,
    Stage,
    Status,
    Vehicle,
)
from app.domain.ports import Ports
from tests.conftest import ex, said


class MemoryCalendar:
    def __init__(self, start: datetime) -> None:
        self.free = [start + timedelta(days=d, hours=h) for d in (1, 2) for h in (0, 2)]
        self.booked: dict[datetime, UUID] = {}

    def available_slots(self, location_id: str, after: datetime, count: int) -> list[datetime]:
        return [s for s in self.free if s >= after and s not in self.booked][:count]

    def book(self, location_id: str, starts_at: datetime, conversation_id: UUID) -> bool:
        if starts_at in self.booked:
            return self.booked[starts_at] == conversation_id
        self.booked[starts_at] = conversation_id
        return True


@pytest.fixture
def calendar(clock: FakeClock) -> MemoryCalendar:
    return MemoryCalendar(clock.now())


@pytest.fixture
def run(
    state: ConversationState,
    config: ClientConfig,
    extractor: ScriptedExtractor,
    clock: FakeClock,
    calendar: MemoryCalendar,
):
    ports = Ports(extractor, TemplateResponder(), clock, calendar)
    holder = {"state": engine.start(state, config, ports).state}

    def say(extraction):
        extractor.push(extraction)
        message = InboundMessage(
            conversation_id=UUID(int=1), text=said(extraction.fields), received_at=clock.now()
        )
        result = engine.turn(holder["state"], message, config, ports)
        holder["state"] = result.state
        return result

    say(ex(yes_no=True))
    say(ex(full_name="Ana López", has_license=True, vehicle=Vehicle.MOTO))
    say(ex(city="Madrid", zone="Retiro", availability=[Availability.FULL_TIME]))
    say(ex(schedule=[Schedule.MORNING], experience_years=1))
    say(ex(start_date=clock.now().date() + timedelta(days=3)))
    return say


def test_qualified_candidate_gets_slots_in_local_time(run, calendar: MemoryCalendar) -> None:
    result = run(ex(yes_no=True))
    assert result.state.status == Status.QUALIFIED
    assert result.state.stage == Stage.SCHEDULING
    assert len(result.state.offered_slots) == 3
    offer = result.replies[-1]
    # Clock starts 2026-11-02 10:00 UTC = 11:00 in Madrid; first slot is a day later.
    assert "1) mar 3/11 11:00" in offer
    assert "Responde con el número" in offer


def test_picking_a_slot_books_the_interview(run, calendar: MemoryCalendar) -> None:
    run(ex(yes_no=True))
    result = run(ExtractionWithSlot(2))
    assert result.state.stage == Stage.CLOSED
    assert result.state.interview_at == calendar.free[1]
    assert calendar.booked[calendar.free[1]] == result.state.conversation_id
    assert "Tu entrevista es el" in result.replies[-1]
    assert result.state.summary is not None
    assert "Interview booked" in result.state.summary


def test_taken_slot_offers_new_ones(run, calendar: MemoryCalendar) -> None:
    run(ex(yes_no=True))
    calendar.booked[calendar.free[0]] = UUID(int=99)  # someone else got it first
    result = run(ExtractionWithSlot(1))
    assert result.state.stage == Stage.SCHEDULING
    assert result.state.interview_at is None
    assert calendar.free[0] not in result.state.offered_slots
    assert any("se acaba de ocupar" in r for r in result.replies)


def test_no_slot_fits_hands_to_recruiter(run) -> None:
    run(ex(yes_no=True))
    result = run(ex(yes_no=False))
    assert result.state.stage == Stage.CLOSED
    assert result.state.no_slot_fits is True
    assert result.state.status == Status.QUALIFIED
    assert "no offered interview slot fits" in (result.state.summary or "")


def test_unclear_answer_repeats_the_offer(run) -> None:
    run(ex(yes_no=True))
    result = run(ex())
    assert result.state.stage == Stage.SCHEDULING
    assert "1)" in result.replies[-1]


def ExtractionWithSlot(n: int):
    extraction = ex()
    extraction.slot_choice = n
    return extraction


def test_utc_slot_fixture_is_aware() -> None:
    assert datetime(2026, 11, 2, tzinfo=UTC).tzinfo is UTC
