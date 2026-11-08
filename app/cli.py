"""Small admin CLI.

uv run python -m app.cli demo [--reset] [--no-seed]   # one step: login, candidates, app running
uv run python -m app.cli hash-password
uv run python -m app.cli create-user [email]   # set up .env and a local login: asks for a password
uv run python -m app.cli rebuild-state [--check]    # projections from the event log
uv run python -m app.cli llm-outputs <conversation id>   # raw LLM replies, if capture was on
uv run python -m app.cli seed-demo    # invented candidates in an empty local database, free
"""

import getpass
import re
import secrets
import socket
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import uvicorn
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
DEMO_HOST = "127.0.0.1"
DEMO_PORT = 8000
DEMO_FLAGS = {"--reset", "--no-seed"}
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
    if argv[1:2] == ["demo"] and set(argv[2:]) <= DEMO_FLAGS:
        return demo(reset="--reset" in argv, seed="--no-seed" not in argv)
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


def create_user(email: str, *, show_start: bool = True) -> int:
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
    try:
        typed = getpass.getpass(
            f"Password for {email} (at least {MIN_TEST_PASSWORD_LENGTH} characters, "
            "or Enter to generate one): "
        )
    except EOFError:  # no terminal to type in, e.g. a `!` command inside Claude Code
        print(
            "This needs a terminal to type the password in: run it in the Terminal app.",
            file=sys.stderr,
        )
        return 1
    if typed and len(typed) < MIN_TEST_PASSWORD_LENGTH:
        print(f"Use at least {MIN_TEST_PASSWORD_LENGTH} characters.", file=sys.stderr)
        return 1
    password = typed or easy_password()

    copy_env_example()
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
    if show_start:
        print("Start the app, or restart it if it's running:")
        print("  uv run python -m app.cli demo")
        print(f"Then log in at http://{DEMO_HOST}:{DEMO_PORT}/dashboard")
    return 0


def copy_env_example() -> None:
    if not ENV_FILE.exists() and ENV_EXAMPLE.exists():
        ENV_FILE.write_text(ENV_EXAMPLE.read_text())


def demo(*, reset: bool, seed: bool) -> int:
    """One command for a reviewer: .env, a login, candidates on the dashboard, the app running.

    Each step is skipped when it's already done, so running it again just starts the app.
    """
    copy_env_example()
    settings = Settings()
    if not settings.demo_mode:
        print("Refused: the demo needs DEMO_MODE=true.", file=sys.stderr)
        return 1
    if port_in_use(DEMO_HOST, DEMO_PORT):  # and a running app would also write to the database
        print(
            f"Something is already running on port {DEMO_PORT}. Stop it (Ctrl+C in its "
            "terminal), then try again.",
            file=sys.stderr,
        )
        return 1
    if reset and (code := reset_database(settings.database_url)):
        return code
    users = parse_users(dotenv_values(ENV_FILE).get("RECRUITER_USERS"))
    if not users:
        if code := create_user(DEFAULT_TEST_EMAIL, show_start=False):
            return code
        users = parse_users(dotenv_values(ENV_FILE).get("RECRUITER_USERS"))
    if seed and database_is_empty(Settings()):
        print("Adding invented candidates to the dashboard (free, offline)…")
        seed_demo_command()  # a refusal explains itself, and the app is still worth starting

    print()
    print(f"Candidate chat:      http://{DEMO_HOST}:{DEMO_PORT}/chat")
    print(f"Voice call:          http://{DEMO_HOST}:{DEMO_PORT}/voice   (Chrome or Edge)")
    print(f"Recruiter dashboard: http://{DEMO_HOST}:{DEMO_PORT}/dashboard")
    if users:
        print(f"  Log in as: {', '.join(users)}   (another: uv run python -m app.cli create-user)")
    print("Stop with Ctrl+C. Fresh start: uv run python -m app.cli demo --reset")
    print()
    # No --reload: it waits for open chat streams to close before restarting, and hangs.
    uvicorn.run(
        "app.api.main:create_app",
        factory=True,
        host=DEMO_HOST,
        port=DEMO_PORT,
        timeout_graceful_shutdown=2,
    )
    return 0


def port_in_use(host: str, port: int) -> bool:
    with socket.socket() as probe:
        return probe.connect_ex((host, port)) == 0


def reset_database(url: str) -> int:
    """Move a local SQLite database aside, so the next start begins empty. Nothing is deleted."""
    if not url.startswith("sqlite:///") or url == "sqlite:///:memory:":
        print("Refused: --reset only works on a local SQLite DATABASE_URL.", file=sys.stderr)
        return 1
    path = Path(url.removeprefix("sqlite:///"))
    if path.exists():
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.stem}-backup-{stamp}{path.suffix}")
        path.rename(backup)
        print(f"Moved the old database to {backup}: chats, candidates and login lockouts.")
    return 0


def database_is_empty(settings: Settings) -> bool:
    engine = make_engine(settings.database_url)
    try:
        return not DashboardQueries(engine, settings.client_id).candidates(Filters()).total
    finally:
        engine.dispose()


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
    if not database_is_empty(settings):
        print(
            "Refused: this database already has conversations. "
            "Point DATABASE_URL at a new file, or delete data/sazon.db, then try again.",
            file=sys.stderr,
        )
        return 1
    engine = make_engine(settings.database_url)
    try:
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
