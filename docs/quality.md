# Code Quality

> How we keep quality high when most of the code is written with AI. Enforced at three points: on every edit (Claude Code hooks), on every commit (pre-commit), and in CI. All three run the same commands. Architecture: [`architecture.md`](architecture.md).

## Tooling

| Concern | Tool | Notes |
| --- | --- | --- |
| Environment and dependencies | **uv** | One lockfile; `uv run` for everything |
| Lint and format | **ruff** | Replaces flake8, isort and black |
| Types | **mypy** | `--strict` on `app/domain/`, looser elsewhere; Pydantic plugin |
| Architecture boundaries | **import-linter** | `domain/` imports nothing from adapters, api, worker or SDKs |
| Unit tests | **pytest** + **hypothesis** | Property tests for parsers and validators, and for the engine: Hypothesis plays an arbitrary (even adversarial) extractor for whole conversations and checks the code-owned rules after every turn (only complete, unflagged candidates qualify; a final status never reopens; every message gets an answer) |
| Coverage | **pytest-cov** | Ratcheted; ~90% target on `domain/`, no target on adapters |
| LLM-dependent tests | Fake adapters + recorded responses | Deterministic and free in CI |
| Evals | Own harness + `evals/baseline.json` | Live LLM; run on demand and required for prompt or policy changes |
| Secrets | **gitleaks** | pre-commit and CI |
| Dependencies | **pip-audit**, **deptry** | Known vulnerabilities; unused or missing dependencies |
| Hooks | **pre-commit** | ruff, mypy, import-linter, gitleaks |
| CI | GitHub Actions | Same commands as locally, nothing CI-only |

## Ratchets: the bar only goes up

1. **Coverage.** `fail_under` starts at the current value. When coverage rises, raise it in the same change. Never lower it.
2. **Typing.** `domain/` is strict from day one. Modules get added to the strict list one by one; none are removed.
3. **Lint ignores.** `per-file-ignores` and `# noqa` are a debt list: they may shrink, never grow.
4. **Eval baseline.** `evals/baseline.json` stores per-scenario metrics: extraction accuracy, knockout correctness, turns to complete, guardrail passes. A prompt or policy change must match or beat it. Raising the baseline is an explicit, reviewed change. This is the ratchet that protects *behaviour*, not just code.

## Test layers

| Layer | Speed / cost | When | What |
| --- | --- | --- | --- |
| Unit | ms | every edit | Validators, policy, engine with fake `Extractor` / `Responder` |
| Contract | fast | every commit | Each adapter against its port: the fake and the real one behave alike |
| Recorded | fast | CI | Real LLM responses recorded once, replayed (VCR-style) |
| Smoke | fast | CI | App boots, chat endpoint answers, dashboard renders |
| Evals | slow, costs money | on demand; required when prompts or policy change | Simulated candidates (cooperative, vague, language-switcher, goes silent, abusive, multiple answers at once) against the full engine with a live LLM |

Two rules:

- **Prompts are code.** They live as versioned files in `app/adapters/llm/prompts/`. Changing one means re-running the live evals before merging. CI has no API key, so this is a review rule, not a CI gate; CI runs the offline suite on every push.
- **Templates are snapshot-tested.** Consent, disqualification and recap texts are legal-ish, so any change shows up as a visible diff.

## Running evals

The suite lives in `evals/scenarios.yaml`: 16 synthetic candidates covering the edge cases in the process design. Each scenario has scripted messages, a persona for LLM-simulated runs, and the expected outcome.

```bash
uv run python -m evals                       # offline: keyword extractor, scripted (free, in CI)
uv run python -m evals --provider openrouter --model <slug>      # live, scripted messages
uv run python -m evals --provider anthropic --simulate           # an LLM plays each candidate
uv run python -m evals --check               # exit 1 on regression vs evals/baseline.json
uv run python -m evals --write-baseline      # record this run (an explicit, reviewed change)
uv run python -m evals -v --scenario abusive # show the transcript of failures
uv run python -m evals.compare --check-models   # free: are the model ids in evals/compare.yaml available?
uv run python -m evals.compare                  # the provider comparison, capped at budget_usd
```

Live runs record cost (from OpenRouter's `usage.cost`) and seconds per candidate turn. `--budget-usd` stops a run at a spending cap, and an incomplete run can't become a baseline. The comparison writes a Markdown table to `evals/results/compare-<time>.md` with pass rate, field accuracy, p50/p95 seconds per turn and cost per conversation.

Scores per scenario: outcome correct, field accuracy against the expected record, number of turns, and the output check on every agent message. `evals/baseline.json` holds one entry per provider/model/mode. The offline entry passing says the engine and policy are right; it says little about extraction quality, because the scripted messages were written so simple keyword rules can parse them. Live entries are the real measure and are added when a key is available.

## Claude Code hooks

- **After a file edit:** `ruff format` + `ruff check --fix` on the changed file.
- **Before finishing a task:** unit tests, `mypy` on `domain/`, `lint-imports`. On failure, Claude keeps working instead of handing back broken code.

Configured in `.claude/settings.json` once the skeleton exists.

## Starting configuration (`pyproject.toml` excerpt)

```toml
[tool.ruff]
line-length = 100
[tool.ruff.lint]
select = ["E","F","I","B","UP","SIM","RUF","PT","S","ANN","TID"]
[tool.ruff.lint.per-file-ignores]
"tests/**" = ["S101","ANN"]

[tool.mypy]
plugins = ["pydantic.mypy"]
[[tool.mypy.overrides]]
module = "app.domain.*"
strict = true

[tool.importlinter]
root_package = "app"
[[tool.importlinter.contracts]]
name = "Domain is pure"
type = "forbidden"
source_modules = ["app.domain"]
forbidden_modules = ["app.adapters","app.api","app.worker","sqlalchemy","fastapi","httpx","anthropic"]

[tool.coverage.report]
fail_under = 0   # ratchet: raise with every change, never lower
```

The `anthropic` entry assumes that SDK; replace it with whichever LLM SDK is chosen.

## Deliberately skipped

Mutation testing (too slow for the timeline), 100% coverage targets (they reward testing adapters over behaviour), SonarQube-style platforms (ruff + mypy + import-linter cover the useful part), and commit-message linting (low value solo).
