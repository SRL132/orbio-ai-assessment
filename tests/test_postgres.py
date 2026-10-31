"""Postgres integration: runs only when TEST_DATABASE_URL points at a disposable database (CI)."""

import os
import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text

from app.adapters.calendar.sql_calendar import SqlCalendar
from app.adapters.clock.clocks import FakeClock
from app.adapters.storage.dashboard import DashboardQueries, Filters
from app.adapters.storage.rate_limit import SqlRateLimiter
from app.adapters.storage.sql import SqlRepository, make_engine
from app.domain.config import ClientConfig
from app.domain.models import Availability, ConversationState, DomainEvent, Stage, Status
from tests.conftest import claim_concurrently, write_concurrently

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
    clock.advance(minutes=11)
    assert calendar.release_orphans() == 1  # the conversation never recorded the interview


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
    a = DashboardQueries(engine, config.client_id).analytics(config.field_priority, start)
    assert (a.started, a.consented, a.completed, a.median_minutes) == (4, 3, 3, 4.0)
    assert dict(a.funnel)["start_date"] == 3


def test_exactly_one_worker_wins_a_lease_race(engine, config: ClientConfig) -> None:
    clock = FakeClock(datetime(2026, 11, 2, 10, tzinfo=UTC))
    repo = SqlRepository(engine, clock, config.client_id)
    state = ConversationState(client_id=config.client_id)
    repo.create(state, [], [])
    barrier = threading.Barrier(16)
    winners: list[str] = []

    def race(owner: str) -> None:
        barrier.wait()
        if repo.try_lease(state.conversation_id, owner, timedelta(minutes=2)):
            winners.append(owner)

    threads = [threading.Thread(target=race, args=(f"w{n}",)) for n in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(winners) == 1


def test_list_filters_on_json_columns(engine, config: ClientConfig) -> None:
    repo = SqlRepository(engine, FakeClock(datetime(2026, 11, 2, tzinfo=UTC)), config.client_id)
    for availability in (
        [Availability.WEEKENDS],
        [Availability.FULL_TIME, Availability.WEEKENDS],
        [],
    ):
        state = ConversationState(client_id=config.client_id)
        state.candidate.availability = availability
        repo.create(state, [], [])
    page = DashboardQueries(engine, config.client_id).candidates(
        Filters(availability=Availability.WEEKENDS), per_page=1
    )
    assert (page.total, len(page.items), page.pages) == (2, 1, 2)


def test_backlog_ages_are_timezone_aware(engine, config: ClientConfig) -> None:
    clock = FakeClock(datetime(2026, 11, 2, 10, tzinfo=UTC))
    repo = SqlRepository(engine, clock, config.client_id)
    state = ConversationState(client_id=config.client_id)
    repo.create(state, [], [])
    repo.add_inbound(state.conversation_id, "hola")
    count, oldest = DashboardQueries(engine, config.client_id).backlog()["pending_messages"]
    assert count == 1
    assert oldest is not None
    assert clock.now() - oldest == timedelta(0)  # would raise if the timestamp were naive


def test_rate_limit_is_exact_under_concurrency(engine) -> None:
    limiter = SqlRateLimiter(engine, FakeClock(datetime(2026, 11, 2, tzinfo=UTC)), "c")
    barrier = threading.Barrier(16)
    allowed: list[str] = []

    def request() -> None:
        barrier.wait()
        if hit := limiter.allow("burst", 5, timedelta(minutes=1)):
            allowed.append(hit)

    threads = [threading.Thread(target=request) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(allowed) == 5


def test_exactly_one_concurrent_copy_of_a_delivery_wins(engine, config: ClientConfig) -> None:
    repo = SqlRepository(engine, FakeClock(datetime(2026, 11, 2, tzinfo=UTC)), config.client_id)
    assert claim_concurrently(repo) == 1
