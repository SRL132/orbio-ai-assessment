# CLAUDE.md

AI candidate screening agent for Grupo Sazón (fictional restaurant chain hiring delivery drivers in Spain and Mexico), built for the Orbio FDE technical assignment. Two parts: a chat agent that screens candidates, and a recruiter dashboard to track them.

## Read first

- [`VISION.md`](VISION.md): goals, build order, open decisions
- [`docs/requirements.md`](docs/requirements.md): the original brief (verbatim, don't edit)
- [`docs/process-design.md`](docs/process-design.md): conversation design (stages, fields, validation, edge cases, outcomes, tone) + [appendix](docs/process-design-appendix.md)
- [`docs/recruiter-dashboard.md`](docs/recruiter-dashboard.md): dashboard spec
- [`docs/architecture.md`](docs/architecture.md): hexagonal core, ports and adapters, code layout, scaling
- [`docs/quality.md`](docs/quality.md): tooling, ratchets, test layers, evals

Where the vision and the process design disagree on details, the process design wins.

## Rules that must hold

- **Code decides, the LLM talks.** Qualification, knockouts and status changes are decided only in Python. The LLM extracts fields and writes messages; it never decides who qualifies or is rejected.
- **The domain is pure.** `app/domain/` imports nothing from `adapters/`, `api/`, `worker/` or any SDK (enforced by import-linter). The engine does no I/O; everything external goes through a port in `domain/ports.py`.
- **Keep the hexagon light.** Pydantic models are the domain (one mapping to SQLAlchemy, in the repository adapter). Add a port only if it has a second implementation, a fake counts. No DI framework: wire by hand in `bootstrap.py`. Dashboard reads may query projections directly; writes go through the core.
- **Rules live in config.** Service areas, license rules, allowed values, persona, templates and FAQ belong to the client config, not to Python code or prompts. Every table has `client_id`.
- **Fixed texts are templates.** Consent/privacy notice, disqualification and the final recap use templates (per language and channel), not free LLM output.
- **Traceable and private.** Every state change is an event in the append-only event log, with its reason. No personal data in application logs.

## Quality rules

- **Ratchets never go down.** Don't lower `fail_under`, remove modules from mypy strict, add `per-file-ignores` / `# noqa`, or lower `evals/baseline.json` to make a check pass. Fix the code instead, or ask.
- **When coverage rises, raise `fail_under`** in the same change.
- **Prompts are code.** Prompts live in `app/adapters/llm/prompts/`. After changing a prompt or the policy, run the evals and compare with the baseline.
- **Test the behaviour you change.** New validator or policy rule → unit test. New adapter → contract test against its fake.

## Working with the docs

When code and design disagree, don't let them drift silently: update the relevant doc in the same change, or ask first if it changes behaviour.

## Commands

```bash
uv sync                                       # install (Python 3.13, pinned in .python-version)
cp .env.example .env                          # LLM_PROVIDER=fake needs no API key
uv run uvicorn app.api.main:app --reload      # run the app
uv run ruff format . && uv run ruff check .   # format and lint
uv run mypy app                               # types (strict on app.domain)
uv run lint-imports                           # architecture boundaries
uv run pytest --cov                           # tests + coverage ratchet
uv run deptry . && uv run pip-audit           # dependency hygiene
uv run pre-commit install                     # enable the git pre-commit hooks
uv run python -m evals                        # live evals vs evals/baseline.json (not built yet; costs money)
```

Claude Code hooks (`.claude/settings.json`): Python files are formatted and lint-fixed after every edit, and pytest, mypy and import-linter run before Claude finishes a task. If they fail, Claude is sent back to fix them.
