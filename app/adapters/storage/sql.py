"""SQLAlchemy storage: conversations, messages and the append-only event log.

SQLite for the demo, Postgres in production (same code). The `conversations` table keeps the
full state as JSON plus a few projected columns the dashboard filters on. Every table carries
`client_id` and uses random UUIDs (docs/process-design-appendix.md, B). A repository serves one
client: every read filters on its `client_id`, so a conversation id alone never reaches another
client's data.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Engine,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    create_engine,
    func,
    select,
    true,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Dialect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.adapters.storage.migrate import upgrade
from app.domain.models import (
    EXTERNAL_CHANNELS,
    ConversationState,
    DomainEvent,
    Outbound,
    StoredMessage,
)
from app.domain.ports import Clock, LeaseLost

REDACTED = "[redacted]"


class UTCDateTime(TypeDecorator[datetime]):
    """Store UTC, always hand back timezone-aware datetimes (SQLite drops the offset)."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return value.replace(tzinfo=UTC) if value is not None else None


class Base(DeclarativeBase):
    pass


ListColumn = JSON().with_variant(JSONB(), "postgresql")


class ConversationRow(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_availability", "availability", postgresql_using="gin"),
        Index("ix_conversations_schedule", "schedule", postgresql_using="gin"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    stage: Mapped[str] = mapped_column(String(32))
    channel: Mapped[str] = mapped_column(String(16), default="chat", index=True)
    language: Mapped[str] = mapped_column(String(8))
    full_name: Mapped[str | None] = mapped_column(String(200))
    city: Mapped[str | None] = mapped_column(String(120))
    zone: Mapped[str | None] = mapped_column(String(120))
    location_id: Mapped[str | None] = mapped_column(String(64), index=True)
    # JSONB with a GIN index on Postgres, so the dashboard's list filters use an index.
    availability: Mapped[list[str]] = mapped_column(ListColumn, default=list)
    schedule: Mapped[list[str]] = mapped_column(ListColumn, default=list)
    start_date: Mapped[date | None] = mapped_column(Date)
    review_reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Analytics inputs, so the dashboard aggregates in SQL instead of loading every state.
    progress: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    dropped_at: Mapped[str | None] = mapped_column(String(32))
    duration_minutes: Mapped[float | None] = mapped_column(Float)
    interview_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    keep_on_file: Mapped[bool | None] = mapped_column(Boolean)  # retention: kept if True
    next_reminder_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    # Highest message seq so far. Reserving seqs here locks the row, so concurrent writers to one
    # conversation (webhook inbound, worker reply) queue up instead of colliding.
    last_seq: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Which worker is processing the conversation, until when (see `try_lease`).
    lease_owner: Mapped[str | None] = mapped_column(String(32))
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    state: Mapped[dict[str, Any]] = mapped_column(JSON)


class MessageRow(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "seq"),
        UniqueConstraint("conversation_id", "channel_message_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    client_id: Mapped[str] = mapped_column(String(64))
    seq: Mapped[int] = mapped_column(Integer)
    direction: Mapped[str] = mapped_column(String(3))
    text: Mapped[str] = mapped_column(Text)
    channel_message_id: Mapped[str | None] = mapped_column(String(128))
    processed: Mapped[bool] = mapped_column(Boolean, default=False)
    redacted: Mapped[bool] = mapped_column(Boolean, default=False)
    # Outbox for external channels: outbound messages wait here until the dispatcher sends them.
    delivered: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), index=True
    )
    delivery_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    delivery_error: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class EventRow(Base):
    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    client_id: Mapped[str] = mapped_column(String(64))
    seq: Mapped[int] = mapped_column(Integer, default=0)
    type: Mapped[str] = mapped_column(String(64), index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)


class ChannelContactRow(Base):
    """Who a WhatsApp number belongs to: their latest conversation."""

    __tablename__ = "channel_contacts"
    __table_args__ = (
        UniqueConstraint("client_id", "channel", "external_id", name="uq_channel_contacts_contact"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(64))
    channel: Mapped[str] = mapped_column(String(16))
    external_id: Mapped[str] = mapped_column(String(64))
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class JobLeaseRow(Base):
    """Background jobs that must run in one process at a time (e.g. the WhatsApp outbox)."""

    __tablename__ = "job_leases"

    client_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner: Mapped[str] = mapped_column(String(32))
    until: Mapped[datetime] = mapped_column(UTCDateTime)


class ErasureRow(Base):
    """Proof that a candidate's data was erased under the retention policy. No personal data:
    the conversation's id, its final status and when, nothing else."""

    __tablename__ = "erasures"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(64), index=True)
    conversation_id: Mapped[str] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(32))
    last_activity_at: Mapped[datetime] = mapped_column(UTCDateTime)
    erased_at: Mapped[datetime] = mapped_column(UTCDateTime)


