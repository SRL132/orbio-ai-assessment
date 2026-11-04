"""Chat invite links: signed, expiring, and made from a list of applications.

Used by the ATS endpoint and the recruiter's invite page. A link names only a conversation, and
no contact details are stored: whoever invites the candidate also sends them the link.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from uuid import UUID

from itsdangerous import URLSafeTimedSerializer

from app.adapters.storage.dashboard import UnfinishedInvite

INVITE_MAX_AGE = 7 * 24 * 3600
MAX_INVITE_ROWS = 1000  # about a week of applications
MAX_UPLOAD_BYTES = 1024 * 1024
_APPLIED_AT = "applied_at"
_URL = "chat_url"
REFERENCE_COLUMN = "application_ref"
# Letters, digits and . _ - only: it's stored and comes back in CSVs that open in a spreadsheet,
# where a leading = or + would run as a formula.
REFERENCE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"


class InviteError(ValueError):
    """The upload can't be used; the message says why, for the recruiter."""


class InviteLinks:
    def __init__(self, secret_key: str) -> None:
        self._signer = URLSafeTimedSerializer(secret_key, salt="chat-invite")

    def path(self, conversation_id: UUID) -> str:
        return f"/chat/invite/{self._signer.dumps(str(conversation_id))}"

    def read(self, token: str) -> str:
        """The conversation id. Raises `BadSignature` if forged or expired."""
        conversation_id: str = self._signer.loads(token, max_age=INVITE_MAX_AGE)
        return conversation_id


def parse_applications(raw: bytes, now: datetime) -> list[dict[str, str]]:
    """The rows of an uploaded CSV, checked before anything is created, so that a bad row leaves
    no half-made batch behind. Needs an `applied_at` column with a date, time and UTC offset."""
    if len(raw) > MAX_UPLOAD_BYTES:
        raise InviteError("That file is too big (1 MB at most).")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise InviteError("Save the file as CSV UTF-8 and try again.") from None
    try:
        reader = csv.DictReader(io.StringIO(text))
        if _APPLIED_AT not in (reader.fieldnames or []):
            raise InviteError(f"The file needs an {_APPLIED_AT} column.")
        if _URL in (reader.fieldnames or []):
            raise InviteError(f"The file already has a {_URL} column: were these links made?")
        rows = list(reader)
    except csv.Error:
        raise InviteError("That file isn't a readable CSV.") from None
    if not rows:
        raise InviteError("The file has no rows.")
    if len(rows) > MAX_INVITE_ROWS:
        raise InviteError(f"At most {MAX_INVITE_ROWS} rows at a time.")
    for number, row in enumerate(rows, start=2):  # line 1 is the header
        _check_applied_at(row[_APPLIED_AT], number, now)
        _check_reference(row.get(REFERENCE_COLUMN), number)
    return rows


def reference_of(row: dict[str, str]) -> str | None:
    return (row.get(REFERENCE_COLUMN) or "").strip() or None


def _check_reference(value: str | None, line: int) -> None:
    reference = (value or "").strip()
    if reference and not re.fullmatch(REFERENCE_PATTERN, reference):
        raise InviteError(
            f"Line {line}: {REFERENCE_COLUMN} can only have letters, digits and . _ - "
            "(64 at most): it's an id from your own file, not contact details."
        )


def _check_applied_at(value: str | None, line: int, now: datetime) -> None:
    hint = f"Line {line}: {_APPLIED_AT} must look like 2026-11-03T09:00:00+01:00."
    try:
        applied = datetime.fromisoformat((value or "").strip())
    except ValueError:
        raise InviteError(hint) from None
    if applied.tzinfo is None:
        raise InviteError(hint)
    if applied > now:
        raise InviteError(f"Line {line}: {_APPLIED_AT} is in the future.")


def unfinished_csv(rows: list[tuple[UnfinishedInvite, str]]) -> str:
    """Who to chase, with a fresh link each: their own stops working after INVITE_MAX_AGE."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow([REFERENCE_COLUMN, _APPLIED_AT, "opened", _URL])
    for invite, url in rows:
        writer.writerow(
            [
                invite.reference,
                invite.applied_at.isoformat() if invite.applied_at else "",
                "yes" if invite.opened else "no",
                url,
            ]
        )
    return out.getvalue()


def links_csv(rows: list[dict[str, str]], urls: list[str]) -> str:
    """The rows as given, each with its link added."""
    out = io.StringIO()
    writer = csv.DictWriter(out, [*rows[0], _URL], extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row, url in zip(rows, urls, strict=True):
        writer.writerow({**row, _URL: url})
    return out.getvalue()
