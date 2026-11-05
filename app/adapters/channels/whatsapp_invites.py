"""WhatsApp invite links: a wa.me link whose ready-typed message carries a signed code.

The candidate taps it and presses send, so they write first (no paid template is needed to open
the conversation) and the code says which application the number belongs to.
"""

from __future__ import annotations

import base64
import re
from urllib.parse import quote
from uuid import UUID

from itsdangerous import BadSignature, TimestampSigner

INVITE_MAX_AGE = 7 * 24 * 3600
_PREFIX = "inv-"
_CODE = re.compile(rf"\b{_PREFIX}([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)")


class WhatsAppInvites:
    def __init__(self, secret_key: str, number: str) -> None:
        self._signer = TimestampSigner(secret_key, salt="whatsapp-invite")
        self._number = re.sub(r"\D", "", number)

    def code(self, conversation_id: UUID) -> str:
        compact = base64.urlsafe_b64encode(conversation_id.bytes).rstrip(b"=")
        return _PREFIX + self._signer.sign(compact).decode()

    def url(self, conversation_id: UUID) -> str:
        """For an invite nobody has claimed yet: opens WhatsApp with the code typed in."""
        return f"{self.chat_url()}?text={quote(f'Hola {self.code(conversation_id)}')}"

    def chat_url(self) -> str:
        """For someone already talking to the agent: opens the chat, with nothing typed in."""
        return f"https://wa.me/{self._number}"

    def read(self, text: str) -> tuple[UUID | None, str]:
        """The invited conversation, if the text has a valid code (forged or expired: None), and
        the text without the code, which never reaches the transcript or the LLM."""
        found = _CODE.search(text)
        if found is None:
            return None, text
        rest = (text[: found.start()] + text[found.end() :]).strip()
        try:
            compact = self._signer.unsign(found.group(1), max_age=INVITE_MAX_AGE)
        except BadSignature:
            return None, rest
        return UUID(bytes=base64.urlsafe_b64decode(compact + b"==")), rest
