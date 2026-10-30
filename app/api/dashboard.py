"""Recruiter dashboard: candidate list, review queue, detail, overrides, notes, JSON export.

Spec: docs/recruiter-dashboard.md. Reads come from the dashboard read model; every write goes
through the service and the domain, and lands in the event log with the recruiter's email.
"""

from __future__ import annotations

import asyncio
import secrets
from datetime import date, datetime, time
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import SecretStr

from app import metrics
from app.adapters.storage.dashboard import Filters
from app.api.auth import parse_users, verify_password
from app.api.limits import QUARTER_HOUR, client_ip
from app.bootstrap import Container
from app.domain.config import ClientConfig
from app.domain.engine import OVERRIDABLE
from app.domain.models import Availability, Candidate, FieldName, Language, Schedule, Status
from app.domain.render import field_values
from app.domain.summary import describe_reason, local_time
from app.worker.service import ConversationBusy

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
TEMPLATES.env.filters["reason"] = describe_reason


def csrf_token(request: Request) -> str:
    """The session's form token, embedded in every dashboard form (`{{ csrf_token(request) }}`).
    SameSite cookies already stop most cross-site posts; this covers the rest (old browsers,
    same-site subdomains)."""
    token = request.session.get("csrf")
    if not isinstance(token, str):
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


TEMPLATES.env.globals["csrf_token"] = csrf_token


def check_csrf(request: Request, csrf: Annotated[str, Form(alias="csrf_token")] = "") -> None:
    expected = request.session.get("csrf")
    if not isinstance(expected, str) or not secrets.compare_digest(csrf, expected):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Form expired: reload the page and retry.")


Csrf = Annotated[None, Depends(check_csrf)]

router = APIRouter()

# The dashboard is in English; field labels and values come from the client's English templates,
# the same ones the candidate's recap uses.
DASHBOARD_LANGUAGE: Language = "en"


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
    _: Csrf,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> HTMLResponse | RedirectResponse:
    users = parse_users(_secret(c.settings.recruiter_users))
    email = email.strip().lower()
    by_ip, by_email = f"login-ip:{client_ip(request)}", f"login-email:{email}"
    s, limiter = c.settings, c.limiter
    if limiter.exceeded(by_ip, s.login_attempts_per_ip_per_15_min, QUARTER_HOUR) or (
        limiter.exceeded(by_email, s.login_failures_per_email_per_15_min, QUARTER_HOUR)
    ):
        metrics.RATE_LIMITED.labels("login").inc()
        return _login_page(request, bool(users), TOO_MANY_LOGINS, status.HTTP_429_TOO_MANY_REQUESTS)
    limiter.hit(by_ip, QUARTER_HOUR)
    stored = users.get(email)
    ok = stored is not None and await asyncio.to_thread(verify_password, password, stored)
    if not ok:
        limiter.hit(by_email, QUARTER_HOUR)  # only failures count against an account
        return _login_page(
            request, bool(users), "Wrong email or password.", status.HTTP_401_UNAUTHORIZED
        )
    request.session.clear()
    request.session["user"] = email
    return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/logout")
def logout(_: Csrf, request: Request) -> RedirectResponse:
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
        availability=_choice(Availability, params.get("availability")),
        schedule=_choice(Schedule, params.get("schedule")),
        language=params.get("language") or None,
        channel=params.get("channel") or None,
        search=params.get("q") or None,
        start_before=_date(params.get("start_before")),
    )
    today = datetime.combine(c.clock.now().date(), time.min, tzinfo=c.clock.now().tzinfo)
    page = c.queries.candidates(filters, page=_page_number(params.get("page")))
    return TEMPLATES.TemplateResponse(
        request,
        "dashboard_list.html",
        {
            "user": user,
            "page": page,
            "items": page.items,
            "page_url": lambda n: "?" + urlencode({**params, "page": n}),
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
            "fields": field_rows(state.candidate, c.config),
            "messages": c.service.messages(conversation_id),
            "timeline": list(reversed(c.service.timeline(conversation_id))),
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
    _: Csrf,
    new_status: Annotated[str, Form(alias="status")],
    reason: Annotated[str, Form()],
) -> RedirectResponse:
    try:
        await asyncio.to_thread(
            c.service.override, conversation_id, Status(new_status), reason, user
        )
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND) from None
    except ConversationBusy:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "The agent is answering this candidate; try again."
        ) from None
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    return RedirectResponse(
        f"/dashboard/candidates/{conversation_id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/dashboard/candidates/{conversation_id}/notes")
async def add_note(
    conversation_id: UUID, c: App, user: Recruiter, _: Csrf, text: Annotated[str, Form()]
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
    state = c.service.export(conversation_id, user)
    if state is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    payload: dict[str, Any] = {
        "candidate": state.candidate.model_dump(mode="json"),
        "status": state.status.value,
        "summary": state.summary,
        "review_reasons": [str(r) for r in state.review_reasons],
        "disqualify_reason": state.disqualify_reason,
        "language": state.language,
        "messages": [
            m.model_dump(mode="json", include={"seq", "direction", "text", "created_at"})
            for m in c.service.messages(conversation_id)
        ],
        "events": [
            {"at": at.isoformat(), **event.model_dump(mode="json")}
            for at, event in c.service.timeline(conversation_id)
        ],
    }
    return JSONResponse(
        payload,
        headers={"Content-Disposition": f'attachment; filename="candidate-{conversation_id}.json"'},
    )


# --- Helpers ------------------------------------------------------------------------------------


def field_rows(candidate: Candidate, config: ClientConfig) -> list[dict[str, Any]]:
    values = field_values(candidate, config, DASHBOARD_LANGUAGE)
    values[FieldName.LICENSE] = _license_text(candidate, values[FieldName.LICENSE])
    labels = config.templates[DASHBOARD_LANGUAGE]
    return [
        {
            "label": labels.get(f"label_{name.value}", name.value),
            "value": values[name],
            "meta": candidate.meta[name],
        }
        for name in FieldName
    ]


TOO_MANY_LOGINS = "Too many login attempts. Wait 15 minutes and try again."


def _login_page(request: Request, configured: bool, error: str, code: int) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request, "login.html", {"configured": configured, "error": error}, status_code=code
    )


def _secret(value: SecretStr | None) -> str | None:
    return value.get_secret_value() if value is not None else None


def _license_text(candidate: Candidate, vehicle: str | None) -> str | None:
    """The recap's vehicle plus what recruiters also need: license type and years held."""
    if vehicle is None:
        return "no" if candidate.has_license is False else None
    details = [d for d in (candidate.license_type, _years(candidate.license_years)) if d]
    return vehicle + (f" ({', '.join(details)})" if details else "")


def _years(value: float | None) -> str | None:
    return f"{value:g} years" if value is not None else None


def _choice[E: StrEnum](enum: type[E], raw: str | None) -> E | None:
    """A filter value from the query string; unknown values mean no filter."""
    try:
        return enum(raw) if raw else None
    except ValueError:
        return None


def _page_number(raw: str | None) -> int:
    return int(raw) if raw and raw.isdigit() and int(raw) > 0 else 1


def _date(raw: str | None) -> date | None:
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None
