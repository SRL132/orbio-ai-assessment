"""Read model for the recruiter dashboard (CQRS-lite: reads query the projection directly).

Writes such as overrides go through the domain and the service, never through here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import Engine, case, func, select
from sqlalchemy.orm import Session

from app.adapters.storage.sql import ConversationRow
from app.domain.models import ConversationState, FieldName, FieldStatus, Stage, Status


class CandidateItem(BaseModel):
    id: str
    full_name: str | None
    city: str | None
    zone: str | None
    location_id: str | None
    status: str
    stage: str
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
    availability: str | None = None
    schedule: str | None = None
    language: str | None = None
    search: str | None = None
    start_before: date | None = None


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

    def candidates(self, filters: Filters, limit: int = 500) -> list[CandidateItem]:
        query = select(ConversationRow).where(ConversationRow.client_id == self._client_id)
        if filters.status:
            query = query.where(ConversationRow.status == filters.status)
        if filters.location_id:
            query = query.where(ConversationRow.location_id == filters.location_id)
        if filters.language:
            query = query.where(ConversationRow.language == filters.language)
        if filters.search:
            query = query.where(ConversationRow.full_name.ilike(f"%{filters.search.strip()}%"))
        if filters.start_before:
            query = query.where(ConversationRow.start_date <= filters.start_before)
        query = query.order_by(_STATUS_ORDER, ConversationRow.updated_at.desc()).limit(limit)
        with Session(self._engine) as session:
            items = [_item(row) for row in session.scalars(query)]
        # JSON list columns: filtered in Python (fine at demo scale; a GIN index in Postgres).
        if filters.availability:
            items = [i for i in items if filters.availability in i.availability]
        if filters.schedule:
            items = [i for i in items if filters.schedule in i.schedule]
        return items

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
        query = select(ConversationRow.state).where(ConversationRow.client_id == self._client_id)
        with Session(self._engine) as session:
            states = [ConversationState.model_validate(raw) for raw in session.scalars(query)]
        return compute_analytics(states, field_order)

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
    by_location: dict[str, tuple[int, int]]
    interviews_booked: int


def compute_analytics(states: list[ConversationState], field_order: list[FieldName]) -> Analytics:
    """Conversation metrics from the projected states (docs/recruiter-dashboard.md, Analytics).

    Computed in Python over the whole set: fine for the demo; production would use SQL views.
    """
    started = len(states)
    consented = [
        s
        for s in states
        if not (s.status == Status.IN_PROGRESS and s.stage == Stage.CONSENT)
        and s.dropped_at != "consent"
    ]
    finished = [s for s in states if s.status in COMPLETED]
    outcomes = {status.value: sum(s.status == status for s in states) for status in Status}

    funnel: list[tuple[str, int]] = [("started", started), ("consented", len(consented))]
    for index, name in enumerate(field_order):
        reached = sum(
            1
            for s in consented
            if all(
                s.candidate.meta[f].status != FieldStatus.MISSING for f in field_order[: index + 1]
            )
        )
        funnel.append((name.value, reached))
    # No "finished" row: knockouts and hand-offs finish early, so it isn't a funnel step.
    # Completion is reported separately.

    drop_off: dict[str, int] = {}
    for s in states:
        if s.status == Status.ABANDONED:
            where = s.dropped_at or "unknown"
            drop_off[where] = drop_off.get(where, 0) + 1

    durations = sorted(
        (s.ended_at - s.started_at).total_seconds() / 60
        for s in finished
        if s.started_at and s.ended_at
    )
    median = durations[len(durations) // 2] if durations else None

    def breakdown(key: Callable[[ConversationState], str | None]) -> dict[str, tuple[int, int]]:
        table: dict[str, tuple[int, int]] = {}
        for s in states:
            label = key(s) or "unknown"
            total, done = table.get(label, (0, 0))
            table[label] = (total + 1, done + (s.status in COMPLETED))
        return dict(sorted(table.items()))

    return Analytics(
        started=started,
        consented=len(consented),
        completed=len(finished),
        completion_rate=round(len(finished) / started, 3) if started else 0.0,
        outcomes=outcomes,
        funnel=funnel,
        drop_off=dict(sorted(drop_off.items(), key=lambda kv: -kv[1])),
        median_minutes=round(median, 1) if median is not None else None,
        by_language=breakdown(lambda s: s.language),
        by_location=breakdown(lambda s: s.candidate.location_id),
        interviews_booked=sum(s.interview_at is not None for s in states),
    )


# Reached a final outcome through the conversation (not dropped off).
COMPLETED = frozenset({Status.QUALIFIED, Status.DISQUALIFIED, Status.NEEDS_REVIEW})
