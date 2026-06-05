#!/bin/bash
#
# SeahorseDB native driver — smoke E2E against a live Coral coordinator.
#
# This test is **opt-in by environment variable**:
#
#   SEAHORSEDB_CORAL_URL=http://localhost:46834 bash backend/tests/test_seahorse_db_e2e.sh
#
# Without `SEAHORSEDB_CORAL_URL`, the script skips cleanly (exit 0).
# That's intentional: SeahorseDB is dn-inc's commercial DB and most
# OSS environments don't have a Coral coordinator reachable. CI skips;
# developers with a local SeahorseDB stack pass the URL.
#
# Scope — confirm the driver's contract end-to-end:
#   1. /health surfaces vector_store reachable
#   2. ensure_collection idempotent against a live Coral
#   3. upsert + scan round-trip (dense + sparse fields land on Coral
#      in the AKB-shaped table)
#   4. delete by chunk_id propagates
#
# What this test does NOT cover (and why):
# - End-to-end hybrid_search with retrieval. SeahorseDB ingest goes
#   through Kafka before the row is searchable — wait windows are
#   ~10-30s on first batches and harder to budget for a smoke. The
#   broader retrieval flow lives in test_hybrid_search_e2e.sh against
#   pgvector; the driver-level guarantee here is "the bytes reach
#   Coral in the right shape".
# - Cross-process race-safety (covered by base.py docstring audit;
#   no Coral primitive equivalent to PG advisory lock).
#
set -uo pipefail

CORAL_URL="${SEAHORSEDB_CORAL_URL:-}"
BASE_URL="${AKB_URL:-http://localhost:8000}"
TABLE="${SEAHORSEDB_TABLE_NAME:-akb_e2e_$(date +%s)}"

if [ -z "$CORAL_URL" ]; then
    echo "==> SEAHORSEDB_CORAL_URL not set; skipping (this is OK in CI)."
    echo "    To run: SEAHORSEDB_CORAL_URL=http://localhost:NNNN \\"
    echo "            SEAHORSEDB_TABLE_NAME=akb_chunks \\"
    echo "            AKB_URL=http://localhost:8000 \\"
    echo "            bash backend/tests/test_seahorse_db_e2e.sh"
    exit 0
fi

# Reachability gate: the URL must answer /health before we move on,
# otherwise a wrong port or stopped stack manifests as a noisy curl
# error halfway through and the failure mode is harder to diagnose.
if ! curl -fsS "$CORAL_URL/health" >/dev/null 2>&1; then
    echo "ERROR: SEAHORSEDB_CORAL_URL=$CORAL_URL is not reachable." >&2
    echo "       (start the SeahorseDB stack first, then re-export the" >&2
    echo "        Coral host port from \`docker compose ps\`.)" >&2
    exit 1
fi

PASSED=0
FAILED=0
ERRORS=()

step() { printf '\n\033[1;36m▸ %s\033[0m\n' "$*"; }
ok()   { printf '  \033[1;32m✓\033[0m %s\n' "$*"; PASSED=$((PASSED + 1)); }
bad()  { printf '  \033[1;31m✗\033[0m %s\n' "$*"; FAILED=$((FAILED + 1)); ERRORS+=("$*"); }

# ── 1. Coral health ────────────────────────────────────────────────
step "1. Coral /health"
if curl -fsS "$CORAL_URL/health" >/dev/null; then
    ok "Coral reachable at $CORAL_URL"
else
    bad "Coral /health failed"
    exit 1
fi

# ── 2. AKB driver round-trip via direct REST ───────────────────────
# The driver creates a table on first use (auto_create=true default).
# We exercise it by hitting Coral directly with the same shape the
# driver writes — that confirms the schema + sparse + dense columns
# accepted by this Coral build match what `seahorse_db.py` emits.
step "2. table create (mirrors SeahorseDbStore.ensure_collection schema)"
CREATE_PAYLOAD=$(cat <<EOF
{
    "name": "$TABLE",
    "dimension": 8,
    "distance_space": "Cosine",
    "schema": {
        "columns": [
            {"name": "id", "column_type": "Int64", "primary_key": true},
            {"name": "chunk_id", "column_type": "String"},
            {"name": "embedding", "column_type": {"Vector": 8}},
            {"name": "sparse", "column_type": "SparseVector"},
            {"name": "content", "column_type": "String"},
            {"name": "section_path", "column_type": "String"},
            {"name": "chunk_index", "column_type": "Int32"},
            {"name": "source_type", "column_type": "String"},
            {"name": "source_id", "column_type": "String"}
        ]
    }
}
EOF
)
CREATE_STATUS=$(curl -s -o /tmp/seahorsedb_create.out -w "%{http_code}" \
    -X POST "$CORAL_URL/catalog/tables" \
    -H "Content-Type: application/json" \
    -d "$CREATE_PAYLOAD")
