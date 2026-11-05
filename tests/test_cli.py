"""The admin CLI that makes password hashes for RECRUITER_USERS."""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import cli
from app.adapters.clock.clocks import SystemClock
from app.adapters.storage.dashboard import DashboardQueries, Filters
from app.adapters.storage.sql import ConversationRow, SqlRepository, make_engine
from app.api.auth import verify_password
from app.domain.models import ConversationState, LlmOutput, Status
from app.seed import DEMO_CANDIDATES, DemoCandidate


def test_hash_password_prints_a_verifiable_hash(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: "a-long-enough-pass")
    assert cli.main(["cli", "hash-password"]) == 0
    assert verify_password("a-long-enough-pass", capsys.readouterr().out.strip())


def test_short_passwords_are_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: "short")
    assert cli.main(["cli", "hash-password"]) == 1
    assert "at least 12" in capsys.readouterr().err


@pytest.fixture
def database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[SqlRepository, UUID]]:
    url = f"sqlite:///{tmp_path / 'cli.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("CLIENT_ID", "grupo_sazon")
    engine = make_engine(url)
    repo = SqlRepository(engine, SystemClock(), "grupo_sazon")
    state = ConversationState(client_id="grupo_sazon")
    repo.create(state, [], [])
    yield repo, state.conversation_id
    engine.dispose()


def test_rebuild_state_check_passes_on_a_clean_database(
    database: tuple[SqlRepository, UUID], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["cli", "rebuild-state", "--check"]) == 0
    assert "0 conversations differ" in capsys.readouterr().err


def test_rebuild_state_finds_then_repairs_damage(
    database: tuple[SqlRepository, UUID], capsys: pytest.CaptureFixture[str]
) -> None:
    repo, conversation_id = database
    with repo._sessions.begin() as session:
        row = session.get(ConversationRow, str(conversation_id))
        assert row is not None
        row.status = "qualified"

    assert cli.main(["cli", "rebuild-state", "--check"]) == 1
    assert str(conversation_id) in capsys.readouterr().out
    assert cli.main(["cli", "rebuild-state"]) == 0
    assert cli.main(["cli", "rebuild-state", "--check"]) == 0


def test_llm_outputs_prints_a_conversations_raw_replies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    url = f"sqlite:///{tmp_path / 'c.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    engine = make_engine(url)
    repo = SqlRepository(engine, SystemClock(), "grupo_sazon")
    state = ConversationState(client_id="grupo_sazon")
    repo.create(state, [], [])
    repo.save_llm_outputs(
        state.conversation_id,
        [LlmOutput(kind="extract", provider="p", model="m", text="the raw reply")],
    )
    engine.dispose()

    assert cli.main(["cli", "llm-outputs", str(state.conversation_id)]) == 0

    assert "the raw reply" in capsys.readouterr().out


@pytest.fixture
def seed_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """An empty demo database, with a paid provider named: seeding must not use it."""
    url = f"sqlite:///{tmp_path / 'seed.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("CLIENT_ID", "grupo_sazon")
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("INVITE_ONLY", "false")
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "not-a-real-key")
    return url


def test_seed_demo_reaches_every_outcome_and_matches_the_event_log(
    seed_env: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["cli", "seed-demo"]) == 0

    out = capsys.readouterr().out
    assert "Raúl Díaz: abandoned" in out
    assert "Diego Navarro: qualified" in out
    assert "Luis Pérez: disqualified" in out
    assert "Jorge Martín: needs_review" in out
    assert "Elena Torres: needs_review" in out
    assert "Marta Ruiz: in_progress" in out
    assert cli.main(["cli", "rebuild-state", "--check"]) == 0


def test_seeded_data_shows_the_dashboards_cases_and_nothing_in_the_future(seed_env: str) -> None:
    started = datetime.now(UTC)
    assert cli.main(["cli", "seed-demo"]) == 0

    engine = make_engine(seed_env)
    queries = DashboardQueries(engine, "grupo_sazon")
    items = {item.full_name: item for item in queries.candidates(Filters()).items}
    with Session(engine) as session:
        rows = session.scalars(select(ConversationRow)).all()
        booked = [row.interview_at for row in rows if row.interview_at is not None]
    engine.dispose()

    assert len(items) == len(DEMO_CANDIDATES)
    assert "borderline_zone:location" in items["Jorge Martín"].review_reasons
    assert "human_requested" in items["Elena Torres"].review_reasons
    assert items["John Smith"].language == "en"
    assert len(booked) == 1
    assert all(item.updated_at.replace(tzinfo=UTC) <= started for item in items.values())


def test_seed_demo_refuses_a_database_that_has_conversations(
    seed_env: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["cli", "seed-demo"]) == 0
    capsys.readouterr()

    assert cli.main(["cli", "seed-demo"]) == 1

    assert "already has conversations" in capsys.readouterr().err
    engine = make_engine(seed_env)
    assert DashboardQueries(engine, "grupo_sazon").candidates(Filters()).total == len(
        DEMO_CANDIDATES
    )
    engine.dispose()


@pytest.mark.parametrize(
    ("variable", "value", "message"),
    [
        ("DEMO_MODE", "false", "DEMO_MODE"),
        ("INVITE_ONLY", "true", "INVITE_ONLY"),
        ("CLIENT_ID", "norte_express", "grupo_sazon"),
    ],
)
def test_seed_demo_refuses_outside_a_local_grupo_sazon_demo(
    seed_env: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    variable: str,
    value: str,
    message: str,
) -> None:
    monkeypatch.setenv(variable, value)

    assert cli.main(["cli", "seed-demo"]) == 1

    assert message in capsys.readouterr().err
    assert not Path(seed_env.removeprefix("sqlite:///")).exists()


def test_seed_demo_reports_a_candidate_who_ends_up_elsewhere(
    seed_env: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    wrong = DemoCandidate("Nadie", Status.QUALIFIED, ("Sí",))
    monkeypatch.setattr(cli, "DEMO_CANDIDATES", (wrong,))

    assert cli.main(["cli", "seed-demo"]) == 1

    captured = capsys.readouterr()
    assert "Nadie: in_progress" in captured.out
    assert "unexpected" in captured.err


def test_unknown_command_shows_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["cli", "nope"]) == 2
    assert "hash-password" in capsys.readouterr().err
