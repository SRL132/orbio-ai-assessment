"""Small admin CLI.

uv run python -m app.cli hash-password
uv run python -m app.cli rebuild-state [--check]    # projections from the event log
uv run python -m app.cli llm-outputs <conversation id>   # raw LLM replies, if capture was on
uv run python -m app.cli seed-demo    # invented candidates in an empty local database, free
"""

import getpass
import sys
from datetime import UTC, datetime
from uuid import UUID

from app.adapters.clock.clocks import FakeClock, SystemClock
from app.adapters.storage.dashboard import DashboardQueries, Filters
from app.adapters.storage.sql import SqlRepository, make_engine
from app.api.auth import hash_password
from app.bootstrap import build_offline_service
from app.seed import DEMO_CANDIDATES, SEED_CLIENT, SEED_SPAN, seed_demo
from app.settings import Settings

MIN_PASSWORD_LENGTH = 12


def main(argv: list[str]) -> int:
    if argv[1:] == ["hash-password"]:
        password = getpass.getpass("Password: ")
        if len(password) < MIN_PASSWORD_LENGTH:
            print(f"Use at least {MIN_PASSWORD_LENGTH} characters.", file=sys.stderr)
            return 1
        print(hash_password(password))
        return 0
    if argv[1:] in (["rebuild-state"], ["rebuild-state", "--check"]):
        return rebuild_state(check_only=argv[2:] == ["--check"])
    if len(argv) == 3 and argv[1] == "llm-outputs":
        return llm_outputs(UUID(argv[2]))
    if argv[1:] == ["seed-demo"]:
        return seed_demo_command()
    print(__doc__, file=sys.stderr)
    return 2


def llm_outputs(conversation_id: UUID) -> int:
    """Print a conversation's raw LLM replies, oldest first. They hold personal data."""
    settings = Settings()
    engine = make_engine(settings.database_url)
    outputs = SqlRepository(engine, SystemClock(), settings.client_id).llm_outputs(conversation_id)
    engine.dispose()
    for output in outputs:
        print(output.model_dump_json())
    return 0


def rebuild_state(*, check_only: bool) -> int:
    """Compare the `conversations` table with the event log and, unless checking, restore it."""
    settings = Settings()
    engine = make_engine(settings.database_url)
    repo = SqlRepository(engine, SystemClock(), settings.client_id)
    differing = repo.rebuild_projections(check_only=check_only)
    engine.dispose()
    for conversation_id in differing:
        print(conversation_id)
    verb = "differ from" if check_only else "rebuilt from"
    print(f"{len(differing)} conversations {verb} the event log", file=sys.stderr)
    return 1 if check_only and differing else 0


def seed_demo_command() -> int:
    """Fill an empty demo database. Never a public site, and never on top of existing data."""
    settings = Settings()
    if not settings.demo_mode or settings.invite_only:
        print("Refused: seeding needs DEMO_MODE=true and INVITE_ONLY=false.", file=sys.stderr)
        return 1
    if settings.client_id != SEED_CLIENT:
        print(f"Refused: the demo candidates are written for {SEED_CLIENT}.", file=sys.stderr)
        return 1
    engine = make_engine(settings.database_url)
    try:
        if DashboardQueries(engine, settings.client_id).candidates(Filters()).total:
            print(
                "Refused: this database already has conversations. "
                "Point DATABASE_URL at a new file, or delete data/sazon.db, then try again.",
                file=sys.stderr,
            )
            return 1
        clock = FakeClock(datetime.now(UTC) - SEED_SPAN)
        service = build_offline_service(settings, engine, clock)
        outcomes = seed_demo(service, clock, DEMO_CANDIDATES)
    finally:
        engine.dispose()
    for candidate, status in outcomes:
        print(f"{candidate.name}: {status.value}")
    if any(status != candidate.expected for candidate, status in outcomes):
        print(
            "Some candidates ended somewhere unexpected. Was the app running? "
            "Seed with it stopped.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
