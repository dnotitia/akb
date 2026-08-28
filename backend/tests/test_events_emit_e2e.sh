#!/bin/bash
#
# AKB Events Emit E2E
# Verifies that each table/file write action emits the expected row
# into the `events` outbox with the correct kind / resource_uri /
# actor_id and payload fields. Reads the events table directly via
# psql exec inside the postgres pod (so this requires kubectl access
# — skip when kubectl is absent). The table DML section also exercises
# the MCP `akb_sql` path for committed, failed, no-op, and bulk writes.
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

# DB verification needs either AKB_PG_EXEC (local override) or kubectl
# (cluster). Skip only when neither is available.
if [ -z "${AKB_PG_EXEC:-}" ] && ! command -v kubectl >/dev/null 2>&1; then
  echo "no AKB_PG_EXEC and kubectl unavailable — skipping events DB verification"
  exit 0
fi

run_psql() {
  # Portable: AKB_PG_EXEC overrides for a local stack (e.g.
  # "docker compose exec -T postgres" or "docker exec -i akb-postgres-1");
  # default targets the cluster via kubectl.
  if [ -n "${AKB_PG_EXEC:-}" ]; then
    ${AKB_PG_EXEC} psql -U "$PG_USER" -d "$PG_DB" -tAc "$1" 2>/dev/null
  else
    kubectl exec -n "$NS" "$PG_POD" -- psql -U "$PG_USER" -d "$PG_DB" -tAc "$1" 2>/dev/null
  fi
}

mcp_session() {
  curl -sk -i -X POST "$BASE_URL/mcp/" \
    -H "Authorization: Bearer $1" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"events-e2e","version":"1.0"}}}' 2>&1 \
    | grep -i "mcp-session-id" | tr -d '\r' | awk '{print $2}'
}

MCP_ID=10
mcp() {
  MCP_ID=$((MCP_ID+1))
  curl -sk -X POST "$BASE_URL/mcp/" \
    -H "Authorization: Bearer $1" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -H "mcp-session-id: $2" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":$MCP_ID,\"method\":\"tools/call\",\"params\":{\"name\":\"$3\",\"arguments\":$4}}" 2>&1 \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['content'][0]['text'])" 2>/dev/null
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

curl -sk -X POST "$BASE_URL/api/v1/vaults?name=$VAULT" \
  -H "Authorization: Bearer $PAT" >/dev/null

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
  -d "{\"name\":\"$TABLE\",\"description\":\"d\",\"columns\":[{\"name\":\"sku\",\"type\":\"text\",\"required\":true}]}")
