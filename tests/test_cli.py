"""The admin CLI that makes password hashes for RECRUITER_USERS."""

import re
import socket
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from dotenv import dotenv_values
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import cli
from app.adapters.clock.clocks import SystemClock
from app.adapters.storage.dashboard import DashboardQueries, Filters
from app.adapters.storage.sql import ConversationRow, SqlRepository, make_engine
from app.api.auth import parse_users, verify_password
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
def env_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A working directory of its own, so these tests can never touch the real .env."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: "")  # Enter: generate one
    return tmp_path / ".env"


def created_password(output: str) -> str:
    return re.findall(r"Password: (\S+)", output)[0]


def test_create_user_sets_up_a_fresh_clone_with_the_typed_password(
    env_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env_dir.with_name(".env.example").write_text(
        "# from the example\nRECRUITER_USERS=\nSESSION_SECRET=\nLLM_PROVIDER=fake\n"
    )
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: "my-own-long-password")

    assert cli.main(["cli", "create-user"]) == 0

    values = dotenv_values(env_dir)
    users = parse_users(values.get("RECRUITER_USERS"))
    assert verify_password("my-own-long-password", users[cli.DEFAULT_TEST_EMAIL])
    assert values["SESSION_SECRET"]
    assert values["LLM_PROVIDER"] == "fake"
    assert "# from the example" in env_dir.read_text()
    assert "my-own-long-password" not in capsys.readouterr().out


def test_create_user_keeps_an_existing_env_and_session_secret(env_dir: Path) -> None:
    env_dir.with_name(".env.example").write_text("FROM_EXAMPLE=1\n")
    existing = "keep-me"
    env_dir.write_text(f"SESSION_SECRET={existing}\n")

    assert cli.main(["cli", "create-user"]) == 0

    values = dotenv_values(env_dir)
    assert values["SESSION_SECRET"] == existing
    assert "FROM_EXAMPLE" not in values


def test_create_user_refuses_a_short_password_and_writes_nothing(
    env_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env_dir.with_name(".env.example").write_text("RECRUITER_USERS=\n")
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: "short")

    assert cli.main(["cli", "create-user"]) == 1

    assert "at least 6" in capsys.readouterr().err
    assert not env_dir.exists()


def test_create_user_accepts_a_six_character_password(
    env_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    typed = "x" * cli.MIN_TEST_PASSWORD_LENGTH
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: typed)

    assert cli.main(["cli", "create-user"]) == 0

    users = parse_users(dotenv_values(env_dir).get("RECRUITER_USERS"))
    assert verify_password(typed, users[cli.DEFAULT_TEST_EMAIL])


def test_create_user_writes_a_login_the_app_accepts(
    env_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["cli", "create-user", "New@Sazon.test"]) == 0

    out = capsys.readouterr().out
    password = created_password(out)
    assert len(password) >= cli.MIN_PASSWORD_LENGTH
    assert "Created" in out
    users = parse_users(dotenv_values(env_dir).get("RECRUITER_USERS"))
    assert verify_password(password, users["new@sazon.test"])


def test_create_user_defaults_to_a_test_account(
    env_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["cli", "create-user"]) == 0

    assert cli.DEFAULT_TEST_EMAIL in capsys.readouterr().out
    assert cli.DEFAULT_TEST_EMAIL in parse_users(dotenv_values(env_dir).get("RECRUITER_USERS"))


def test_create_user_keeps_other_accounts_and_the_rest_of_the_file(env_dir: Path) -> None:
    before = "# a comment\nOTHER=1\nRECRUITER_USERS=a@sazon.test:hash-a;b@sazon.test:hash-b\nZ=2\n"
    env_dir.write_text(before)

    assert cli.main(["cli", "create-user", "c@sazon.test"]) == 0

    users = parse_users(dotenv_values(env_dir).get("RECRUITER_USERS"))
    assert users["a@sazon.test"] == "hash-a"
    assert users["b@sazon.test"] == "hash-b"
    assert "c@sazon.test" in users
    lines = env_dir.read_text().splitlines()
    assert [lines[0], lines[1], lines[3]] == ["# a comment", "OTHER=1", "Z=2"]


