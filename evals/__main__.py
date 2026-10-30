"""Run the eval suite.

    uv run python -m evals                                  # offline (keyword extractor), scripted
    uv run python -m evals --provider openrouter --model anthropic/claude-sonnet-5.5
    uv run python -m evals --provider anthropic --simulate  # an LLM plays each candidate
    uv run python -m evals --check                          # fail on regression vs baseline.json
    uv run python -m evals --write-baseline                 # record this run as the new baseline

Live providers cost money and must only ever see synthetic candidates (these scenarios).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.adapters.config.yaml_loader import load_client_config
from app.bootstrap import make_llm
from app.settings import Settings
from evals.runner import Completer, Harness, ScenarioResult, load_scenarios

EVALS_DIR = Path(__file__).parent
BASELINE = EVALS_DIR / "baseline.json"
RESULTS = EVALS_DIR / "results"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals", description=__doc__)
    parser.add_argument("--provider", choices=["fake", "openrouter", "anthropic"], default="fake")
    parser.add_argument("--model", default=None, help="model id/slug for the provider")
    parser.add_argument("--simulate", action="store_true", help="LLM-simulated candidates")
    parser.add_argument("--scenario", action="append", help="run only these scenario ids")
    parser.add_argument("--check", action="store_true", help="exit 1 on regression")
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true", help="print failing transcripts")
    args = parser.parse_args(argv)

    if args.simulate and args.provider == "fake":
        parser.error("--simulate needs a real provider (openrouter or anthropic)")

    overrides: dict[str, Any] = {"llm_provider": args.provider}
    if args.model:
        overrides["openrouter_model" if args.provider == "openrouter" else "anthropic_model"] = (
            args.model
        )
    settings = Settings(**overrides)
    config = load_client_config(settings.client_id)
    extractor, responder = make_llm(settings)
    harness = Harness(config, extractor, responder)
    key = _key(args, settings)

    results: list[ScenarioResult] = []
    for scenario in load_scenarios(args.scenario):
        if args.simulate:
            if any(isinstance(step, dict) for step in scenario.steps):
                continue  # waiting needs the scripted clock
            completer: Completer = extractor  # type: ignore[assignment]
            result = harness.run_simulated(scenario, completer)
        else:
            result = harness.run_scripted(scenario)
        results.append(result)
        _print_row(result)
        if args.verbose and not result.passed:
            for direction, text in result.transcript:
                who = "BOT" if direction == "out" else "YOU"
                print(f"      {who}: {text.replace(chr(10), ' / ')}")

    run = _summarize(results)
    print(
        f"\n{key}: {run['passed']}/{run['total']} passed · pass rate {run['pass_rate']:.0%} · "
        f"field accuracy {run['field_accuracy']:.0%} · {run['seconds']:.1f}s"
    )
    _save(key, run)

    baseline = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
    regressions = _regressions(baseline.get(key), run) if not args.scenario else []
    for line in regressions:
        print(f"REGRESSION {line}")
    if args.write_baseline:
        baseline[key] = run
        BASELINE.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n")
        print(f"baseline updated for {key}")
    if args.check and baseline.get(key) is None:
        print(f"no baseline for {key}; run with --write-baseline first")
        return 1
    return 1 if args.check and regressions else 0


def _key(args: argparse.Namespace, settings: Settings) -> str:
    model = {
        "fake": "keyword",
        "openrouter": settings.openrouter_model,
        "anthropic": settings.anthropic_model,
    }[args.provider]
    return f"{args.provider}/{model}/{'simulated' if args.simulate else 'scripted'}"


def _print_row(r: ScenarioResult) -> None:
    mark = "PASS" if r.passed else "FAIL"
    print(
        f"{mark}  {r.id:<26} fields {r.field_accuracy:>4.0%}  turns {r.turns:>2}  "
        f"{r.seconds:5.1f}s  {'; '.join(r.failures)}"
    )


def _summarize(results: list[ScenarioResult]) -> dict[str, Any]:
    total = len(results)
    passed = sum(r.passed for r in results)
    return {
        "passed": passed,
        "total": total,
        "pass_rate": round(passed / total, 3) if total else 0.0,
        "field_accuracy": round(sum(r.field_accuracy for r in results) / total, 3)
        if total
        else 0.0,
        "seconds": round(sum(r.seconds for r in results), 1),
        "scenarios": {r.id: r.summary() for r in results},
    }


def _regressions(baseline: dict[str, Any] | None, run: dict[str, Any]) -> list[str]:
    """The ratchet: nothing that passed may fail, and no score may drop."""
    if baseline is None:
        return []
    problems: list[str] = []
    if run["pass_rate"] < baseline["pass_rate"]:
        problems.append(f"pass rate {run['pass_rate']} < {baseline['pass_rate']}")
    for sid, before in baseline["scenarios"].items():
        now = run["scenarios"].get(sid)
        if now is None:
            problems.append(f"{sid}: missing from this run")
        elif before["passed"] and not now["passed"]:
            problems.append(f"{sid}: passed before, fails now")
        elif now["field_accuracy"] < before["field_accuracy"]:
            problems.append(
                f"{sid}: field accuracy {now['field_accuracy']} < {before['field_accuracy']}"
            )
    return problems


def _save(key: str, run: dict[str, Any]) -> None:
    RESULTS.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = RESULTS / f"{stamp}-{key.replace('/', '_')}.json"
    path.write_text(json.dumps({"key": key, **run}, indent=2) + "\n")


if __name__ == "__main__":
    sys.exit(main())
