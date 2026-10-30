"""Run Alembic migrations from code (app startup, tests, the container entrypoint)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

ROOT = Path(__file__).resolve().parents[3]


def alembic_config(url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def upgrade(url: str) -> None:
    """Bring the database to the latest schema. Refuses databases made before migrations."""
    engine = create_engine(url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    if "conversations" in tables and "alembic_version" not in tables:
        raise RuntimeError(
            "This database was created before migrations existed. Delete it (demo data) or "
            "run `uv run alembic stamp 0001` if its schema matches the first migration."
        )
    command.upgrade(alembic_config(url), "head")
