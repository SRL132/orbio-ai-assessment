"""Alembic environment: metadata from the app's models, URL from DATABASE_URL."""

from __future__ import annotations

from typing import Any

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.adapters.storage.sql import Base, UTCDateTime
from app.settings import Settings

config = context.config
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", Settings().database_url)


def render_item(type_: str, obj: object, autogen_context: object) -> str | bool:
    """Render the app's UTC datetime as a plain DateTime: migrations don't import app code."""
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime()"
    return False


def _options(url: str) -> dict[str, Any]:
    return {
        "target_metadata": Base.metadata,
        "render_as_batch": url.startswith("sqlite"),  # SQLite can't ALTER most things in place
        "render_item": render_item,
        "compare_type": True,
    }


def run_offline() -> None:
    url = config.get_main_option("sqlalchemy.url") or ""
    context.configure(url=url, literal_binds=True, **_options(url))
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    url = config.get_main_option("sqlalchemy.url") or ""
    engine = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with engine.connect() as connection:
        context.configure(connection=connection, **_options(url))
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
