"""The comparison tooling, offline: cost metering, budget stop, model-id check, the table."""

import json
from pathlib import Path

import httpx
import pytest

from app.adapters.llm.openrouter import OpenRouterLLM
from app.adapters.llm.usage import UsageMeter
from app.domain.config import ClientConfig
from app.domain.models import AskField, ConversationState, FieldName
from evals import compare
from evals.runner import Harness, load_scenarios, percentile, run_suite

EMPTY_EXTRACTION = json.dumps({"language": "es", "intent": "answer"})


def fake_openrouter(cost: float = 0.01, models: tuple[str, ...] = ()) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": m} for m in models]})
        body = json.loads(request.content)
        content = EMPTY_EXTRACTION if "response_format" in body else "¿Cómo te llamas?"
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10, "cost": cost},
            },
        )

    return httpx.MockTransport(handler)


def test_openrouter_records_cost(config: ClientConfig, state: ConversationState) -> None:
    meter = UsageMeter()
    llm = OpenRouterLLM("k", "m", transport=fake_openrouter(0.002), meter=meter)
    llm.compose(AskField(field=FieldName.FULL_NAME), state, config)
    llm.compose(AskField(field=FieldName.FULL_NAME), state, config)
    assert (meter.calls, meter.prompt_tokens, meter.completion_tokens) == (2, 200, 20)
    assert meter.cost_usd == pytest.approx(0.004)


def test_run_suite_stops_at_the_budget(config: ClientConfig) -> None:
    meter = UsageMeter()
    llm = OpenRouterLLM("k", "m", transport=fake_openrouter(0.01), meter=meter)
    harness = Harness(config, llm, llm, meter)
    results, complete = run_suite(harness, load_scenarios(), budget_usd=0.05)
    assert not complete
    assert 0 < len(results) < len(load_scenarios())
    assert results[0].cost_usd > 0
    assert results[0].turn_seconds


def test_percentile() -> None:
    assert percentile([], 0.5) is None
    assert percentile([3.0, 1.0, 2.0], 0.5) == 2.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.95) == 4.0


def _matrix(tmp_path: Path) -> Path:
    path = tmp_path / "matrix.yaml"
    path.write_text(
        "budget_usd: 1\n"
        "simulator_model: s/sim\n"
        "runs:\n"
        "  - {name: A, model: a/one}\n"
        "  - {name: B extract, model: b/small, responder_model: a/one}\n"
    )
    return path


def test_unknown_model_ids_stop_before_spending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    transport = fake_openrouter(models=("a/one",))
    assert compare.main(["--config", str(_matrix(tmp_path))], transport) == 2
    assert "b/small" in capsys.readouterr().err


def test_check_models_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    transport = fake_openrouter(models=("a/one", "b/small", "s/sim"))
    assert compare.main(["--config", str(_matrix(tmp_path)), "--check-models"], transport) == 0


def test_missing_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    assert compare.main(["--config", str(_matrix(tmp_path))]) == 2


def test_full_run_writes_a_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setattr(compare, "RESULTS", tmp_path / "results")
    transport = fake_openrouter(cost=0.001, models=("a/one", "b/small", "s/sim"))
    code = compare.main(
        ["--config", str(_matrix(tmp_path)), "--scenario", "no_license", "--budget-usd", "1"],
        transport,
    )
    assert code == 0
    report = next((tmp_path / "results").glob("compare-*.md")).read_text()
    assert "| A | scripted |" in report
    assert "| B extract | scripted |" in report


def test_table_marks_runs_skipped_by_the_budget() -> None:
    run = compare.Run(name="X", model="x/y")
    assert "not run: budget reached" in compare.table([(run, None)], 5.0, 5.0)
