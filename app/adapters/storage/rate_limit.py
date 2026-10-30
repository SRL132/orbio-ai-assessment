"""Rate limiting counted in the database, so every worker process sees the same counts.

A sliding window: a request is allowed while fewer than `limit` hits for its key happened in
the last `window`. Counting is exact under concurrency: each check first locks the key's row
(an UPDATE, which Postgres holds as a row lock and SQLite as its write lock until commit), so
two requests for one key are counted one after the other, never both under the limit.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import Engine, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.storage.sql import RateLimitHitRow, RateLimitKeyRow
from app.domain.ports import Clock


class SqlRateLimiter:
    def __init__(self, engine: Engine, clock: Clock, client_id: str) -> None:
        self._engine = engine
        self._clock = clock
        self._client_id = client_id

    def allow(self, key: str, limit: int, window: timedelta) -> str | None:
        """Count this request if it's within the limit. Returns the hit's id (to `forget` it,
        e.g. after a successful login), or None when the limit is reached."""
        hit, lock = RateLimitHitRow, RateLimitKeyRow
        now = self._clock.now()
        mine = (lock.client_id == self._client_id) & (lock.key == key)
        with Session(self._engine) as session, session.begin():
            locked = session.scalar(
                update(lock).where(mine).values(touched_at=now).returning(lock.key)
            )
            if locked is None:
                try:  # first request for this key: create its lock row
                    with session.begin_nested():
                        session.add(lock(client_id=self._client_id, key=key, touched_at=now))
                except IntegrityError:  # another request created it first
                    session.execute(update(lock).where(mine).values(touched_at=now))
            ours = (hit.client_id == self._client_id) & (hit.key == key)
            session.execute(delete(hit).where(ours, hit.at < now - window))  # housekeeping
            count = session.scalar(select(func.count()).where(ours)) or 0
            if count >= limit:
                return None
            hit_id = str(uuid4())
            session.add(hit(id=hit_id, client_id=self._client_id, key=key, at=now))
        return hit_id

    def forget(self, hit_id: str) -> None:
        """Take back a hit that shouldn't count after all (a login that succeeded)."""
        hit = RateLimitHitRow
        with Session(self._engine) as session, session.begin():
            session.execute(delete(hit).where(hit.client_id == self._client_id, hit.id == hit_id))
