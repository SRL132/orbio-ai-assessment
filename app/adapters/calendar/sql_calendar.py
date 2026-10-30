"""Mock interview calendar backed by the database (demo): fixed weekday slots per location.

Slots come from the client config (`interview.slot_times`) in each location's local time.
Bookings live in the `interviews` table, whose unique constraint is what prevents double
booking (two candidates racing for a slot); the check before it makes rebooking idempotent.
In production this adapter would call each location's real calendar instead.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.storage.sql import ConversationRow, InterviewRow
from app.domain.config import ClientConfig
from app.domain.ports import Clock

SEARCH_DAYS = 21
SATURDAY = 5
# A booking whose turn never saved (the conversation doesn't show it) is released after this.
ORPHAN_GRACE = timedelta(minutes=10)


class SqlCalendar:
    def __init__(self, engine: Engine, config: ClientConfig, clock: Clock) -> None:
        self._engine = engine
        self._config = config
        self._clock = clock

    def available_slots(self, location_id: str, after: datetime, count: int) -> list[datetime]:
        location = self._config.location(location_id)
        if location is None:
            return []
        tz = ZoneInfo(location.timezone)
        times = [time.fromisoformat(t) for t in self._config.interview.slot_times]
        with Session(self._engine) as session:
            booked = set(
                session.scalars(
                    select(InterviewRow.starts_at).where(
                        InterviewRow.client_id == self._config.client_id,
                        InterviewRow.location_id == location_id,
                        InterviewRow.starts_at >= after,
                    )
                )
            )
        slots: list[datetime] = []
        day = after.astimezone(tz).date()
        for _ in range(SEARCH_DAYS):
            if day.weekday() < SATURDAY:  # interviews on weekdays only
                for t in times:
                    slot = datetime.combine(day, t, tzinfo=tz).astimezone(UTC)
                    if slot >= after and slot not in booked:
                        slots.append(slot)
                        if len(slots) == count:
                            return slots
            day += timedelta(days=1)
        return slots

    def book(self, location_id: str, starts_at: datetime, conversation_id: UUID) -> bool:
        """Idempotent per candidate: booking the slot they already hold returns True, so a turn
        that booked but failed to save can be retried."""
        with Session(self._engine) as session:
            holder = session.scalar(
                select(InterviewRow.conversation_id).where(
                    InterviewRow.client_id == self._config.client_id,
                    InterviewRow.location_id == location_id,
                    InterviewRow.starts_at == starts_at,
                )
            )
        if holder is not None:
            return holder == str(conversation_id)
        row = InterviewRow(
            id=str(uuid4()),
            client_id=self._config.client_id,
            conversation_id=str(conversation_id),
            location_id=location_id,
            starts_at=starts_at,
            created_at=self._clock.now(),
        )
        try:
            with Session(self._engine) as session, session.begin():
                session.add(row)
        except IntegrityError:
            return False
        return True

    def release_orphans(self) -> int:
        """Free slots booked by a turn that then failed to save and was never retried: the
        conversation doesn't record the interview. Returns how many were released."""
        cutoff = self._clock.now() - ORPHAN_GRACE
        recorded = select(ConversationRow.id).where(
            ConversationRow.id == InterviewRow.conversation_id,
            ConversationRow.interview_at == InterviewRow.starts_at,
        )
        with Session(self._engine) as session, session.begin():
            released = session.scalars(
                delete(InterviewRow)
                .where(
                    InterviewRow.client_id == self._config.client_id,
                    InterviewRow.created_at < cutoff,
                    ~recorded.exists(),
                )
                .returning(InterviewRow.id)
            ).all()
        return len(released)
