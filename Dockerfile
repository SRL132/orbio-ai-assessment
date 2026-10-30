# syntax=docker/dockerfile:1
# Build: dependencies first (cached), then the project. Runtime: slim image, non-root user.

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev

FROM python:3.13-slim-bookworm
RUN useradd --create-home --uid 1000 app && mkdir -p /app/data && chown app:app /app/data
WORKDIR /app
COPY --from=builder --chown=app:app /app /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    DATABASE_URL=sqlite:////app/data/sazon.db \
    DEMO_MODE=false
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"
# Migrations run before the server starts (and are a no-op when already up to date).
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn --factory app.api.main:create_app --host 0.0.0.0 --port 8000"]
