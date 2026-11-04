"""A second client is a new config directory and no code: same engine, its own rules and name."""

import secrets
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.auth import hash_password
from app.api.main import create_app
from app.bootstrap import Container, build
from app.domain.models import Status
from app.settings import Settings
from tests.conftest import submit

PASSWORD = secrets.token_urlsafe(16)


@pytest.fixture
def container(tmp_path: Path) -> Container:
    return build(
        Settings(
            _env_file=None,
            client_id="norte_express",
            database_url=f"sqlite:///{tmp_path / 'd.db'}",
            debounce_seconds=0,
            recruiter_users=f"rec@norte.test:{hash_password(PASSWORD)}",
            session_secret=secrets.token_hex(16),
        )
    )


def talk(container: Container, *texts: str) -> list[str]:
    cid = container.service.start_conversation()
    for text in texts:
        container.service.receive(cid, text)
        container.service.process_pending(cid)
    return [m.text for m in container.service.messages(cid)]


def test_its_own_service_area_decides(container: Container) -> None:
    def status_for(place: str) -> Status:
        cid = container.service.start_conversation()
        for text in ("si", "Ana Ruiz", "Coche", place):
            container.service.receive(cid, text)
            container.service.process_pending(cid)
        state = container.service.get(cid)
        assert state is not None
        return state.status

    assert status_for("Bilbao, Deusto") == Status.IN_PROGRESS
    assert status_for("Sevilla") == Status.DISQUALIFIED  # the first client's city


def test_nothing_of_the_first_client_reaches_a_candidate(container: Container) -> None:
    text = " ".join(talk(container, "si", "Ana Ruiz", "Coche", "Bilbao, Deusto"))
    assert "Ane" in text
    assert "Norte Express" in text
    for other in ("Sazón", "Lucía", "Madrid", "restaurante"):
        assert other not in text


def test_the_dashboard_carries_its_name(container: Container) -> None:
    with TestClient(create_app(container)) as client:
        assert "Norte Express" in client.get("/login").text
        submit(client, "/login", {"email": "rec@norte.test", "password": PASSWORD})
        page = client.get("/dashboard").text
    assert "Norte Express" in page
    assert "Sazón" not in page
