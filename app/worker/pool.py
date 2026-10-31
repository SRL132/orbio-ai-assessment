"""Run waiting turns concurrently: one worker process handles up to `size` conversations at once.

A turn spends almost all its time waiting for the LLM, so one turn at a time per process would
need ~10 processes for a 10x peak (2 turns/s x ~5 s per turn, by Little's law). Threads are
enough: the work is I/O-bound. Safety comes from the conversation lease, as before: a
conversation already leased by another worker or thread is skipped (`process_pending` returns
at once), and the worker holding it picks up any message that arrives meanwhile.

`fill()` never waits for turns to finish, so a slow turn doesn't stop the process from taking
new work; it only tops up free slots. Workers see the waiting list in a random order, so
several processes polling at once spread over different conversations instead of all
contending for the first ones.
"""

from __future__ import annotations

import logging
import random
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import UUID

from app.worker.service import ScreeningService

logger = logging.getLogger("app.pool")


class TurnPool:
    def __init__(self, service: ScreeningService, size: int, debounce: timedelta) -> None:
        if size < 1:
            raise ValueError("the turn pool needs at least one thread")
        self._service = service
        self._size = size
        self._debounce = debounce
        self._executor = ThreadPoolExecutor(size, thread_name_prefix="turn")
        self._lock = threading.Lock()
        self._in_flight: set[UUID] = set()
        self._shuffle = random.SystemRandom().shuffle

    @property
    def in_flight(self) -> int:
        with self._lock:
            return len(self._in_flight)

    def fill(self) -> int:
        """Start turns for waiting conversations, up to the free slots. Returns how many."""
        with self._lock:
            free = self._size - len(self._in_flight)
        if free <= 0:
            return 0
        waiting = self._service.waiting(self._debounce)
        self._shuffle(waiting)
        started = 0
        for conversation_id in waiting:
            if started == free:
                break
            with self._lock:
                if conversation_id in self._in_flight:
                    continue
                self._in_flight.add(conversation_id)
            self._executor.submit(self._run, conversation_id)
            started += 1
        return started

    def _run(self, conversation_id: UUID) -> None:
        try:
            self._service.process_pending(conversation_id)
        except Exception:
            logger.exception("turn_failed id=%s", conversation_id)
        finally:
            with self._lock:
                self._in_flight.discard(conversation_id)

    def shutdown(self) -> None:
        """Let running turns finish (they hold leases), start no new ones."""
        self._executor.shutdown(wait=True, cancel_futures=True)
