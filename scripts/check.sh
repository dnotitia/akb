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

# ─── preflight: this gate's own prerequisites ─────────────────────
# Both classes below used to fail as something other than "you are missing
# a prerequisite", which is the most expensive way for a gate to be wrong.
step "preflight (analyzer parsers, node deps)"

PREFLIGHT_TMP="$(mktemp -d)"
trap 'rm -rf "${PREFLIGHT_TMP}"' EXIT

# 1. The Python analyzers must be able to PARSE this repo.
#
# mypy and bandit both build their AST with the interpreter they are running
# on, so the `--python-version 3.14` passed to mypy below selects typing
# semantics — it is not a parser selector. An analyzer installed against an
# older interpreter therefore cannot read a repo that uses newer syntax, and
# the two fail in opposite, equally unhelpful ways:
#
#   mypy    stops at the first unparseable file with a `[syntax]` error that
#           reads like a defect in the source. "errors prevented further
#           checking" is the important half: on Python 3.13 that was 1 file
#           reported and the other 316 never examined.
#   bandit  skips the file, lists it under "Files skipped", and exits 0.
#           `-q` suppressed that list, so the security scan reported success
#           having silently skipped 11 files — among them auth_service,
#           admin_auth_service, keycloak_oidc, local_session_keys and
#           sso_browser_session_crypto. Exactly the surface it exists for.
#
# CI gets this right by construction (setup-python, then pip install into
# it). Nothing asserted it locally, so whichever interpreter a contributor's
# `mypy` happened to be installed against silently decided how much of the
# repo got analysed. Assert it here instead, before any analyzer runs, so
# the gate names the interpreter rather than blaming the source.
#
# The canary is the newest syntax this repo actually relies on. It is a
# behavioural assertion on purpose: a version-string comparison would still
# pass for a tool that cannot parse us, and this one cannot.
cat >"${PREFLIGHT_TMP}/canary.py" <<'CANARY'
def _canary() -> None:
    try:
        pass
    except ValueError, TypeError:  # PEP 758 — Python 3.14
        pass
CANARY

REQUIRED_PYTHON="$(sed -n 's/^requires-python *= *">=\([0-9.]*\)".*/\1/p' backend/pyproject.toml)"
: "${REQUIRED_PYTHON:=3.14}"

# Best effort, for the error message only: a pip/uv/pipx console script names
# its interpreter in its shebang.
analyzer_interpreter() {
  local bin shebang
  bin="$(command -v "$1" 2>/dev/null)" || return 0
  shebang="$(head -c 256 "$bin" 2>/dev/null | sed -n '1s/^#!//p')" || return 0
  [ -n "$shebang" ] || return 0
  "${shebang%% *}" -c 'import sys; print("%d.%d.%d at %s" % (sys.version_info[:3] + (sys.executable,)))' 2>/dev/null
}

preflight_parser_fail() {
  local tool="$1" running
  running="$(analyzer_interpreter "$tool")"
  echo >&2
  echo "  ✗ ${tool} cannot parse Python ${REQUIRED_PYTHON} syntax." >&2
  echo >&2
  echo "    backend/pyproject.toml declares requires-python = \">=${REQUIRED_PYTHON}\" and this" >&2
  echo "    repo uses syntax older interpreters cannot parse. ${tool} parses with the" >&2
  echo "    interpreter it runs on, so --python-version does not help." >&2
  echo "    Running on: ${running:-could not determine}" >&2
  echo >&2
  echo "    Reinstall it against Python ${REQUIRED_PYTHON} — the versions CI pins are in" >&2
  echo "    .github/workflows/check.yml and backend/pyproject.toml [dev]:" >&2
  echo "      uv tool install --python ${REQUIRED_PYTHON} --force mypy==2.1.0" >&2
  echo "      uv tool install --python ${REQUIRED_PYTHON} --force 'bandit[toml]==1.9.4'" >&2
  echo >&2
  echo "    Refusing to run: on the wrong interpreter mypy examines one file and" >&2
  echo "    bandit exits 0 having read nothing." >&2
  exit 1
}

mypy --python-version "${REQUIRED_PYTHON}" --no-error-summary \
  "${PREFLIGHT_TMP}/canary.py" >/dev/null 2>&1 || preflight_parser_fail mypy

# bandit reports an unreadable file as a skip and still exits 0, so its
# canary has to be read from the report, not the exit code.
if ! bandit "${PREFLIGHT_TMP}/canary.py" 2>&1 | grep -q '^Files skipped (0):'; then
  preflight_parser_fail bandit
