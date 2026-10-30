"""Recruiter dashboard over HTTP, with real login and the offline extractor."""

import secrets
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.api.auth import hash_password, parse_users, verify_password
from app.api.main import create_app
from app.bootstrap import Container, build
from app.settings import Settings

PASSWORD = secrets.token_urlsafe(16)


@pytest.fixture
def container(tmp_path: Path) -> Container:
    return build(
        Settings(
            _env_file=None,
            database_url=f"sqlite:///{tmp_path / 'd.db'}",
            debounce_seconds=0,
            recruiter_users=f"rec@sazon.test:{hash_password(PASSWORD)}",
            session_secret=secrets.token_hex(16),
        )
    )


@pytest.fixture
def client(container: Container) -> TestClient:
    with TestClient(create_app(container)) as client:
        yield client


@pytest.fixture
def logged_in(client: TestClient) -> TestClient:
    response = client.post("/login", data={"email": "REC@sazon.test", "password": PASSWORD})
    assert response.status_code == 200  # followed the redirect to /dashboard
    return client


def chat(container: Container, *texts: str) -> UUID:
    cid = container.service.start_conversation()
    for text in texts:
        container.service.receive(cid, text)
        container.service.process_pending(cid)
    return cid


def test_dashboard_requires_login(client: TestClient) -> None:
    response = client.get("/dashboard", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_wrong_password(client: TestClient) -> None:
    response = client.post("/login", data={"email": "rec@sazon.test", "password": "nope"})
    assert response.status_code == 401
    assert "Wrong email or password" in response.text


def test_list_filters_and_counters(logged_in: TestClient, container: Container) -> None:
    chat(container, "sí", "Ana López", "moto", "Madrid, Chamberí")
    chat(container, "sí", "Luis Pérez", "no tengo")
    page = logged_in.get("/dashboard").text
    assert "Ana López" in page
    assert "Luis Pérez" in page
    filtered = logged_in.get("/dashboard?status=disqualified").text
    assert "Luis Pérez" in filtered
    assert "Ana López" not in filtered
    by_location = logged_in.get("/dashboard?location=es-madrid").text
    assert "Ana López" in by_location
    assert "Luis Pérez" not in by_location
    assert "New today" in page


def test_review_queue_shows_reason(logged_in: TestClient, container: Container) -> None:
    chat(container, "sí", "Eva Ruiz", "quiero hablar con una persona")
    page = logged_in.get("/dashboard/review").text
    assert "Eva Ruiz" in page
    assert "asked to talk to a person" in page


def test_detail_override_note_and_export(logged_in: TestClient, container: Container) -> None:
    cid = chat(container, "sí", "Eva Ruiz", "quiero hablar con una persona")
    detail = logged_in.get(f"/dashboard/candidates/{cid}").text
    assert "Needs review" in detail
    assert "Transcript" in detail

    response = logged_in.post(
        f"/dashboard/candidates/{cid}/override",
        data={"status": "qualified", "reason": "Called her, all fine"},
    )
    assert response.status_code == 200
    logged_in.post(f"/dashboard/candidates/{cid}/notes", data={"text": "Prefers mornings"})

    export = logged_in.get(f"/dashboard/candidates/{cid}/export.json").json()
    assert export["status"] == "qualified"
    types = [e["type"] for e in export["events"]]
    assert {"viewed", "override", "note_added"} <= set(types)
    override = next(e for e in export["events"] if e["type"] == "override")
    assert override["data"]["actor"] == "rec@sazon.test"
    assert override["data"]["from"] == "needs_review"


def test_override_without_reason_is_rejected(logged_in: TestClient, container: Container) -> None:
    cid = chat(container, "sí")
    response = logged_in.post(
        f"/dashboard/candidates/{cid}/override", data={"status": "qualified", "reason": " "}
    )
    assert response.status_code == 400


def test_unknown_candidate_is_404(logged_in: TestClient) -> None:
    assert logged_in.get(f"/dashboard/candidates/{UUID(int=7)}").status_code == 404


def test_logout(logged_in: TestClient) -> None:
    logged_in.post("/logout")
    assert logged_in.get("/dashboard", follow_redirects=False).status_code == 303


def test_login_page_explains_missing_accounts(tmp_path: Path) -> None:
    container = build(Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'n.db'}"))
    with TestClient(create_app(container)) as client:
        assert "No recruiter accounts are configured" in client.get("/login").text


def test_password_helpers() -> None:
    stored = hash_password(PASSWORD)
    assert verify_password(PASSWORD, stored)
    assert not verify_password("wrong", stored)
    assert not verify_password(PASSWORD, "garbage")
    assert parse_users(" A@x.io:h1 ; b@x.io:h2;bad") == {"a@x.io": "h1", "b@x.io": "h2"}


def test_every_filter(logged_in: TestClient, container: Container) -> None:
    chat(container, "sí", "Ana López", "moto", "Madrid, Chamberí", "fines de semana", "noches")
    chat(container, "yes", "John Smith", "car", "Sevilla", "full time", "mornings")
    assert "Ana López" in logged_in.get("/dashboard?availability=weekends").text
    assert "Ana López" not in logged_in.get("/dashboard?availability=full_time").text
    assert "John Smith" in logged_in.get("/dashboard?schedule=morning").text
    assert "Ana López" not in logged_in.get("/dashboard?schedule=morning").text
    assert "John Smith" in logged_in.get("/dashboard?language=en").text
    assert "Ana López" not in logged_in.get("/dashboard?language=en").text
    assert "John Smith" in logged_in.get("/dashboard?q=smith").text
    assert "No candidates match" in logged_in.get("/dashboard?start_before=2000-01-01").text
    assert logged_in.get("/dashboard?start_before=not-a-date").status_code == 200


def test_actions_on_unknown_candidate(logged_in: TestClient) -> None:
    unknown = UUID(int=9)
    assert logged_in.get(f"/dashboard/candidates/{unknown}/export.json").status_code == 404
    note = logged_in.post(f"/dashboard/candidates/{unknown}/notes", data={"text": "x"})
    assert note.status_code == 404
    override = logged_in.post(
        f"/dashboard/candidates/{unknown}/override", data={"status": "qualified", "reason": "x"}
    )
    assert override.status_code == 404


def test_analytics_page(logged_in: TestClient, container: Container) -> None:
    chat(container, "sí", "Luis Pérez", "no tengo")
    page = logged_in.get("/dashboard/analytics").text
    assert "Completion rate" in page
    assert "Funnel" in page


def test_interview_booked_end_to_end(logged_in: TestClient, container: Container) -> None:
    cid = chat(
        container,
        "sí",
        "Ana López",
        "moto",
        "Madrid, Chamberí",
        "jornada completa",
        "noches",
        "nunca",
        "mañana",
        "sí",
        "2",
    )
    state = container.service.repo.get(cid)
    assert state is not None
    assert state.interview_at is not None
    assert "Interview" in logged_in.get(f"/dashboard/candidates/{cid}").text


def test_channel_filter_and_analytics(logged_in: TestClient, container: Container) -> None:
    voice = container.service.start_conversation("voice")
    container.service.receive(voice, "sí")
    container.service.process_pending(voice)
    container.service.receive(voice, "carla soto")
    container.service.process_pending(voice)
    chat(container, "sí", "Pablo Gil")
    page = logged_in.get("/dashboard?channel=voice").text
    assert "Carla Soto" in page
    assert "Pablo Gil" not in page
    assert "voice" in logged_in.get(f"/dashboard/candidates/{voice}").text
    assert "By channel" in logged_in.get("/dashboard/analytics").text
