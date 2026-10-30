"""Read model for the recruiter dashboard (CQRS-lite: reads query the projection directly).

Writes such as overrides go through the domain and the service, never through here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import Engine, case, func, select
from sqlalchemy.orm import Session

from app.adapters.storage.sql import ConversationRow


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
