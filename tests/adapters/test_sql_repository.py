from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from app.adapters.clock.clocks import FakeClock
from app.adapters.storage.sql import (
    DELIVERY_ID_TTL,
    REDACTED,
    ConversationRow,
    EventRow,
    SqlRepository,
    make_engine,
)
from app.domain.config import ClientConfig
from app.domain.models import ConversationState, DomainEvent, EventType, Stage, Status
from tests.conftest import claim_concurrently, write_concurrently


@pytest.fixture
def repo(clock: FakeClock, config: ClientConfig):
    engine = make_engine("sqlite:///:memory:")
    yield SqlRepository(engine, clock, config.client_id)
    engine.dispose()


def test_create_and_get_round_trip(repo: SqlRepository, state: ConversationState) -> None:
    state.candidate.full_name = "Ana López"
    repo.create(state, [DomainEvent(type="conversation_started")], ["Hola"])
    loaded = repo.get(state.conversation_id)
    assert loaded == state
    assert [m.text for m in repo.messages(state.conversation_id)] == ["Hola"]
    assert [e.type for e in repo.events(state.conversation_id)] == ["conversation_started"]


def test_the_event_log_adds_up_to_the_state(repo: SqlRepository, state: ConversationState) -> None:
    repo.create(state, [], [])
    assert repo.replay(state.conversation_id) == state
    changed = state.model_copy(update={"stage": Stage.COLLECTING, "turns": 1})
    repo.save_turn(changed, [], [], [])
    assert repo.replay(state.conversation_id) == changed == repo.get(state.conversation_id)


def test_a_conversation_nobody_has_is_not_replayed(
    repo: SqlRepository, state: ConversationState
) -> None:
    assert repo.replay(state.conversation_id) is None


def test_state_changes_stay_out_of_the_recruiter_timeline(
    repo: SqlRepository, state: ConversationState
) -> None:
    repo.create(state, [DomainEvent(type=EventType.CONVERSATION_STARTED)], [])
    assert [e.type for e in repo.events(state.conversation_id)] == ["conversation_started"]


def test_a_save_that_changes_nothing_records_no_change(
    repo: SqlRepository, state: ConversationState
) -> None:
    repo.create(state, [], [])
    repo.save_turn(state, [], ["hello"], [])
    assert len(_stored_events(repo, state)) == 1


def test_events_of_the_same_instant_keep_their_order(
    repo: SqlRepository, state: ConversationState
) -> None:
    """The fake clock doesn't move, so only their position can order these."""
    repo.create(state, [], [])
    first = state.model_copy(update={"turns": 1})
    second = state.model_copy(update={"turns": 2})
    repo.save_turn(first, [DomainEvent(type=EventType.VIEWED)], [], [])
    repo.append_events(first, [DomainEvent(type=EventType.NOTE_ADDED)])
    repo.save_turn(second, [DomainEvent(type=EventType.EXPORTED)], [], [])
    assert [e.type for e in repo.events(state.conversation_id)] == [
        "viewed",
        "note_added",
        "exported",
    ]
    assert [e.seq for e in _stored_events(repo, state)] == [1, 2, 3, 4, 5, 6]
    assert repo.replay(state.conversation_id) == second


def test_a_clean_projection_needs_no_rebuild(repo: SqlRepository, state: ConversationState) -> None:
    repo.create(state, [], [])
    repo.save_turn(state.model_copy(update={"status": Status.QUALIFIED}), [], [], [])
    assert repo.rebuild_projections(check_only=True) == []
    assert repo.rebuild_projections() == []


def test_a_damaged_projection_is_found_and_rebuilt_from_the_log(
    repo: SqlRepository, state: ConversationState, clock: FakeClock
) -> None:
    repo.create(state, [], [])
    kept = _row(repo, state).updated_at
    clock.advance(hours=1)
    _damage(repo, state)

    assert repo.rebuild_projections(check_only=True) == [state.conversation_id]
    assert _row(repo, state).status == "disqualified"  # a check changes nothing

    assert repo.rebuild_projections() == [state.conversation_id]
    row = _row(repo, state)
    assert row.status == state.status.value
    assert repo.get(state.conversation_id) == state
    assert row.updated_at == kept
    assert repo.rebuild_projections(check_only=True) == []


