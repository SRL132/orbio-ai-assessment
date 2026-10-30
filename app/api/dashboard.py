"""Recruiter dashboard: candidate list, review queue, detail, overrides, notes, JSON export.

Spec: docs/recruiter-dashboard.md. Reads come from the dashboard read model; every write goes
through the service and the domain, and lands in the event log with the recruiter's email.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import SecretStr

from app.adapters.storage.dashboard import Filters
from app.api.auth import parse_users, verify_password
from app.bootstrap import Container
from app.domain.engine import OVERRIDABLE
from app.domain.models import Candidate, DomainEvent, FieldName, Status
from app.domain.summary import describe_reason, local_time

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
TEMPLATES.env.filters["reason"] = describe_reason

router = APIRouter()

FIELD_LABELS = {
    FieldName.FULL_NAME: "Name",
    FieldName.LICENSE: "Driver's license",
    FieldName.LOCATION: "City / zone",
    FieldName.AVAILABILITY: "Availability",
    FieldName.SCHEDULE: "Preferred hours",
    FieldName.EXPERIENCE: "Experience",
    FieldName.START_DATE: "Start date",
}


def container(request: Request) -> Container:
    c: Container = request.app.state.container
    return c


def recruiter(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    return str(user)


Recruiter = Annotated[str, Depends(recruiter)]
App = Annotated[Container, Depends(container)]


# --- Login --------------------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, c: App) -> HTMLResponse:
    configured = bool(parse_users(_secret(c.settings.recruiter_users)))
    return TEMPLATES.TemplateResponse(
        request, "login.html", {"configured": configured, "error": None}
    )


@router.post("/login", response_model=None)
async def login(
    request: Request,
    c: App,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> HTMLResponse | RedirectResponse:
    users = parse_users(_secret(c.settings.recruiter_users))
    stored = users.get(email.strip().lower())
    ok = stored is not None and await asyncio.to_thread(verify_password, password, stored)
    if not ok:
        return TEMPLATES.TemplateResponse(
            request,
            "login.html",
            {"configured": bool(users), "error": "Wrong email or password."},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    request.session.clear()
    request.session["user"] = email.strip().lower()
    return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/logout")
def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


# --- Pages --------------------------------------------------------------------------------------


@router.get("/dashboard", response_class=HTMLResponse)
def candidate_list(
    request: Request,
    c: App,
    user: Recruiter,
) -> HTMLResponse:
    params = request.query_params
    filters = Filters(
        status=params.get("status") or None,
        location_id=params.get("location") or None,
        availability=params.get("availability") or None,
        schedule=params.get("schedule") or None,
        language=params.get("language") or None,
        channel=params.get("channel") or None,
        search=params.get("q") or None,
        start_before=_date(params.get("start_before")),
    )
    today = datetime.combine(c.clock.now().date(), time.min, tzinfo=c.clock.now().tzinfo)
    return TEMPLATES.TemplateResponse(
        request,
        "dashboard_list.html",
        {
            "user": user,
            "items": c.queries.candidates(filters),
            "counters": c.queries.counters(since=today),
            "filters": params,
            "locations": c.config.locations,
            "statuses": [s.value for s in Status],
        },
    )


@router.get("/dashboard/review", response_class=HTMLResponse)
def review_queue(request: Request, c: App, user: Recruiter) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request,
        "dashboard_review.html",
        {"user": user, "items": c.queries.review_queue(), "now": c.clock.now()},
    )


@router.get("/dashboard/analytics", response_class=HTMLResponse)
def analytics(request: Request, c: App, user: Recruiter) -> HTMLResponse:
    stats = c.queries.analytics(c.config.field_priority)
    return TEMPLATES.TemplateResponse(
        request,
        "dashboard_analytics.html",
        {"user": user, "a": stats, "locations": {loc.id: loc.city for loc in c.config.locations}},
    )


@router.get("/dashboard/candidates/{conversation_id}", response_class=HTMLResponse)
async def candidate_detail(
    request: Request, conversation_id: UUID, c: App, user: Recruiter
) -> HTMLResponse:
    state = await asyncio.to_thread(c.service.record_view, conversation_id, user)
    if state is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    location = c.config.location(state.candidate.location_id or "")
    return TEMPLATES.TemplateResponse(
        request,
        "dashboard_detail.html",
        {
            "user": user,
            "state": state,
            "location": location,
            "fields": field_rows(state.candidate),
            "messages": c.service.repo.messages(conversation_id),
            "timeline": list(reversed(c.service.repo.timeline(conversation_id))),
            "overridable": sorted(s.value for s in OVERRIDABLE),
            "interview": local_time(state.interview_at, state, c.config)
            if state.interview_at
            else None,
        },
    )


@router.post("/dashboard/candidates/{conversation_id}/override")
async def override(
    conversation_id: UUID,
    c: App,
    user: Recruiter,
    new_status: Annotated[str, Form(alias="status")],
    reason: Annotated[str, Form()],
) -> RedirectResponse:
    try:
        await asyncio.to_thread(
            c.service.override, conversation_id, Status(new_status), reason, user
        )
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND) from None
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    return RedirectResponse(
        f"/dashboard/candidates/{conversation_id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/dashboard/candidates/{conversation_id}/notes")
async def add_note(
    conversation_id: UUID, c: App, user: Recruiter, text: Annotated[str, Form()]
) -> RedirectResponse:
    try:
        await asyncio.to_thread(c.service.add_note, conversation_id, text, user)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND) from None
    return RedirectResponse(
        f"/dashboard/candidates/{conversation_id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/dashboard/candidates/{conversation_id}/export.json")
def export(conversation_id: UUID, c: App, user: Recruiter) -> JSONResponse:
    state = c.service.repo.get(conversation_id)
    if state is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    c.service.repo.append_events(state, [_exported(user)])
    payload: dict[str, Any] = {
        "candidate": state.candidate.model_dump(mode="json"),
        "status": state.status.value,
        "summary": state.summary,
        "review_reasons": state.review_reasons,
        "disqualify_reason": state.disqualify_reason,
        "language": state.language,
        "messages": [
            m.model_dump(mode="json", include={"seq", "direction", "text", "created_at"})
            for m in c.service.repo.messages(conversation_id)
        ],
        "events": [
            {"at": at.isoformat(), **event.model_dump(mode="json")}
            for at, event in c.service.repo.timeline(conversation_id)
        ],
    }
    return JSONResponse(
        payload,
        headers={"Content-Disposition": f'attachment; filename="candidate-{conversation_id}.json"'},
    )


# --- Helpers ------------------------------------------------------------------------------------


def field_rows(candidate: Candidate) -> list[dict[str, Any]]:
    values = {
        FieldName.FULL_NAME: candidate.full_name,
        FieldName.LICENSE: _license_text(candidate),
        FieldName.LOCATION: ", ".join(
            p for p in (candidate.zone, candidate.city, candidate.postal_code) if p
        )
        or None,
        FieldName.AVAILABILITY: ", ".join(a.value for a in candidate.availability) or None,
        FieldName.SCHEDULE: ", ".join(s.value for s in candidate.schedule) or None,
        FieldName.EXPERIENCE: (
            f"{candidate.experience_years:g} years"
            + (f" ({', '.join(candidate.platforms)})" if candidate.platforms else "")
            if candidate.experience_years is not None
            else None
        ),
        FieldName.START_DATE: candidate.start_date.strftime("%d/%m/%Y")
        if candidate.start_date
        else None,
    }
    return [
        {
            "label": FIELD_LABELS[name],
            "value": values[name],
            "meta": candidate.meta[name],
        }
        for name in FieldName
    ]


def _exported(user: str) -> DomainEvent:
    return DomainEvent(type="exported", data={"actor": user})


def _secret(value: SecretStr | None) -> str | None:
    return value.get_secret_value() if value is not None else None


def _license_text(candidate: Candidate) -> str | None:
    if candidate.vehicle is None:
        return "no" if candidate.has_license is False else None
    details = [d for d in (candidate.license_type, _years(candidate.license_years)) if d]
    return candidate.vehicle.value + (f" ({', '.join(details)})" if details else "")


def _years(value: float | None) -> str | None:
    return f"{value:g} years" if value is not None else None


def _date(raw: str | None) -> date | None:
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None
