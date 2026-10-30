#!/usr/bin/env bash
# Stop: run tests, types and architecture checks. On failure, block stopping and
# send the output back so Claude keeps working (see docs/quality.md).
set -uo pipefail
input=$(cat)
# Already continuing because of this hook: don't block again, to avoid a loop.
if [[ "$(jq -r '.stop_hook_active // false' <<<"$input")" == "true" ]]; then
  exit 0
fi
cd "${CLAUDE_PROJECT_DIR:-.}"
out=""
fail=0
for cmd in "uv run pytest -q" "uv run mypy app" "uv run lint-imports --no-cache"; do
  res=$($cmd 2>&1) || { fail=1; out+=$'\n'"\$ $cmd"$'\n'"$(tail -n 40 <<<"$res")"$'\n'; }
done
if [[ $fail -eq 1 ]]; then
  jq -n --arg r "Quality checks failed. Fix them before finishing:$out" '{decision: "block", reason: $r}'
fi
exit 0