TABLE_URI=$(echo "$CREATE_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin)["uri"])')
EXPECTED_TABLE_URI="akb://$VAULT/table/$TABLE"

[ "$(events_for table.create)" = "1" ] && pass "table.create count=1" || fail "table.create" "expected 1, got $(events_for table.create)"

# Canonical URI + actor match
ROW=$(run_psql "SELECT resource_uri || '|' || actor_id FROM events WHERE vault_id = '$VAULT_ID' AND kind = 'table.create'")
[ "$ROW" = "$EXPECTED_TABLE_URI|$USER" ] && pass "table.create resource_uri+actor match" || fail "table.create uri" "got $ROW (expected $EXPECTED_TABLE_URI|$USER)"

# payload includes table_name
TBL_PAYLOAD=$(run_psql "SELECT payload->>'table_name' FROM events WHERE vault_id = '$VAULT_ID' AND kind = 'table.create'")
[ "$TBL_PAYLOAD" = "$TABLE" ] && pass "table.create payload.table_name" || fail "table.create payload" "got $TBL_PAYLOAD"

# Same URI is what the create response advertises (contract check)
[ "$TABLE_URI" = "$EXPECTED_TABLE_URI" ] && pass "table create response.uri matches event.resource_uri" || fail "uri parity" "resp=$TABLE_URI"

echo ""
echo "▸ 2. table.rows_changed events through MCP akb_sql"

SID=$(mcp_session "$PAT")
[ -n "$SID" ] && pass "MCP session" || { fail "MCP session" "no session"; exit 1; }

ROWS_CHANGED_BASE=$(events_for table.rows_changed)
R=$(mcp "$PAT" "$SID" akb_sql "{\"vault\":\"$VAULT\",\"sql\":\"INSERT INTO $TABLE (sku) VALUES ('one')\"}")
[ "$(events_for table.rows_changed)" = "$((ROWS_CHANGED_BASE + 1))" ] && pass "single INSERT emits one event" || fail "single INSERT" "response=$R"

R=$(mcp "$PAT" "$SID" akb_sql "{\"vault\":\"$VAULT\",\"sql\":\"INSERT INTO $TABLE (sku) VALUES ('bulk-a'), ('bulk-b')\"}")
[ "$(events_for table.rows_changed)" = "$((ROWS_CHANGED_BASE + 2))" ] && pass "bulk INSERT emits one event" || fail "bulk INSERT" "response=$R"

R=$(mcp "$PAT" "$SID" akb_sql "{\"vault\":\"$VAULT\",\"sql\":\"UPDATE $TABLE SET sku = 'updated' WHERE sku LIKE 'bulk-%'\"}")
[ "$(events_for table.rows_changed)" = "$((ROWS_CHANGED_BASE + 3))" ] && pass "bulk UPDATE emits one event" || fail "bulk UPDATE" "response=$R"

R=$(mcp "$PAT" "$SID" akb_sql "{\"vault\":\"$VAULT\",\"sql\":\"UPDATE $TABLE SET sku = 'missing' WHERE sku = 'does-not-exist'\"}")
[ "$(events_for table.rows_changed)" = "$((ROWS_CHANGED_BASE + 3))" ] && pass "no-op UPDATE emits no event" || fail "no-op UPDATE" "response=$R"

# NOT NULL violation aborts the statement transaction after the trigger path
# would otherwise be eligible, so no wake-up event may survive the rollback.
R=$(mcp "$PAT" "$SID" akb_sql "{\"vault\":\"$VAULT\",\"sql\":\"INSERT INTO $TABLE (sku) VALUES (NULL)\"}")
[ "$(events_for table.rows_changed)" = "$((ROWS_CHANGED_BASE + 3))" ] && pass "failed INSERT rolls back its event" || fail "failed INSERT rollback" "response=$R"

R=$(mcp "$PAT" "$SID" akb_sql "{\"vault\":\"$VAULT\",\"sql\":\"DELETE FROM $TABLE WHERE sku = 'updated'\"}")
[ "$(events_for table.rows_changed)" = "$((ROWS_CHANGED_BASE + 4))" ] && pass "bulk DELETE emits one event" || fail "bulk DELETE" "response=$R"

ENVELOPE=$(run_psql "SELECT resource_uri || '|' || actor_id || '|' || (payload->>'operation') FROM events WHERE vault_id = '$VAULT_ID' AND kind = 'table.rows_changed' ORDER BY id LIMIT 1")
[ "$ENVELOPE" = "$EXPECTED_TABLE_URI|$USER|insert" ] && pass "rows_changed envelope uri+actor+operation" || fail "rows_changed envelope" "got $ENVELOPE"

[ "$(run_psql "SELECT COUNT(*) FROM events WHERE vault_id = '$VAULT_ID' AND kind = 'table.rows_changed'")" = "$((ROWS_CHANGED_BASE + 4))" ] && pass "rows_changed statement cardinality" || fail "rows_changed cardinality" "unexpected count"

echo ""
echo "▸ 3. table.drop event"

curl -sk -X DELETE "$BASE_URL/api/v1/tables/$VAULT/$TABLE" -H "Authorization: Bearer $PAT" >/dev/null

[ "$(events_for table.drop)" = "1" ] && pass "table.drop count=1" || fail "table.drop" "expected 1, got $(events_for table.drop)"

DROP_URI=$(run_psql "SELECT resource_uri FROM events WHERE vault_id = '$VAULT_ID' AND kind = 'table.drop'")
[ "$DROP_URI" = "$EXPECTED_TABLE_URI" ] && pass "table.drop resource_uri matches" || fail "table.drop uri" "got $DROP_URI"

echo ""
echo "── Cleanup ──"
curl -sk -X DELETE "$BASE_URL/api/v1/vaults/$VAULT" \
  -H "Authorization: Bearer $PAT" >/dev/null 2>&1 || true

echo ""
echo "═══════════════════════════════════════════"
echo "  Results: $PASS passed, $FAIL failed"
if [ $FAIL -gt 0 ]; then
  echo "  Failures:"
  printf '    - %s\n' "${ERRORS[@]}"
  exit 1
fi
exit 0
