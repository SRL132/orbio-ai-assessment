"""SQLAlchemy storage: conversations, messages and the append-only event log.

SQLite for the demo, Postgres in production (same code). The `conversations` table keeps the
full state as JSON plus a few projected columns the dashboard filters on. Every table carries
`client_id` and uses random UUIDs (docs/process-design-appendix.md, B).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Engine,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    create_engine,
    func,
    select,
    update,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.domain.models import ConversationState, DomainEvent, StoredMessage
from app.domain.ports import Clock

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


class ConversationRow(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    stage: Mapped[str] = mapped_column(String(32))
    language: Mapped[str] = mapped_column(String(8))
    full_name: Mapped[str | None] = mapped_column(String(200))
    city: Mapped[str | None] = mapped_column(String(120))
    zone: Mapped[str | None] = mapped_column(String(120))
    location_id: Mapped[str | None] = mapped_column(String(64), index=True)
    availability: Mapped[list[str]] = mapped_column(JSON, default=list)
    schedule: Mapped[list[str]] = mapped_column(JSON, default=list)
    start_date: Mapped[date | None] = mapped_column(Date)
    review_reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    next_reminder_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
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


class InterviewRow(Base):
    __tablename__ = "interviews"
    __table_args__ = (UniqueConstraint("location_id", "starts_at"),)  # no double booking

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(64))
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    location_id: Mapped[str] = mapped_column(String(64))
    starts_at: Mapped[datetime] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite:///") and not url.startswith("sqlite:///:memory:"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args)
    Base.metadata.create_all(engine)
    return engine


class SqlRepository:
    def __init__(self, engine: Engine, clock: Clock) -> None:
        self._sessions = sessionmaker(engine, expire_on_commit=False)
        self._clock = clock

    # --- writes -----------------------------------------------------------------------------------

    def create(
        self, state: ConversationState, events: list[DomainEvent], replies: list[str]
    ) -> None:
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
            row = session.get(ConversationRow, str(conversation_id))
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
                    seq=_next_seq(session, row.id),
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
    ) -> None:
        now = self._clock.now()
        with self._sessions.begin() as session:
            row = session.get(ConversationRow, str(state.conversation_id))
            if row is None:
                raise KeyError(state.conversation_id)
            _project(row, state, now)
            if processed:
                ids = [str(m.id) for m in processed]
                values: dict[str, Any] = {"processed": True}
                if redact:
                    values |= {"text": REDACTED, "redacted": True}
                session.execute(update(MessageRow).where(MessageRow.id.in_(ids)).values(**values))
            self._append(session, state, events, replies, now)

    def append_events(self, state: ConversationState, events: list[DomainEvent]) -> None:
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
        seq = _next_seq(session, conversation_id)
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
                    created_at=now,
                )
            )

    # --- reads ------------------------------------------------------------------------------------

    def get(self, conversation_id: UUID) -> ConversationState | None:
        with self._sessions() as session:
            row = session.get(ConversationRow, str(conversation_id))
            return ConversationState.model_validate(row.state) if row else None

    def pending_inbound(self, conversation_id: UUID) -> list[StoredMessage]:
        with self._sessions() as session:
            rows = session.scalars(
                select(MessageRow)
                .where(
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
                    MessageRow.conversation_id == str(conversation_id),
                    MessageRow.seq > after_seq,
                )
                .order_by(MessageRow.seq)
            ).all()
            return [_message(r) for r in rows]

    def due_reminders(self, now: datetime) -> list[UUID]:
        with self._sessions() as session:
            ids = session.scalars(
                select(ConversationRow.id).where(
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
                .where(EventRow.conversation_id == str(conversation_id))
                .order_by(EventRow.created_at, EventRow.seq)
            ).all()
            return [(r.created_at, DomainEvent(type=r.type, data=r.data)) for r in rows]


def _next_seq(session: Session, conversation_id: str) -> int:
    current = session.scalar(
        select(func.max(MessageRow.seq)).where(MessageRow.conversation_id == conversation_id)
    )
    return (current or 0) + 1


def _project(row: ConversationRow, state: ConversationState, now: datetime) -> None:
    candidate = state.candidate
    row.client_id = state.client_id
    row.status = state.status.value
    row.stage = state.stage.value
    row.language = state.language
    row.full_name = candidate.full_name
    row.city = candidate.city
    row.zone = candidate.zone
    row.location_id = candidate.location_id
    row.availability = [a.value for a in candidate.availability]
    row.schedule = [s.value for s in candidate.schedule]
    row.start_date = candidate.start_date
    row.review_reasons = list(state.review_reasons)
    row.next_reminder_at = state.next_reminder_at
    row.updated_at = now
    row.state = state.model_dump(mode="json")


def _message(row: MessageRow) -> StoredMessage:
    return StoredMessage(
        id=UUID(row.id),
        conversation_id=UUID(row.conversation_id),
        seq=row.seq,
        direction="in" if row.direction == "in" else "out",
        text=row.text,
        created_at=row.created_at,
    )
