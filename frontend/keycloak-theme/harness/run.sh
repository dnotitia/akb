#!/usr/bin/env bash
# Render the AKB login theme in a real Keycloak and check it in a browser.
#
#   keycloak-theme/harness/run.sh            # assemble, start, seed, test, stop
#   keycloak-theme/harness/run.sh --keep     # leave Keycloak running afterwards
#   keycloak-theme/harness/run.sh --up       # assemble, start, seed; no tests
#   keycloak-theme/harness/run.sh --down     # stop a kept instance
#
# Extra arguments after `--` go to Playwright. Needs docker and the frontend's
# node_modules (`pnpm install`); the browser is Playwright's own pinned build
# (`pnpm exec playwright install chromium`). AKB_THEME_SCREENSHOTS=<dir> keeps
# a screenshot of every page in every scheme and width for a human pass.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
frontend="$(cd "$here/../.." && pwd)"
project="akb-kc-theme-harness"
base="${AKB_THEME_KEYCLOAK:-http://localhost:18480}"
export AKB_THEME_ROOT="$frontend/dist-keycloak-theme"
export AKB_THEME_KEYCLOAK="$base"

compose() { docker compose -p "$project" -f "$here/compose.yaml" "$@"; }

mode=test
passthrough=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep) mode=keep ;;
    --up) mode=up ;;
    --down) compose down -v --remove-orphans; exit 0 ;;
    --) shift; passthrough=("$@"); break ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

node "$frontend/keycloak-theme/scripts/assemble.mjs"
# The originals this theme replaces must still be the ones it was written
# against; a Keycloak bump that moved one fails here, before any browser.
node "$frontend/keycloak-theme/scripts/upstream-hashes.mjs"

compose down -v --remove-orphans >/dev/null 2>&1 || true
compose up -d
if [[ "$mode" == test ]]; then
  trap 'compose down -v --remove-orphans >/dev/null 2>&1 || true' EXIT
fi

for _ in $(seq 1 90); do
  if curl -fsS -o /dev/null "$base/realms/master" 2>/dev/null; then break; fi
  sleep 2
done
curl -fsS -o /dev/null "$base/realms/master" || { compose logs --tail 80; exit 1; }

node "$here/seed.mjs"

if [[ "$mode" == up ]]; then
  echo "Keycloak is up at $base (realm akb). Stop it with $0 --down."
  exit 0
fi

status=0
(cd "$frontend" && pnpm exec playwright test -c keycloak-theme/harness/playwright.config.ts "${passthrough[@]}") || status=$?
if [[ "$status" -ne 0 ]]; then
  compose logs --tail 200 keycloak | grep -E 'ERROR|FreeMarker|Template' | tail -40 || true
fi
exit "$status"
