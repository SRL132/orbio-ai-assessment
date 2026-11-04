"""Migrations: they build the schema the models describe, and refuse pre-migration databases."""

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, text

from app.adapters.clock.clocks import FakeClock
from app.adapters.storage.migrate import alembic_config, upgrade
from app.adapters.storage.sql import Base, SqlRepository
from app.domain.models import ConversationState, DomainEvent, EventType
from tests.conftest import START


def test_migrations_match_the_models(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'm.db'}"
    upgrade(url)
    engine = create_engine(url)
    with engine.connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    engine.dispose()
    assert diff == [], f"models changed without a migration: {diff}"


def test_upgrade_is_idempotent(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'm.db'}"
    upgrade(url)
    upgrade(url)


def test_refuses_database_created_before_migrations(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'old.db'}"
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE conversations (id TEXT)"))
    engine.dispose()
    with pytest.raises(RuntimeError, match="before migrations"):
        upgrade(url)


def test_analytics_columns_are_backfilled_from_the_stored_state(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'm.db'}"
    command.upgrade(alembic_config(url), "0003")
    state = {
        "candidate": {"meta": {"full_name": {"status": "valid"}, "license": {"status": "valid"}}},
        "started_at": "2026-11-02T10:00:00+00:00",
        "ended_at": "2026-11-02T11:30:00+01:00",
        "dropped_at": None,
        "interview_at": "2026-11-03T09:00:00+00:00",
    }
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO conversations (id, client_id, status, stage, channel, language, "
                "availability, schedule, review_reasons, created_at, updated_at, state) VALUES "
                "('c1', 'x', 'qualified', 'closed', 'chat', 'es', '[]', '[]', '[]', "
                "'2026-11-02', '2026-11-02', :state)"
            ),
            {"state": json.dumps(state)},
        )
    upgrade(url)
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT progress, duration_minutes, interview_at FROM conversations")
        ).one()
    engine.dispose()
    assert row == (2, 30.0, "2026-11-03 09:00:00.000000")


def test_time_to_screen_is_backfilled_from_the_stored_state(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'm.db'}"
    command.upgrade(alembic_config(url), "0011")
    started = {"started_at": "2026-11-02T10:00:00+00:00", "ended_at": "2026-11-02T13:00:00+00:00"}
    applied = {**started, "applied_at": "2026-11-01T10:00:00+00:00"}
    engine = create_engine(url)
    with engine.begin() as connection:
        for row_id, state in (("c1", started), ("c2", applied), ("c3", {})):
            connection.execute(
                text(
                    "INSERT INTO conversations (id, client_id, status, stage, channel, language, "
                    "availability, schedule, review_reasons, created_at, updated_at, state) "
                    "VALUES (:id, 'x', 'qualified', 'closed', 'chat', 'es', '[]', '[]', '[]', "
                    "'2026-11-02', '2026-11-02', :state)"
                ),
                {"id": row_id, "state": json.dumps(state)},
            )
    upgrade(url)
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT id, applied_at, hours_to_outcome FROM conversations ORDER BY id")
        ).all()
    engine.dispose()
    assert rows == [
        ("c1", "2026-11-02 10:00:00.000000", 3.0),
        ("c2", "2026-11-01 10:00:00.000000", 27.0),
        ("c3", None, None),
    ]


def test_existing_conversations_get_an_ordered_history_they_can_replay(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'm.db'}"
    command.upgrade(alembic_config(url), "0014")
    client_id, conversation_id = "grupo_sazon", str(uuid4())
    state = ConversationState(client_id=client_id, conversation_id=UUID(conversation_id), turns=3)
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO conversations (id, client_id, status, stage, channel, language, "
                "availability, schedule, review_reasons, created_at, updated_at, state) VALUES "
                "(:id, :client, 'in_progress', 'consent', 'chat', 'es', '[]', '[]', '[]', "
                "'2026-11-01 10:00:00', '2026-11-01 10:00:00', :state)"
            ),
            {
                "id": conversation_id,
                "client": client_id,
                # As a real old row: saved before this field existed.
                "state": json.dumps(state.model_dump(mode="json", exclude={"recap_rounds"})),
            },
        )
        # Two batches, each numbered from 0, the second one saved in the same instant.
        for number, (seq, kind, at) in enumerate(
            [
                (0, "conversation_started", "2026-11-01 10:00:00"),
                (0, "message_received", "2026-11-01 10:05:00"),
                (1, "consent_given", "2026-11-01 10:05:00"),
            ]
        ):
            connection.execute(
                text(
                    "INSERT INTO events (id, conversation_id, client_id, seq, type, data, "
                    "created_at) VALUES (:id, :conversation, :client, :seq, :type, '{}', :at)"
                ),
                {
                    "id": f"event-{number}",
                    "conversation": conversation_id,
                    "client": client_id,
                    "seq": seq,
                    "type": kind,
                    "at": at,
                },
            )
    engine.dispose()

    upgrade(url)

    engine = create_engine(url)
    repo = SqlRepository(engine, FakeClock(START), client_id)
    assert repo.replay(UUID(conversation_id)) == state
    assert repo.rebuild_projections(check_only=True) == []
    assert [e.type for e in repo.events(UUID(conversation_id))] == [
        "conversation_started",
        "message_received",
        "consent_given",
    ]
    repo.append_events(state, [DomainEvent(type=EventType.VIEWED)])  # continues after the base
    assert repo.events(UUID(conversation_id))[-1].type == "viewed"
    engine.dispose()


def test_event_sourcing_can_be_rolled_back(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'm.db'}"
    upgrade(url)
    command.downgrade(alembic_config(url), "0014")
    command.upgrade(alembic_config(url), "head")
