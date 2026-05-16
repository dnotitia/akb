#!/usr/bin/env bash
# Static-analysis check entry point. Run locally (pre-commit) and from
# CI on every push / PR. Fails on the first violation so the diff is
# small enough to fix in one round.
#
# Adding a check? Pick the smallest tool that fits and slot it here.
# Resist adding "warnings-only" steps — they always rot into noise.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${REPO_ROOT}"

step() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }

# ─── backend: ruff (lint) ──────────────────────────────────────────
step "ruff (backend)"
ruff check backend/

# ─── backend: mypy (types) ─────────────────────────────────────────
step "mypy (backend)"
(cd backend && mypy app/ mcp_server/)

# ─── backend: bandit (security) ────────────────────────────────────
# Gate at medium severity — low-level findings on `random`, `try/except
# pass`, etc. would drown out the real signals. The pyproject [tool.bandit]
# section explains the two skipped tests (B104, B608).
step "bandit (backend)"
(cd backend && bandit -r app/ mcp_server/ -c pyproject.toml --severity-level medium -q)

# ─── frontend: eslint (lint) ──────────────────────────────────────
step "eslint (frontend)"
(cd frontend && npx --no-install eslint src)

# ─── frontend: tsc --noEmit (type) ────────────────────────────────
# `frontend/` has its own tsconfig; running tsc from inside the dir
# picks it up automatically. node_modules must already be installed —
# CI does `pnpm install --frozen-lockfile` upstream of this script.
step "tsc (frontend)"
(cd frontend && npx --no-install tsc --noEmit)

# ─── frontend: vitest (unit + RTL + MSW) ──────────────────────────
# Closes the biggest gate gap: previously a broken test could merge
# because check.sh only ran lint/type. Stage 3 (Playwright) lives
# behind `npm run test:e2e` and needs a live docker-compose stack —
# wire that into a separate e2e workflow when it's ready.
step "vitest (frontend)"
(cd frontend && npx --no-install vitest run)

# ─── secrets: detect-secrets ──────────────────────────────────────
# Catches accidental commits of API keys, JWTs, AWS credentials, etc.
# Baseline at .secrets.baseline pins the known-acceptable matches
# (placeholder tokens in docs, test fixtures, k8s manifest defaults).
# `detect-secrets-hook` exits non-zero if a tracked file contains
# a high-entropy string that isn't in the baseline.
step "detect-secrets (tracked files)"
if command -v detect-secrets-hook >/dev/null 2>&1; then
  # Scope: git-tracked files only — skips node_modules, .venv, dist, etc.
  # for free, and prevents the scan from drowning in third-party noise.
  git ls-files -z | xargs -0 detect-secrets-hook --baseline .secrets.baseline
else
  echo "  ! detect-secrets not installed — pipx install detect-secrets" >&2
  exit 1
fi

printf '\n\033[1;32mAll checks passed.\033[0m\n'
