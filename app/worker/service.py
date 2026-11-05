"""Application service: the use cases the API and the background runner call.

Synchronous on purpose: LLM calls block, so the async runner calls these in worker threads.
A lease on the conversation row guarantees one conversation is never processed twice at the
same time, by any worker in any process (docs/architecture.md, "One conversation at a time").
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from app import metrics, tracing
from app.domain import engine
from app.domain.config import ClientConfig
from app.domain.models import (
    Channel,
    ConversationState,
    DomainEvent,
    EventType,
    InboundMessage,
    Status,
    StoredMessage,
    TurnResult,
)
from app.domain.ports import Ports, Repository

logger = logging.getLogger("app.service")

MAX_MESSAGE_CHARS = 2000
# Longer than any turn can take: two LLM calls, each up to a timeout and a retry on the primary
# plus a timeout on the fallback (2 x 60 s with the default 20 s timeout). A worker that dies
# holding a lease blocks its conversation for at most this long.
LEASE = timedelta(minutes=3)
LEASE_POLL_SECONDS = 0.05
# LLM context per turn: enough for "lo mismo" or a follow-up question, without long prompts.
RECENT_MESSAGES = 6
RECENT_MESSAGE_CHARS = 300


class ConversationBusy(RuntimeError):
    """Another worker is processing the conversation; try again in a moment."""


class ScreeningService:
    def __init__(
        self,
        repo: Repository,
        config: ClientConfig,
        ports: Ports,
        recruiter_wait_seconds: float = 10.0,
    ) -> None:
        self.repo = repo
        self.config = config
        self.ports = ports
        self.recruiter_wait_seconds = recruiter_wait_seconds

    @contextmanager
    def _leased(self, conversation_id: UUID, wait_seconds: float = 0.0) -> Iterator[str | None]:
        """Hold the conversation's lease for the block. Yields the owner token, or None if
        another worker kept it for `wait_seconds`."""
        owner = uuid4().hex
        deadline = time.monotonic() + wait_seconds
        while not self.repo.try_lease(conversation_id, owner, LEASE):
            if time.monotonic() >= deadline:
                yield None
                return
            time.sleep(LEASE_POLL_SECONDS)
        try:
            yield owner
        finally:
            self.repo.release_lease(conversation_id, owner)

    def start_conversation(
        self,
        channel: Channel = "chat",
        applied_at: datetime | None = None,
        invited_by: str | None = None,
        application_ref: str | None = None,
    ) -> UUID:
        """Open a conversation. `applied_at` comes from the ATS or the recruiter who invites the
        candidate, so time to screen counts from the application, not from the first message.
        With `invited_by`, it waits for the candidate to open their link (see `open_invite`)."""
        if applied_at is not None and applied_at > self.ports.clock.now():
            raise ValueError("applied_at is in the future")
        state = ConversationState(
            client_id=self.config.client_id,
            channel=channel,
            applied_at=applied_at,
            application_ref=application_ref,
            invite_pending=invited_by is not None,
        )
        result = engine.start(state, self.config, self.ports)
        events = result.events
        if invited_by is not None:
            events = [
                *events,
                DomainEvent(type=EventType.INVITE_CREATED, data={"actor": invited_by}),
            ]
        self.repo.create(result.state, events, result.replies)
        metrics.record_events(events)
        logger.info("conversation_started id=%s channel=%s", state.conversation_id, channel)
        return state.conversation_id

    def open_invite(self, conversation_id: UUID) -> bool:
        """The candidate opened their invite link: start the reminder clock. True only for the
        call that opened it, so two people can't both claim one WhatsApp code."""
        with self._leased(conversation_id, self.recruiter_wait_seconds) as owner:
            state = self.repo.get(conversation_id)
            if owner is None or state is None:
                return False
            result = engine.on_invite_opened(state, self.config, self.ports)
            if result is None:
                return False
            self.repo.save_turn(result.state, result.events, [], [], lease_owner=owner)
            metrics.record_events(result.events)
            return True

    def receive(
        self, conversation_id: UUID, text: str, channel_message_id: str | None = None
    ) -> bool:
        """Store an inbound message. Processing happens later, after the debounce."""
        return self.repo.add_inbound(
            conversation_id, text.strip()[:MAX_MESSAGE_CHARS], channel_message_id
        )

    def process_pending(self, conversation_id: UUID) -> TurnResult | None:
        """Run engine turns until no unprocessed message is left. Returns the last turn.

        If another worker holds the conversation, return at once: that worker checks for new
        messages after releasing it (messages are stored before anyone asks for the lease), so
        nothing is left behind.
        """
        result: TurnResult | None = None
        while True:
            with self._leased(conversation_id) as owner:
                if owner is None:
                    return result
                turn = self._turn(conversation_id, owner)
            if turn is None:
                return result
            result = turn
            if not self.repo.pending_inbound(conversation_id):
                return result

    def _turn(self, conversation_id: UUID, owner: str) -> TurnResult | None:
        state = self.repo.get(conversation_id)
        pending = self.repo.pending_inbound(conversation_id)
        if state is None or not pending:
            return None
        if state.retry_turn_at is not None and self.ports.clock.now() < state.retry_turn_at:
            return None  # held while no LLM answered: wait for the retry time
        message = InboundMessage(
            conversation_id=conversation_id,
            text="\n".join(m.text for m in pending),
            received_at=pending[-1].created_at,
        )
        state.recent_messages = self._recent_messages(conversation_id, pending)
        started = time.monotonic()
        attributes = {"conversation.id": str(conversation_id), "channel": state.channel}
        with tracing.tracer.start_as_current_span("turn", attributes=attributes) as span:
            with metrics.turn_cost() as cost:
                result = engine.turn(state, message, self.config, self.ports)
            result.state.llm_cost_usd += cost.usd
            if result.hold_inbound:
                metrics.TURNS_HELD.labels("held").inc()
            else:
                result.state.turns += 1
            if any(e.data.get("gave_up") for e in result.events):
                metrics.TURNS_HELD.labels("gave_up").inc()
            self.repo.save_turn(
                result.state,
                result.events,
                result.replies,
                [] if result.hold_inbound else pending,  # held: retried at retry_turn_at
                redact=result.redact_inbound,
                lease_owner=owner,
            )
            span.set_attribute("messages", len(pending))
            span.set_attribute("status", result.state.status.value)
            span.set_attribute("stage", result.state.stage.value)
        metrics.TURN_SECONDS.labels(state.channel).observe(time.monotonic() - started)
        metrics.record_events(result.events)
        metrics.record_finished(state, result.state)
        logger.info(
            "turn id=%s messages=%d status=%s stage=%s",
            conversation_id,
            len(pending),
            result.state.status.value,
            result.state.stage.value,
        )
        return result

    def _recent_messages(
        self, conversation_id: UUID, pending: list[StoredMessage]
    ) -> list[tuple[Literal["in", "out"], str]]:
        """The messages before this turn, as stored: redacted text stays redacted."""
        waiting = {m.id for m in pending}
        earlier = [m for m in self.repo.messages(conversation_id) if m.id not in waiting]
        return [(m.direction, m.text[:RECENT_MESSAGE_CHARS]) for m in earlier[-RECENT_MESSAGES:]]

    def waiting(self, debounce: timedelta) -> list[UUID]:
        """Conversations whose messages have waited out the debounce (the worker's queue)."""
        return self.repo.waiting_conversations(self.ports.clock.now() - debounce)

    def process_waiting(self, debounce: timedelta) -> int:
        """Pick up every conversation whose messages have waited out the debounce: how a
        worker process finds work (and how any process recovers turns that failed)."""
        quiet_since = self.ports.clock.now() - debounce
        return sum(
            self.process_pending(cid) is not None
            for cid in self.repo.waiting_conversations(quiet_since)
        )

    def send_due_reminders(self) -> int:
        """Send due reminders (or mark candidates abandoned). Returns how many changed.
        A conversation that is busy right now is skipped: its turn reschedules the reminder."""
        changed = 0
        for conversation_id in self.repo.due_reminders(self.ports.clock.now()):
            with self._leased(conversation_id) as owner:
                if owner is None:
                    continue
                state = self.repo.get(conversation_id)
                result = engine.on_silence(state, self.config, self.ports) if state else None
                if state is None or result is None:
                    continue
                self.repo.save_turn(
                    result.state, result.events, result.replies, [], lease_owner=owner
                )
                metrics.record_events(result.events)
                metrics.record_finished(state, result.state)
                changed += 1
                logger.info(
                    "silence id=%s status=%s reminders=%d",
                    conversation_id,
                    result.state.status.value,
                    result.state.reminders_sent,
                )
        return changed

    def reply_text_only(self, conversation_id: UUID) -> None:
        """The candidate sent something that isn't text (audio, photo): say we only read text."""
        with self._leased(conversation_id, self.recruiter_wait_seconds) as owner:
            state = self.repo.get(conversation_id)
            if owner is None or state is None:
                return  # busy for 10 s: skip the notice rather than block the webhook
            text = self.config.template(state.language, "text_only", state.channel)
            event = DomainEvent(type=EventType.NON_TEXT_MESSAGE)
            self.repo.save_turn(state, [event], [text], [], lease_owner=owner)
            metrics.record_events([event])

    # --- Recruiter actions (writes go through the domain, never straight to the database) -----

    def override(self, conversation_id: UUID, status: Status, reason: str, actor: str) -> None:
        """Waits briefly for a running turn to finish; raises `ConversationBusy` if it doesn't."""
        if self.repo.get(conversation_id) is None:  # no row to lease: unknown, not busy
            raise KeyError(conversation_id)
        with self._leased(conversation_id, self.recruiter_wait_seconds) as owner:
            if owner is None:
                raise ConversationBusy(conversation_id)
            state = self.repo.get(conversation_id)
            if state is None:
                raise KeyError(conversation_id)
            new_state, events = engine.override_status(
                state, status, reason, actor, self.config, self.ports
            )
            self.repo.save_turn(new_state, events, [], [], lease_owner=owner)
            metrics.record_events(events)
            metrics.record_finished(state, new_state)
            logger.info("override id=%s to=%s", conversation_id, status.value)

    def erase(self, conversation_id: UUID, actor: str) -> None:
        """A recruiter erases a candidate on request: no undo. Waits for a running turn, like an
        override, so no turn can write the conversation back. Raises `KeyError` if it's unknown,
        `ConversationBusy` if a turn doesn't finish in time."""
        if self.repo.get(conversation_id) is None:
            raise KeyError(conversation_id)
        with self._leased(conversation_id, self.recruiter_wait_seconds) as owner:
            if owner is None:
                raise ConversationBusy(conversation_id)
            if not self.repo.erase(conversation_id, actor, owner):
                raise KeyError(conversation_id)
        logger.info("erased id=%s", conversation_id)

    # Notes, views and exports only append audit events, which never conflict with a turn.

    def add_note(self, conversation_id: UUID, text: str, actor: str) -> None:
        note = text.strip()[:MAX_MESSAGE_CHARS]
        state = self.repo.get(conversation_id)
        if state is None:
            raise KeyError(conversation_id)
        if note:
            self.repo.append_events(
                state,
                [DomainEvent(type=EventType.NOTE_ADDED, data={"text": note, "actor": actor})],
            )

    def record_view(self, conversation_id: UUID, actor: str) -> ConversationState | None:
        return self._read_logged(conversation_id, EventType.VIEWED, actor)

    def export(self, conversation_id: UUID, actor: str) -> ConversationState | None:
        return self._read_logged(conversation_id, EventType.EXPORTED, actor)

    def _read_logged(
        self, conversation_id: UUID, event: EventType, actor: str
    ) -> ConversationState | None:
        """Recruiter reads of personal data are logged too (who saw or exported what)."""
        state = self.repo.get(conversation_id)
        if state is not None:
            self.repo.append_events(state, [DomainEvent(type=event, data={"actor": actor})])
        return state

    # --- Reads for the API (it never reaches into the repository) -----------------------------

    def get(self, conversation_id: UUID) -> ConversationState | None:
        return self.repo.get(conversation_id)

    def messages(self, conversation_id: UUID, after_seq: int = 0) -> list[StoredMessage]:
        return self.repo.messages(conversation_id, after_seq)

    def timeline(self, conversation_id: UUID) -> list[tuple[datetime, DomainEvent]]:
        return self.repo.timeline(conversation_id)