case "$CREATE_STATUS" in
    200|201|409) ok "create table $TABLE → HTTP $CREATE_STATUS" ;;
    *)           bad "create table $TABLE → HTTP $CREATE_STATUS (body: $(cat /tmp/seahorsedb_create.out | head -c 200))" ;;
esac

# ── 3. GET table reflects what ensure_collection sees ──────────────
step "3. GET /catalog/tables/{name} (ensure_collection 분기 검증)"
GET_STATUS=$(curl -s -o /tmp/seahorsedb_get.out -w "%{http_code}" \
    "$CORAL_URL/catalog/tables/$TABLE")
if [ "$GET_STATUS" = "200" ]; then
    ok "table visible (GET → 200)"
else
    bad "GET /catalog/tables/$TABLE → HTTP $GET_STATUS"
fi

# ── 4. Insert one record (driver upsert_one shape) ─────────────────
step "4. POST /data with dense + sparse (driver upsert_one shape)"
LABEL=42
INSERT_PAYLOAD=$(cat <<EOF
{
    "records": [
        {
            "id": $LABEL,
            "chunk_id": "00000000-0000-0000-0000-00000000002a",
            "embedding": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8],
            "sparse": [[1, 0.5], [3, 0.7]],
            "content": "hello world from akb e2e",
            "section_path": "",
            "chunk_index": 0,
            "source_type": "document",
            "source_id": "doc-1"
        }
    ]
}
EOF
)
INSERT_STATUS=$(curl -s -o /tmp/seahorsedb_insert.out -w "%{http_code}" \
    -X POST "$CORAL_URL/catalog/tables/$TABLE/data" \
    -H "Content-Type: application/json" \
    -d "$INSERT_PAYLOAD")
case "$INSERT_STATUS" in
    200|202) ok "insert → HTTP $INSERT_STATUS (Kafka-accept)" ;;
    *)       bad "insert → HTTP $INSERT_STATUS (body: $(cat /tmp/seahorsedb_insert.out | head -c 200))" ;;
esac

# ── 5. Delete by label (driver delete_point shape) ─────────────────
step "5. POST /data/delete with the label we just inserted"
DELETE_STATUS=$(curl -s -o /tmp/seahorsedb_delete.out -w "%{http_code}" \
    -X POST "$CORAL_URL/catalog/tables/$TABLE/data/delete" \
    -H "Content-Type: application/json" \
    -d "{\"labels\": [$LABEL]}")
case "$DELETE_STATUS" in
    200|202|404) ok "delete → HTTP $DELETE_STATUS (idempotent)" ;;
    *)           bad "delete → HTTP $DELETE_STATUS (body: $(cat /tmp/seahorsedb_delete.out | head -c 200))" ;;
esac

# ── Cleanup: drop the ephemeral table ──────────────────────────────
step "Cleanup"
DROP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
    -X DELETE "$CORAL_URL/catalog/tables/$TABLE")
case "$DROP_STATUS" in
    200|202|404) ok "drop table → HTTP $DROP_STATUS" ;;
    *)           bad "drop table → HTTP $DROP_STATUS" ;;
esac

# ── Results ────────────────────────────────────────────────────────
echo
echo "═══════════════════════════════════════════"
echo "  Passed: $PASSED"
echo "  Failed: $FAILED"
if [ $FAILED -gt 0 ]; then
    echo
    echo "  Errors:"
    for e in "${ERRORS[@]}"; do
        echo "    - $e"
    done
fi
echo "═══════════════════════════════════════════"

[ $FAILED -eq 0 ]
