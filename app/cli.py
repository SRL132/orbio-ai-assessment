"""Small admin CLI.

uv run python -m app.cli hash-password
uv run python -m app.cli rebuild-state [--check]    # projections from the event log
"""

import getpass
import sys

from app.adapters.clock.clocks import SystemClock
from app.adapters.storage.sql import SqlRepository, make_engine
from app.api.auth import hash_password
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
    print(__doc__, file=sys.stderr)
    return 2


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


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
