#!/usr/bin/env bash
# Local pre-push checks (no hosted CI by design): lint + unit tests, and the
# integration smoke test when the dev stack is up.
set -euo pipefail
cd "$(dirname "$0")/../backend"

echo "== ruff =="
.venv/bin/ruff check sopilot tests

echo "== unit tests =="
.venv/bin/pytest -q

if curl -sf -m 2 localhost:8100/health > /dev/null 2>&1; then
  echo "== integration smoke (API on :8100) =="
  bash ../scripts/smoke_test.sh
else
  echo "== integration smoke skipped (API not running on :8100) =="
fi
echo "ALL CHECKS DONE"
