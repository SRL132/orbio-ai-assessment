"""WhatsApp Cloud API adapter (Meta): send messages, parse webhooks, check signatures.

Checked against Meta's docs (Cloud API reference and the interactive reply-buttons guide):
`POST https://graph.facebook.com/{version}/{phone_number_id}/messages` with a bearer token;
reply buttons: at most 3, titles up to 20 characters; incoming messages arrive under
`entry[].changes[].value.messages[]`, button taps as `interactive.button_reply` / `list_reply`.
List-message limits (10 rows, 24-character titles) follow the same docs and are kept
conservative here.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger("app.whatsapp")

GRAPH_URL = "https://graph.facebook.com"
MAX_BUTTONS = 3
MAX_BUTTON_TITLE = 20
MAX_LIST_ROWS = 10
MAX_ROW_TITLE = 24
MAX_INTERACTIVE_BODY = 1024
MAX_TEXT = 4096
TYPING_TIMEOUT_SECONDS = 3.0  # sent while the webhook waits: it must not delay the acknowledgement


@dataclass(frozen=True)
class WhatsAppInbound:
    wa_id: str  # the sender's WhatsApp number: our contact id
    message_id: str  # used to drop duplicate deliveries (Meta retries for up to 36 h)
    text: str | None  # None for audio, images, stickers…


def build_message(to: str, text: str, options: list[str] | None = None) -> dict[str, Any]:
    """Reply buttons for up to 3 short options, a list for up to 10, plain text otherwise."""
    base: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
    }
    options = options or []
    if options and len(options) <= MAX_BUTTONS and all(len(o) <= MAX_BUTTON_TITLE for o in options):
        return base | {
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": text[:MAX_INTERACTIVE_BODY]},
                "action": {
                    "buttons": [
                        {"type": "reply", "reply": {"id": option, "title": option}}
                        for option in options
                    ]
                },
            },
        }
    if options and len(options) <= MAX_LIST_ROWS and all(len(o) <= MAX_ROW_TITLE for o in options):
        return base | {
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": text[:MAX_INTERACTIVE_BODY]},
                "action": {
                    "button": "Opciones · Options",
                    "sections": [{"rows": [{"id": o, "title": o} for o in options]}],
                },
            },
        }
    return base | {"type": "text", "text": {"body": text[:MAX_TEXT], "preview_url": False}}


def build_template(to: str, name: str, language_code: str) -> dict[str, Any]:
    """A pre-approved template: the only kind of message allowed outside the 24 h window."""
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "template",
        "template": {"name": name, "language": {"code": language_code}},
    }


def build_typing(message_id: str) -> dict[str, Any]:
    return {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
        "typing_indicator": {"type": "text"},
    }


_LONG_NUMBER = re.compile(r"\d{6,}")  # an error title must never put a phone number in the logs


@dataclass(frozen=True)
class DeliveryReport:
    status: str  # sent, delivered, read or failed
    error_code: int | None = None
    error_title: str = ""


# A 👍 on an earlier message or a system notice isn't something the candidate is telling us.
_NOT_MESSAGES = frozenset({"reaction", "system"})


def parse_webhook(payload: dict[str, Any]) -> list[WhatsAppInbound]:
    """Incoming candidate messages. Delivery statuses and other events are ignored."""
    found: list[WhatsAppInbound] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for message in value.get("messages", []):
                text: str | None = None
                kind = message.get("type")
                if kind in _NOT_MESSAGES:
                    continue
                if kind == "text":
                    text = message.get("text", {}).get("body")
                elif kind == "interactive":
                    interactive = message.get("interactive", {})
                    reply = interactive.get(interactive.get("type", ""), {})
                    text = reply.get("title") or reply.get("id")
                elif kind == "button":  # quick-reply button on a template
                    text = message.get("button", {}).get("text")
                found.append(
                    WhatsAppInbound(
                        wa_id=str(message["from"]), message_id=str(message["id"]), text=text
                    )
                )
    return found


def failed_recipients(payload: dict[str, Any]) -> list[tuple[str, int | None]]:
    """Who a message failed for, and the error code: to act on, never to log."""
    return [
        (str(status["recipient_id"]), (status.get("errors") or [{}])[0].get("code"))
        for entry in payload.get("entry", [])
        for change in entry.get("changes", [])
        for status in change.get("value", {}).get("statuses", [])
        if status.get("status") == "failed" and status.get("recipient_id")
    ]


def delivery_reports(payload: dict[str, Any]) -> list[DeliveryReport]:
    """What Meta says happened to our messages. Accepting a send doesn't mean it arrived: a message
    can fail afterwards (payment, recipient, window), and this is the only place that says so.
    Keeps no recipient or message id, so a report is safe to log."""
    found: list[DeliveryReport] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for status in change.get("value", {}).get("statuses", []):
                errors = status.get("errors") or [{}]
                found.append(
                    DeliveryReport(
                        status=str(status.get("status", "unknown")),
                        error_code=errors[0].get("code"),
                        error_title=_LONG_NUMBER.sub("<number>", str(errors[0].get("title", ""))),
                    )
                )
    return found


def valid_signature(app_secret: str, raw_body: bytes, header: str | None) -> bool:
    """`X-Hub-Signature-256: sha256=<HMAC-SHA256 of the raw body with the app secret>`."""
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


class WhatsAppClient:
    def __init__(
        self,
        access_token: str,
        phone_number_id: str,
        api_version: str = "v26.0",
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not phone_number_id:
            raise ValueError("WHATSAPP_PHONE_NUMBER_ID is not set (see .env.example)")
        self._path = f"/{api_version}/{phone_number_id}/messages"
        self._client = httpx.Client(
            base_url=GRAPH_URL,
            timeout=timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    def send(self, to: str, text: str, options: list[str] | None = None) -> str:
        return self._post(build_message(to, text, options))

    def send_template(self, to: str, name: str, language_code: str) -> str:
        return self._post(build_template(to, name, language_code))

    def show_typing(self, message_id: str) -> None:
        """Mark the candidate's message as read and show "typing…" until we reply (or 25 s)."""
        response = self._client.post(
            self._path, json=build_typing(message_id), timeout=TYPING_TIMEOUT_SECONDS
        )
        response.raise_for_status()

    def _post(self, body: dict[str, Any]) -> str:
        response = self._client.post(self._path, json=body)
        response.raise_for_status()
        message_id = str(response.json()["messages"][0]["id"])
        logger.info("whatsapp_sent type=%s id=%s", body["type"], message_id)
        return message_id
