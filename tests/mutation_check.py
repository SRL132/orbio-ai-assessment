"""Fail if more mutants survive than the baseline allows (run after `uv run mutmut run`).

Usage: uv run python -m tests.mutation_check

Reads mutmut's result files (mutants/**/*.meta): exit code 0 means the tests still passed with
the mutation in place, i.e. the mutant survived.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = Path(__file__).with_name("mutation_baseline.json")


def survivors() -> list[str]:
    found: list[str] = []
    for meta in sorted((ROOT / "mutants").rglob("*.meta")):
        codes = json.loads(meta.read_text())["exit_code_by_key"]
        found += [key for key, code in codes.items() if code == 0]
    return found


def main() -> int:
    if not (ROOT / "mutants").exists():
        print("no mutmut results: run `uv run mutmut run` first")
        return 1
    alive = survivors()
    allowed = json.loads(BASELINE.read_text())["max_survivors"]
    for key in alive:
        print(f"survived: {key}")
    print(f"{len(alive)} surviving mutants (baseline allows {allowed})")
    if len(alive) > allowed:
        print("New survivors: add a test that kills them (don't raise the baseline).")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