def test_create_user_gives_an_existing_account_a_new_password(
    env_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_dir.write_text("RECRUITER_USERS=a@sazon.test:old-hash\n")

    assert cli.main(["cli", "create-user", "a@sazon.test"]) == 0

    out = capsys.readouterr().out
    assert "Updated" in out
    users = parse_users(dotenv_values(env_dir).get("RECRUITER_USERS"))
    assert list(users) == ["a@sazon.test"]
    assert verify_password(created_password(out), users["a@sazon.test"])


def test_create_user_refuses_outside_demo_mode(
    env_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DEMO_MODE", "false")

    assert cli.main(["cli", "create-user"]) == 1

    assert "DEMO_MODE" in capsys.readouterr().err
    assert not env_dir.exists()


@pytest.mark.parametrize(
    "email", ["not-an-email", "a:b@sazon.test", "a;b@sazon.test", "a b@x.test"]
)
def test_create_user_refuses_an_email_that_would_break_the_list(
    env_dir: Path, capsys: pytest.CaptureFixture[str], email: str
) -> None:
    assert cli.main(["cli", "create-user", email]) == 1

    assert "email" in capsys.readouterr().err
    assert not env_dir.exists()


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


@pytest.fixture
def demo_env(env_dir: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """A fresh clone in a directory of its own, with the server start recorded, not run."""
    env_dir.with_name(".env.example").write_text("RECRUITER_USERS=\nSESSION_SECRET=\n")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{env_dir.with_name('data') / 'sazon.db'}")
    monkeypatch.setenv("CLIENT_ID", "grupo_sazon")
    monkeypatch.setenv("INVITE_ONLY", "false")
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setattr(cli, "port_in_use", lambda _host, _port: False)
    started: dict[str, object] = {}
    monkeypatch.setattr(
        cli.uvicorn, "run", lambda app, **options: started.update(app=app, **options)
    )
    return started


def candidate_count() -> int:
    return (
        cli.DashboardQueries(make_engine(cli.Settings().database_url), "grupo_sazon")
        .candidates(Filters())
        .total
    )


def test_demo_sets_up_a_fresh_clone_and_starts_the_app(
    demo_env: dict[str, object], env_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["cli", "demo"]) == 0

    out = capsys.readouterr().out
    users = parse_users(dotenv_values(env_dir).get("RECRUITER_USERS"))
    assert verify_password(created_password(out), users["tester@sazon.test"])
    assert candidate_count() == len(DEMO_CANDIDATES)
    assert "http://127.0.0.1:8000/dashboard" in out
    assert "Log in as: tester@sazon.test" in out
    assert demo_env == {
        "app": "app.api.main:create_app",
        "factory": True,
        "host": "127.0.0.1",
        "port": 8000,
        "timeout_graceful_shutdown": 2,  # no reload: an open chat stream would hang it
    }


def test_demo_run_again_keeps_the_login_and_the_candidates(
    demo_env: dict[str, object], env_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert cli.main(["cli", "demo"]) == 0
    before = env_dir.read_text()

    def no_prompt(_prompt: str) -> str:
        raise AssertionError("asked for a password again")

    monkeypatch.setattr(cli.getpass, "getpass", no_prompt)
    demo_env.clear()
    assert cli.main(["cli", "demo"]) == 0

    assert env_dir.read_text() == before
    assert candidate_count() == len(DEMO_CANDIDATES)
    assert demo_env["port"] == 8000


def test_demo_no_seed_leaves_the_dashboard_empty(demo_env: dict[str, object]) -> None:
    assert cli.main(["cli", "demo", "--no-seed"]) == 0

    assert candidate_count() == 0
    assert demo_env


def test_demo_reset_moves_the_old_database_aside_and_starts_fresh(
    demo_env: dict[str, object], env_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["cli", "demo", "--no-seed"]) == 0
    assert cli.main(["cli", "seed-demo"]) == 0
    capsys.readouterr()

    assert cli.main(["cli", "demo", "--reset", "--no-seed"]) == 0

    assert "Moved the old database" in capsys.readouterr().out
    (backup,) = env_dir.with_name("data").glob("sazon-backup-*.db")
    assert backup.stat().st_size
    assert candidate_count() == 0


def test_demo_refuses_while_something_runs_on_its_port(
    demo_env: dict[str, object],
    env_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "port_in_use", lambda _host, _port: True)

    assert cli.main(["cli", "demo", "--reset"]) == 1

    assert "already running" in capsys.readouterr().err
    assert not demo_env
    assert not env_dir.with_name("data").exists()  # no reset, no seeding under a live app


def test_demo_refuses_outside_demo_mode(
    demo_env: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEMO_MODE", "false")

    assert cli.main(["cli", "demo"]) == 1
    assert not demo_env


def test_demo_stops_if_the_login_cant_be_made(
    demo_env: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: "short")

    assert cli.main(["cli", "demo"]) == 1
    assert not demo_env


def test_reset_only_touches_local_sqlite(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.reset_database("postgresql+psycopg://db/sazon") == 1
    assert cli.reset_database("sqlite:///:memory:") == 1
    assert "local SQLite" in capsys.readouterr().err


def test_create_user_without_a_terminal_says_so(
    env_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def no_terminal(_prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr(cli.getpass, "getpass", no_terminal)

    assert cli.main(["cli", "create-user"]) == 1
    assert "Terminal app" in capsys.readouterr().err
    assert not env_dir.exists()


def test_port_in_use_sees_a_listening_socket() -> None:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        assert cli.port_in_use("127.0.0.1", port)
    assert not cli.port_in_use("127.0.0.1", port)


def test_demo_with_an_unknown_flag_shows_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["cli", "demo", "--fast"]) == 2
    assert "demo [--reset]" in capsys.readouterr().err


def test_demo_no_prompt_saves_the_generated_password_instead_of_showing_it(
    demo_env: dict[str, object],
    env_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """For Claude Code's /start: there's no terminal, and the password stays out of the chat."""

    def no_terminal(_prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr(cli.getpass, "getpass", no_terminal)

    assert cli.main(["cli", "demo", "--no-prompt", "--no-seed"]) == 0

    saved = env_dir.with_name("data") / "demo-login.txt"
    email, password = saved.read_text().split()
    users = parse_users(dotenv_values(env_dir).get("RECRUITER_USERS"))
    assert email == "tester@sazon.test"
    assert verify_password(password, users[email])
    assert saved.stat().st_mode & 0o777 == 0o600
    out = capsys.readouterr().out
    assert password not in out
    assert "data/demo-login.txt" in out
    assert demo_env["port"] == 8000


def test_demo_no_prompt_keeps_an_existing_login(demo_env: dict[str, object], env_dir: Path) -> None:
    assert cli.main(["cli", "demo", "--no-seed"]) == 0
    before = env_dir.read_text()

    assert cli.main(["cli", "demo", "--no-prompt", "--no-seed"]) == 0

    assert env_dir.read_text() == before
    assert not (env_dir.with_name("data") / "demo-login.txt").exists()
