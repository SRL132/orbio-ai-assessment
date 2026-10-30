"""WhatsApp channel: webhook handling (inbound) and the outbox dispatcher (outbound).

Inbound messages go through the same service as the web chat. Agent replies and reminders are
stored as undelivered outbound messages and sent from here, so a reminder created hours later
by the scheduler reaches the candidate's phone the same way as an immediate reply.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx

from app import metrics
from app.adapters.channels.quick_replies import quick_replies
from app.adapters.channels.whatsapp import WhatsAppClient, parse_webhook
from app.domain.models import TERMINAL_STATUSES, Channel, Stage
from app.worker.service import ScreeningService

logger = logging.getLogger("app.whatsapp")

CHANNEL: Channel = "whatsapp"
SERVICE_WINDOW = timedelta(hours=24) - timedelta(minutes=5)  # a small safety margin
MAX_ATTEMPTS = 5
RETRY_STATUSES = frozenset({408, 429})
SERVER_ERROR = 500
RESTART_WORDS = frozenset({"hola", "hi", "hello", "start", "/start", "empezar", "empezamos"})
TEMPLATE_LANGUAGE = {"es": "es", "en": "en_US"}
# One dispatcher sends at a time, across processes, so nothing is sent twice and each
# conversation's messages keep their order. It renews its lease every run; if its process
# dies, another takes over after this long.
DISPATCH_LEASE = timedelta(seconds=30)


class WhatsAppChannel:
    def __init__(
        self,
        service: ScreeningService,
        client: WhatsAppClient,
        reminder_template: str | None = None,
    ) -> None:
        self.service = service
        self.client = client
        self.reminder_template = reminder_template or None
        self._dispatcher_id = uuid4().hex

    # --- Inbound --------------------------------------------------------------------------------

    def handle(self, payload: dict[str, Any]) -> list[UUID]:
        """Store incoming messages. Returns the conversations that now have work to do."""
        repo = self.service.repo
        to_process: list[UUID] = []
        for message in parse_webhook(payload):
            cid = repo.conversation_for(CHANNEL, message.wa_id)
            state = repo.get(cid) if cid else None
            ended = state is None or (
                state.stage == Stage.CLOSED and state.status in TERMINAL_STATUSES
            )
            wants_restart = (message.text or "").strip().lower() in RESTART_WORDS
            if cid is None or state is None or (ended and wants_restart):
                # A new candidate (or a returning one saying hello): open a conversation.
                # The greeting asks for consent, so this first message isn't an answer.
                new_id = self.service.start_conversation(CHANNEL)
                repo.link_contact(CHANNEL, message.wa_id, new_id)
                logger.info("whatsapp_conversation_started id=%s", new_id)
                continue
            if message.text is None:
                self.service.reply_text_only(cid)
                continue
            if self.service.receive(cid, message.text, channel_message_id=message.message_id):
                to_process.append(cid)
        return list(dict.fromkeys(to_process))

    # --- Outbound -------------------------------------------------------------------------------

    def dispatch_once(self) -> int:
        """Send pending agent messages in order. Returns how many were sent (0 when another
        process is the dispatcher)."""
        repo = self.service.repo
        now = self.service.ports.clock.now()
        if not repo.try_job_lease("whatsapp_outbox", self._dispatcher_id, now + DISPATCH_LEASE):
            return 0
        sent = 0
        blocked: set[UUID] = set()  # keep per-conversation order: stop at the first failure
        for item in repo.undelivered(CHANNEL):
            if item.conversation_id in blocked:
                continue
            state = repo.get(item.conversation_id)
            last_inbound = state.last_inbound_at or state.started_at if state else None
            outside_window = last_inbound is None or now - last_inbound > SERVICE_WINDOW
            try:
                if not outside_window:
                    self.client.send(item.external_id, item.text, quick_replies(item.text))
                elif self.reminder_template and state is not None:
                    language = TEMPLATE_LANGUAGE.get(state.language, "es")
                    self.client.send_template(item.external_id, self.reminder_template, language)
                else:
                    # Free-form messages aren't allowed after 24 h and no template is set up.
                    repo.mark_delivery(item.message_id, True, "skipped: outside 24h window")
                    metrics.WHATSAPP_SENT.labels("skipped").inc()
                    logger.info("whatsapp_skipped_outside_window id=%s", item.conversation_id)
                    continue
            except httpx.HTTPError as exc:
                blocked.add(item.conversation_id)
                retryable = not isinstance(exc, httpx.HTTPStatusError) or (
                    exc.response.status_code in RETRY_STATUSES
                    or exc.response.status_code >= SERVER_ERROR
                )
                give_up = not retryable or item.attempts + 1 >= MAX_ATTEMPTS
                repo.mark_delivery(item.message_id, give_up, f"failed: {type(exc).__name__}")
                metrics.WHATSAPP_SENT.labels("gave_up" if give_up else "retrying").inc()
                logger.warning(
                    "whatsapp_send_failed id=%s attempts=%d give_up=%s",
                    item.conversation_id,
                    item.attempts + 1,
                    give_up,
                )
                continue
            repo.mark_delivery(item.message_id, True)
            metrics.WHATSAPP_SENT.labels("sent").inc()
            sent += 1
        return sent
