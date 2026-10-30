"""Read model for the recruiter dashboard (CQRS-lite: reads query the projection directly).

Writes such as overrides go through the domain and the service, never through here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import ColumnElement, Engine, String, case, cast, func, select
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.adapters.storage.sql import ConversationRow
from app.domain.models import Availability, FieldName, Schedule, Stage, Status


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
    column: InstrumentedAttribute[list[str]], value: Availability | Schedule
) -> ColumnElement[bool]:
    """A JSON list column contains `value`, portably: match '"value"' in its text. Enum values
    hold no quotes or LIKE wildcards. (At scale: a GIN index on JSONB in Postgres.)"""
    return cast(column, String).like(f'%"{value.value}"%')


# Recruiters act on qualified and needs-review candidates first.
_STATUS_ORDER = case(
    (ConversationRow.status == "needs_review", 0),
    (ConversationRow.status == "qualified", 1),
    (ConversationRow.status == "in_progress", 2),
    else_=3,
)


class DashboardQueries:
    def __init__(self, engine: Engine, client_id: str) -> None:
        self._engine = engine
        self._client_id = client_id

    def candidates(self, filters: Filters, page: int = 1, per_page: int = PER_PAGE) -> Page:
        """One page of the filtered list, recruiters' priorities first; every filter is SQL, so
        the total and the pages agree."""
        query = select(ConversationRow).where(ConversationRow.client_id == self._client_id)
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
            query = query.where(_json_list_has(ConversationRow.availability, filters.availability))
        if filters.schedule:
            query = query.where(_json_list_has(ConversationRow.schedule, filters.schedule))
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

    def analytics(self, field_order: list[FieldName]) -> Analytics:
        """Conversation metrics (docs/recruiter-dashboard.md, Analytics), aggregated in SQL over
        the projected columns: a handful of small queries, whatever the number of candidates."""
        row = ConversationRow
        mine = row.client_id == self._client_id
        completed = row.status.in_([s.value for s in COMPLETED])
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
            func.count(),
            func.count().filter(consented),
            func.count().filter(completed),
            func.count().filter(row.interview_at.is_not(None)),
            func.count().filter(timed),
            *steps,
        ).where(mine)
        with Session(self._engine) as session:
            started, n_consented, n_completed, booked, n_timed, *reached = session.execute(
                totals
            ).one()
            outcomes = dict(
                session.execute(
                    select(row.status, func.count()).where(mine).group_by(row.status)
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
                    .where(mine)
                    .group_by(column)
                ).all()
                return {label: (total, done) for label, total, done in sorted(rows)}

            by_language = breakdown(row.language)
            by_channel = breakdown(row.channel)
            by_location = breakdown(row.location_id)

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
            interviews_booked=booked,
        )

    def counters(self, since: datetime) -> dict[str, int]:
        query = select(
            func.count().filter(ConversationRow.created_at >= since),
            func.count().filter(ConversationRow.status == "qualified"),
            func.count().filter(ConversationRow.status == "needs_review"),
            func.count().filter(ConversationRow.status == "in_progress"),
        ).where(ConversationRow.client_id == self._client_id)
        with Session(self._engine) as session:
            new, qualified, review, in_progress = session.execute(query).one()
        return {
            "new_today": new,
            "qualified": qualified,
            "needs_review": review,
            "in_progress": in_progress,
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
    interviews_booked: int


# Reached a final outcome through the conversation (not dropped off).
COMPLETED = frozenset({Status.QUALIFIED, Status.DISQUALIFIED, Status.NEEDS_REVIEW})
