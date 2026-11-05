"""Rate limits for the public endpoints (login, web chat), shared by every worker."""

from __future__ import annotations

from datetime import timedelta
from ipaddress import ip_address

from fastapi import HTTPException, Request, status

from app import metrics

QUARTER_HOUR = timedelta(minutes=15)
HOUR = timedelta(hours=1)
MINUTE = timedelta(minutes=1)


def client_ip(request: Request) -> str:
    """The caller's address, for rate limits. Behind a proxy, CLIENT_IP_HEADER names the one
    header that proxy always overwrites. Don't trust X-Forwarded-For for this: some proxies only
    add to what the caller sent, so a caller could pick a new address for every request."""
    header = request.app.state.container.settings.client_ip_header
    if header:
        value = request.headers.get(header, "").strip()
        try:
            return str(ip_address(value))  # anything else could be made up, and any length
        except ValueError:
            pass
    return request.client.host if request.client else "unknown"


def too_many(endpoint: str, retry_after: timedelta) -> HTTPException:
    metrics.RATE_LIMITED.labels(endpoint).inc()
    return HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        "Too many requests, try again later.",
        headers={"Retry-After": str(int(retry_after.total_seconds()))},
    )
