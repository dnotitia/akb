#!/usr/bin/env bash
# Run the same curated E2E manifest used by hosted CI.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export AKB_URL="${AKB_URL:-http://localhost:8001}"
exec python3 "$REPO_ROOT/backend/scripts/ci/e2e_suite_runner.py" --repo-root "$REPO_ROOT"
