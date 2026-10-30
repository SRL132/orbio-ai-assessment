"""Recruiter login: one account per recruiter, scrypt-hashed passwords, no defaults.

Accounts come from RECRUITER_USERS as `email:hash;email:hash`. Create a hash with
`uv run python -m app.cli hash-password`.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P)
    # "." separators: no "$", which shells and env-file loaders would try to expand.
    return f"scrypt.{salt.hex()}.{digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split(".")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=_N, r=_R, p=_P)
    return hmac.compare_digest(digest.hex(), digest_hex)


def parse_users(raw: str | None) -> dict[str, str]:
    users: dict[str, str] = {}
    for entry in (raw or "").split(";"):
        email, _, hashed = entry.strip().partition(":")
        if email and hashed:
            users[email.strip().lower()] = hashed.strip()
    return users
