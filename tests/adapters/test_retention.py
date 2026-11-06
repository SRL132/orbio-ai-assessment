"""Retention: erase disqualified and abandoned candidates after the retention period."""

from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.calendar.sql_calendar import SqlCalendar
from app.adapters.clock.clocks import FakeClock
from app.adapters.storage.retention import SqlRetention
from app.adapters.storage.sql import (
    ConversationRow,
    ErasureRow,
    EventRow,
    InterviewRow,
    MessageRow,
    SqlRepository,
    make_engine,
)
from app.domain.config import ClientConfig
from app.domain.models import ConversationState, DomainEvent, EventType, Status


@pytest.fixture
def setup(config: ClientConfig, clock: FakeClock):
    engine = make_engine("sqlite:///:memory:")
    repo = SqlRepository(engine, clock, config.client_id)
    yield engine, repo, SqlRetention(engine, config, clock)
    engine.dispose()


def candidate(
    repo: SqlRepository,
    status: Status,
    keep_on_file: bool | None = None,
    client: str = "grupo_sazon",
) -> str:
    state = ConversationState(client_id=client, status=status, keep_on_file=keep_on_file)
    state.candidate.full_name = "Ana López"
    repo.create(state, [DomainEvent(type=EventType.CONVERSATION_STARTED)], ["Hola"])
    repo.add_inbound(state.conversation_id, "mi nombre es Ana López")
    repo.append_events(state, [DomainEvent(type=EventType.NOTE_ADDED, data={"text": "llamar"})])
    return str(state.conversation_id)


def count(engine, table, conversation_id: str) -> int:
    with Session(engine) as session:
        return session.scalar(
            select(func.count()).select_from(table).where(table.conversation_id == conversation_id)
        )


def test_expired_candidates_are_erased_completely(
    setup, config: ClientConfig, clock: FakeClock
) -> None:
    engine, repo, retention = setup
    gone = candidate(repo, Status.DISQUALIFIED)
    SqlCalendar(engine, config, clock).book("es-madrid", clock.now(), gone)
    clock.advance(days=config.retention_days - 1)
    assert retention.erase_expired() == 0  # not yet
    clock.advance(days=2)
    assert retention.erase_expired() == 1

    for table in (MessageRow, EventRow, InterviewRow):
        assert count(engine, table, gone) == 0
    with Session(engine) as session:
        assert session.get(ConversationRow, gone) is None
        proof = session.scalars(select(ErasureRow)).one()
    assert (proof.conversation_id, proof.status) == (gone, "disqualified")
    assert retention.erase_expired() == 0  # idempotent


def test_kept_on_file_and_open_candidates_stay(
    setup, config: ClientConfig, clock: FakeClock
) -> None:
    engine, repo, retention = setup
    kept = candidate(repo, Status.DISQUALIFIED, keep_on_file=True)
    abandoned = candidate(repo, Status.ABANDONED, keep_on_file=False)
    qualified = candidate(repo, Status.QUALIFIED)
    review = candidate(repo, Status.NEEDS_REVIEW)
    clock.advance(days=config.retention_days + 1)
    assert retention.erase_expired() == 1
    with Session(engine) as session:
        assert session.get(ConversationRow, abandoned) is None
        for keep in (kept, qualified, review):
            assert session.get(ConversationRow, keep) is not None


def test_erases_in_batches(setup, config: ClientConfig, clock: FakeClock) -> None:
    _, repo, retention = setup
    for _ in range(205):  # more than one batch
        candidate(repo, Status.ABANDONED)
    clock.advance(days=config.retention_days + 1)
    assert retention.erase_expired() == 205


def test_late_activity_restarts_the_clock(setup, config: ClientConfig, clock: FakeClock) -> None:
    engine, repo, retention = setup
    cid = candidate(repo, Status.ABANDONED)
    clock.advance(days=config.retention_days - 10)
    state = repo.get(UUID(cid))
    assert state is not None
    repo.save_turn(state, [], [], [])  # the candidate came back
    clock.advance(days=20)
    assert retention.erase_expired() == 0
    assert count(engine, MessageRow, cid) > 0


def test_other_clients_are_untouched(setup, config: ClientConfig, clock: FakeClock) -> None:
    engine, _, _ = setup
    other = SqlRepository(engine, clock, "another_client")
    cid = candidate(other, Status.ABANDONED, client="another_client")
    clock.advance(days=config.retention_days + 1)
    assert SqlRetention(engine, config, clock).erase_expired() == 0
    assert count(engine, MessageRow, cid) == 2  # the greeting and the reply


def test_invite_never_opened_is_erased_after_the_retention_period(
    setup, config: ClientConfig, clock: FakeClock
) -> None:
    engine, repo, retention = setup
    state = ConversationState(client_id=config.client_id, invite_pending=True)
    repo.create(state, [], ["hola"])
    clock.advance(days=config.retention_days - 1)
    assert retention.erase_expired() == 0
    clock.advance(days=2)
    assert retention.erase_expired() == 1
    assert count(engine, MessageRow, str(state.conversation_id)) == 0


# --- Periods counsel sets for everyone else (docs/failure-modes.md, gap 16) ----------------------


@pytest.mark.parametrize("status", [Status.QUALIFIED, Status.NEEDS_REVIEW])
def test_kept_until_a_period_is_set(setup, clock: FakeClock, status: Status) -> None:
    _engine, repo, retention = setup
    cid = candidate(repo, status)
    clock.advance(days=4000)
    assert retention.erase_expired() == 0
    assert repo.get(UUID(cid)) is not None


@pytest.mark.parametrize("status", [Status.QUALIFIED, Status.NEEDS_REVIEW])
def test_a_period_erases_them_after_it(
    setup, config: ClientConfig, clock: FakeClock, status: Status
) -> None:
    engine, repo, _ = setup
    periods = {"retention_days_by_status": {status: 10}}
    retention = SqlRetention(engine, config.model_copy(update=periods), clock)
    cid = candidate(repo, status)
    clock.advance(days=9)
    assert retention.erase_expired() == 0
    clock.advance(days=2)
    assert retention.erase_expired() == 1
    assert repo.get(UUID(cid)) is None


def test_a_status_period_leaves_on_file_candidates_alone(
    setup, config: ClientConfig, clock: FakeClock
) -> None:
    engine, repo, _ = setup
    periods = {"retention_days_by_status": {Status.QUALIFIED: 10}}
    retention = SqlRetention(engine, config.model_copy(update=periods), clock)
    cid = candidate(repo, Status.QUALIFIED, keep_on_file=True)
    clock.advance(days=30)
    assert retention.erase_expired() == 0
    assert repo.get(UUID(cid)) is not None


def test_an_on_file_period_erases_them_after_it(
    setup, config: ClientConfig, clock: FakeClock
) -> None:
    engine, repo, _ = setup
    retention = SqlRetention(
        engine, config.model_copy(update={"retention_days_on_file": 10}), clock
    )
    cid = candidate(repo, Status.DISQUALIFIED, keep_on_file=True)
    other = candidate(repo, Status.QUALIFIED)
    clock.advance(days=9)
    assert retention.erase_expired() == 0
    clock.advance(days=2)
    assert retention.erase_expired() == 1
    assert repo.get(UUID(cid)) is None
    assert repo.get(UUID(other)) is not None


def test_open_candidates_cant_be_given_a_period(config: ClientConfig) -> None:
    data = config.model_dump()
    data["retention_days_by_status"] = {"in_progress": 10}
    with pytest.raises(ValueError, match="only covers"):
        ClientConfig.model_validate(data)
