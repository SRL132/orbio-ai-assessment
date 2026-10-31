"""FastAPI application: candidate web chat (the demo channel) and its JSON API."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer
from opentelemetry import trace
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, CollectorRegistry, generate_latest
from pydantic import AwareDatetime, BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from app import metrics, tracing
from app.adapters.clock.clocks import OffsetClock
from app.adapters.voice.elevenlabs import MAX_CHARS as MAX_TTS_CHARS
from app.api.dashboard import router as dashboard_router
from app.api.limits import HOUR, MINUTE, client_ip, too_many
from app.api.whatsapp import router as whatsapp_router
from app.bootstrap import Container, build
from app.domain.models import Channel
from app.worker.service import MAX_MESSAGE_CHARS, ScreeningService

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
logger = logging.getLogger("app.api")

# The browser that starts a conversation gets a signed cookie naming it; the message endpoints
# serve only that conversation. Conversation ids appear in logs and URLs, so the id alone must
# not be enough to read a transcript. Longer than a screening can last (reminders, abandonment).
CHAT_COOKIE = "sazon_chat"
CHAT_COOKIE_MAX_AGE = 7 * 24 * 3600
# How long the chat link the ATS sends to a new applicant stays valid.
INVITE_MAX_AGE = 7 * 24 * 3600


class MessageIn(BaseModel):
    text: Annotated[str, Field(min_length=1, max_length=MAX_MESSAGE_CHARS)]


class StartIn(BaseModel):
    channel: Channel = "chat"


class ApplicationIn(BaseModel):
    """An application registered by the ATS. Only the time is needed: the screening collects
    everything else, so no personal data has to cross over."""

    applied_at: AwareDatetime


class SpeakIn(BaseModel):
    text: Annotated[str, Field(min_length=1, max_length=MAX_TTS_CHARS)]


class AdvanceIn(BaseModel):
    hours: Annotated[float, Field(gt=0, le=72)]


def create_app(container: Container | None = None) -> FastAPI:
    """App factory: `uv run uvicorn --factory app.api.main:create_app`."""
    c = container or build()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        background = c.settings.role == "all"  # "web": a separate worker does the rest
        if background:
            c.runner.start()
        yield
        if background:
            await c.runner.stop()
        c.close()

    app = FastAPI(title="Sazón Screening Agent", lifespan=lifespan)
    app.state.container = c
    secret = c.settings.session_secret
    if secret is None:
        logger.warning("SESSION_SECRET not set: using a random one, logins reset on restart")
    secret_key = secret.get_secret_value() if secret else secrets.token_urlsafe(32)
    chat_signer = URLSafeTimedSerializer(secret_key, salt="chat-conversation")
    invite_signer = URLSafeTimedSerializer(secret_key, salt="chat-invite")
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret_key,
        session_cookie="sazon_session",
        max_age=8 * 3600,
        same_site="lax",
        https_only=not c.settings.demo_mode,
    )

    @app.middleware("http")
    async def trace_request(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """A request id for logs (`req=`), the response (`X-Request-ID`) and the trace: FastAPI
        makes the request's spans itself; this tags them with the id."""
        incoming = request.headers.get("x-request-id", "")
        rid = incoming if _REQUEST_ID.fullmatch(incoming) else secrets.token_hex(8)
        token = tracing.request_id.set(rid)
        trace.get_current_span().set_attribute("request.id", rid)
        try:
            response = await call_next(request)
        finally:
            tracing.request_id.reset(token)
        response.headers["X-Request-ID"] = rid
        return response

    app.include_router(dashboard_router)
    app.include_router(whatsapp_router)

    def get(request: Request) -> Container:
        c: Container = request.app.state.container
        return c

    def give_ownership(response: Response, conversation_id: str) -> None:
        """This browser may now read and write the conversation (see `require_owner`)."""
        response.set_cookie(
            CHAT_COOKIE,
            chat_signer.dumps(conversation_id),
            max_age=CHAT_COOKIE_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=not c.settings.demo_mode,
        )

    def require_owner(request: Request, conversation_id: UUID) -> None:
        """404, not 403, for someone else's conversation: don't confirm that it exists."""
        try:
            owned = chat_signer.loads(
                request.cookies.get(CHAT_COOKIE, ""), max_age=CHAT_COOKIE_MAX_AGE
            )
        except BadSignature:
            owned = None
        if owned != str(conversation_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    backlog = CollectorRegistry()
    backlog.register(metrics.BacklogCollector(c.queries.backlog, c.clock.now))

    @app.get("/metrics")
    def prometheus(request: Request) -> Response:
        token = c.settings.metrics_token
        sent = request.headers.get("authorization", "").removeprefix("Bearer ")
        if token is not None and not secrets.compare_digest(sent, token.get_secret_value()):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED)
        body = generate_latest(REGISTRY) + generate_latest(backlog)
        return Response(body, media_type=CONTENT_TYPE_LATEST)

    @app.post("/api/v1/applications", status_code=status.HTTP_201_CREATED)
    async def register_application(request: Request, body: ApplicationIn) -> dict[str, str]:
        """The ATS registers a new application and gets a chat link to send the candidate, so
        time to screen counts from the application (docs/ats-integration.md)."""
        c = get(request)
        token = c.settings.ats_token
        if token is None:  # not configured: the endpoint doesn't exist
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        sent = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not secrets.compare_digest(sent, token.get_secret_value()):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED)
        if body.applied_at > c.clock.now():
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "applied_at is in the future"
            )
        conversation_id = await asyncio.to_thread(
            c.service.start_conversation, "chat", body.applied_at
        )
        invite = invite_signer.dumps(str(conversation_id))
        return {"id": str(conversation_id), "chat_url": f"/chat/invite/{invite}"}

    @app.get("/chat/invite/{invite}")
    def accept_invite(invite: str) -> RedirectResponse:
        """The candidate opens the link from the ATS: this browser now owns the conversation."""
        try:
            conversation_id = invite_signer.loads(invite, max_age=INVITE_MAX_AGE)
        except BadSignature as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "invite not found") from exc
        response = RedirectResponse(f"/chat?conversation={conversation_id}", status_code=303)
        give_ownership(response, conversation_id)
        return response

    @app.get("/")
    def root() -> RedirectResponse:
        return RedirectResponse("/chat")

    @app.get("/chat", response_class=HTMLResponse)
    def chat_page(request: Request) -> HTMLResponse:
        c = get(request)
        return TEMPLATES.TemplateResponse(
            request,
            "chat.html",
            {"company": c.config.persona.company, "demo_mode": c.settings.demo_mode},
        )

    @app.post("/api/conversations", status_code=status.HTTP_201_CREATED)
    async def start_conversation(
        request: Request, response: Response, body: StartIn | None = None
    ) -> dict[str, str]:
        channel = body.channel if body else "chat"
        c = get(request)
        if not c.limiter.allow(
            f"chat-start:{client_ip(request)}", c.settings.chat_starts_per_ip_per_hour, HOUR
        ):
            raise too_many("chat_start", HOUR)
        conversation_id = await asyncio.to_thread(c.service.start_conversation, channel)
        give_ownership(response, str(conversation_id))
        return {"id": str(conversation_id)}

    @app.get("/api/conversations/{conversation_id}/messages")
    def list_messages(
        request: Request, conversation_id: UUID, after: Annotated[int, Query(ge=0)] = 0
    ) -> dict[str, object]:
        require_owner(request, conversation_id)
        payload = _snapshot(get(request).service, conversation_id, after)
        if payload is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
        return payload

    @app.get("/api/conversations/{conversation_id}/stream")
    async def stream(
        request: Request, conversation_id: UUID, after: Annotated[int, Query(ge=0)] = 0
    ) -> StreamingResponse:
        """Server-sent events: new messages are pushed as they're stored, in the same shape as
        GET .../messages. The browser reconnects by itself, resuming from Last-Event-ID.
        Without a pub/sub the server still checks the database (every 0.5 s per open stream),
        but the browser no longer polls and replies arrive as soon as they're saved."""
        require_owner(request, conversation_id)
        service = get(request).service
        resume = request.headers.get("last-event-id", "")
        start = int(resume) if resume.isdigit() else after
        seconds = get(request).settings.chat_stream_seconds

        async def events() -> AsyncIterator[str]:
            last, shown, deadline = start, None, time.monotonic() + seconds
            yield "retry: 1000\n\n"
            while time.monotonic() < deadline and not await request.is_disconnected():
                payload = await asyncio.to_thread(_snapshot, service, conversation_id, last)
                if payload is None:
                    return
                current = (payload["status"], payload["stage"])
                if payload["messages"] or current != shown:
                    last = payload["messages"][-1]["seq"] if payload["messages"] else last
                    shown = current
                    yield f"id: {last}\ndata: {json.dumps(payload)}\n\n"
                await asyncio.sleep(STREAM_POLL_SECONDS)

        headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        return StreamingResponse(events(), media_type="text/event-stream", headers=headers)

    @app.post("/api/conversations/{conversation_id}/messages", status_code=status.HTTP_202_ACCEPTED)
    async def post_message(
        request: Request, conversation_id: UUID, body: MessageIn
    ) -> dict[str, bool]:
        require_owner(request, conversation_id)
        c = get(request)
        limit = c.settings.chat_messages_per_conversation_per_minute
        if not c.limiter.allow(f"chat-message:{conversation_id}", limit, MINUTE):
            raise too_many("chat_message", MINUTE)
        try:
            await asyncio.to_thread(c.service.receive, conversation_id, body.text)
        except KeyError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found") from None
        state = c.service.get(conversation_id)
        # Push-to-talk sends one complete utterance: voice skips the debounce to cut latency.
        if c.settings.debounce_seconds <= 0 or (state is not None and state.channel == "voice"):
            await asyncio.to_thread(c.service.process_pending, conversation_id)
        elif c.settings.role == "all":
            c.runner.notify(conversation_id)
        # role "web": a worker process picks it up once the debounce has passed
        return {"accepted": True}

    @app.get("/voice", response_class=HTMLResponse)
    def voice_page(request: Request) -> HTMLResponse:
        c = get(request)
        return TEMPLATES.TemplateResponse(
            request,
            "voice.html",
            {
                "company": c.config.persona.company,
                "demo_mode": c.settings.demo_mode,
                "server_tts": c.tts is not None,
            },
        )

    @app.post("/api/tts")
    async def speak(request: Request, body: SpeakIn) -> Response:
        """Agent reply → MP3, when ElevenLabs is configured (else 404: the browser speaks)."""
        tts = get(request).tts
        if tts is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "server text-to-speech not configured")
        try:
            audio = await asyncio.to_thread(tts.synthesize, body.text)
        except httpx.HTTPError:
            logger.exception("tts_failed")
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "text-to-speech failed") from None
        return Response(content=audio, media_type="audio/mpeg")

    @app.post("/api/demo/advance")
    async def advance_clock(request: Request, body: AdvanceIn) -> dict[str, object]:
        c = get(request)
        if not c.settings.demo_mode or not isinstance(c.clock, OffsetClock):
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        c.clock.advance(hours=body.hours)
        changed = await c.runner.tick()
        return {"now": c.clock.now().isoformat(), "conversations_changed": changed}

    return app


STREAM_POLL_SECONDS = 0.5
# Accept a caller's request id (e.g. from a proxy) only if it's short and plain.
_REQUEST_ID = re.compile(r"[A-Za-z0-9-]{8,64}")


def _snapshot(
    service: ScreeningService, conversation_id: UUID, after: int
) -> dict[str, Any] | None:
    """The conversation's status and its messages after `after` (None if it doesn't exist)."""
    state = service.get(conversation_id)
    if state is None:
        return None
    return {
        "status": state.status.value,
        "stage": state.stage.value,
        "channel": state.channel,
        "language": state.language,
        "messages": [
            {
                "seq": m.seq,
                "direction": m.direction,
                "text": m.text,
                "created_at": m.created_at.isoformat(),
            }
            for m in service.messages(conversation_id, after)
        ],
    }
