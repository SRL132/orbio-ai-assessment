from datetime import timedelta
from pathlib import Path

import pytest

from app.adapters.clock.clocks import FakeClock
from app.adapters.storage.sql import REDACTED, SqlRepository, make_engine
from app.domain.models import ConversationState, DomainEvent, Status
from tests.conftest import write_concurrently


@pytest.fixture
def repo(clock: FakeClock):
    engine = make_engine("sqlite:///:memory:")
    yield SqlRepository(engine, clock)
    engine.dispose()


def test_create_and_get_round_trip(repo: SqlRepository, state: ConversationState) -> None:
    state.candidate.full_name = "Ana López"
    repo.create(state, [DomainEvent(type="conversation_started")], ["Hola"])
    loaded = repo.get(state.conversation_id)
    assert loaded == state
    assert [m.text for m in repo.messages(state.conversation_id)] == ["Hola"]
    assert [e.type for e in repo.events(state.conversation_id)] == ["conversation_started"]


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
    tmp_path: Path, clock: FakeClock, state: ConversationState
) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'race.db'}")  # a file: threads share it for real
    repo = SqlRepository(engine, clock)
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
