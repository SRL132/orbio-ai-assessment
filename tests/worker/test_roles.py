"""Process roles: web processes only serve HTTP, worker processes find and do the work."""

import asyncio
import secrets
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.main import create_app
from app.bootstrap import build
from app.settings import Settings
from app.worker.__main__ import run


def settings(tmp_path: Path, role: str) -> Settings:
    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'roles.db'}",  # shared by both processes
        role=role,
        debounce_seconds=1.5,
        session_secret=secrets.token_hex(8),
    )


def test_web_stores_messages_and_a_worker_answers_them(tmp_path: Path) -> None:
    worker = build(settings(tmp_path, "worker"))
    with TestClient(create_app(build(settings(tmp_path, "web")))) as web:
        cid = web.post("/api/conversations").json()["id"]
        web.post(f"/api/conversations/{cid}/messages", json={"text": "sí"})
        assert worker.service.process_waiting(timedelta(seconds=1.5)) == 0  # still typing?
        assert worker.service.process_waiting(timedelta(0)) == 1  # the debounce has passed
        messages = web.get(f"/api/conversations/{cid}/messages").json()["messages"]
    assert [m["direction"] for m in messages] == ["out", "in", "out"]
    assert worker.service.process_waiting(timedelta(0)) == 0  # nothing left
    worker.close()


def test_the_worker_process_starts_and_stops_cleanly(tmp_path: Path) -> None:
    async def scenario() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(run(settings(tmp_path, "worker"), stop))
        await asyncio.sleep(0.2)  # the background loops are running
        assert not task.done()
        stop.set()
        await asyncio.wait_for(task, 5)

    asyncio.run(scenario())
