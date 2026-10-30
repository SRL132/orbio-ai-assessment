"""FastAPI application: candidate web chat (the demo channel) and its JSON API."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app.adapters.clock.clocks import OffsetClock
from app.bootstrap import Container, build
from app.worker.service import MAX_MESSAGE_CHARS

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")


class MessageIn(BaseModel):
    text: Annotated[str, Field(min_length=1, max_length=MAX_MESSAGE_CHARS)]


class AdvanceIn(BaseModel):
    hours: Annotated[float, Field(gt=0, le=72)]


def create_app(container: Container | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        c = container or build()
        app.state.container = c
        c.runner.start()
        yield
        await c.runner.stop()

    app = FastAPI(title="Sazón Screening Agent", lifespan=lifespan)

    def get(request: Request) -> Container:
        c: Container = request.app.state.container
        return c

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

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
    async def start_conversation(request: Request) -> dict[str, str]:
        conversation_id = await asyncio.to_thread(get(request).service.start_conversation)
        return {"id": str(conversation_id)}

    @app.get("/api/conversations/{conversation_id}/messages")
    def list_messages(
        request: Request, conversation_id: UUID, after: Annotated[int, Query(ge=0)] = 0
    ) -> dict[str, object]:
        service = get(request).service
        state = service.repo.get(conversation_id)
        if state is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
        messages = service.repo.messages(conversation_id, after)
        return {
            "status": state.status.value,
            "stage": state.stage.value,
            "language": state.language,
            "messages": [
                {
                    "seq": m.seq,
                    "direction": m.direction,
                    "text": m.text,
                    "created_at": m.created_at.isoformat(),
                }
                for m in messages
            ],
        }

    @app.post("/api/conversations/{conversation_id}/messages", status_code=status.HTTP_202_ACCEPTED)
    async def post_message(
        request: Request, conversation_id: UUID, body: MessageIn
    ) -> dict[str, bool]:
        c = get(request)
        try:
            await asyncio.to_thread(c.service.receive, conversation_id, body.text)
        except KeyError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found") from None
        if c.settings.debounce_seconds <= 0:
            await asyncio.to_thread(c.service.process_pending, conversation_id)
        else:
            c.runner.notify(conversation_id)
        return {"accepted": True}

    @app.post("/api/demo/advance")
    async def advance_clock(request: Request, body: AdvanceIn) -> dict[str, object]:
        c = get(request)
        if not c.settings.demo_mode or not isinstance(c.clock, OffsetClock):
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        c.clock.advance(hours=body.hours)
        changed = await c.runner.tick()
        return {"now": c.clock.now().isoformat(), "conversations_changed": changed}

    return app


app = create_app()
