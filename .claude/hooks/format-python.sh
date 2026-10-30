#!/usr/bin/env bash
# PostToolUse (Edit|Write): format and lint-fix the edited file if it is Python.
set -euo pipefail
f=$(jq -r '.tool_response.filePath // .tool_input.file_path // empty')
[[ "$f" == *.py ]] || exit 0
cd "${CLAUDE_PROJECT_DIR:-.}"
uv run ruff format --quiet "$f"
uv run ruff check --fix --quiet "$f"
