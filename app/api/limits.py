"""Rate limits for the public endpoints (login, web chat), shared by every worker."""

from __future__ import annotations

from datetime import timedelta

from fastapi import HTTPException, Request, status

from app import metrics

QUARTER_HOUR = timedelta(minutes=15)
HOUR = timedelta(hours=1)
MINUTE = timedelta(minutes=1)


def client_ip(request: Request) -> str:
    """The caller's address. Behind a proxy, run uvicorn with --proxy-headers so this is the
    real client and not the proxy."""
    return request.client.host if request.client else "unknown"


def too_many(endpoint: str, retry_after: timedelta) -> HTTPException:
    metrics.RATE_LIMITED.labels(endpoint).inc()
    return HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        "Too many requests, try again later.",
        headers={"Retry-After": str(int(retry_after.total_seconds()))},
    )
