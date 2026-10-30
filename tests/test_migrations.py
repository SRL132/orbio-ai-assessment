"""Migrations: they build the schema the models describe, and refuse pre-migration databases."""

from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, text

from app.adapters.storage.migrate import upgrade
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
