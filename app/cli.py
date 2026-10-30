"""Small admin CLI. Usage: uv run python -m app.cli hash-password"""

import getpass
import sys

from app.api.auth import hash_password

MIN_PASSWORD_LENGTH = 12


def main(argv: list[str]) -> int:
    if argv[1:] == ["hash-password"]:
        password = getpass.getpass("Password: ")
        if len(password) < MIN_PASSWORD_LENGTH:
            print(f"Use at least {MIN_PASSWORD_LENGTH} characters.", file=sys.stderr)
            return 1
        print(hash_password(password))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