class RateLimitKeyRow(Base):
    """One row per rate-limited key, locked while a request is counted so counting is exact."""

    __tablename__ = "rate_limit_keys"

    client_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    touched_at: Mapped[datetime] = mapped_column(UTCDateTime)


class RateLimitHitRow(Base):
    """One counted request (a login attempt, a chat message) for rate limiting."""

    __tablename__ = "rate_limit_hits"
    __table_args__ = (Index("ix_rate_limit_hits_key", "client_id", "key", "at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(64))
    key: Mapped[str] = mapped_column(String(200))
    at: Mapped[datetime] = mapped_column(UTCDateTime)


class InterviewRow(Base):
    __tablename__ = "interviews"
    __table_args__ = (  # no double booking
        UniqueConstraint("client_id", "location_id", "starts_at", name="uq_interviews_slot"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(64))
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    location_id: Mapped[str] = mapped_column(String(64))
    starts_at: Mapped[datetime] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


def make_engine(url: str) -> Engine:
    """Engine for `url`. In-memory SQLite (tests, evals) gets `create_all`; real databases are
    brought up to date with the Alembic migrations instead."""
    in_memory = url in ("sqlite://", "sqlite:///:memory:")
    if url.startswith("sqlite:///") and not in_memory:
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    if not in_memory:
        upgrade(url)
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args)
    if in_memory:
        Base.metadata.create_all(engine)
    return engine


class SqlRepository:
    def __init__(
        self, engine: Engine, clock: Clock, client_id: str, lease_clock: Clock | None = None
    ) -> None:
        """`lease_clock` times leases. Production passes the system clock even when `clock`
        is the demo's fast-forwardable one, so skipping ahead never expires a running turn's
        lease. It defaults to `clock` (tests drive both with one fake clock)."""
        self._sessions = sessionmaker(engine, expire_on_commit=False)
        self._clock = clock
        self._client_id = client_id
        self._lease_clock = lease_clock or clock

    def _conversation(self, session: Session, conversation_id: UUID) -> ConversationRow | None:
        row = session.get(ConversationRow, str(conversation_id))
        return row if row is not None and row.client_id == self._client_id else None

    def _check_client(self, state: ConversationState) -> None:
        if state.client_id != self._client_id:
            raise ValueError(f"this repository serves {self._client_id}, not {state.client_id}")

    # --- writes -----------------------------------------------------------------------------------

    def create(
        self, state: ConversationState, events: list[DomainEvent], replies: list[str]
    ) -> None:
        self._check_client(state)
        now = self._clock.now()
        with self._sessions.begin() as session:
            row = ConversationRow(id=str(state.conversation_id), created_at=now)
            _project(row, state, now)
            session.add(row)
            session.flush()
            self._append(session, state, events, replies, now)

    def add_inbound(
        self, conversation_id: UUID, text: str, channel_message_id: str | None = None
    ) -> bool:
        now = self._clock.now()
        with self._sessions.begin() as session:
            row = self._conversation(session, conversation_id)
            if row is None:
                raise KeyError(conversation_id)
            if channel_message_id is not None:
                duplicate = session.scalar(
                    select(MessageRow.id).where(
                        MessageRow.conversation_id == row.id,
                        MessageRow.channel_message_id == channel_message_id,
                    )
                )
                if duplicate is not None:
                    return False
            session.add(
                MessageRow(
                    id=str(uuid4()),
                    conversation_id=row.id,
                    client_id=row.client_id,
                    seq=_reserve_seqs(session, row.id, 1),
                    direction="in",
                    text=text,
                    channel_message_id=channel_message_id,
                    processed=False,
                    created_at=now,
                )
            )
        return True

    def save_turn(
        self,
        state: ConversationState,
        events: list[DomainEvent],
        replies: list[str],
        processed: list[StoredMessage],
        redact: bool = False,
        lease_owner: str | None = None,
    ) -> None:
        self._check_client(state)
        now = self._clock.now()
        with self._sessions.begin() as session:
            row = self._conversation(session, state.conversation_id)
            if row is None:
                raise KeyError(state.conversation_id)
            if lease_owner is not None and row.lease_owner != lease_owner:
                raise LeaseLost(state.conversation_id)
            _project(row, state, now)
            if processed:
                ids = [str(m.id) for m in processed]
                values: dict[str, Any] = {"processed": True}
                if redact:
                    values |= {"text": REDACTED, "redacted": True}
                session.execute(update(MessageRow).where(MessageRow.id.in_(ids)).values(**values))
            self._append(session, state, events, replies, now)

    def try_lease(self, conversation_id: UUID, owner: str, duration: timedelta) -> bool:
        """One atomic UPDATE: of two workers racing, exactly one matches the free lease."""
        row = ConversationRow
        now = self._lease_clock.now()
        until = now + duration
        free = row.lease_until.is_(None) | (row.lease_until < now)
        with self._sessions.begin() as session:
            taken = session.scalar(
                update(row)
                .where(row.id == str(conversation_id), row.client_id == self._client_id, free)
                .values(lease_owner=owner, lease_until=until)
                .returning(row.id)
            )
        return taken is not None

    def release_lease(self, conversation_id: UUID, owner: str) -> None:
        row = ConversationRow
        with self._sessions.begin() as session:
            session.execute(
                update(row)
                .where(row.id == str(conversation_id), row.lease_owner == owner)
                .values(lease_owner=None, lease_until=None)
            )

    def try_job_lease(self, name: str, owner: str, duration: timedelta) -> bool:
        """Like `try_lease`, for a background job: the first caller creates the row (the
        primary key rejects a second), later ones take it over once it has expired."""
        now = self._lease_clock.now()
        until = now + duration
        job = JobLeaseRow
        mine = (job.client_id == self._client_id) & (job.name == name)
        try:
            with self._sessions.begin() as session:
                if session.get(job, (self._client_id, name)) is None:
                    session.add(job(client_id=self._client_id, name=name, owner=owner, until=until))
                    return True
                taken = session.scalar(
                    update(job)
                    .where(mine, (job.owner == owner) | (job.until < now))
                    .values(owner=owner, until=until)
                    .returning(job.name)
                )
                return taken is not None
        except IntegrityError:  # another process created the row first
            return False

    def append_events(self, state: ConversationState, events: list[DomainEvent]) -> None:
        self._check_client(state)
        with self._sessions.begin() as session:
            self._append(session, state, events, [], self._clock.now())

    def _append(
        self,
        session: Session,
        state: ConversationState,
        events: list[DomainEvent],
        replies: list[str],
        now: datetime,
    ) -> None:
        conversation_id = str(state.conversation_id)
        for index, event in enumerate(events):
            session.add(
                EventRow(
                    id=str(uuid4()),
                    conversation_id=conversation_id,
                    client_id=state.client_id,
                    seq=index,
                    type=event.type,
                    data=json.loads(json.dumps(event.data, default=str)),
                    created_at=now,
                )
            )
        if not replies:
            return
        seq = _reserve_seqs(session, conversation_id, len(replies))
        external = state.channel in EXTERNAL_CHANNELS  # web chat and voice poll; others are pushed
        for offset, text in enumerate(replies):
            session.add(
                MessageRow(
                    id=str(uuid4()),
                    conversation_id=conversation_id,
                    client_id=state.client_id,
                    seq=seq + offset,
                    direction="out",
                    text=text,
                    processed=True,
                    delivered=not external,
                    created_at=now,
                )
            )

    # --- reads ------------------------------------------------------------------------------------

    def get(self, conversation_id: UUID) -> ConversationState | None:
        with self._sessions() as session:
            row = self._conversation(session, conversation_id)
            return ConversationState.model_validate(row.state) if row else None

    def pending_inbound(self, conversation_id: UUID) -> list[StoredMessage]:
        with self._sessions() as session:
            rows = session.scalars(
                select(MessageRow)
                .where(
                    MessageRow.client_id == self._client_id,
                    MessageRow.conversation_id == str(conversation_id),
                    MessageRow.direction == "in",
                    MessageRow.processed.is_(False),
                )
                .order_by(MessageRow.seq)
            ).all()
            return [_message(r) for r in rows]

    def messages(self, conversation_id: UUID, after_seq: int = 0) -> list[StoredMessage]:
        with self._sessions() as session:
            rows = session.scalars(
                select(MessageRow)
                .where(
                    MessageRow.client_id == self._client_id,
                    MessageRow.conversation_id == str(conversation_id),
                    MessageRow.seq > after_seq,
                )
                .order_by(MessageRow.seq)
            ).all()
            return [_message(r) for r in rows]

    def waiting_conversations(self, quiet_since: datetime) -> list[UUID]:
        """Conversations with unprocessed candidate messages, none newer than `quiet_since`
        (so a burst still being typed waits for its debounce)."""
        msg = MessageRow
        with self._sessions() as session:
            ids = session.scalars(
                select(msg.conversation_id)
                .where(msg.client_id == self._client_id, msg.direction == "in")
                .where(msg.processed.is_(False))
                .group_by(msg.conversation_id)
                .having(func.max(msg.created_at) <= quiet_since)
            ).all()
            return [UUID(i) for i in ids]

    def due_reminders(self, now: datetime) -> list[UUID]:
        with self._sessions() as session:
            ids = session.scalars(
                select(ConversationRow.id).where(
                    ConversationRow.client_id == self._client_id,
                    ConversationRow.status == "in_progress",
                    ConversationRow.next_reminder_at.is_not(None),
                    ConversationRow.next_reminder_at <= now,
                )
            ).all()
            return [UUID(i) for i in ids]

    def events(self, conversation_id: UUID) -> list[DomainEvent]:
        return [event for _, event in self.timeline(conversation_id)]

    def timeline(self, conversation_id: UUID) -> list[tuple[datetime, DomainEvent]]:
        with self._sessions() as session:
            rows = session.scalars(
                select(EventRow)
                .where(
                    EventRow.client_id == self._client_id,
                    EventRow.conversation_id == str(conversation_id),
                )
                .order_by(EventRow.created_at, EventRow.seq)
            ).all()
            return [(r.created_at, DomainEvent(type=r.type, data=r.data)) for r in rows]

    # --- External channels -------------------------------------------------------------------

    def undelivered(self, channel: str, limit: int = 50) -> list[Outbound]:
        query = (
            select(MessageRow, ChannelContactRow.external_id)
            .join(ConversationRow, ConversationRow.id == MessageRow.conversation_id)
            .join(
                ChannelContactRow,
                (ChannelContactRow.conversation_id == MessageRow.conversation_id)
                & (ChannelContactRow.channel == channel),
            )
            .where(
                MessageRow.client_id == self._client_id,
                MessageRow.direction == "out",
                MessageRow.delivered.is_(False),
                ConversationRow.channel == channel,
            )
            .order_by(MessageRow.created_at, MessageRow.seq)
            .limit(limit)
        )
        with self._sessions() as session:
            return [
                Outbound(
                    message_id=UUID(row.id),
                    conversation_id=UUID(row.conversation_id),
                    external_id=external_id,
                    text=row.text,
                    attempts=row.delivery_attempts,
                )
                for row, external_id in session.execute(query)
            ]

    def mark_delivery(self, message_id: UUID, delivered: bool, error: str | None = None) -> None:
        with self._sessions.begin() as session:
            session.execute(
                update(MessageRow)
                .where(MessageRow.client_id == self._client_id, MessageRow.id == str(message_id))
                .values(
                    delivered=delivered,
                    delivery_attempts=MessageRow.delivery_attempts + 1,
                    delivery_error=error[:300] if error else None,
                )
            )

    def conversation_for(self, channel: str, external_id: str) -> UUID | None:
        with self._sessions() as session:
            found = session.scalar(
                select(ChannelContactRow.conversation_id).where(
                    ChannelContactRow.client_id == self._client_id,
                    ChannelContactRow.channel == channel,
                    ChannelContactRow.external_id == external_id,
                )
            )
            return UUID(found) if found else None

    def link_contact(self, channel: str, external_id: str, conversation_id: UUID) -> None:
        now = self._clock.now()
        with self._sessions.begin() as session:
            row = session.scalar(
                select(ChannelContactRow).where(
                    ChannelContactRow.client_id == self._client_id,
                    ChannelContactRow.channel == channel,
                    ChannelContactRow.external_id == external_id,
                )
            )
            if row is None:
                session.add(
                    ChannelContactRow(
                        id=str(uuid4()),
                        client_id=self._client_id,
                        channel=channel,
                        external_id=external_id,
                        conversation_id=str(conversation_id),
                        updated_at=now,
                    )
                )
            else:
                row.conversation_id = str(conversation_id)
                row.updated_at = now


def _reserve_seqs(session: Session, conversation_id: str, count: int) -> int:
    """Reserve `count` consecutive message seqs and return the first. One atomic UPDATE: it holds
    the conversation row lock until commit, unlike reading max(seq) and adding one."""
    last = session.execute(
        update(ConversationRow)
        .where(ConversationRow.id == conversation_id)
        .values(last_seq=ConversationRow.last_seq + count)
        .returning(ConversationRow.last_seq)
    ).scalar_one()
    return int(last) - count + 1


def _project(row: ConversationRow, state: ConversationState, now: datetime) -> None:
    candidate = state.candidate
    row.client_id = state.client_id
    row.status = state.status.value
    row.stage = state.stage.value
    row.channel = state.channel
    row.language = state.language
    row.full_name = candidate.full_name
    row.city = candidate.city
    row.zone = candidate.zone
    row.location_id = candidate.location_id
    row.availability = [a.value for a in candidate.availability]
    row.schedule = [s.value for s in candidate.schedule]
    row.start_date = candidate.start_date
    row.review_reasons = [str(r) for r in state.review_reasons]
    row.progress = state.progress
    row.dropped_at = state.dropped_at
    row.duration_minutes = _duration_minutes(state)
    row.interview_at = state.interview_at
    row.keep_on_file = state.keep_on_file
    row.next_reminder_at = state.next_reminder_at
    row.updated_at = now
    row.state = state.model_dump(mode="json")


def _duration_minutes(state: ConversationState) -> float | None:
    if state.started_at is None or state.ended_at is None:
        return None
    return (state.ended_at - state.started_at).total_seconds() / 60


def _message(row: MessageRow) -> StoredMessage:
    return StoredMessage(
        id=UUID(row.id),
        conversation_id=UUID(row.conversation_id),
        seq=row.seq,
        direction="in" if row.direction == "in" else "out",
        text=row.text,
        created_at=row.created_at,
    )
