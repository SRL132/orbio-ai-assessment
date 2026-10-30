"""Migrations: they build the schema the models describe, and refuse pre-migration databases."""

import json
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, text

from app.adapters.storage.migrate import alembic_config, upgrade
from app.adapters.storage.sql import Base


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