fi

echo "  mypy + bandit parse Python ${REQUIRED_PYTHON}"

# 2. Node deps must be installed in every node project this gate runs in.
#
# There are three, they do not share a package manager, and each has its own
# lockfile and its own node_modules: frontend/ and packages/akb-client/ are
# pnpm, packages/akb-mcp-client/ is npm. Installing only some of them dies
# several steps later, inside one of the others, as something that names
# neither the package nor the missing install:
#
#     undefined
#      ERR_PNPM_RECURSIVE_EXEC_FIRST_FAIL  Command "vitest" not found
#     Error [ERR_MODULE_NOT_FOUND]: Cannot find package '@modelcontextprotocol/client'
#
# — which has already been misread once. Worse, the steps before it can PASS
# out of a global tsc on PATH, so the run appears to get further than it did
# and to have typechecked against a compiler nobody pinned.
#
# The list is DERIVED from the committed lockfiles, not restated. It used to be
# restated — two entries and a hardcoded "2" — and it went stale the moment a
# third project arrived: the preflight announced "2 of the 2 pnpm projects this
# gate runs in" while the gate ran in three, so the one it did not know about
# failed as ERR_MODULE_NOT_FOUND minutes later, which is precisely the failure
# this preflight exists to prevent. A hand-maintained list reports the wrong
# number confidently, and a preflight that is confidently wrong is worse than
# none.
#
# A committed lockfile is this repository's own statement that the directory is
# an installed node project. If one is ever added that this gate does not step
# into, this will ask for an install it does not strictly need — a cheap wrong,
# and the opposite of the one it replaces. CI installs each explicitly
# (.github/workflows/check.yml); nothing else said so, so a fresh clone could
# not run this script. Say it here and in CONTRIBUTING.md.
node_install_command() {   # $1 = project directory
  if [ -f "$1/pnpm-lock.yaml" ]; then
    printf '(cd %s && pnpm install --frozen-lockfile)' "$1"
  else
    printf '(cd %s && npm ci)' "$1"
  fi
}

node_projects=()
while IFS= read -r lockfile; do
  node_projects+=("$(dirname "${lockfile}")")
done < <(git ls-files | grep -E '(^|/)(pnpm-lock\.yaml|package-lock\.json)$' | sort)

if [ "${#node_projects[@]}" -eq 0 ]; then
  echo "  ✗ found no committed node lockfiles — this preflight is measuring the wrong tree" >&2
  exit 1
fi

missing_installs=()
for node_project in "${node_projects[@]}"; do
  [ -d "${node_project}/node_modules" ] || missing_installs+=("${node_project}")
done
if [ "${#missing_installs[@]}" -ne 0 ]; then
  echo >&2
  echo "  ✗ node_modules missing in ${#missing_installs[@]} of the ${#node_projects[@]} node projects this gate runs in:" >&2
  for node_project in "${missing_installs[@]}"; do
    echo "      ${node_project}" >&2
  done
  echo >&2
  echo "    All of them are required — separate projects, separate lockfiles," >&2
  echo "    and not all the same package manager:" >&2
  for node_project in "${missing_installs[@]}"; do
    echo "      $(node_install_command "${node_project}")" >&2
  done
  echo >&2
  echo "    Refusing to run: without them eslint/tsc/vitest/node either fail without" >&2
  echo "    naming their package or silently resolve to a global toolchain." >&2
  exit 1
fi
echo "  node deps present in frontend + packages/akb-client"

# ─── E2E suite manifest ───────────────────────────────────────────
# Fails fast when a new shell E2E suite is neither run by the hosted gate nor
# deliberately deferred with a reviewed reason.
step "E2E suite manifest"
python backend/scripts/ci/e2e_suite_runner.py --check-manifest --repo-root "${REPO_ROOT}"

# ─── backend: ruff (lint) ──────────────────────────────────────────
step "ruff (backend)"
ruff check backend/

# ─── backend: mypy (types) ─────────────────────────────────────────
# `--python-version 3.14` matches pyproject.toml's `requires-python` and the
# CI runner's `actions/setup-python` pin, so the typing semantics analysed
# here are the ones we ship against — third-party stubs can otherwise
# disagree between runtimes, which is the bug shape we hit in 0.6.4 (local:
# 18 errors, CI: green).
#
# It does NOT make this step independent of the active interpreter, and the
# comment that used to claim it did is why that went unnoticed for so long:
# mypy parses with the running interpreter's `ast`, so on an older one this
# flag is accepted and the file still fails to parse. The preflight above is
# what actually pins the parser; this flag pins the type system.
step "mypy (backend)"
(cd backend && mypy --python-version 3.14 app/ mcp_server/)

