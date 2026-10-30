"""Candidate list: paged in SQL, with every filter in SQL so totals and pages agree."""

from datetime import UTC, datetime

import pytest

from app.adapters.clock.clocks import FakeClock
from app.adapters.storage.dashboard import DashboardQueries, Filters
from app.adapters.storage.sql import SqlRepository, make_engine
from app.domain.config import ClientConfig
from app.domain.models import Availability, ConversationState, Schedule


@pytest.fixture
def queries(config: ClientConfig):
    engine = make_engine("sqlite:///:memory:")
    repo = SqlRepository(engine, FakeClock(datetime(2026, 11, 2, tzinfo=UTC)), config.client_id)
    for n in range(120):
        state = ConversationState(client_id=config.client_id)
        state.candidate.full_name = f"Candidate {n:03}"
        state.candidate.availability = (
            [Availability.WEEKENDS] if n % 3 == 0 else [Availability.FULL_TIME]
        )
        state.candidate.schedule = [Schedule.MORNING, Schedule.EVENING] if n % 2 else []
        repo.create(state, [], [])
    yield DashboardQueries(engine, config.client_id)
    engine.dispose()


def test_pages_cover_every_candidate_once(queries: DashboardQueries) -> None:
    pages = [queries.candidates(Filters(), page=n, per_page=50) for n in (1, 2, 3)]
    assert [len(p.items) for p in pages] == [50, 50, 20]
    assert {p.total for p in pages} == {120}
    assert pages[0].pages == 3
    assert (pages[0].first, pages[2].first) == (1, 101)
    ids = [i.id for p in pages for i in p.items]
    assert len(set(ids)) == 120


def test_list_filters_run_in_sql_so_the_total_is_right(queries: DashboardQueries) -> None:
    weekends = queries.candidates(Filters(availability=Availability.WEEKENDS), per_page=10)
    assert (weekends.total, len(weekends.items)) == (40, 10)
    assert all("weekends" in i.availability for i in weekends.items)
    evenings = queries.candidates(Filters(schedule=Schedule.EVENING), per_page=100)
    assert evenings.total == 60
    both = queries.candidates(
        Filters(availability=Availability.WEEKENDS, schedule=Schedule.MORNING), per_page=100
    )
    assert both.total == 20  # odd multiples of 3


def test_a_page_past_the_end_is_empty(queries: DashboardQueries) -> None:
    page = queries.candidates(Filters(), page=9, per_page=50)
    assert (page.items, page.total, page.first) == ([], 120, 0)
