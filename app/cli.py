"""Small admin CLI.

uv run python -m app.cli hash-password
uv run python -m app.cli create-user [email]   # set up .env and a local login: asks for a password
uv run python -m app.cli rebuild-state [--check]    # projections from the event log
uv run python -m app.cli llm-outputs <conversation id>   # raw LLM replies, if capture was on
uv run python -m app.cli seed-demo    # invented candidates in an empty local database, free
"""

import getpass
import re
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from dotenv import dotenv_values, set_key

from app.adapters.clock.clocks import FakeClock, SystemClock
from app.adapters.storage.dashboard import DashboardQueries, Filters
from app.adapters.storage.sql import SqlRepository, make_engine
from app.api.auth import hash_password, parse_users
from app.bootstrap import build_offline_service
from app.seed import DEMO_CANDIDATES, SEED_CLIENT, SEED_SPAN, seed_demo
from app.settings import Settings

MIN_PASSWORD_LENGTH = 12
MIN_TEST_PASSWORD_LENGTH = 6  # create-user only, which refuses to run outside a demo
ENV_FILE = Path(".env")
ENV_EXAMPLE = Path(".env.example")
DEFAULT_TEST_EMAIL = "tester@sazon.test"
# ":" and ";" separate entries in RECRUITER_USERS, so an email can't contain them.
EMAIL_PATTERN = re.compile(r"[^\s:;@]+@[^\s:;@]+")
PASSWORD_WORDS = (
    "albahaca",
    "arroz",
    "canela",
    "cilantro",
    "comino",
    "chile",
    "frijol",
    "limon",
    "mango",
    "nopal",
    "oregano",
    "paella",
    "pimienta",
    "salsa",
    "tomate",
    "tortilla",
)


def main(argv: list[str]) -> int:
    if argv[1:] == ["hash-password"]:
        password = getpass.getpass("Password: ")
        if len(password) < MIN_PASSWORD_LENGTH:
            print(f"Use at least {MIN_PASSWORD_LENGTH} characters.", file=sys.stderr)
            return 1
        print(hash_password(password))
        return 0
    if argv[1:] == ["create-user"]:
        return create_user(DEFAULT_TEST_EMAIL)
    if len(argv) == 3 and argv[1] == "create-user":
        return create_user(argv[2])
    if argv[1:] in (["rebuild-state"], ["rebuild-state", "--check"]):
        return rebuild_state(check_only=argv[2:] == ["--check"])
    if len(argv) == 3 and argv[1] == "llm-outputs":
        return llm_outputs(UUID(argv[2]))
    if argv[1:] == ["seed-demo"]:
        return seed_demo_command()
    print(__doc__, file=sys.stderr)
    return 2


def easy_password() -> str:
    """Quick to type and read out, so only fit for testing: create-user refuses outside a demo."""
    words = [secrets.choice(PASSWORD_WORDS) for _ in range(3)]
    return f"{'-'.join(words)}-{secrets.randbelow(100):02d}"


def create_user(email: str) -> int:
    """Everything a fresh clone needs to log in: .env, a session secret and one account.

    Asks for the password, or generates one on Enter. Other accounts and settings stay.
    """
    if not Settings().demo_mode:
        print("Refused: test logins need DEMO_MODE=true.", file=sys.stderr)
        return 1
    email = email.strip().lower()
    if not EMAIL_PATTERN.fullmatch(email):
        print(f"Not a usable email: {email!r}", file=sys.stderr)
        return 1
    typed = getpass.getpass(
        f"Password for {email} (at least {MIN_TEST_PASSWORD_LENGTH} characters, "
        "or Enter to generate one): "
    )
    if typed and len(typed) < MIN_TEST_PASSWORD_LENGTH:
        print(f"Use at least {MIN_TEST_PASSWORD_LENGTH} characters.", file=sys.stderr)
        return 1
    password = typed or easy_password()

    if not ENV_FILE.exists() and ENV_EXAMPLE.exists():
        ENV_FILE.write_text(ENV_EXAMPLE.read_text())
    values = dotenv_values(ENV_FILE)
    if not values.get("SESSION_SECRET"):  # otherwise logins would reset on every restart
        set_key(ENV_FILE, "SESSION_SECRET", secrets.token_urlsafe(32), quote_mode="never")
    users = parse_users(values.get("RECRUITER_USERS"))
    existed = email in users
    users[email] = hash_password(password)
    value = ";".join(f"{user}:{hashed}" for user, hashed in users.items())
    set_key(ENV_FILE, "RECRUITER_USERS", value, quote_mode="never")

    print(f"{'Updated' if existed else 'Created'} a login in {ENV_FILE}:")
    print(f"  Email:    {email}")
    if not typed:
        print(f"  Password: {password}   (save it now, it isn't stored)")
    print("Start the app, or restart it if it's running:")
    print("  uv run uvicorn --factory app.api.main:create_app --reload")
    print("Then log in at http://127.0.0.1:8000/dashboard")
    return 0


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
