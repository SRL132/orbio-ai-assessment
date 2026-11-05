"""A worker process: turns, reminders, the WhatsApp outbox and cleanup jobs. Its only HTTP is
metrics, on METRICS_PORT if set: turns happen here, so the web process can't report them.

    uv run python -m app.worker

Pair it with web processes started with ROLE=web. Run as many workers as needed: each
conversation is processed by one at a time (a lease in the database), and singleton jobs
(the WhatsApp outbox) hold a job lease.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from prometheus_client import start_http_server

from app.bootstrap import build
from app.settings import Settings

logger = logging.getLogger("app.worker")


async def run(settings: Settings, stop: asyncio.Event | None = None) -> None:
    """Run until SIGINT/SIGTERM (or until `stop` is set)."""
    container = build(settings)
    if settings.metrics_port is not None:
        start_http_server(settings.metrics_port)
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    container.runner.start()
    logger.info("worker_started client=%s", container.config.client_id)
    await stop.wait()
    await container.runner.stop()
    container.close()
    logger.info("worker_stopped")


def main() -> None:
    asyncio.run(run(Settings(role="worker")))


if __name__ == "__main__":
    main()