def test_a_state_saved_before_a_field_existed_is_not_damage(
    repo: SqlRepository, state: ConversationState
) -> None:
    repo.create(state, [], [])
    with repo._sessions.begin() as session:
        row = session.get(ConversationRow, str(state.conversation_id))
        assert row is not None
        row.state = {k: v for k, v in row.state.items() if k != "recap_rounds"}
    assert repo.rebuild_projections(check_only=True) == []


def test_an_unreadable_projection_is_rebuilt(repo: SqlRepository, state: ConversationState) -> None:
    repo.create(state, [], [])
    with repo._sessions.begin() as session:
        row = session.get(ConversationRow, str(state.conversation_id))
        assert row is not None
        row.state = {"status": "not a status"}
    assert repo.rebuild_projections() == [state.conversation_id]
    assert repo.get(state.conversation_id) == state


def test_a_conversation_with_no_history_is_left_alone(
    repo: SqlRepository, state: ConversationState
) -> None:
    repo.create(state, [], [])
    with repo._sessions.begin() as session:
        session.execute(delete(EventRow))
    assert repo.rebuild_projections() == []
    assert repo.get(state.conversation_id) == state


def _stored_events(repo: SqlRepository, state: ConversationState) -> list[EventRow]:
    with repo._sessions() as session:
        return list(
            session.scalars(
                select(EventRow)
                .where(EventRow.conversation_id == str(state.conversation_id))
                .order_by(EventRow.seq)
            )
        )


def _row(repo: SqlRepository, state: ConversationState) -> ConversationRow:
    with repo._sessions() as session:
        row = session.get(ConversationRow, str(state.conversation_id))
        assert row is not None
        return row


def _damage(repo: SqlRepository, state: ConversationState) -> None:
    with repo._sessions.begin() as session:
        row = session.get(ConversationRow, str(state.conversation_id))
        assert row is not None
        row.status = "disqualified"
        row.state = {**row.state, "status": "disqualified"}


def test_messages_are_ordered_and_pending_until_processed(
    repo: SqlRepository, state: ConversationState
) -> None:
    repo.create(state, [], ["greeting"])
    repo.add_inbound(state.conversation_id, "sí")
    repo.add_inbound(state.conversation_id, "soy Ana")
    pending = repo.pending_inbound(state.conversation_id)
    assert [(m.seq, m.text) for m in pending] == [(2, "sí"), (3, "soy Ana")]

    repo.save_turn(state, [], ["¿Apellido?"], pending)
    assert repo.pending_inbound(state.conversation_id) == []
    assert [m.seq for m in repo.messages(state.conversation_id, after_seq=1)] == [2, 3, 4]


def test_concurrent_writers_get_distinct_seqs(
    tmp_path: Path, clock: FakeClock, config: ClientConfig, state: ConversationState
) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'race.db'}")  # a file: threads share it for real
    repo = SqlRepository(engine, clock, config.client_id)
    repo.create(state, [], ["greeting"])
    seqs = write_concurrently(repo, state)
    engine.dispose()
    assert seqs == list(range(1, 162))  # greeting + 160 messages, no gaps or duplicates


def test_duplicate_channel_message_is_ignored(
    repo: SqlRepository, state: ConversationState
) -> None:
    repo.create(state, [], [])
    assert repo.add_inbound(state.conversation_id, "hola", channel_message_id="wamid.1") is True
    assert repo.add_inbound(state.conversation_id, "hola", channel_message_id="wamid.1") is False
    assert len(repo.pending_inbound(state.conversation_id)) == 1


