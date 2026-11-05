"""The gate never spends money here: the live run is a stand-in."""

import json
from pathlib import Path

import pytest

from evals.__main__ import behaviour_fingerprint
from evals.gate import DEFAULT_MODEL, main, measured

KEY = f"openrouter/{DEFAULT_MODEL}/scripted"
BASELINE = {"scenarios": {"must_pass": {"passed": True}, "known_gap": {"passed": False}}}


def entry(**changes: object) -> dict[str, object]:
    return {
        "key": KEY,
        "fingerprint": "abc",
        "scenarios_file": "scenarios.yaml",
        "complete": True,
        "failed": [],
        **changes,
    }


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, True),
        ({"failed": ["known_gap"]}, True),
        ({"failed": ["must_pass"]}, False),
        ({"fingerprint": "older"}, False),
        ({"fingerprint": None}, False),
        ({"key": "openrouter/other/scripted"}, False),
        ({"scenarios_file": "holdout.yaml"}, False),
        ({"complete": False}, False),
    ],
)
def test_what_counts_as_measured(changes: dict[str, object], expected: bool) -> None:
    assert measured([entry(**changes)], KEY, "abc", BASELINE) is expected


@pytest.fixture
def paths(tmp_path: Path) -> tuple[Path, Path]:
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({KEY: BASELINE}))
    return tmp_path / "history.jsonl", baseline


def never(_: list[str]) -> int:
    raise AssertionError("this must not spend money")


def test_the_free_check_fails_without_spending(paths: tuple[Path, Path]) -> None:
    assert main([], never, *paths) == 1


def test_the_free_check_passes_on_a_clean_run(paths: tuple[Path, Path]) -> None:
    history, baseline = paths
    history.write_text(json.dumps(entry(fingerprint=behaviour_fingerprint())) + "\n")
    assert main([], never, history, baseline) == 0


def test_an_already_measured_change_does_not_run_again(paths: tuple[Path, Path]) -> None:
    history, baseline = paths
    history.write_text(json.dumps(entry(fingerprint=behaviour_fingerprint())) + "\n")
    assert main(["--run", "--budget-usd", "2"], never, history, baseline) == 0


@pytest.mark.parametrize("args", [["--run"], ["--run", "--budget-usd", "0"]])
def test_a_paid_run_needs_a_spending_cap(args: list[str], paths: tuple[Path, Path]) -> None:
    with pytest.raises(SystemExit):
        main(args, never, *paths)


def test_a_paid_run_uses_the_cap_and_passes_once_clean(paths: tuple[Path, Path]) -> None:
    history, baseline = paths
    seen: list[list[str]] = []

    def live(argv: list[str]) -> int:
        seen.append(argv)
        history.write_text(json.dumps(entry(fingerprint=behaviour_fingerprint())) + "\n")
        return 0

    assert main(["--run", "--budget-usd", "2"], live, history, baseline) == 0
    assert seen == [["--provider", "openrouter", "--model", DEFAULT_MODEL, "--budget-usd", "2.0"]]


def test_a_paid_run_stopped_at_the_budget_fails(paths: tuple[Path, Path]) -> None:
    history, baseline = paths

    def live(_: list[str]) -> int:
        line = entry(fingerprint=behaviour_fingerprint(), complete=False)
        history.write_text(json.dumps(line) + "\n")
        return 0

    assert main(["--run", "--budget-usd", "0.1"], live, history, baseline) == 1


def test_a_paid_run_with_a_regression_fails(paths: tuple[Path, Path]) -> None:
    history, baseline = paths

    def live(_: list[str]) -> int:
        line = entry(fingerprint=behaviour_fingerprint(), failed=["must_pass"])
        history.write_text(json.dumps(line) + "\n")
        return 0

    assert main(["--run", "--budget-usd", "2"], live, history, baseline) == 1
