"""Application service: the use cases the API and the background runner call.

Synchronous on purpose: LLM calls block, so the async runner calls these in worker threads.
A per-conversation lock guarantees one conversation is never processed twice at the same time
(docs/architecture.md, "One conversation at a time").
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from uuid import UUID

from app.domain import engine
from app.domain.config import ClientConfig
from app.domain.models import (
    Channel,
    ConversationState,
    DomainEvent,
    InboundMessage,
    Status,
    TurnResult,
)
from app.domain.ports import Ports, Repository

logger = logging.getLogger("app.service")

MAX_MESSAGE_CHARS = 2000


class ScreeningService:
    def __init__(self, repo: Repository, config: ClientConfig, ports: Ports) -> None:
        self.repo = repo
        self.config = config
        self.ports = ports
        self._locks: defaultdict[UUID, threading.Lock] = defaultdict(threading.Lock)
        self._locks_guard = threading.Lock()

    def _lock(self, conversation_id: UUID) -> threading.Lock:
        with self._locks_guard:
            return self._locks[conversation_id]

    def start_conversation(self, channel: Channel = "chat") -> UUID:
        state = ConversationState(client_id=self.config.client_id, channel=channel)
        result = engine.start(state, self.config, self.ports)
        self.repo.create(result.state, result.events, result.replies)
        logger.info("conversation_started id=%s channel=%s", state.conversation_id, channel)
        return state.conversation_id

    def receive(
        self, conversation_id: UUID, text: str, channel_message_id: str | None = None
    ) -> bool:
        """Store an inbound message. Processing happens later, after the debounce."""
        return self.repo.add_inbound(
            conversation_id, text.strip()[:MAX_MESSAGE_CHARS], channel_message_id
        )

    def process_pending(self, conversation_id: UUID) -> TurnResult | None:
        """Run one engine turn over every unprocessed message of the conversation."""
        with self._lock(conversation_id):
            state = self.repo.get(conversation_id)
            pending = self.repo.pending_inbound(conversation_id)
            if state is None or not pending:
                return None
            message = InboundMessage(
                conversation_id=conversation_id,
                text="\n".join(m.text for m in pending),
                received_at=pending[-1].created_at,
            )
            result = engine.turn(state, message, self.config, self.ports)
            self.repo.save_turn(
                result.state, result.events, result.replies, pending, redact=result.redact_inbound
            )
            logger.info(
                "turn id=%s messages=%d status=%s stage=%s",
                conversation_id,
                len(pending),
                result.state.status.value,
                result.state.stage.value,
            )
            return result

    def send_due_reminders(self) -> int:
        """Send due reminders (or mark candidates abandoned). Returns how many changed."""
        changed = 0
        for conversation_id in self.repo.due_reminders(self.ports.clock.now()):
            with self._lock(conversation_id):
                state = self.repo.get(conversation_id)
                if state is None:
                    continue
                result = engine.on_silence(state, self.config, self.ports)
                if result is None:
                    continue
                self.repo.save_turn(result.state, result.events, result.replies, [])
                changed += 1
                logger.info(
                    "silence id=%s status=%s reminders=%d",
                    conversation_id,
                    result.state.status.value,
                    result.state.reminders_sent,
                )
        return changed

    # --- Recruiter actions (writes go through the domain, never straight to the database) -----

    def override(self, conversation_id: UUID, status: Status, reason: str, actor: str) -> None:
        with self._lock(conversation_id):
            state = self.repo.get(conversation_id)
            if state is None:
                raise KeyError(conversation_id)
            new_state, events = engine.override_status(
                state, status, reason, actor, self.config, self.ports
            )
            self.repo.save_turn(new_state, events, [], [])
            logger.info("override id=%s to=%s", conversation_id, status.value)

    def add_note(self, conversation_id: UUID, text: str, actor: str) -> None:
        state = self.repo.get(conversation_id)
        if state is None:
            raise KeyError(conversation_id)
        note = text.strip()[:MAX_MESSAGE_CHARS]
        if note:
            self.repo.append_events(
                state, [DomainEvent(type="note_added", data={"text": note, "actor": actor})]
            )

    def record_view(self, conversation_id: UUID, actor: str) -> ConversationState | None:
        state = self.repo.get(conversation_id)
        if state is not None:
            self.repo.append_events(state, [DomainEvent(type="viewed", data={"actor": actor})])
        return state
