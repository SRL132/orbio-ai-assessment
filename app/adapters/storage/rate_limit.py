"""Rate limiting counted in the database, so every worker process sees the same counts.

A sliding window: a request is allowed while fewer than `limit` hits for its key happened in
the last `window`. Two requests racing at the limit can both pass (one over): fine for abuse
protection, and it needs no locks.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import Engine, delete, func, select
from sqlalchemy.orm import Session

from app.adapters.storage.sql import RateLimitHitRow
from app.domain.ports import Clock


class SqlRateLimiter:
    def __init__(self, engine: Engine, clock: Clock, client_id: str) -> None:
        self._engine = engine
        self._clock = clock
        self._client_id = client_id

    def exceeded(self, key: str, limit: int, window: timedelta) -> bool:
        """True once `key` has `limit` hits in the window. Doesn't count as a hit itself."""
        hit = RateLimitHitRow
        since = self._clock.now() - window
        with Session(self._engine) as session:
            count = session.scalar(
                select(func.count()).where(
                    hit.client_id == self._client_id, hit.key == key, hit.at >= since
                )
            )
        return (count or 0) >= limit

    def hit(self, key: str, window: timedelta) -> None:
        """Count one request for `key`, and forget its hits older than the window."""
        hit = RateLimitHitRow
        now = self._clock.now()
        with Session(self._engine) as session, session.begin():
            session.execute(
                delete(hit).where(
                    hit.client_id == self._client_id, hit.key == key, hit.at < now - window
                )
            )
            session.add(hit(id=str(uuid4()), client_id=self._client_id, key=key, at=now))

    def allow(self, key: str, limit: int, window: timedelta) -> bool:
        """Count this request if it's within the limit. Returns whether it is."""
        if self.exceeded(key, limit, window):
            return False
        self.hit(key, window)
        return True
