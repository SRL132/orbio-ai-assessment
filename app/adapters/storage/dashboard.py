"""Read model for the recruiter dashboard (CQRS-lite: reads query the projection directly).

Writes such as overrides go through the domain and the service, never through here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import ColumnElement, Engine, String, case, cast, func, select, type_coerce
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.adapters.storage.sql import ChannelContactRow, ConversationRow, EventRow, MessageRow
from app.domain.models import Availability, EventType, FieldName, Schedule, Stage, Status


class CandidateItem(BaseModel):
    id: str
    full_name: str | None
    city: str | None
    zone: str | None
    location_id: str | None
    status: str
    stage: str
    channel: str
    language: str
    availability: list[str]
    schedule: list[str]
    start_date: date | None
    review_reasons: list[str]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class Filters:
    status: str | None = None
    location_id: str | None = None
    availability: Availability | None = None
    schedule: Schedule | None = None
    language: str | None = None
    channel: str | None = None
    search: str | None = None
    start_before: date | None = None
    asked_for_human: bool = False


PER_PAGE = 50


class Page(BaseModel):
    items: list[CandidateItem]
    total: int
    page: int
    per_page: int

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.per_page))

    @property
    def first(self) -> int:
        """1-based position of the first item shown (0 when the page is empty)."""
        return (self.page - 1) * self.per_page + 1 if self.items else 0


def _json_list_has(
    column: InstrumentedAttribute[list[str]], value: Availability | Schedule, postgres: bool
) -> ColumnElement[bool]:
    """A list column contains `value`. Postgres: JSONB containment (`@>`), which uses the GIN
    index. Elsewhere (SQLite): match '"value"' in the JSON text; enum values hold no quotes
    or LIKE wildcards."""
    if postgres:
        return type_coerce(column, JSONB).contains([value.value])
    return cast(column, String).like(f'%"{value.value}"%')


# Recruiters act on qualified and needs-review candidates first.
_STATUS_ORDER = case(
    (ConversationRow.status == "needs_review", 0),
    (ConversationRow.status == "qualified", 1),
    (ConversationRow.status == "in_progress", 2),
    else_=3,
)


# Rejected, then asked for a person: the rejection stands until a recruiter looks at it.
_ASKED_FOR_HUMAN = ConversationRow.human_after_knockout.is_(True) & (
    ConversationRow.status == Status.DISQUALIFIED.value
)


@dataclass(frozen=True)
class UnfinishedInvite:
    conversation_id: UUID
    reference: str
    applied_at: datetime | None
    opened: bool
    channel: str = "chat"


class DashboardQueries:
    def __init__(self, engine: Engine, client_id: str) -> None:
        self._engine = engine
        self._client_id = client_id
        self._postgres = engine.dialect.name == "postgresql"

    def candidates(self, filters: Filters, page: int = 1, per_page: int = PER_PAGE) -> Page:
        """One page of the filtered list, recruiters' priorities first; every filter is SQL, so
        the total and the pages agree."""
        query = select(ConversationRow).where(
            ConversationRow.client_id == self._client_id, ConversationRow.invite_pending.is_(False)
        )
        if filters.asked_for_human:
            query = query.where(_ASKED_FOR_HUMAN)
        if filters.status:
            query = query.where(ConversationRow.status == filters.status)
        if filters.location_id:
            query = query.where(ConversationRow.location_id == filters.location_id)
        if filters.language:
            query = query.where(ConversationRow.language == filters.language)
        if filters.channel:
            query = query.where(ConversationRow.channel == filters.channel)
        if filters.search:
            query = query.where(ConversationRow.full_name.ilike(f"%{filters.search.strip()}%"))
        if filters.start_before:
            query = query.where(ConversationRow.start_date <= filters.start_before)
        if filters.availability:
            query = query.where(
                _json_list_has(ConversationRow.availability, filters.availability, self._postgres)
            )
        if filters.schedule:
            query = query.where(
                _json_list_has(ConversationRow.schedule, filters.schedule, self._postgres)
            )
        page = max(page, 1)
        listed = (
            query.order_by(_STATUS_ORDER, ConversationRow.updated_at.desc(), ConversationRow.id)
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        with Session(self._engine) as session:
            total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
            items = [_item(row) for row in session.scalars(listed)]
        return Page(items=items, total=total, page=page, per_page=per_page)

    def unfinished_invites(self) -> list[UnfinishedInvite]:
        """Invited applicants who haven't finished, oldest first: web chat can't reach someone who
        closed the tab, so recruiters chase these. That includes those abandoned for silence (a
        reply resumes them), never those who declined consent. Only those with a reference can
        be matched to a person in the recruiter's own file."""
        with Session(self._engine) as session:
            rows = session.scalars(
                select(ConversationRow)
                .where(
                    ConversationRow.client_id == self._client_id,
                    ConversationRow.status.in_([Status.IN_PROGRESS.value, Status.ABANDONED.value]),
                    ConversationRow.channel.in_(["chat", "voice", "whatsapp"]),
                )
                .order_by(ConversationRow.applied_at)
            ).all()
            return [
                UnfinishedInvite(
                    conversation_id=UUID(row.id),
                    reference=row.state["application_ref"],
                    applied_at=row.applied_at,
                    opened=not row.invite_pending,
                    channel=row.channel,
                )
                for row in rows
                if row.state.get("application_ref")
                and (row.status == Status.IN_PROGRESS.value or row.state.get("went_silent"))
            ]

    def channel_contact(self, conversation_id: UUID, channel: str) -> str | None:
        """The number a channel knows this candidate by (WhatsApp), for a recruiter to write to."""
        with Session(self._engine) as session:
            contact: str | None = session.scalar(
                select(ChannelContactRow.external_id).where(
                    ChannelContactRow.client_id == self._client_id,
                    ChannelContactRow.channel == channel,
                    ChannelContactRow.conversation_id == str(conversation_id),
                )
            )
            return contact

    def review_queue(self) -> list[CandidateItem]:
        query = (
            select(ConversationRow)
            .where(
                ConversationRow.client_id == self._client_id,
                ConversationRow.status == "needs_review",
            )
            .order_by(ConversationRow.updated_at)  # oldest first: nobody waits too long
        )
        with Session(self._engine) as session:
            return [_item(row) for row in session.scalars(query)]

    def analytics(
        self, field_order: list[FieldName], now: datetime, target_hours: float = 24.0
    ) -> Analytics:
        """Conversation metrics (docs/recruiter-dashboard.md, Analytics), aggregated in SQL over
        the projected columns: a handful of small queries, whatever the number of candidates."""
        row = ConversationRow
        mine = row.client_id == self._client_id
        opened = row.invite_pending.is_(False)
        completed = row.status.in_([s.value for s in COMPLETED])
        # Screened on time: an outcome within the target after applying. Counted against
        # everyone whose window has closed, so candidates still inside it don't drag it down.
        on_time = completed & (row.hours_to_outcome <= target_hours)
        window_closed = row.applied_at <= now - timedelta(hours=target_hours)
        due = on_time | window_closed
        consented = ~(
            ((row.status == Status.IN_PROGRESS.value) & (row.stage == Stage.CONSENT.value))
            | (func.coalesce(row.dropped_at, "") == "consent")
        )
        timed = completed & row.duration_minutes.is_not(None)
        steps = [
            func.count().filter(consented & (row.progress >= i)).label(f"step_{i}")
            for i in range(1, len(field_order) + 1)
        ]
        totals = select(
            func.count().filter(opened),
            func.count().filter(consented),
            func.count().filter(completed),
            func.count().filter(row.interview_at.is_not(None)),
            func.count().filter(timed),
            func.count().filter(on_time),
            func.count().filter(due),
            func.count().filter(row.invite_pending.is_(True)),
            *steps,
        ).where(mine)
        with Session(self._engine) as session:
            (
                started,
                n_consented,
                n_completed,
                booked,
                n_timed,
                n_on_time,
                n_due,
                n_unopened,
                *reached,
            ) = session.execute(totals).one()
            outcomes = dict(
                session.execute(
                    select(row.status, func.count()).where(mine, opened).group_by(row.status)
                ).all()
            )
            drop_off = session.execute(
                select(func.coalesce(row.dropped_at, "unknown"), func.count())
                .where(mine, row.status == Status.ABANDONED.value)
                .group_by(row.dropped_at)
                .order_by(func.count().desc())
            ).all()
            median = session.scalar(  # the middle row: portable, unlike percentile functions
                select(row.duration_minutes)
                .where(mine, timed)
                .order_by(row.duration_minutes)
                .offset(n_timed // 2)
                .limit(1)
            )

            def breakdown(column: InstrumentedAttribute[Any]) -> dict[str, tuple[int, int]]:
                rows = session.execute(
                    select(
                        func.coalesce(column, "unknown"),
                        func.count(),
                        func.count().filter(completed),
                    )
                    .where(mine, opened)
                    .group_by(column)
                ).all()
                return {label: (total, done) for label, total, done in sorted(rows)}

            by_language = breakdown(row.language)
            by_channel = breakdown(row.channel)
            by_location = breakdown(row.location_id)
            by_version = breakdown(row.agent_version)
            knockouts, overturned = self._knockouts_overturned(session)

        funnel = [("started", started), ("consented", n_consented)]
        funnel += [(name.value, count) for name, count in zip(field_order, reached, strict=True)]
        # No "finished" row: knockouts and hand-offs finish early, so it isn't a funnel step.
        return Analytics(
            started=started,
            consented=n_consented,
            completed=n_completed,
            completion_rate=round(n_completed / started, 3) if started else 0.0,
            outcomes={status.value: outcomes.get(status.value, 0) for status in Status},
            funnel=funnel,
            drop_off=dict(drop_off),
            median_minutes=round(median, 1) if median is not None else None,
            by_language=by_language,
            by_channel=by_channel,
            by_location=by_location,
            by_version=by_version,
            interviews_booked=booked,
            target_hours=target_hours,
            screened_on_time=n_on_time,
            screening_due=n_due,
            screened_on_time_rate=round(n_on_time / n_due, 3) if n_due else None,
            invites_unopened=n_unopened,
            knockouts=knockouts,
            knockouts_overturned=overturned,
            overturn_rate=round(overturned / knockouts, 3) if knockouts else None,
        )

    def _knockouts_overturned(self, session: Session) -> tuple[int, int]:
        """Candidates knocked out, and how many of them a recruiter moved away from disqualified.
        A candidate correcting their own answer isn't an overturn: no override event."""
        event = EventRow
        knocked_out = (
            select(event.conversation_id)
            .where(event.client_id == self._client_id, event.type == EventType.KNOCKOUT.value)
            .subquery()
        )
        knockouts = session.scalar(select(func.count(func.distinct(knocked_out.c.conversation_id))))
        overturned = session.scalar(
            select(func.count(func.distinct(event.conversation_id))).where(
                event.client_id == self._client_id,
                event.type == EventType.OVERRIDE.value,
                event.data["from"].as_string() == Status.DISQUALIFIED.value,
                event.data["to"].as_string() != Status.DISQUALIFIED.value,
                event.conversation_id.in_(select(knocked_out.c.conversation_id)),
            )
        )
        return knockouts or 0, overturned or 0

    def backlog(self) -> dict[str, tuple[int, datetime | None]]:
        """Operational backlogs for monitoring: how many items wait, and since when."""
        conv, msg = ConversationRow, MessageRow
        pending = select(func.count(), func.min(msg.created_at)).where(
            msg.client_id == self._client_id, msg.direction == "in", msg.processed.is_(False)
        )
        review = select(func.count(), func.min(conv.updated_at)).where(
            conv.client_id == self._client_id, conv.status == Status.NEEDS_REVIEW.value
        )
        outbox = select(func.count(), func.min(msg.created_at)).where(
            msg.client_id == self._client_id, msg.direction == "out", msg.delivered.is_(False)
        )
        backlog: dict[str, tuple[int, datetime | None]] = {}
        with Session(self._engine) as session:
            for name, query in (
                ("pending_messages", pending),
                ("review_queue", review),
                ("whatsapp_outbox", outbox),
            ):
                count, oldest = session.execute(query).one()
                backlog[name] = (count, oldest)
        return backlog

    def counters(self, since: datetime) -> dict[str, int]:
        query = select(
            func.count().filter(ConversationRow.created_at >= since),
            func.count().filter(ConversationRow.status == "qualified"),
            func.count().filter(ConversationRow.status == "needs_review"),
            func.count().filter(ConversationRow.status == "in_progress"),
            func.count().filter(_ASKED_FOR_HUMAN),
        ).where(
            ConversationRow.client_id == self._client_id, ConversationRow.invite_pending.is_(False)
        )
        with Session(self._engine) as session:
            new, qualified, review, in_progress, asked = session.execute(query).one()
        return {
            "new_today": new,
            "qualified": qualified,
            "needs_review": review,
            "in_progress": in_progress,
            "asked_for_human": asked,
        }


def _item(row: ConversationRow) -> CandidateItem:
    data: dict[str, Any] = {
        name: getattr(row, name) for name in CandidateItem.model_fields if name != "id"
    }
    return CandidateItem(id=row.id, **data)


class Analytics(BaseModel):
    started: int
    consented: int
    completed: int
    completion_rate: float
    outcomes: dict[str, int]
    funnel: list[tuple[str, int]]
    drop_off: dict[str, int]
    median_minutes: float | None
    by_language: dict[str, tuple[int, int]]
    by_channel: dict[str, tuple[int, int]]
    by_location: dict[str, tuple[int, int]]
    by_version: dict[str, tuple[int, int]]
    interviews_booked: int
    target_hours: float
    screened_on_time: int
    screening_due: int  # applicants whose window has closed, or who were already screened
    screened_on_time_rate: float | None
    invites_unopened: int
    knockouts: int
    knockouts_overturned: int  # by a recruiter; the pilot's main safety number
    overturn_rate: float | None


# Reached a final outcome through the conversation (not dropped off).
COMPLETED = frozenset({Status.QUALIFIED, Status.DISQUALIFIED, Status.NEEDS_REVIEW})