def test_a_delivery_is_claimed_once_per_client(clock: FakeClock, config: ClientConfig) -> None:
    engine = make_engine("sqlite:///:memory:")
    repo = SqlRepository(engine, clock, config.client_id)
    assert repo.claim_delivery("whatsapp", "wamid.1") is True
    assert repo.claim_delivery("whatsapp", "wamid.1") is False  # Meta retried it
    assert repo.claim_delivery("whatsapp", "wamid.2") is True
    assert SqlRepository(engine, clock, "another_client").claim_delivery("whatsapp", "wamid.1")
    engine.dispose()


def test_a_released_delivery_can_be_claimed_again(repo: SqlRepository) -> None:
    assert repo.claim_delivery("whatsapp", "wamid.1") is True
    repo.release_delivery("whatsapp", "wamid.1")
    assert repo.claim_delivery("whatsapp", "wamid.1") is True


def test_old_delivery_ids_are_pruned(repo: SqlRepository, clock: FakeClock) -> None:
    repo.claim_delivery("whatsapp", "wamid.1")
    clock.advance(days=DELIVERY_ID_TTL.days, minutes=1)
    repo.claim_delivery("whatsapp", "wamid.2")  # prunes on the way in
    assert repo.claim_delivery("whatsapp", "wamid.1") is True


def test_exactly_one_concurrent_copy_of_a_delivery_wins(
    tmp_path: Path, clock: FakeClock, config: ClientConfig
) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'race.db'}")  # a file: threads share it for real
    wins = claim_concurrently(SqlRepository(engine, clock, config.client_id))
    engine.dispose()
    assert wins == 1


def test_a_released_job_lease_is_free_at_once(repo: SqlRepository) -> None:
    assert repo.try_job_lease("job", "a", timedelta(minutes=1))
    repo.release_job_lease("job", "b")  # not the holder: nothing happens
    assert not repo.try_job_lease("job", "b", timedelta(minutes=1))
    repo.release_job_lease("job", "a")
    assert repo.try_job_lease("job", "b", timedelta(minutes=1))


def test_sensitive_messages_are_redacted(repo: SqlRepository, state: ConversationState) -> None:
    repo.create(state, [], [])
    repo.add_inbound(state.conversation_id, "mi DNI es 12345678Z")
    pending = repo.pending_inbound(state.conversation_id)
    repo.save_turn(state, [], [], pending, redact=True)
    assert repo.messages(state.conversation_id)[0].text == REDACTED


def test_unknown_conversation_raises(repo: SqlRepository, state: ConversationState) -> None:
    with pytest.raises(KeyError):
        repo.add_inbound(state.conversation_id, "hola")
    assert repo.get(state.conversation_id) is None


def test_due_reminders(repo: SqlRepository, state: ConversationState, clock: FakeClock) -> None:
    state.next_reminder_at = clock.now() + timedelta(hours=2)
    repo.create(state, [], [])
    assert repo.due_reminders(clock.now()) == []
    assert repo.due_reminders(clock.now() + timedelta(hours=2)) == [state.conversation_id]

    state.status = Status.QUALIFIED
    repo.save_turn(state, [], [], [])
    assert repo.due_reminders(clock.now() + timedelta(hours=3)) == []


def test_datetimes_come_back_timezone_aware(repo: SqlRepository, state: ConversationState) -> None:
    repo.create(state, [], ["hola"])
    assert repo.messages(state.conversation_id)[0].created_at.tzinfo is not None


def test_another_clients_conversation_is_invisible(
    clock: FakeClock, config: ClientConfig, state: ConversationState
) -> None:
    engine = make_engine("sqlite:///:memory:")
    SqlRepository(engine, clock, config.client_id).create(state, [], ["Hola"])
    other = SqlRepository(engine, clock, "another_client")
    assert other.get(state.conversation_id) is None
    assert other.messages(state.conversation_id) == []
    assert other.timeline(state.conversation_id) == []
    with pytest.raises(KeyError):
        other.add_inbound(state.conversation_id, "hola")
    with pytest.raises(ValueError, match="serves another_client"):
        other.save_turn(state, [], [], [])
    engine.dispose()
