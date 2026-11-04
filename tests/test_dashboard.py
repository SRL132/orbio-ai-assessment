"""Recruiter dashboard over HTTP, with real login and the offline extractor."""

import csv
import io
import re
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.storage.dashboard import Filters
from app.adapters.storage.sql import ErasureRow, EventRow, MessageRow
from app.api.auth import hash_password, parse_users, verify_password
from app.api.main import create_app
from app.bootstrap import Container, build
from app.settings import Settings
from tests.conftest import submit

PASSWORD = secrets.token_urlsafe(16)
START_OF_TIME = datetime(2000, 1, 1, tzinfo=UTC)


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
    response = submit(client, "/login", {"email": "REC@sazon.test", "password": PASSWORD})
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
    response = submit(client, "/login", {"email": "rec@sazon.test", "password": "nope"})
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

    response = submit(
        logged_in,
        f"/dashboard/candidates/{cid}/override",
        {"status": "qualified", "reason": "Called her, all fine"},
    )
    assert response.status_code == 200
    submit(logged_in, f"/dashboard/candidates/{cid}/notes", {"text": "Prefers mornings"})

    export = logged_in.get(f"/dashboard/candidates/{cid}/export.json").json()
    assert export["status"] == "qualified"
    types = [e["type"] for e in export["events"]]
    assert {"viewed", "override", "note_added"} <= set(types)
    override = next(e for e in export["events"] if e["type"] == "override")
    assert override["data"]["actor"] == "rec@sazon.test"
    assert override["data"]["from"] == "needs_review"


def test_override_without_reason_is_rejected(logged_in: TestClient, container: Container) -> None:
    cid = chat(container, "sí")
    response = submit(
        logged_in, f"/dashboard/candidates/{cid}/override", {"status": "qualified", "reason": " "}
    )
    assert response.status_code == 400


def test_unknown_candidate_is_404(logged_in: TestClient) -> None:
    assert logged_in.get(f"/dashboard/candidates/{UUID(int=7)}").status_code == 404


def test_logout(logged_in: TestClient) -> None:
    submit(logged_in, "/logout", {})
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
    note = submit(logged_in, f"/dashboard/candidates/{unknown}/notes", {"text": "x"})
    assert note.status_code == 404
    override = submit(
        logged_in,
        f"/dashboard/candidates/{unknown}/override",
        {"status": "qualified", "reason": "x"},
    )
    assert override.status_code == 404


def test_analytics_page(logged_in: TestClient, container: Container) -> None:
    chat(container, "sí", "Luis Pérez", "no tengo")
    page = logged_in.get("/dashboard/analytics").text
    assert "Completion rate" in page
    assert "Screened within 24 h of applying (1 of 1)" in page
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
    container.service.receive(voice, "sí")  # voice reads the name back before it counts
    container.service.process_pending(voice)
    chat(container, "sí", "Pablo Gil")
    page = logged_in.get("/dashboard?channel=voice").text
    assert "Carla Soto" in page
    assert "Pablo Gil" not in page
    assert "voice" in logged_in.get(f"/dashboard/candidates/{voice}").text
    assert "By channel" in logged_in.get("/dashboard/analytics").text


def test_candidate_list_is_paged(logged_in: TestClient, container: Container) -> None:
    for _ in range(55):
        container.service.start_conversation()
    first = logged_in.get("/dashboard?status=in_progress").text
    assert "1 to 50 of 55" in first
    assert "?status=in_progress&amp;page=2" in first  # the filters survive paging
    second = logged_in.get("/dashboard?status=in_progress&page=2").text
    assert "51 to 55 of 55" in second
    assert "Next" not in second
    assert logged_in.get("/dashboard?page=abc").status_code == 200  # bad input: first page


def test_forms_without_the_page_token_are_refused(
    logged_in: TestClient, container: Container
) -> None:
    cid = chat(container, "sí")
    url = f"/dashboard/candidates/{cid}/notes"
    assert logged_in.post(url, data={"text": "forged"}).status_code == 403  # no token
    forged = {"text": "forged", "csrf_token": "guess"}
    assert logged_in.post(url, data=forged).status_code == 403
    assert submit(logged_in, url, {"text": "real"}).status_code == 200
    assert "forged" not in logged_in.get(f"/dashboard/candidates/{cid}").text
    assert logged_in.post("/logout").status_code == 403
    assert submit(logged_in, "/logout", {}).status_code == 200


