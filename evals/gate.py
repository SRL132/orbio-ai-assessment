"""Have these prompts and this client config been measured by a live run?

    uv run python -m evals.gate                      # free: exit 1 if they haven't
    uv run python -m evals.gate --run --budget-usd 2 # costs money: run the live suite if needed

Live runs need an API key and cost money, so CI can't do this. The free check only reads
evals/history.jsonl, which is why a live run's new line must be committed with the change.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from evals.__main__ import BASELINE, HISTORY, behaviour_fingerprint
from evals.__main__ import main as run_evals

PROVIDER = "openrouter"
DEFAULT_MODEL = "openai/gpt-5.6-terra"
FULL_SUITE = "scenarios.yaml"


def measured(
    history: list[dict[str, Any]], key: str, fingerprint: str, baseline: dict[str, Any]
) -> bool:
    """True if a complete run of the full suite on this fingerprint kept everything that the
    baseline says passes."""
    must_pass = {
        name for name, scenario in baseline.get("scenarios", {}).items() if scenario["passed"]
    }
    return any(
        entry["key"] == key
        and entry["fingerprint"] == fingerprint
        and entry["scenarios_file"] == FULL_SUITE
        and entry["complete"]
        and not must_pass & set(entry["failed"])
        for entry in history
    )


def read_history(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def main(
    argv: list[str] | None = None,
    run_live: Callable[[list[str]], int] = run_evals,
    history_path: Path = HISTORY,
    baseline_path: Path = BASELINE,
) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.gate", description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL, help="OpenRouter model slug")
    parser.add_argument("--run", action="store_true", help="run the live suite if not measured")
    parser.add_argument("--budget-usd", type=float, help="spending cap, required with --run")
    args = parser.parse_args(argv)
    if args.run and (args.budget_usd is None or args.budget_usd <= 0):
        parser.error("--run costs money: give a positive --budget-usd")

    key = f"{PROVIDER}/{args.model}/scripted"
    fingerprint = behaviour_fingerprint()
    baseline = json.loads(baseline_path.read_text()).get(key, {})

    def is_measured() -> bool:
        return measured(read_history(history_path), key, fingerprint, baseline)

    if is_measured():
        print(f"measured: {key} has a clean live run on {fingerprint}")
        return 0
    print(f"NOT MEASURED: {key} has no clean live run on {fingerprint}")
    if not args.run:
        print("run it with: uv run python -m evals.gate --run --budget-usd 2")
        return 1
    status = run_live(
        ["--provider", PROVIDER, "--model", args.model, "--budget-usd", str(args.budget_usd)]
    )
    if status != 0 or not is_measured():
        print("the live run didn't come back clean: see the failures above")
        return 1
    print(f"measured: commit {history_path.name} with this change")
    return 0


if __name__ == "__main__":
    sys.exit(main())
