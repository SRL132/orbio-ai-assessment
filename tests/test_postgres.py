"""Postgres integration: runs only when TEST_DATABASE_URL points at a disposable database (CI)."""

import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text

from app.adapters.calendar.sql_calendar import SqlCalendar
from app.adapters.clock.clocks import FakeClock
from app.adapters.storage.sql import SqlRepository, make_engine
from app.domain.config import ClientConfig
from app.domain.models import ConversationState, DomainEvent

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")


@pytest.fixture
def engine():
    assert URL is not None
    reset = create_engine(URL)
    with reset.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    reset.dispose()
    engine = make_engine(URL)  # runs the migrations
    yield engine
    engine.dispose()


def test_round_trip_and_double_booking(engine, config: ClientConfig) -> None:
    clock = FakeClock(datetime(2026, 11, 2, 10, tzinfo=UTC))
    repo = SqlRepository(engine, clock)
    state = ConversationState(client_id=config.client_id, channel="voice")
    state.candidate.full_name = "Ana López"
    repo.create(state, [DomainEvent(type="conversation_started", data={"n": 1})], ["Hola"])
    assert repo.get(state.conversation_id) == state
    assert repo.messages(state.conversation_id)[0].created_at.tzinfo is not None

    other = ConversationState(client_id=config.client_id)
    repo.create(other, [], [])
    calendar = SqlCalendar(engine, config, clock)
    slot = calendar.available_slots("es-madrid", clock.now(), 1)[0]
    assert calendar.book("es-madrid", slot, state.conversation_id) is True
    # A real second candidate: the unique constraint (not a foreign key) must reject it.
    assert calendar.book("es-madrid", slot, other.conversation_id) is False
