"""Ports: the only way the domain reaches the outside world.

Adapters implement these Protocols (real LLM, fake LLM, system clock, fake clock). The engine
depends on these interfaces only, never on an SDK.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Protocol
from uuid import UUID

from app.domain.config import ClientConfig
from app.domain.models import (
    AskField,
    ConversationState,
    DomainEvent,
    ExtractionResult,
    GeoPlace,
    LlmOutput,
    Outbound,
    ReAsk,
    StoredMessage,
)


class LLMUnavailable(RuntimeError):
    """No LLM provider answered (outage, rate limit, timeout, or no free slot). Unlike a bad
    answer, retrying later can help: the engine keeps the candidate's message for a retry."""


class Extractor(Protocol):
    def extract(
        self, state: ConversationState, text: str, config: ClientConfig, today: date
    ) -> ExtractionResult:
        """Turn one candidate message into structured fields, language, intent and sentiment."""
        ...


class Responder(Protocol):
    def compose(
        self,
        action: AskField | ReAsk,
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


class Geocoder(Protocol):
    """Finds a place by name, for places the client config doesn't list. Only the place name
    is sent, never who asked. Raises when the lookup itself fails (the caller decides)."""

    def locate(self, place: str, country_codes: list[str]) -> GeoPlace | None:
        """The best match in these countries (ISO codes, e.g. ["ES", "MX"]), or None."""
        ...


class Clock(Protocol):
    def now(self) -> datetime: ...


class LeaseLost(RuntimeError):
    """Another worker took over the conversation (our lease expired): don't save over it."""


class Repository(Protocol):
    """Persistence of conversations, messages and the append-only event log, for one client:
    reads never return another client's data, and writing another client's state is an error."""

    def create(
        self, state: ConversationState, events: list[DomainEvent], replies: list[str]
    ) -> None: ...

    def get(self, conversation_id: UUID) -> ConversationState | None: ...

    def erase(self, conversation_id: UUID, erased_by: str, lease_owner: str) -> bool:
        """Delete all of a conversation's data at once, keeping only proof of who did it and
        when. Returns False if it isn't this client's or `lease_owner` doesn't hold its lease."""
        ...

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
        lease_owner: str | None = None,
    ) -> None:
        """Atomically save the new state, events and replies, and mark inbound messages done.
        With `lease_owner`, raises `LeaseLost` unless that owner still holds the lease."""
        ...

    def try_lease(self, conversation_id: UUID, owner: str, duration: timedelta) -> bool:
        """Take the conversation's lease if nobody holds it (or theirs expired): one worker
        at a time, across processes. Expires on its own if the holder dies."""
        ...

    def release_lease(self, conversation_id: UUID, owner: str) -> None: ...

    def try_job_lease(self, name: str, owner: str, duration: timedelta) -> bool:
        """A lease for a background job that must run in one process at a time. The owner can
        renew it; anyone can take it once it expires."""
        ...

    def release_job_lease(self, name: str, owner: str) -> None:
        """Give a job lease up early (and delete its row) if `owner` still holds it."""
        ...

    def append_events(self, state: ConversationState, events: list[DomainEvent]) -> None:
        """Audit events that don't change the state (a recruiter viewed it, added a note)."""
        ...

    def events(self, conversation_id: UUID) -> list[DomainEvent]: ...

    def save_llm_outputs(self, conversation_id: UUID, outputs: list[LlmOutput]) -> None:
        """Keep raw LLM replies for debugging. They are erased with the conversation."""
        ...

    def llm_outputs(self, conversation_id: UUID) -> list[LlmOutput]: ...

    def timeline(self, conversation_id: UUID) -> list[tuple[datetime, DomainEvent]]:
        """Every event with its timestamp, oldest first."""
        ...

    def messages(self, conversation_id: UUID, after_seq: int = 0) -> list[StoredMessage]: ...

    def due_reminders(self, now: datetime) -> list[UUID]: ...

    def waiting_conversations(self, quiet_since: datetime) -> list[UUID]:
        """Conversations with unprocessed messages, the newest no later than `quiet_since`."""
        ...

    # --- External channels (outbox + contacts) ---

    def undelivered(self, channel: str, limit: int = 50) -> list[Outbound]: ...

    def mark_delivery(self, message_id: UUID, delivered: bool, error: str | None = None) -> None:
        """Record a send attempt; `delivered=False` with an error leaves it for a retry."""
        ...

    def claim_delivery(self, channel: str, message_id: str) -> bool:
        """Record a webhook delivery by the channel's message id. Returns False if it was
        claimed before (a retry), atomically, so of two concurrent copies exactly one wins."""
        ...

    def release_delivery(self, channel: str, message_id: str) -> None:
        """Give a claim back when handling the delivery failed, so the channel's retry counts."""
        ...

    def conversation_for(self, channel: str, external_id: str) -> UUID | None: ...

    def link_contact(self, channel: str, external_id: str, conversation_id: UUID) -> None: ...


@dataclass(frozen=True)
class Ports:
    extractor: Extractor
    responder: Responder
    clock: Clock
    calendar: CalendarProvider | None = None
    geocoder: Geocoder | None = None  # None: unlisted places are outside the area, as before
