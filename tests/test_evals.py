"""The eval harness itself: offline scripted run, scoring, and the regression ratchet."""

from app.adapters.llm.fake import TemplateResponder
from app.adapters.llm.keyword import KeywordExtractor
from app.domain.config import ClientConfig
from evals.__main__ import _regressions, _summarize, main
from evals.runner import Expect, Harness, Scenario, load_scenarios


def test_offline_suite_passes(config: ClientConfig) -> None:
    harness = Harness(config, KeywordExtractor(), TemplateResponder())
    results = [harness.run_scripted(s) for s in load_scenarios()]
    failing = {r.id: r.failures for r in results if not r.passed}
    assert failing == {}


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
    baseline = _summarize([harness.run_scripted(s) for s in load_scenarios(["no_license"])])
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
