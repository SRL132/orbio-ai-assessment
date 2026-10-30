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

import httpx

from app.adapters.config.yaml_loader import load_client_config
from app.adapters.llm.openrouter import OpenRouterLLM
from app.adapters.llm.usage import UsageMeter
from app.bootstrap import make_llm
from app.settings import Settings
from evals.runner import (
    Completer,
    Harness,
    Scenario,
    ScenarioResult,
    load_scenarios,
    percentile,
    run_suite,
)

EVALS_DIR = Path(__file__).parent
BASELINE = EVALS_DIR / "baseline.json"
RESULTS = EVALS_DIR / "results"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals", description=__doc__)
    parser.add_argument("--provider", choices=["fake", "openrouter", "anthropic"], default="fake")
    parser.add_argument("--model", default=None, help="model id/slug for the provider")
    parser.add_argument("--responder-model", help="OpenRouter: a different model writes replies")
    parser.add_argument("--simulator-model", help="OpenRouter: the model playing the candidate")
    parser.add_argument("--simulate", action="store_true", help="LLM-simulated candidates")
    parser.add_argument("--budget-usd", type=float, help="stop once this much has been spent")
    parser.add_argument("--scenario", action="append", help="run only these scenario ids")
    parser.add_argument("--check", action="store_true", help="exit 1 on regression")
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true", help="print failing transcripts")
    parser.add_argument("--export-samples", type=Path, help="write each transcript as Markdown")
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
    meter = UsageMeter()
    if args.provider == "openrouter":
        extractor, responder, simulator = openrouter_trio(
            settings, settings.openrouter_model, args.responder_model, args.simulator_model, meter
        )
    else:
        extractor, responder = make_llm(settings)
        simulator = extractor if isinstance(extractor, Completer) else None
    harness = Harness(config, extractor, responder, meter)
    key = _key(args, settings)

    def report(scenario: Scenario, result: ScenarioResult) -> None:
        _print_row(result)
        if args.export_samples:
            _export_sample(args.export_samples, scenario.description, key, result)
        if args.verbose and not result.passed:
            for direction, text in result.transcript:
                who = "BOT" if direction == "out" else "YOU"
                print(f"      {who}: {text.replace(chr(10), ' / ')}")

    results, complete = run_suite(
        harness,
        load_scenarios(args.scenario),
        simulator if args.simulate else None,
        args.budget_usd,
        report,
    )
    run = summarize(results, complete)
    print(
        f"\n{key}: {run['passed']}/{run['total']} passed · pass rate {run['pass_rate']:.0%} · "
        f"field accuracy {run['field_accuracy']:.0%} · p95 turn {run['p95_turn_s']}s · "
        f"cost ${run['cost_usd']:.4f}{'' if complete else ' · STOPPED AT BUDGET'}"
    )
    _save(key, run)

    baseline = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
    regressions = _regressions(baseline.get(key), run) if not args.scenario else []
    for line in regressions:
        print(f"REGRESSION {line}")
    if args.write_baseline:
        if not complete:
            print("not writing a baseline from an incomplete run")
            return 1
        baseline[key] = _baseline_entry(run)
        BASELINE.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n")
        print(f"baseline updated for {key}")
    if args.check and baseline.get(key) is None:
        print(f"no baseline for {key}; run with --write-baseline first")
        return 1
    return 1 if args.check and regressions else 0


def openrouter_trio(
    settings: Settings,
    model: str,
    responder_model: str | None,
    simulator_model: str | None,
    meter: UsageMeter,
    transport: httpx.BaseTransport | None = None,
) -> tuple[OpenRouterLLM, OpenRouterLLM, OpenRouterLLM]:
    """Extractor, responder and simulated candidate, all on one meter."""
    if settings.openrouter_api_key is None:
        raise SystemExit("OPENROUTER_API_KEY is not set (add it to .env)")
    key = settings.openrouter_api_key.get_secret_value()

    def llm(slug: str) -> OpenRouterLLM:
        return OpenRouterLLM(
            key, slug, timeout=settings.llm_timeout_seconds, transport=transport, meter=meter
        )

    extractor = llm(model)
    responder = llm(responder_model) if responder_model else extractor
    simulator = llm(simulator_model) if simulator_model else extractor
    return extractor, responder, simulator


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


def summarize(results: list[ScenarioResult], complete: bool = True) -> dict[str, Any]:
    total = len(results)
    passed = sum(r.passed for r in results)
    turns = [t for r in results for t in r.turn_seconds]
    cost = sum(r.cost_usd for r in results)
    p50, p95 = percentile(turns, 0.5), percentile(turns, 0.95)
    return {
        "passed": passed,
        "total": total,
        "complete": complete,
        "pass_rate": round(passed / total, 3) if total else 0.0,
        "field_accuracy": round(sum(r.field_accuracy for r in results) / total, 3)
        if total
        else 0.0,
        "seconds": round(sum(r.seconds for r in results), 1),
        "p50_turn_s": round(p50, 2) if p50 is not None else None,
        "p95_turn_s": round(p95, 2) if p95 is not None else None,
        "cost_usd": round(cost, 4),
        "cost_per_conversation_usd": round(cost / total, 5) if total else 0.0,
        "scenarios": {r.id: r.summary() for r in results},
    }


def _baseline_entry(run: dict[str, Any]) -> dict[str, Any]:
    """What the ratchet compares: quality only (latency and cost vary between runs)."""
    keep = ("passed", "total", "pass_rate", "field_accuracy", "scenarios")
    return {k: run[k] for k in keep}


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


def _export_sample(directory: Path, description: str, key: str, result: ScenarioResult) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# {result.id.replace('_', ' ').capitalize()}",
        "",
        f"{description}",
        "",
        f"Run: `{key}` · {'passed' if result.passed else 'FAILED'} · "
        f"{result.turns} candidate messages",
        "",
    ]
    for direction, text in result.transcript:
        who = "**Lucía**" if direction == "out" else "**Candidate**"
        body = text.replace("\n", "  \n")
        lines.append(f"{who}: {body}  \n")
    (directory / f"{result.id}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _save(key: str, run: dict[str, Any]) -> None:
    RESULTS.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = RESULTS / f"{stamp}-{key.replace('/', '_')}.json"
    path.write_text(json.dumps({"key": key, **run}, indent=2) + "\n")


if __name__ == "__main__":
    sys.exit(main())
