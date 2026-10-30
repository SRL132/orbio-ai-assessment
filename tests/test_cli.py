"""The admin CLI that makes password hashes for RECRUITER_USERS."""

import pytest

from app import cli
from app.api.auth import verify_password


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


def test_unknown_command_shows_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["cli", "nope"]) == 2
    assert "hash-password" in capsys.readouterr().err
