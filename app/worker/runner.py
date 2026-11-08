"""Async background work for the one-process demo: debounced turns and the reminder loop.

In production the same service methods run in N worker processes that poll the database
(`python -m app.worker`; docs/architecture.md, "Demo vs production").
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from uuid import UUID

from app.worker.service import ScreeningService

logger = logging.getLogger("app.runner")


class Runner:
    def __init__(
        self,
        service: ScreeningService,
        debounce_seconds: float = 1.5,
        reminder_interval_seconds: float = 30.0,
    ) -> None:
        self._service = service
        self._debounce = debounce_seconds
        self._interval = reminder_interval_seconds
        self._timers: dict[UUID, asyncio.TimerHandle] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._reminders: asyncio.Task[None] | None = None
        self._periodic: list[tuple[str, Callable[[], object], float]] = []
        self._periodic_tasks: list[asyncio.Task[None]] = []

    def add_periodic(self, name: str, job: Callable[[], object], every_seconds: float) -> None:
        """Run a blocking job in a worker thread every N seconds (e.g. the WhatsApp outbox)."""
        self._periodic.append((name, job, every_seconds))

    async def _loop(self, name: str, job: Callable[[], object], every: float) -> None:
        while True:
            try:
                await asyncio.to_thread(job)
            except Exception:
                logger.exception("periodic_failed name=%s", name)
            await asyncio.sleep(every)

    def notify(self, conversation_id: UUID) -> None:
        """A message arrived: (re)start this conversation's debounce timer."""
        loop = asyncio.get_running_loop()
        if (timer := self._timers.pop(conversation_id, None)) is not None:
            timer.cancel()
        self._timers[conversation_id] = loop.call_later(
            self._debounce, self._spawn, conversation_id
        )

    def _spawn(self, conversation_id: UUID) -> None:
        self._timers.pop(conversation_id, None)
        task = asyncio.create_task(self._process(conversation_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _process(self, conversation_id: UUID) -> None:
        try:
            await asyncio.to_thread(self._service.process_pending, conversation_id)
        except Exception:
            logger.exception("turn_failed id=%s", conversation_id)

    async def tick(self) -> int:
        return await asyncio.to_thread(self._service.send_due_reminders)

    async def _reminder_loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("reminder_tick_failed")
            await asyncio.sleep(self._interval)

    def start(self) -> None:
        self._reminders = asyncio.create_task(self._reminder_loop())
        self._periodic_tasks = [
            asyncio.create_task(self._loop(name, job, every)) for name, job, every in self._periodic
        ]

    async def stop(self) -> None:
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
        for task in [self._reminders, *self._periodic_tasks]:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
