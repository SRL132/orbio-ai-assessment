# Sazón Screening Agent

AI candidate screening agent and recruiter dashboard for Grupo Sazón, built for the Orbio FDE technical assignment.

> Work in progress. Start with [`VISION.md`](VISION.md) and the design docs in [`docs/`](docs/).

## Setup

```bash
uv sync
cp .env.example .env   # LLM_PROVIDER=fake works without any API key
uv run uvicorn app.api.main:app --reload
```

Then open http://127.0.0.1:8000/chat. To use a real model, set `LLM_PROVIDER=openrouter` with
`OPENROUTER_API_KEY` and `OPENROUTER_MODEL`, or `LLM_PROVIDER=anthropic` with `ANTHROPIC_API_KEY`.
