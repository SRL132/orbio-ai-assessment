"""Prometheus metrics: the monitoring signals from docs/architecture.md, without personal data."""

import secrets
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from app.api.main import create_app
from app.bootstrap import build
from app.settings import Settings


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def make_client(tmp_path: Path, **settings: object) -> TestClient:
    return TestClient(
        create_app(
            build(
                Settings(
                    _env_file=None,
                    database_url=f"sqlite:///{tmp_path / 'm.db'}",
                    **{"debounce_seconds": 0, **settings},
                )
            )
        )
    )


def test_a_turn_shows_up_in_the_metrics(tmp_path: Path) -> None:
    turns = sample("sazon_turn_seconds_count", channel="chat")
    started = sample("sazon_events_total", type="conversation_started")
    with make_client(tmp_path) as client:
        cid = client.post("/api/conversations").json()["id"]
        client.post(f"/api/conversations/{cid}/messages", json={"text": "no, gracias"})
        body = client.get("/metrics").text
    assert sample("sazon_turn_seconds_count", channel="chat") == turns + 1
    assert sample("sazon_events_total", type="conversation_started") == started + 1
    assert 'sazon_outcomes_total{status="abandoned"}' in body  # declined consent
    assert "sazon_pending_messages 0.0" in body
    assert "sazon_review_queue_oldest_seconds" in body
    assert cid not in body  # no ids (or any personal data) in labels


def test_backlog_gauges_count_waiting_messages(tmp_path: Path) -> None:
    with make_client(tmp_path, debounce_seconds=60) as client:  # nothing gets processed
        cid = client.post("/api/conversations").json()["id"]
        client.post(f"/api/conversations/{cid}/messages", json={"text": "hola"})
        body = client.get("/metrics").text
    assert "sazon_pending_messages 1.0" in body


@pytest.mark.parametrize(("header", "code"), [(None, 401), ("Bearer wrong", 401)])
def test_metrics_can_require_a_token(tmp_path: Path, header: str | None, code: int) -> None:
    token = secrets.token_hex(8)
    with make_client(tmp_path, metrics_token=token) as client:
        headers = {"Authorization": header} if header else {}
        assert client.get("/metrics", headers=headers).status_code == code
        ok = client.get("/metrics", headers={"Authorization": f"Bearer {token}"})
        assert ok.status_code == 200
