"""The admin CLI that makes password hashes for RECRUITER_USERS."""

from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest

from app import cli
from app.adapters.clock.clocks import SystemClock
from app.adapters.storage.sql import ConversationRow, SqlRepository, make_engine
from app.api.auth import verify_password
from app.domain.models import ConversationState


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


def test_unknown_command_shows_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["cli", "nope"]) == 2
    assert "hash-password" in capsys.readouterr().err
