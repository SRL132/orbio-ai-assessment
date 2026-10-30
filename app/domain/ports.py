"""Ports: the only way the domain reaches the outside world.

Adapters implement these Protocols (real LLM, fake LLM, system clock, fake clock). The engine
depends on these interfaces only, never on an SDK.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol
from uuid import UUID

from app.domain.config import ClientConfig
from app.domain.models import (
    AnswerQuestion,
    AskField,
    ConversationState,
    DomainEvent,
    ExtractionResult,
    Outbound,
    ReAsk,
    StoredMessage,
)


class Extractor(Protocol):
    def extract(
        self, state: ConversationState, text: str, config: ClientConfig, today: date
    ) -> ExtractionResult:
        """Turn one candidate message into structured fields, language, intent and sentiment."""
        ...


class Responder(Protocol):
    def compose(
        self,
        action: AskField | ReAsk | AnswerQuestion,
        state: ConversationState,
        config: ClientConfig,
    ) -> str:
        """Write one short chat message for an action, in persona and candidate language."""
        ...


class CalendarProvider(Protocol):
    """Interview slots per location. Booking must be atomic: a slot is booked at most once."""

    def available_slots(self, location_id: str, after: datetime, count: int) -> list[datetime]: ...

    def book(self, location_id: str, starts_at: datetime, conversation_id: UUID) -> bool:
        """Returns False if another candidate took the slot in the meantime. Idempotent: the
        candidate who already holds the slot gets True again (the turn may be retried after a
        failed save, and an external calendar can't join our database transaction)."""
        ...


class Clock(Protocol):
    def now(self) -> datetime: ...


class Repository(Protocol):
    """Persistence of conversations, messages and the append-only event log."""

    def create(
        self, state: ConversationState, events: list[DomainEvent], replies: list[str]
    ) -> None: ...

    def get(self, conversation_id: UUID) -> ConversationState | None: ...

    def add_inbound(
        self, conversation_id: UUID, text: str, channel_message_id: str | None = None
    ) -> bool:
        """Store a candidate message. Returns False for a duplicate channel message id."""
        ...

    def pending_inbound(self, conversation_id: UUID) -> list[StoredMessage]: ...

    def save_turn(
        self,
        state: ConversationState,
        events: list[DomainEvent],
        replies: list[str],
        processed: list[StoredMessage],
        redact: bool = False,
    ) -> None:
        """Atomically save the new state, events and replies, and mark inbound messages done."""
        ...

    def append_events(self, state: ConversationState, events: list[DomainEvent]) -> None:
        """Audit events that don't change the state (a recruiter viewed it, added a note)."""
        ...

    def events(self, conversation_id: UUID) -> list[DomainEvent]: ...

    def timeline(self, conversation_id: UUID) -> list[tuple[datetime, DomainEvent]]:
        """Every event with its timestamp, oldest first."""
        ...

    def messages(self, conversation_id: UUID, after_seq: int = 0) -> list[StoredMessage]: ...

    def due_reminders(self, now: datetime) -> list[UUID]: ...

    # --- External channels (outbox + contacts) ---

    def undelivered(self, channel: str, limit: int = 50) -> list[Outbound]: ...

    def mark_delivery(self, message_id: UUID, delivered: bool, error: str | None = None) -> None:
        """Record a send attempt; `delivered=False` with an error leaves it for a retry."""
        ...

    def conversation_for(self, channel: str, external_id: str) -> UUID | None: ...

    def link_contact(
        self, channel: str, external_id: str, conversation_id: UUID, client_id: str
    ) -> None: ...


@dataclass(frozen=True)
class Ports:
    extractor: Extractor
    responder: Responder
    clock: Clock
    calendar: CalendarProvider | None = None
