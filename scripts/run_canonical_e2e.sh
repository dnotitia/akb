#!/usr/bin/env bash
# Run the canonical E2E suites listed in CLAUDE.md, plus the repro suite.
set -uo pipefail
cd "$(dirname "$0")/.."
export AKB_URL="${AKB_URL:-http://localhost:8001}"

SUITES=(
  test_mcp_e2e.sh
  test_edit_e2e.sh
  test_security_edge_e2e.sh
  test_graph_replace_e2e.sh
  test_defensive_e2e.sh
  test_probes_e2e.sh
  test_stdio_files_e2e.sh
  test_put_file_param_e2e.sh
  test_concurrency_repro_e2e.sh
  test_jwt_revocation_e2e.sh
)

LINES=()
TOTAL_PASS=0
TOTAL_FAIL=0
for s in "${SUITES[@]}"; do
  echo "═══ $s ═══"
  OUT=$(bash "backend/tests/$s" 2>&1)
  SUMMARY=$(echo "$OUT" | grep -E 'Results: [0-9]+ passed' | tail -1)
  echo "  $SUMMARY"
  P=$(echo "$SUMMARY" | grep -oE '[0-9]+ passed' | head -1 | grep -oE '^[0-9]+')
  F=$(echo "$SUMMARY" | grep -oE '[0-9]+ failed' | head -1 | grep -oE '^[0-9]+')
  P=${P:-0}; F=${F:-0}
  LINES+=("$(printf '  %-45s %s passed, %s failed' "$s" "$P" "$F")")
  TOTAL_PASS=$((TOTAL_PASS + P))
  TOTAL_FAIL=$((TOTAL_FAIL + F))
done

echo ""
echo "═════════════════════════════════════════"
echo "Per-suite breakdown:"
for l in "${LINES[@]}"; do echo "$l"; done
echo ""
echo "TOTAL across canonical suites: $TOTAL_PASS passed, $TOTAL_FAIL failed"
echo "═════════════════════════════════════════"