# ─── backend: bandit (security) ────────────────────────────────────
# Gate at medium severity — low-level findings on `random`, `try/except
# pass`, etc. would drown out the real signals. The pyproject [tool.bandit]
# section explains the two skipped tests (B104, B608).
#
# Run without `-q` and assert the skip list is empty, rather than trusting
# the exit code. bandit exits 0 for a file it could not read, so "found no
# issues" and "read no files" are indistinguishable by status alone — and
# `-q` suppressed the "Files skipped" line that was the only evidence. The
# preflight above removes the syntax cause specifically; this removes the
# whole class, because a file skipped for permissions or encoding is just
# as unscanned. Any skip is a failure here: unscanned is not clean.
step "bandit (backend)"
bandit_report="$(cd backend && bandit -r app/ mcp_server/ -c pyproject.toml --severity-level medium 2>&1)" || {
  printf '%s\n' "${bandit_report}" >&2
  exit 1
}
bandit_skipped="$(printf '%s\n' "${bandit_report}" | sed -n 's/^Files skipped (\([0-9]*\)):.*/\1/p')"
if [ "${bandit_skipped:-0}" != "0" ]; then
  printf '%s\n' "${bandit_report}" >&2
  echo >&2
  echo "  ✗ bandit skipped ${bandit_skipped} file(s) and still exited 0 — listed above." >&2
  echo "    A skipped file is unscanned, not clean." >&2
  exit 1
fi
echo "  0 files skipped,$(printf '%s\n' "${bandit_report}" | sed -n 's/^[[:space:]]*Total lines of code:\(.*\)/\1/p') lines scanned"

# ─── frontend: eslint (lint) ──────────────────────────────────────
step "eslint (frontend)"
(cd frontend && npx --no-install eslint src)

# ─── frontend: tsc --noEmit (type) ────────────────────────────────
# `frontend/` has its own tsconfig; running tsc from inside the dir
# picks it up automatically. node_modules must already be installed —
# CI does `pnpm install --frozen-lockfile` upstream of this script.
step "tsc (frontend)"
(cd frontend && npx --no-install tsc --noEmit)

# ─── @akb/client: build + typecheck + vitest + generated-type drift ──
# `packages/akb-client` now carries its own pnpm install, tsc, and vitest —
# it no longer borrows frontend's tsc binary. `pnpm run build` emits dist/,
# which both the drift-check scripts and the published package consume, so
# it must run before vitest (whose codegen guards spawn against dist/) and
# before the drift check.
step "build (@akb/client)"
(cd packages/akb-client && pnpm run build)

step "typecheck (@akb/client)"
(cd packages/akb-client && pnpm run typecheck)

step "vitest (@akb/client)"
(cd packages/akb-client && pnpm exec vitest run)

step "generated type drift (@akb/client)"
(cd packages/akb-client && pnpm run codegen:check)

step "packed SDK consumer proof (@akb/client)"
(cd packages/akb-client && pnpm run proof:packed)

# ─── stdio proxy + MCP Inspector developer contract ──────────────
# The package owns its exact Inspector devDependency, command, and focused
# redaction/cleanup regression. The live HTTP+stdio smoke is invoked by the
# existing isolated E2E runtime gate.
step "stdio proxy + MCP Inspector contract"
(cd packages/akb-mcp-client && npm test)

# ─── frontend: vitest (unit + RTL + MSW) ──────────────────────────
# Closes the biggest gate gap: previously a broken test could merge
# because check.sh only ran lint/type. Stage 3 (Playwright) lives
# behind `npm run test:e2e` and needs a live docker-compose stack —
# wire that into a separate e2e workflow when it's ready.
step "vitest (frontend)"
(cd frontend && pnpm run test)

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
  # The generated MSW worker also carries an integrity checksum.
  # Both pnpm-lock.yaml files are excluded because package integrity hashes
  # (sha512-… base64) are expected high-entropy data, not secrets.
  git ls-files -z -- . ':!frontend/pnpm-lock.yaml' ':!packages/akb-client/pnpm-lock.yaml' ':!frontend/.storybook/public/mockServiceWorker.js' |
    xargs -0 detect-secrets-hook --baseline .secrets.baseline
else
  echo "  ! detect-secrets not installed — pipx install detect-secrets" >&2
  exit 1
fi

printf '\n\033[1;32mAll checks passed.\033[0m\n'
