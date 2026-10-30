"""Postgres integration: runs only when TEST_DATABASE_URL points at a disposable database (CI)."""

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text

from app.adapters.calendar.sql_calendar import SqlCalendar
from app.adapters.clock.clocks import FakeClock
from app.adapters.storage.dashboard import DashboardQueries
from app.adapters.storage.sql import SqlRepository, make_engine
from app.domain.config import ClientConfig
from app.domain.models import ConversationState, DomainEvent, Stage, Status
from tests.conftest import write_concurrently

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
    repo = SqlRepository(engine, clock, config.client_id)
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


def test_concurrent_writers_get_distinct_seqs(engine, config: ClientConfig) -> None:
    repo = SqlRepository(engine, FakeClock(datetime(2026, 11, 2, 10, tzinfo=UTC)), config.client_id)
    state = ConversationState(client_id=config.client_id)
    repo.create(state, [], ["greeting"])
    assert write_concurrently(repo, state) == list(range(1, 162))


def test_analytics_aggregate_in_sql(engine, config: ClientConfig) -> None:
    start = datetime(2026, 11, 2, 10, tzinfo=UTC)
    repo = SqlRepository(engine, FakeClock(start), config.client_id)
    for minutes in (4, 2, 9):
        state = ConversationState(
            client_id=config.client_id,
            status=Status.QUALIFIED,
            stage=Stage.CLOSED,
            started_at=start,
            ended_at=start + timedelta(minutes=minutes),
            progress=7,
        )
        repo.create(state, [], [])
    repo.create(ConversationState(client_id=config.client_id), [], [])
    a = DashboardQueries(engine, config.client_id).analytics(config.field_priority)
    assert (a.started, a.consented, a.completed, a.median_minutes) == (4, 3, 3, 4.0)
    assert dict(a.funnel)["start_date"] == 3
