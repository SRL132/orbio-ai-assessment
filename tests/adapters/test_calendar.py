from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from app.adapters.calendar.sql_calendar import SqlCalendar
from app.adapters.clock.clocks import FakeClock
from app.adapters.storage.sql import ConversationRow, make_engine
from app.domain.config import ClientConfig

_ENGINES = []


@pytest.fixture(autouse=True)
def _dispose_engines():
    yield
    while _ENGINES:
        _ENGINES.pop().dispose()


def calendar(config: ClientConfig, clock: FakeClock) -> SqlCalendar:
    engine = make_engine("sqlite:///:memory:")
    _ENGINES.append(engine)
    with Session(engine) as session, session.begin():  # bookings reference a conversation
        for n in (1, 2):
            session.add(
                ConversationRow(
                    id=str(UUID(int=n)),
                    client_id="c",
                    status="qualified",
                    stage="closed",
                    language="es",
                    availability=[],
                    schedule=[],
                    review_reasons=[],
                    created_at=clock.now(),
                    updated_at=clock.now(),
                    state={},
                )
            )
    return SqlCalendar(engine, config, clock)


def test_weekday_slots_in_local_time(config: ClientConfig, clock: FakeClock) -> None:
    friday_evening = datetime(2026, 11, 6, 20, 0, tzinfo=UTC)
    slots = calendar(config, clock).available_slots("es-madrid", friday_evening, 3)
    # Next slots are Monday 10:00, 12:00, 17:00 Madrid time (UTC+1 in November).
    assert slots == [
        datetime(2026, 11, 9, 9, 0, tzinfo=UTC),
        datetime(2026, 11, 9, 11, 0, tzinfo=UTC),
        datetime(2026, 11, 9, 16, 0, tzinfo=UTC),
    ]


def test_mexico_city_slots_use_its_timezone(config: ClientConfig, clock: FakeClock) -> None:
    slots = calendar(config, clock).available_slots("mx-cdmx", clock.now(), 1)
    assert slots == [datetime(2026, 11, 2, 16, 0, tzinfo=UTC)]  # 10:00 in Mexico City


def test_a_slot_can_only_be_booked_once(config: ClientConfig, clock: FakeClock) -> None:
    cal = calendar(config, clock)
    slot = cal.available_slots("es-madrid", clock.now(), 1)[0]
    assert cal.book("es-madrid", slot, UUID(int=1)) is True
    assert cal.book("es-madrid", slot, UUID(int=2)) is False
    assert slot not in cal.available_slots("es-madrid", clock.now(), 3)


def test_unknown_location_has_no_slots(config: ClientConfig, clock: FakeClock) -> None:
    assert calendar(config, clock).available_slots("nowhere", clock.now(), 3) == []
