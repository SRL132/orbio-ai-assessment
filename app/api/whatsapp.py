"""WhatsApp webhook: Meta's verification handshake and incoming messages."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse

from app.adapters.channels.whatsapp import delivery_reports, valid_signature
from app.bootstrap import Container
from app.worker.whatsapp import WhatsAppChannel

logger = logging.getLogger("app.whatsapp")
router = APIRouter()


def _container(request: Request) -> tuple[Container, WhatsAppChannel]:
    c: Container = request.app.state.container
    if c.whatsapp is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "WhatsApp channel not configured")
    return c, c.whatsapp


@router.get("/webhooks/whatsapp", response_class=PlainTextResponse)
def verify(
    request: Request,
    mode: Annotated[str, Query(alias="hub.mode")] = "",
    token: Annotated[str, Query(alias="hub.verify_token")] = "",
    challenge: Annotated[str, Query(alias="hub.challenge")] = "",
) -> str:
    """Meta calls this once when the webhook is configured: echo the challenge if the token fits."""
    c, _ = _container(request)
    expected = c.settings.whatsapp_verify_token
    if mode != "subscribe" or expected is None or token != expected.get_secret_value():
        raise HTTPException(status.HTTP_403_FORBIDDEN)
    return challenge


@router.post("/webhooks/whatsapp")
async def receive(request: Request) -> dict[str, str]:
    """Store incoming messages and acknowledge fast; the debounce and workers do the rest."""
    c, channel = _container(request)
    raw = await request.body()
    secret = c.settings.whatsapp_app_secret
    header = request.headers.get("X-Hub-Signature-256")
    if secret is None or not valid_signature(secret.get_secret_value(), raw, header):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad signature")
    try:
        payload = json.loads(raw)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid JSON") from None
    for report in delivery_reports(payload):
        if report.status == "failed":
            logger.warning(
                "whatsapp_delivery_failed code=%s title=%s", report.error_code, report.error_title
            )
        else:
            logger.info("whatsapp_delivery status=%s", report.status)
    await asyncio.to_thread(channel.handle_delivery_failures, payload)
    conversations = await asyncio.to_thread(channel.handle, payload)
    if c.settings.role == "all":  # "web": a worker process picks them up after the debounce
        for conversation_id in conversations:
            c.runner.notify(conversation_id)
    return {"status": "ok"}
