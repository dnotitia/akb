#!/usr/bin/env bash
# Run the repository's HTTP-only CI E2E suite with the existing aggregation semantics.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../../.."

# Curated subset of CLAUDE.md's canonical list — every suite here exercises
# the HTTP/MCP surface end-to-end without needing the npm proxy or a real
# embedding provider.
#
# Deferred (need follow-up PRs):
#   - test_defensive_e2e.sh — its "search after delete" assertion relies on
#     the embedding pipeline producing unique vectors per document; our CI
#     stub returns a fixed vector for every input, so cosine similarity is
#     constant and a deleted doc still scores as a near-hit until the
#     vector_delete_outbox drains. Wire MinIO + a real embedding provider (or
#     a smarter stub) to re-enable.
#   - test_concurrency_repro_e2e.sh — T10 posts a publication body with
#     mode:"live" which the publication model now rejects as extra_forbidden
#     ("mode" is output-only since the live/snapshot rewrite). Fix the test in
#     its own PR, then re-enable.
SUITES=(
  test_probes_e2e.sh
  test_mcp_e2e.sh
  test_edit_e2e.sh
  test_security_edge_e2e.sh
  test_pg_rbac_e2e.sh
  test_graph_replace_e2e.sh
  test_relations_rest_e2e.sh
  test_collection_lifecycle_e2e.sh
  test_history_rest_e2e.sh
  test_jwt_revocation_e2e.sh
  test_table_constraints_e2e.sh
  test_forbidden_permission_code_e2e.sh
  test_okf_export_import_e2e.sh
)

# The GitHub workflow also provides MinIO for the publication suites.  The
# PostgreSQL-only Apple VM profile deliberately leaves this unset, so it
# keeps the original suite set without pretending S3 is available.
if [ -n "${AKB_E2E_S3_ENDPOINT:-}" ]; then
  SUITES+=(
    test_publication_resolution_e2e.sh
    test_publications_e2e.sh
  )
fi

TOTAL_PASS=0
TOTAL_FAIL=0
FAILED=()
for s in "${SUITES[@]}"; do
  echo "::group::$s"
  OUT=$(bash "backend/tests/$s" 2>&1) && RC=0 || RC=$?
  echo "$OUT" | tail -80
  # Match both summary styles: some suites prefix with a box-drawing
  # "║ Results:" line, others use bare "Results:" with leading whitespace.
  SUMMARY=$(echo "$OUT" | grep -E '(║.*)?Results: *[0-9]+ passed' | tail -1)
  P=$(echo "$SUMMARY" | grep -oE '[0-9]+ passed' | head -1 | grep -oE '^[0-9]+' || echo 0)
  F=$(echo "$SUMMARY" | grep -oE '[0-9]+ failed' | head -1 | grep -oE '^[0-9]+' || echo 0)
  TOTAL_PASS=$((TOTAL_PASS + ${P:-0}))
  TOTAL_FAIL=$((TOTAL_FAIL + ${F:-0}))
  echo "::endgroup::"
  echo "▸ $s: ${P:-0} passed, ${F:-0} failed (rc=$RC)"
  if [ "$RC" != "0" ] || [ "${F:-0}" != "0" ]; then
    FAILED+=("$s")
  fi
done

echo ""
echo "═════════════════════════════════════════"
echo "TOTAL: $TOTAL_PASS passed, $TOTAL_FAIL failed"
echo "═════════════════════════════════════════"
if [ ${#FAILED[@]} -gt 0 ]; then
  echo "FAILED SUITES:"
  for s in "${FAILED[@]}"; do echo "  - $s"; done
  exit 1
fi
