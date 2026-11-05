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

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import SecretStr

from app import metrics
from app.adapters.channels.whatsapp_invites import WhatsAppInvites
from app.adapters.storage.dashboard import Filters, UnfinishedInvite
from app.api.auth import parse_users, verify_password
from app.api.invites import (
    INVITE_MAX_AGE,
    MAX_INVITE_ROWS,
    MAX_UPLOAD_BYTES,
    VOICE_URL,
    WHATSAPP_URL,
    InviteError,
    InviteLinks,
    links_csv,
    parse_applications,
    public_base,
    reference_of,
    unfinished_csv,
)
from app.api.limits import QUARTER_HOUR, client_ip
from app.bootstrap import Container
from app.domain.config import ClientConfig
from app.domain.engine import OVERRIDABLE
from app.domain.models import (
    Availability,
    Candidate,
    Channel,
    FieldName,
    Language,
    Schedule,
    Status,
)
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

ERASE_CONFIRMATION = "ERASE"

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
    s, limiter = c.settings, c.limiter
    # Count the attempt before checking the password, so parallel guesses can't all slip in;
    # a successful login gives its account slot back (only failures lock an account).
    by_ip = f"login-ip:{client_ip(request)}"
    email_hit = None
    if limiter.allow(by_ip, s.login_attempts_per_ip_per_15_min, QUARTER_HOUR):
        email_hit = limiter.allow(
            f"login-email:{email}", s.login_failures_per_email_per_15_min, QUARTER_HOUR
        )
    if email_hit is None:
        metrics.RATE_LIMITED.labels("login").inc()
        return _login_page(request, bool(users), TOO_MANY_LOGINS, status.HTTP_429_TOO_MANY_REQUESTS)
    stored = users.get(email)
    ok = stored is not None and await asyncio.to_thread(verify_password, password, stored)
    if not ok:
        return _login_page(
            request, bool(users), "Wrong email or password.", status.HTTP_401_UNAUTHORIZED
        )
    limiter.forget(email_hit)
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
        asked_for_human=params.get("human") == "1",
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
    stats = c.queries.analytics(
        c.config.field_priority, c.clock.now(), c.config.screening_target_hours
    )
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
            "whatsapp_number": c.queries.channel_contact(conversation_id, "whatsapp"),
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


@router.get("/dashboard/invite", response_class=HTMLResponse)
def invite_page(request: Request, c: App, user: Recruiter) -> HTMLResponse:
    return _invite_page(request, c, user)


def _invite_page(
    request: Request, c: Container, user: str, error: str | None = None, code: int = 200
) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request,
        "dashboard_invite.html",
        {
            "user": user,
            "active": "invite",
            "error": error,
            "max_rows": MAX_INVITE_ROWS,
            "max_age_days": INVITE_MAX_AGE // (24 * 3600),
            "base_url": _public_base(request, c),
            "whatsapp_invites": _whatsapp_invites(c) is not None,
        },
        status_code=code,
    )


def _public_base(request: Request, c: Container) -> str:
    return public_base(c.settings.public_base_url, c.settings.demo_mode, str(request.base_url))


def _whatsapp_invites(c: Container) -> WhatsAppInvites | None:
    return c.whatsapp.invites if c.whatsapp is not None else None


@router.post("/dashboard/invite", response_model=None)
async def create_invites(
    request: Request,
    c: App,
    user: Recruiter,
    _: Csrf,
    count: Annotated[int | None, Form()] = None,
    applications: Annotated[UploadFile | None, File()] = None,
    voice: Annotated[bool, Form()] = False,
) -> Response:
    """Signed links as a CSV, in the order given. Other columns in the upload (a phone, say) come
    back as they were, for the recruiter's mail merge: they're never stored or logged."""
    base = _public_base(request, c)
    if not base:
        return _invite_page(request, c, user, "Set PUBLIC_BASE_URL first.", 400)
    now = c.clock.now()
    raw = await applications.read(MAX_UPLOAD_BYTES + 1) if applications else b""
    try:
        if raw:
            rows = parse_applications(raw, now)
        elif count is not None and 1 <= count <= MAX_INVITE_ROWS:
            rows = [{"applied_at": now.isoformat()} for _ in range(count)]
        else:
            raise InviteError(f"Upload a CSV, or enter a number from 1 to {MAX_INVITE_ROWS}.")
    except InviteError as exc:
        return _invite_page(request, c, user, str(exc), 400)

    links: InviteLinks = request.app.state.invites
    whatsapp = None if voice else _whatsapp_invites(c)
    channel: Channel = "voice" if voice else "whatsapp" if whatsapp else "chat"

    def create() -> list[str]:
        ids = [
            c.service.start_conversation(
                channel,
                datetime.fromisoformat(row["applied_at"]),
                invited_by=user,
                application_ref=reference_of(row),
            )
            for row in rows
        ]
        return [whatsapp.url(i) if whatsapp else base + links.path(i) for i in ids]

    urls = await asyncio.to_thread(create)
    column = VOICE_URL if voice else WHATSAPP_URL if whatsapp else None
    return Response(
        links_csv(rows, urls, column) if column else links_csv(rows, urls),
        media_type="text/csv",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{"voice" if voice else "chat"}-links.csv"'
            ),
            "Cache-Control": "no-store",
        },
    )


@router.get("/dashboard/invite/unfinished.csv", response_model=None)
async def unfinished_invites(request: Request, c: App, user: Recruiter) -> Response:
    """Who hasn't finished, for the recruiter's SMS or email tool: web chat has no way to reach
    someone who closed the tab. Fresh links, because the first ones expire."""
    base = _public_base(request, c)
    if not base:
        return _invite_page(request, c, user, "Set PUBLIC_BASE_URL first.", 400)
    links: InviteLinks = request.app.state.invites
    whatsapp = _whatsapp_invites(c)

    def link(invite: UnfinishedInvite) -> str:
        if whatsapp is None or invite.channel != "whatsapp":
            return base + links.path(invite.conversation_id)
        return whatsapp.chat_url() if invite.opened else whatsapp.url(invite.conversation_id)

    invites = await asyncio.to_thread(c.queries.unfinished_invites)
    body = unfinished_csv(
        [(i, link(i)) for i in invites if whatsapp or i.channel != "whatsapp"],
        whatsapp=whatsapp is not None,
    )
    return Response(
        body,
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="unfinished-applications.csv"',
            "Cache-Control": "no-store",
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


@router.post("/dashboard/candidates/{conversation_id}/erase")
async def erase(
    conversation_id: UUID, c: App, user: Recruiter, _: Csrf, confirm: Annotated[str, Form()]
) -> RedirectResponse:
    """Permanent, so it must be asked for in words, not just a click."""
    if confirm.strip() != ERASE_CONFIRMATION:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Type {ERASE_CONFIRMATION} to confirm.")
    try:
        await asyncio.to_thread(c.service.erase, conversation_id, user)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND) from None
    except ConversationBusy:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "The agent is answering this candidate; try again."
        ) from None
    return RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)


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
