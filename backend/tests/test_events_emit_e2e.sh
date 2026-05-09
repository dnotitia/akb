#!/bin/bash
#
# AKB Events Emit E2E
# Verifies that each table/file write action emits the expected row
# into the `events` outbox with the correct kind/ref_type/ref_id and
# payload fields. Reads the events table directly via psql exec inside
# the postgres pod (so this requires kubectl access — skip via env).
#
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
NS="${AKB_NS:-akb}"
PG_POD="${AKB_PG_POD:-postgres-0}"
PG_USER="${AKB_PG_USER:-akbuser}"
PG_DB="${AKB_PG_DB:-akb}"

VAULT="ev-emit-$(date +%s)"
USER="ev-emit-$(date +%s)"
TABLE="orders"
PASS=0
FAIL=0
ERRORS=()

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

# Skip when not running against a k8s-deployed backend.
if ! command -v kubectl >/dev/null 2>&1; then
  echo "kubectl not available — skipping events DB verification"
  exit 0
fi

run_psql() {
  kubectl exec -n "$NS" "$PG_POD" -- psql -U "$PG_USER" -d "$PG_DB" -tAc "$1" 2>/dev/null
}

echo "▸ Setup"

curl -sk -X POST "$BASE_URL/api/v1/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER\",\"email\":\"$USER@test.dev\",\"password\":\"test1234\"}" >/dev/null 2>&1

JWT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER\",\"password\":\"test1234\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

PAT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" \
  -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' \
  -d '{"name":"ev-emit"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

[ -n "$PAT" ] || { echo "FATAL: could not get PAT"; exit 1; }

curl -sk -X POST "$BASE_URL/api/v1/vaults" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"$VAULT\"}" >/dev/null

VAULT_ID=$(run_psql "SELECT id FROM vaults WHERE name = '$VAULT'")
[ -n "$VAULT_ID" ] && pass "vault registered ($VAULT_ID)" || { fail "vault" "not in DB"; exit 1; }

# Helper: count events for this vault matching kind.
events_for() {
  local kind="$1"
  run_psql "SELECT COUNT(*) FROM events WHERE vault_id = '$VAULT_ID' AND kind = '$kind'"
}

echo ""
echo "▸ 1. table.create event"

CREATE_RESP=$(curl -sk -X POST "$BASE_URL/api/v1/tables/$VAULT" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"$TABLE\",\"description\":\"d\",\"columns\":[{\"name\":\"sku\",\"type\":\"text\"}]}")
TABLE_ID=$(echo "$CREATE_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')

[ "$(events_for table.create)" = "1" ] && pass "table.create count=1" || fail "table.create" "expected 1, got $(events_for table.create)"

# ref fields
ROW=$(run_psql "SELECT ref_type || '|' || ref_id || '|' || actor_id FROM events WHERE vault_id = '$VAULT_ID' AND kind = 'table.create'")
[ "$ROW" = "table|$TABLE_ID|$USER" ] && pass "table.create ref+actor match" || fail "table.create ref" "got $ROW"

# payload includes table_name
TBL_PAYLOAD=$(run_psql "SELECT payload->>'table_name' FROM events WHERE vault_id = '$VAULT_ID' AND kind = 'table.create'")
[ "$TBL_PAYLOAD" = "$TABLE" ] && pass "table.create payload.table_name" || fail "table.create payload" "got $TBL_PAYLOAD"

echo ""
echo "▸ 2. table.drop event"

curl -sk -X DELETE "$BASE_URL/api/v1/tables/$VAULT/$TABLE" -H "Authorization: Bearer $PAT" >/dev/null

[ "$(events_for table.drop)" = "1" ] && pass "table.drop count=1" || fail "table.drop" "expected 1, got $(events_for table.drop)"

DROP_REF=$(run_psql "SELECT ref_id FROM events WHERE vault_id = '$VAULT_ID' AND kind = 'table.drop'")
[ "$DROP_REF" = "$TABLE_ID" ] && pass "table.drop ref_id matches created table_id" || fail "table.drop ref_id" "got $DROP_REF"

echo ""
echo "── Cleanup ──"
curl -sk -X DELETE "$BASE_URL/api/v1/vaults/$VAULT" \
  -H "Authorization: Bearer $PAT" >/dev/null 2>&1 || true

echo ""
echo "═══════════════════════════════════════════"
echo "  Passed: $PASS   Failed: $FAIL"
if [ $FAIL -gt 0 ]; then
  echo "  Failures:"
  printf '    - %s\n' "${ERRORS[@]}"
  exit 1
fi
exit 0
