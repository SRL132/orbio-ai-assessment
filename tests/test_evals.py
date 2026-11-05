"""The eval harness itself: offline scripted run, scoring, and the regression ratchet."""

import json
from pathlib import Path

import pytest

from app.adapters.llm.fake import TemplateResponder
from app.adapters.llm.keyword import KeywordExtractor
from app.domain.config import ClientConfig
from evals.__main__ import (
    BASELINE,
    _regressions,
    append_history,
    behaviour_fingerprint,
    history_entry,
    keeps_history,
    main,
    summarize,
)
from evals.runner import (
    HOLDOUT,
    SIMULATOR_ATTEMPTS,
    Expect,
    Harness,
    Scenario,
    _candidate_says,
    load_scenarios,
)


def test_offline_suite_passes(config: ClientConfig) -> None:
    harness = Harness(config, KeywordExtractor(), TemplateResponder())
    results = [harness.run_scripted(s) for s in load_scenarios(offline=True)]
    failing = {r.id: r.failures for r in results if not r.passed}
    assert failing == {}


def test_offline_skips_scenarios_that_need_an_llm() -> None:
    every = load_scenarios()
    offline = load_scenarios(offline=True)
    assert any(s.requires_llm for s in every)
    assert not any(s.requires_llm for s in offline)
    assert len(offline) < len(every)


def test_reply_language_is_checked_per_candidate_message(config: ClientConfig) -> None:
    scenario = Scenario(
        id="x",
        description="",
        persona="",
        steps=["Sí", "Ana López"],
        expect=Expect(status="in_progress", reply_languages=["es", "en"]),
    )
    result = Harness(config, KeywordExtractor(), TemplateResponder()).run_scripted(scenario)
    assert result.failures == ["reply to message 2 in es, not en"]


def test_status_accepts_any_of_a_list(config: ClientConfig) -> None:
    scenario = Scenario(
        id="x",
        description="",
        persona="",
        steps=["Sí"],
        expect=Expect(status=["qualified", "in_progress"]),
    )
    assert Harness(config, KeywordExtractor(), TemplateResponder()).run_scripted(scenario).passed


def test_wrong_expectation_is_reported(config: ClientConfig) -> None:
    scenario = Scenario(
        id="x",
        description="",
        persona="",
        steps=["Sí", "Ana López"],
        expect=Expect(status="qualified", fields={"full_name": "Eva Ruiz"}),
    )
    result = Harness(config, KeywordExtractor(), TemplateResponder()).run_scripted(scenario)
    assert not result.passed
    assert result.field_accuracy == 0
    assert any("status in_progress" in f for f in result.failures)


def test_regressions_ratchet(config: ClientConfig) -> None:
    harness = Harness(config, KeywordExtractor(), TemplateResponder())
    baseline = summarize([harness.run_scripted(s) for s in load_scenarios(["no_license"])])
    assert _regressions(baseline, baseline) == []
    worse = {
        **baseline,
        "pass_rate": 0.0,
        "scenarios": {"no_license": {"passed": False, "field_accuracy": 0.0}},
    }
    problems = _regressions(baseline, worse)
    assert any("pass rate" in p for p in problems)
    assert any("passed before" in p for p in problems)
    assert _regressions(None, worse) == []


def test_cli_check_against_baseline() -> None:
    assert main(["--check"]) == 0


def test_cli_refuses_a_baseline_from_a_subset(capsys: pytest.CaptureFixture[str]) -> None:
    before = BASELINE.read_text()
    with pytest.raises(SystemExit) as refused:
        main(["--write-baseline", "--scenario", "no_license"])
    assert refused.value.code == 2
    assert "full suite" in capsys.readouterr().err
    assert BASELINE.read_text() == before


def test_holdout_scenarios_are_valid_and_need_a_real_model() -> None:
    scenarios = load_scenarios(path=HOLDOUT)
    assert len(scenarios) >= 12
    assert len({s.id for s in scenarios}) == len(scenarios)
    assert all(s.requires_llm and s.id.startswith("holdout_") for s in scenarios)
    assert not {s.id for s in scenarios} & {
        s.id for s in load_scenarios()
    }  # never in the main suite


def test_an_empty_answer_from_the_simulated_candidate_is_retried() -> None:
    class Flaky:
        calls = 0

        def complete(self, system: str, user: str, max_tokens: int = 400) -> str:
            self.calls += 1
            if self.calls < SIMULATOR_ATTEMPTS:
                raise ValueError("empty completion")
            return " hola "

    assert _candidate_says(Flaky(), "s", "u") == "hola"


def test_a_simulated_candidate_that_never_answers_stops_the_run() -> None:
    class Silent:
        def complete(self, system: str, user: str, max_tokens: int = 400) -> str:
            raise ValueError("empty completion")

    with pytest.raises(ValueError, match="empty"):
        _candidate_says(Silent(), "s", "u")


def test_only_live_runs_of_a_whole_file_are_kept_in_the_history() -> None:
    assert keeps_history("openrouter", None)
    assert not keeps_history("fake", None)  # runs on every test run: it would churn the file
    assert not keeps_history("openrouter", ["no_license"])  # a subset compares with nothing


def test_a_history_line_has_scores_and_failed_ids_but_no_transcript(tmp_path: Path) -> None:
    run = {
        "passed": 1,
        "total": 2,
        "complete": True,
        "pass_rate": 0.5,
        "field_accuracy": 0.9,
        "p50_turn_s": 1.0,
        "p95_turn_s": 2.0,
        "cost_usd": 0.03,
        "cost_per_conversation_usd": 0.015,
        "scenarios": {"good": {"passed": True}, "bad": {"passed": False, "failures": ["said x"]}},
    }
    history = tmp_path / "history.jsonl"

    append_history(history, history_entry("openrouter/m/scripted", run, "scenarios.yaml"))
    append_history(history, history_entry("openrouter/m/scripted", run, "holdout.yaml"))

    first, second = [json.loads(line) for line in history.read_text().splitlines()]
    assert first["failed"] == ["bad"]
    assert first["scenarios_file"] == "scenarios.yaml"
    assert second["scenarios_file"] == "holdout.yaml"
    assert "scenarios" not in first
    assert "said x" not in history.read_text()


def test_the_fingerprint_changes_with_a_prompt_or_the_config(tmp_path: Path) -> None:
    prompts = tmp_path / "app/adapters/llm/prompts"
    config = tmp_path / "app/config/client"
    prompts.mkdir(parents=True)
    config.mkdir(parents=True)
    (prompts / "extractor.md").write_text("one")
    (config / "client.yaml").write_text("cities: 7")
    before = behaviour_fingerprint(tmp_path)

    assert behaviour_fingerprint(tmp_path) == before
    (config / "client.yaml").write_text("cities: 45")
    changed_config = behaviour_fingerprint(tmp_path)
    (prompts / "extractor.md").write_text("two")

    assert changed_config != before
    assert behaviour_fingerprint(tmp_path) != changed_config