def test_logged_out_form_posts_go_to_login(client: TestClient, container: Container) -> None:
    cid = chat(container, "sí")
    response = client.post(
        f"/dashboard/candidates/{cid}/notes", data={"text": "x"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_license_details_are_shown_once(logged_in: TestClient, container: Container) -> None:
    cid = chat(container, "sí", "Pedro Gómez", "moto")
    repo = container.service.repo
    state = repo.get(cid)
    assert state is not None
    state.candidate.license_type, state.candidate.license_years = "B", 1
    repo.save_turn(state, [], [], [])
    detail = logged_in.get(f"/dashboard/candidates/{cid}").text
    assert detail.count("(B license") == 1
    assert "1 years" not in detail


# --- Rejected candidates who ask for a human ------------------------------------------------------


def test_rejected_candidate_who_asks_for_a_human_can_be_found_and_overturned(
    logged_in: TestClient, container: Container
) -> None:
    asked = chat(container, "sí", "Ana López", "no tengo", "quiero hablar con una persona")
    chat(container, "sí", "Eva Ruiz", "no tengo")  # rejected too, but didn't ask
    state = container.service.get(asked)
    assert state is not None
    assert state.status.value == "disqualified"  # asking doesn't undo the knockout

    page = logged_in.get("/dashboard?human=1").text
    assert "Ana López" in page
    assert "Eva Ruiz" not in page
    assert container.queries.counters(since=START_OF_TIME)["asked_for_human"] == 1

    submit(
        logged_in,
        f"/dashboard/candidates/{asked}/override",
        {"status": "needs_review", "reason": "Has a license after all"},
    )
    assert "Ana López" not in logged_in.get("/dashboard?human=1").text
    assert container.queries.counters(since=START_OF_TIME)["asked_for_human"] == 0


# --- Invite page ----------------------------------------------------------------------------------

CSV_OK = (
    "phone,applied_at\n"
    "+34600000001,2026-01-05T09:00:00+01:00\n"
    "+34600000002,2026-01-05T10:00:00+01:00\n"
)


def upload(client: TestClient, body: bytes | None = None, **data: str) -> httpx.Response:
    found = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/dashboard/invite").text)
    assert found is not None
    files = {"applications": ("apps.csv", body, "text/csv")} if body is not None else None
    if files is None:  # a browser sends an empty file field: force a multipart form anyway
        files = {"applications": ("", b"", "application/octet-stream")}
    return client.post(
        "/dashboard/invite", data={"csrf_token": found.group(1), **data}, files=files
    )


def links(response: httpx.Response) -> list[dict[str, str]]:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["cache-control"] == "no-store"
    return list(csv.DictReader(io.StringIO(response.text)))


def cid_of(client: TestClient, url: str) -> UUID:
    opened = client.get(url.removeprefix("http://testserver"), follow_redirects=False)
    assert opened.status_code == 303
    return UUID(opened.headers["location"].split("conversation=")[1])


def test_invite_page_needs_login(client: TestClient) -> None:
    assert client.get("/dashboard/invite", follow_redirects=False).status_code == 303
    assert client.post("/dashboard/invite", data={"count": "1"}, follow_redirects=False).is_redirect


def test_invite_needs_the_form_token(logged_in: TestClient) -> None:
    response = logged_in.post("/dashboard/invite", data={"count": "1", "csrf_token": "wrong"})
    assert response.status_code == 403


def test_csv_gets_one_link_per_row_and_keeps_other_columns_out_of_storage(
    logged_in: TestClient, container: Container
) -> None:
    rows = links(upload(logged_in, CSV_OK.encode()))
    assert [r["phone"] for r in rows] == ["+34600000001", "+34600000002"]

    cid = cid_of(logged_in, rows[0]["chat_url"])
    state = container.service.get(cid)
    assert state is not None
    assert state.applied_at == datetime(2026, 1, 5, 8, tzinfo=UTC)
    assert "+34600000001" not in state.model_dump_json()
    assert [t.value for _, e in container.service.timeline(cid) for t in [e.type]][:2] == [
        "conversation_started",
        "invite_created",
    ]
    created = [e for _, e in container.service.timeline(cid) if e.type.value == "invite_created"]
    assert created[0].data["actor"] == "rec@sazon.test"


def test_a_number_makes_that_many_links(logged_in: TestClient) -> None:
    assert len(links(upload(logged_in, count="3"))) == 3


@pytest.mark.parametrize(
    ("body", "data", "message"),
    [
        (None, {}, "Upload a CSV"),
        (None, {"count": "0"}, "Upload a CSV"),
        (None, {"count": "1001"}, "Upload a CSV"),
        (b"phone\n+34600000001\n", {}, "applied_at column"),
        (b"applied_at\n", {}, "no rows"),
        (b"applied_at\nyesterday\n", {}, "Line 2"),
        (b"applied_at\n2026-01-05T09:00:00\n", {}, "Line 2"),
        (b"applied_at\n2999-01-05T09:00:00+00:00\n", {}, "in the future"),
        (b"applied_at,chat_url\n2026-01-05T09:00:00+00:00,x\n", {}, "chat_url column"),
        (b"\xff\xfe\x00bad", {}, "CSV UTF-8"),
        (b"applied_at\n" + b"2026-01-05T09:00:00+00:00\n" * 1001, {}, "1000 rows"),
        (b"applied_at\n" + b"x" * (1024 * 1024 + 1), {}, "too big"),
        (b'applied_at\n"' + b"x" * 140_000 + b"\n", {}, "readable CSV"),
    ],
)
def test_bad_upload_is_explained_and_creates_nothing(
    logged_in: TestClient,
    container: Container,
    body: bytes | None,
    data: dict[str, str],
    message: str,
) -> None:
    response = upload(logged_in, body, **data)
    assert response.status_code == 400
    assert message in response.text
    assert container.queries.analytics([], container.clock.now()).invites_unopened == 0


def test_a_bad_row_anywhere_stops_the_whole_batch(
    logged_in: TestClient, container: Container
) -> None:
    upload(logged_in, b"applied_at\n2026-01-05T09:00:00+00:00\nnonsense\n")
    assert container.queries.analytics([], container.clock.now()).invites_unopened == 0


def test_links_need_a_public_address_outside_a_demo(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'p.db'}",
        recruiter_users=f"rec@sazon.test:{hash_password(PASSWORD)}",
        session_secret=secrets.token_hex(16),
        demo_mode=False,
    )
    with TestClient(create_app(build(settings)), base_url="https://testserver") as client:
        submit(client, "/login", {"email": "rec@sazon.test", "password": PASSWORD})
        response = upload(client, count="1")
        assert response.status_code == 400
        assert "PUBLIC_BASE_URL" in response.text


def test_unopened_invite_gets_no_reminders_and_stays_out_of_the_lists(
    logged_in: TestClient, container: Container
) -> None:
    url = links(upload(logged_in, count="1"))[0]["chat_url"]
    queries, now = container.queries, container.clock.now()

    assert queries.counters(since=START_OF_TIME)["in_progress"] == 0
    assert queries.candidates(Filters()).total == 0
    analytics = queries.analytics([], now)
    assert (analytics.started, analytics.invites_unopened) == (0, 1)
    assert analytics.drop_off == {}

    container.clock.advance(hours=72)
    assert container.service.send_due_reminders() == 0

    before_opening = container.clock.now()
    cid = cid_of(logged_in, url)
    state = container.service.get(cid)
    assert state is not None
    assert state.invite_pending is False
    assert state.started_at is not None
    assert state.started_at >= before_opening  # the conversation really starts now
    assert state.next_reminder_at is not None
    analytics = queries.analytics([], container.clock.now())
    assert (analytics.started, analytics.invites_unopened) == (1, 0)
    assert queries.candidates(Filters()).total == 1

    container.clock.advance(hours=2)
    assert container.service.send_due_reminders() == 1


def test_opening_a_link_twice_changes_nothing(logged_in: TestClient, container: Container) -> None:
    url = links(upload(logged_in, count="1"))[0]["chat_url"]
    cid = cid_of(logged_in, url)
    first = container.service.get(cid)
    assert first is not None
    container.clock.advance(hours=1)
    cid_of(logged_in, url)
    assert container.service.get(cid) == first


def test_unopened_applicant_still_counts_against_screening_on_time(
    logged_in: TestClient, container: Container
) -> None:
    """Otherwise the applicants the agent never reached would flatter the reach metric."""
    links(upload(logged_in, count="1"))
    container.clock.advance(hours=25)
    analytics = container.queries.analytics([], container.clock.now())
    assert (analytics.screening_due, analytics.screened_on_time) == (1, 0)


def erasures(container: Container) -> list[ErasureRow]:
    with Session(container.db) as session:
        return list(session.scalars(select(ErasureRow)))


def test_erasing_a_candidate_removes_everything_and_names_the_recruiter(
    logged_in: TestClient, container: Container
) -> None:
    cid = chat(container, "sí", "Ana López", "moto")
    page = logged_in.get(f"/dashboard/candidates/{cid}").text
    assert "Erase candidate" in page

    response = submit(logged_in, f"/dashboard/candidates/{cid}/erase", {"confirm": "ERASE"})
    assert response.status_code == 200  # followed the redirect to the list

    assert container.service.repo.get(cid) is None
    assert logged_in.get(f"/dashboard/candidates/{cid}").status_code == 404
    assert "Ana López" not in logged_in.get("/dashboard").text
    with Session(container.db) as session:
        for table in (EventRow, MessageRow):
            found = session.scalars(select(table).where(table.conversation_id == str(cid))).all()
            assert not found
    [proof] = erasures(container)
    assert (proof.conversation_id, proof.erased_by) == (str(cid), "rec@sazon.test")
    assert proof.status == "in_progress"


@pytest.mark.parametrize("confirm", ["", "erase", "yes"])
def test_erasing_needs_the_word_typed(
    logged_in: TestClient, container: Container, confirm: str
) -> None:
    cid = chat(container, "sí", "Ana López")
    response = submit(logged_in, f"/dashboard/candidates/{cid}/erase", {"confirm": confirm})
    assert response.status_code in (400, 422)
    assert container.service.repo.get(cid) is not None
    assert not erasures(container)


def test_erasing_needs_a_login_and_a_csrf_token(client: TestClient, container: Container) -> None:
    cid = chat(container, "sí")
    anonymous = client.post(
        f"/dashboard/candidates/{cid}/erase", data={"confirm": "ERASE"}, follow_redirects=False
    )
    assert anonymous.status_code in (303, 403)
    submit(client, "/login", {"email": "rec@sazon.test", "password": PASSWORD})
    forged = client.post(
        f"/dashboard/candidates/{cid}/erase", data={"confirm": "ERASE", "csrf_token": "guess"}
    )
    assert forged.status_code == 403
    assert container.service.repo.get(cid) is not None


def test_erasing_twice_or_an_unknown_candidate_is_404(
    logged_in: TestClient, container: Container
) -> None:
    cid = chat(container, "sí")
    url = f"/dashboard/candidates/{cid}/erase"
    submit(logged_in, url, {"confirm": "ERASE"})
    assert submit(logged_in, url, {"confirm": "ERASE"}).status_code == 404


def test_erasing_waits_for_a_running_turn(logged_in: TestClient, container: Container) -> None:
    cid = chat(container, "sí")
    container.service.recruiter_wait_seconds = 0
    assert container.service.repo.try_lease(cid, "a-turn", timedelta(minutes=1))
    response = submit(logged_in, f"/dashboard/candidates/{cid}/erase", {"confirm": "ERASE"})
    assert response.status_code == 409
    assert container.service.repo.get(cid) is not None


def test_an_erased_chat_is_gone_for_the_candidate_too(
    logged_in: TestClient, container: Container
) -> None:
    cid = chat(container, "sí")
    submit(logged_in, f"/dashboard/candidates/{cid}/erase", {"confirm": "ERASE"})
    assert (
        logged_in.post(f"/api/conversations/{cid}/messages", json={"text": "hola"}).status_code
        == 404
    )


def test_erasing_leaves_no_history_to_rebuild_and_other_candidates_alone(
    logged_in: TestClient, container: Container
) -> None:
    erased = chat(container, "sí", "Ana López", "moto")
    kept = chat(container, "sí", "Luis Pérez", "moto")
    repo = container.service.repo
    with Session(container.db) as session:
        types = session.scalars(
            select(EventRow.type).where(EventRow.conversation_id == str(erased))
        )
        assert "state_changed" in set(types)  # these hold personal data, so they must go too

    submit(logged_in, f"/dashboard/candidates/{erased}/erase", {"confirm": "ERASE"})

    assert repo.rebuild_projections() == []  # nothing is brought back from the log
    assert repo.get(erased) is None
    assert repo.get(kept) is not None
    with Session(container.db) as session:
        assert not session.scalars(
            select(EventRow).where(EventRow.conversation_id == str(erased))
        ).all()
        assert session.scalars(select(EventRow).where(EventRow.conversation_id == str(kept))).all()
    # The kept log still accepts new events at the next position.
    container.service.receive(kept, "Madrid, Chamberí")
    container.service.process_pending(kept)
    assert repo.rebuild_projections(check_only=True) == []
