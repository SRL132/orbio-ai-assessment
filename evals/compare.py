"""Compare models on the eval suite, under one spending cap.

    uv run python -m evals.compare --check-models     # free: are all model ids available?
    uv run python -m evals.compare                    # runs evals/compare.yaml (costs money)
    uv run python -m evals.compare --budget-usd 2 --scenario cooperative_es

Writes a Markdown table to evals/results/compare-<time>.md: pass rate, field accuracy,
p50/p95 seconds per turn and cost per conversation. Synthetic candidates only.
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from pydantic import BaseModel

from app.adapters.config.yaml_loader import load_client_config
from app.adapters.llm.fast_path import FastPathExtractor
from app.adapters.llm.usage import UsageMeter
from app.settings import Settings
from evals.__main__ import RESULTS, openrouter_trio, summarize
from evals.runner import Harness, load_scenarios, run_suite

CONFIG = Path(__file__).parent / "compare.yaml"
MODELS_URL = "https://openrouter.ai/api/v1/models"


class Run(BaseModel):
    name: str
    model: str
    responder_model: str | None = None
    simulate: bool = False
    reasoning: str | None = "low"  # OpenRouter reasoning effort: none, minimal, low, …
    cap_usd: float | None = None  # this run's spending cap; default: an equal share of the budget


class Matrix(BaseModel):
    budget_usd: float = 5.0
    simulator_model: str
    simulator_reasoning: str | None = "none"
    runs: list[Run]

    def slugs(self) -> set[str]:
        found = {self.simulator_model}
        for run in self.runs:
            found |= {run.model, *([run.responder_model] if run.responder_model else [])}
        return found


def available_models(api_key: str, transport: httpx.BaseTransport | None = None) -> set[str]:
    with httpx.Client(transport=transport, timeout=20) as client:
        response = client.get(MODELS_URL, headers={"Authorization": f"Bearer {api_key}"})
        response.raise_for_status()
        return {str(model["id"]) for model in response.json()["data"]}


def table(rows: list[tuple[Run, dict[str, Any]]], spent: float, budget: float) -> str:
    lines = [
        f"# Model comparison · {datetime.now(UTC):%Y-%m-%d %H:%M} UTC",
        "",
        f"Spent ${spent:.2f} of a ${budget:.2f} budget. Synthetic scenarios only "
        "(evals/scenarios.yaml). Latency is seconds per candidate turn, including both LLM calls.",
        "",
        "| Run | Mode | Pass rate | Field accuracy | p50 s/turn | p95 s/turn | $/conversation |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for run, summary in rows:
        mode = "simulated" if run.simulate else "scripted"
        partial = "" if summary["complete"] else " (partial)"
        lines.append(
            f"| {run.name} | {mode} | {summary['passed']}/{summary['total']}{partial} | "
            f"{summary['field_accuracy']:.0%} | {summary['p50_turn_s']} | "
            f"{summary['p95_turn_s']} | {summary['cost_per_conversation_usd']:.4f} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None, transport: httpx.BaseTransport | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.compare", description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--budget-usd", type=float, help="override the budget in the config")
    parser.add_argument("--check-models", action="store_true", help="only check the model ids")
    parser.add_argument("--scenario", action="append", help="run only these scenario ids")
    parser.add_argument("--parallel", type=int, default=1, help="runs executed side by side")
    args = parser.parse_args(argv)

    matrix = Matrix.model_validate(yaml.safe_load(args.config.read_text(encoding="utf-8")))
    budget = args.budget_usd if args.budget_usd is not None else matrix.budget_usd
    settings = Settings(llm_provider="openrouter")
    if settings.openrouter_api_key is None:
        print("OPENROUTER_API_KEY is not set (add it to .env)", file=sys.stderr)
        return 2
    missing = sorted(
        matrix.slugs() - available_models(settings.openrouter_api_key.get_secret_value(), transport)
    )
    if missing:
        print(
            "These model ids aren't available on OpenRouter: " + ", ".join(missing), file=sys.stderr
        )
        return 2
    if args.check_models:
        print(f"All {len(matrix.slugs())} model ids are available.")
        return 0

    config = load_client_config(settings.client_id)
    scenarios = load_scenarios(args.scenario)
    # One meter and one share of the budget per run: side by side, a shared meter would charge
    # each run for the others' calls, and could stop a cheap run because of an expensive one.
    meters = [UsageMeter() for _ in matrix.runs]
    share = budget / len(matrix.runs)

    def spent() -> float:
        return sum(meter.cost_usd for meter in meters)

    def execute(run: Run, meter: UsageMeter) -> tuple[Run, dict[str, Any]]:
        simulator_model = matrix.simulator_model if run.simulate else None
        extractor, responder, simulator = openrouter_trio(
            settings, run.model, run.responder_model, simulator_model, meter, transport
        )
        for llm in {id(x): x for x in (extractor, responder)}.values():
            llm.reasoning_effort = run.reasoning
        if run.simulate:
            simulator.reasoning_effort = matrix.simulator_reasoning
        harness = Harness(config, FastPathExtractor(extractor), responder, meter)
        results, complete = run_suite(
            harness, scenarios, simulator if run.simulate else None, run.cap_usd or share
        )
        summary = summarize(results, complete)
        print(f"{run.name}: {summary['passed']}/{summary['total']} · ${spent():.3f} so far")
        return run, summary

    with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
        rows: list[tuple[Run, dict[str, Any]]] = list(pool.map(execute, matrix.runs, meters))

    RESULTS.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report = table(rows, spent(), budget)
    (RESULTS / f"compare-{stamp}.md").write_text(report, encoding="utf-8")
    (RESULTS / f"compare-{stamp}.json").write_text(
        json.dumps([{"run": r.model_dump(), "summary": s} for r, s in rows], indent=2) + "\n"
    )
    print("\n" + report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
