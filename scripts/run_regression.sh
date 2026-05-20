#!/usr/bin/env bash
set -uo pipefail
cd "$(dirname "$0")/.."
export AKB_URL="${AKB_URL:-http://localhost:8001}"

SUITES=(
  test_mcp_e2e.sh
  test_edit_e2e.sh
  test_defensive_e2e.sh
  test_graph_replace_e2e.sh
  test_probes_e2e.sh
  test_security_edge_e2e.sh
  test_concurrency_repro_e2e.sh
  test_collection_lifecycle_e2e.sh
  test_collection_hierarchy_e2e.sh
  test_events_emit_e2e.sh
  test_publications_e2e.sh
  test_self_heal_e2e.sh
  test_auth_password_e2e.sh
  test_table_crud_envelope_e2e.sh
)

TOTAL_PASS=0
TOTAL_FAIL=0
FAILED=()
for s in "${SUITES[@]}"; do
  echo "═══ $s ═══"
  OUT=$(bash "backend/tests/$s" 2>&1)
  RC=$?
  SUMMARY=$(echo "$OUT" | grep -E '^║.*Results:|^Results:' | tail -1)
  echo "$SUMMARY"
  P=$(echo "$SUMMARY" | grep -oE '[0-9]+ passed' | head -1 | grep -oE '^[0-9]+' || echo 0)
  F=$(echo "$SUMMARY" | grep -oE '[0-9]+ failed' | head -1 | grep -oE '^[0-9]+' || echo 0)
  TOTAL_PASS=$((TOTAL_PASS + ${P:-0}))
  TOTAL_FAIL=$((TOTAL_FAIL + ${F:-0}))
  if [ "$RC" != "0" ] || [ "${F:-0}" != "0" ]; then
    FAILED+=("$s ($SUMMARY)")
  fi
done

echo ""
echo "═════════════════════════════════════════"
echo "TOTAL: $TOTAL_PASS passed, $TOTAL_FAIL failed"
echo "═════════════════════════════════════════"
if [ ${#FAILED[@]} -gt 0 ]; then
  echo "FAILED SUITES:"
  for s in "${FAILED[@]}"; do
    echo "  - $s"
  done
fi
