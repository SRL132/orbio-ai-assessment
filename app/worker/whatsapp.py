"""WhatsApp channel: webhook handling (inbound) and the outbox dispatcher (outbound).

Inbound messages go through the same service as the web chat. Agent replies and reminders are
stored as undelivered outbound messages and sent from here, so a reminder created hours later
by the scheduler reaches the candidate's phone the same way as an immediate reply.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections import defaultdict
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx

from app import metrics
from app.adapters.channels.quick_replies import quick_replies
from app.adapters.channels.whatsapp import WhatsAppClient, WhatsAppInbound, parse_webhook
from app.domain.models import TERMINAL_STATUSES, Channel, Outbound, Stage
from app.worker.service import ScreeningService

logger = logging.getLogger("app.whatsapp")

CHANNEL: Channel = "whatsapp"
SERVICE_WINDOW = timedelta(hours=24) - timedelta(minutes=5)  # a small safety margin
MAX_ATTEMPTS = 5
RETRY_STATUSES = frozenset({408, 429})
SERVER_ERROR = 500
RESTART_WORDS = frozenset({"hola", "hi", "hello", "start", "/start", "empezar", "empezamos"})
TEMPLATE_LANGUAGE = {"es": "es", "en": "en_US"}
# One dispatcher at a time, across processes, so nothing is sent twice and each
# conversation's messages keep their order; it sends to several conversations in parallel.
# It renews its lease every run; if its process dies, another takes over after this long.
DISPATCH_LEASE = timedelta(seconds=30)
# Opening a conversation for a number is done by one message at a time, across processes, so
# two different first messages arriving together ("hola" / "soy Ana") open one conversation,
# not two. A message waits this long for the lock, then fails (and Meta retries it).
CONTACT_LOCK_WAIT = timedelta(seconds=5)
CONTACT_LEASE = timedelta(seconds=30)  # expires on its own if the holder's process dies
_CONTACT_LOCK_POLL = 0.05


class ContactBusy(RuntimeError):
    """Another message for the same number held the lock for too long."""


def contact_lock_name(wa_id: str) -> str:
    """The lock's name: a hash, so the number itself isn't stored in `job_leases`."""
    return "whatsapp_contact:" + hashlib.sha256(wa_id.encode()).hexdigest()[:40]


class WhatsAppChannel:
    def __init__(
        self,
        service: ScreeningService,
        client: WhatsAppClient,
        reminder_template: str | None = None,
        concurrency: int = 1,
        batch: int = 200,
    ) -> None:
        self.service = service
        self.client = client
        self.reminder_template = reminder_template or None
        self.concurrency = concurrency  # conversations sent at once (see dispatch_once)
        self.batch = batch  # messages read from the outbox per run
        self._dispatcher_id = uuid4().hex

    # --- Inbound --------------------------------------------------------------------------------

    def handle(self, payload: dict[str, Any]) -> list[UUID]:
        """Store incoming messages. Returns the conversations that now have work to do.

        Idempotent per message: Meta retries deliveries (for up to 36 h, sometimes two copies
        at once), so each message id is claimed before anything else and a retry is dropped,
        whatever kind of message it is. If handling fails the claim is given back, so the
        retry that follows the error response is handled instead of lost."""
        repo = self.service.repo
        to_process: list[UUID] = []
        for message in parse_webhook(payload):
            if not repo.claim_delivery(CHANNEL, message.message_id):
                metrics.WHATSAPP_DUPLICATES.inc()
                continue
            try:
                cid = self._handle_message(message)
            except Exception:
                repo.release_delivery(CHANNEL, message.message_id)
                raise
            if cid is not None:
                to_process.append(cid)
                self._show_typing(message)
        return list(dict.fromkeys(to_process))

    def _show_typing(self, message: WhatsAppInbound) -> None:
        """Only for a message that will get a reply. A nicety: failing must not lose the message."""
        try:
            self.client.show_typing(message.message_id)
        except httpx.HTTPError as exc:
            logger.warning("whatsapp_typing_failed error=%s", type(exc).__name__)

    def _handle_message(self, message: WhatsAppInbound) -> UUID | None:
        """One new (not retried) message. Returns its conversation if it needs a turn."""
        repo = self.service.repo
        cid = self._continues(message)
        if cid is None:
            with self._contact_lock(message.wa_id):
                cid = self._continues(message)  # another message may have opened it meanwhile
                if cid is None:
                    # A new candidate (or a returning one saying hello): open a conversation.
                    # The greeting asks for consent, so this first message isn't an answer.
                    new_id = self.service.start_conversation(CHANNEL)
                    repo.link_contact(CHANNEL, message.wa_id, new_id)
                    logger.info("whatsapp_conversation_started id=%s", new_id)
                    return None
        if message.text is None:
            self.service.reply_text_only(cid)
            return None
        if self.service.receive(cid, message.text, channel_message_id=message.message_id):
            return cid
        return None

    def _continues(self, message: WhatsAppInbound) -> UUID | None:
        """The conversation this message belongs to, or None if it opens a new one."""
        repo = self.service.repo
        cid = repo.conversation_for(CHANNEL, message.wa_id)
        state = repo.get(cid) if cid else None
        if cid is None or state is None:
            return None
        ended = state.stage == Stage.CLOSED and state.status in TERMINAL_STATUSES
        wants_restart = (message.text or "").strip().lower() in RESTART_WORDS
        return None if ended and wants_restart else cid

    @contextmanager
    def _contact_lock(self, wa_id: str) -> Iterator[None]:
        repo = self.service.repo
        name, owner = contact_lock_name(wa_id), uuid4().hex
        deadline = time.monotonic() + CONTACT_LOCK_WAIT.total_seconds()
        while not repo.try_job_lease(name, owner, CONTACT_LEASE):
            if time.monotonic() >= deadline:
                raise ContactBusy("another message is opening this contact's conversation")
            time.sleep(_CONTACT_LOCK_POLL)
        try:
            yield
        finally:
            repo.release_job_lease(name, owner)

    # --- Outbound -------------------------------------------------------------------------------

    def dispatch_once(self) -> int:
        """Send pending agent messages. Returns how many were sent (0 when another process is
        the dispatcher).

        Conversations are sent in parallel (up to `concurrency` at once); each one's messages
        go in order, one at a time, stopping at its first failure. So a slow or failing
        recipient holds up only its own conversation, and one dispatcher can send several
        messages a second while still being the only sender (no message goes out twice)."""
        repo = self.service.repo
        if not repo.try_job_lease("whatsapp_outbox", self._dispatcher_id, DISPATCH_LEASE):
            return 0
        by_conversation: dict[UUID, list[Outbound]] = defaultdict(list)
        for item in repo.undelivered(CHANNEL, limit=self.batch):
            by_conversation[item.conversation_id].append(item)
        # Stop starting sends well before the lease runs out, so it's still ours while we send.
        deadline = time.monotonic() + DISPATCH_LEASE.total_seconds() / 2
        groups = list(by_conversation.values())
        if self.concurrency == 1 or len(groups) <= 1:
            return sum(self._send_conversation(items, deadline) for items in groups)
        with ThreadPoolExecutor(min(self.concurrency, len(groups))) as pool:
            return sum(pool.map(lambda items: self._send_conversation(items, deadline), groups))

    def _send_conversation(self, items: list[Outbound], deadline: float) -> int:
        """One conversation's messages, in order. Returns how many were sent."""
        repo = self.service.repo
        now = self.service.ports.clock.now()
        state = repo.get(items[0].conversation_id)
        last_inbound = state.last_inbound_at or state.started_at if state else None
        outside_window = last_inbound is None or now - last_inbound > SERVICE_WINDOW
        sent = 0
        for item in items:
            if time.monotonic() > deadline:
                break  # the rest go out on the next run, still in order
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
                break  # keep this conversation's order: the rest wait for the retry
            repo.mark_delivery(item.message_id, True)
            metrics.WHATSAPP_SENT.labels("sent").inc()
            sent += 1
        return sent
