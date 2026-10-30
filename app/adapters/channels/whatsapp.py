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


def parse_webhook(payload: dict[str, Any]) -> list[WhatsAppInbound]:
    """Incoming candidate messages. Delivery statuses and other events are ignored."""
    found: list[WhatsAppInbound] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for message in value.get("messages", []):
                text: str | None = None
                kind = message.get("type")
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

    def _post(self, body: dict[str, Any]) -> str:
        response = self._client.post(self._path, json=body)
        response.raise_for_status()
        message_id = str(response.json()["messages"][0]["id"])
        logger.info("whatsapp_sent type=%s id=%s", body["type"], message_id)
        return message_id
