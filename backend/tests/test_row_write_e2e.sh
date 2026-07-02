#!/bin/bash
#
# AKB row-write REST E2E tests.
#
# Verifies POST/PATCH/DELETE /api/v1/tables/{vault}/{table}/rows plus write
# AST POST .../query against a live backend.
#
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
PASS=0
FAIL=0
ERRORS=()

pass() { PASS=$((PASS+1)); echo "  OK $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  FAIL $1 - $2"; }

echo "AKB row-write REST E2E"
echo "Target: $BASE_URL"
echo ""

setup_user() {
  local user=$1
  curl -sk -X POST "$BASE_URL/api/v1/auth/register" \
    -H 'Content-Type: application/json' \
    -d "{\"username\":\"$user\",\"email\":\"$user@test.dev\",\"password\":\"test1234\"}" >/dev/null 2>&1
  local jwt
  jwt=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" \
    -H 'Content-Type: application/json' \
    -d "{\"username\":\"$user\",\"password\":\"test1234\"}" |
    python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)
  curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" \
    -H "Authorization: Bearer $jwt" \
    -H 'Content-Type: application/json' \
    -d '{"name":"row-write-e2e"}' |
    python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null
}

USER="row-write-$(date +%s)"
VAULT="row-write-vault-$(date +%s)"
PAT=$(setup_user "$USER")
[ -n "$PAT" ] && pass "user + PAT created" || { fail "setup" "user/PAT creation failed"; exit 1; }

cleanup() {
  curl -sk -X DELETE "$BASE_URL/api/v1/vaults/$VAULT" \
    -H "Authorization: Bearer $PAT" >/dev/null 2>&1 || true
}
trap cleanup EXIT

R=$(curl -sk -X POST "$BASE_URL/api/v1/vaults?name=$VAULT&description=row-write-e2e" \
  -H "Authorization: Bearer $PAT")
echo "$R" | python3 -c 'import sys,json; json.load(sys.stdin)["vault_id"]' >/dev/null 2>&1 &&
  pass "vault created" || fail "vault create" "$R"

R=$(curl -sk -X POST "$BASE_URL/api/v1/tables/$VAULT" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d '{"name":"incidents","columns":[{"name":"external_id","type":"text","required":true},{"name":"title","type":"text"},{"name":"severity","type":"text"},{"name":"metadata","type":"json"}],"unique_keys":[{"columns":["external_id"]}]}')
echo "$R" | python3 -c 'import sys,json; d=json.load(sys.stdin); assert d["name"] == "incidents"; assert d["unique_keys"]' >/dev/null 2>&1 &&
  pass "table with unique key created" || fail "table create" "$R"

HDR=$(mktemp)
BODY=$(curl -sk -D "$HDR" -X POST "$BASE_URL/api/v1/tables/$VAULT/incidents/rows" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -H 'Prefer: return=representation' \
  -d '[{"external_id":"INC-1","title":"A","severity":"high","created_by":"mallory"},{"external_id":"INC-2","title":"B","metadata":{"tier":"gold"}}]')
grep -q '^HTTP/.* 201' "$HDR" &&
  grep -qi '^Content-Range: 0-1/2' "$HDR" &&
  pass "bulk insert returns 201 + Content-Range" || fail "bulk insert headers" "$(cat "$HDR")"
echo "$BODY" | python3 -c "
import sys,json
d=json.load(sys.stdin)
assert d['kind'] == 'table_query'
assert d['total'] == 2
assert [r['external_id'] for r in d['items']] == ['INC-1', 'INC-2']
assert all(r['created_by'] == '$USER' for r in d['items'])
" >/dev/null 2>&1 &&
  pass "bulk insert body and created_by spoof blocked" || fail "bulk insert body" "$BODY"
rm -f "$HDR"

TMP=$(mktemp)
STATUS=$(curl -sk -o "$TMP" -w "%{http_code}" -X PATCH "$BASE_URL/api/v1/tables/$VAULT/incidents/rows?order=id.asc" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d '{"severity":"critical"}')
[ "$STATUS" = "400" ] &&
  python3 -c 'import sys,json; assert json.load(open(sys.argv[1]))["code"] == "unfiltered_mutation"' "$TMP" >/dev/null 2>&1 &&
  pass "unfiltered PATCH with read params rejected" || fail "unfiltered PATCH" "status=$STATUS body=$(cat "$TMP")"
rm -f "$TMP"

HDR=$(mktemp)
BODY=$(curl -sk -D "$HDR" -X PATCH "$BASE_URL/api/v1/tables/$VAULT/incidents/rows?external_id=eq.INC-1&select=external_id,severity,created_by" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -H 'Prefer: return=representation' \
  -d '{"severity":"critical","created_by":"mallory","updated_at":"2099-01-01T00:00:00Z"}')
grep -q '^HTTP/.* 200' "$HDR" &&
  grep -qi '^Content-Range: 0-0/1' "$HDR" &&
  pass "filtered PATCH returns row" || fail "filtered PATCH headers" "$(cat "$HDR")"
echo "$BODY" | python3 -c "
import sys,json
d=json.load(sys.stdin)
row=d['items'][0]
assert row['external_id'] == 'INC-1'
assert row['severity'] == 'critical'
assert row['created_by'] == '$USER'
" >/dev/null 2>&1 &&
  pass "PATCH ignores server-managed fields" || fail "PATCH body" "$BODY"
rm -f "$HDR"

BODY=$(curl -sk -X POST "$BASE_URL/api/v1/tables/$VAULT/incidents/rows?on_conflict=external_id&select=external_id,title,severity" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -H 'Prefer: return=representation' \
  -d '{"external_id":"INC-1","title":"A merged","severity":"medium"}')
echo "$BODY" | python3 -c '
import sys,json
d=json.load(sys.stdin)
row=d["items"][0]
assert row["external_id"] == "INC-1"
assert row["title"] == "A merged"
assert row["severity"] == "medium"
' >/dev/null 2>&1 &&
  pass "upsert merge on unique key" || fail "upsert merge" "$BODY"

TMP=$(mktemp)
STATUS=$(curl -sk -o "$TMP" -w "%{http_code}" -X POST "$BASE_URL/api/v1/tables/$VAULT/incidents/rows?on_conflict=severity" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d '{"external_id":"INC-3","severity":"low"}')
[ "$STATUS" = "400" ] &&
  python3 -c 'import sys,json; assert json.load(open(sys.argv[1]))["code"] == "no_unique_constraint"' "$TMP" >/dev/null 2>&1 &&
  pass "non-unique upsert target rejected" || fail "non-unique upsert" "status=$STATUS body=$(cat "$TMP")"
rm -f "$TMP"

HDR=$(mktemp)
BODY=$(curl -sk -D "$HDR" -X POST "$BASE_URL/api/v1/tables/$VAULT/incidents/query" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d '{"delete":true,"where":{"col":"external_id","op":"eq","val":"INC-2"},"returning":["external_id"]}')
grep -q '^HTTP/.* 200' "$HDR" &&
  pass "write AST delete returns 200" || fail "write AST delete headers" "$(cat "$HDR")"
echo "$BODY" | python3 -c '
import sys,json
d=json.load(sys.stdin)
assert d["items"] == [{"external_id":"INC-2"}]
' >/dev/null 2>&1 &&
  pass "write AST delete body" || fail "write AST delete body" "$BODY"
rm -f "$HDR"

BODY=$(curl -sk -G "$BASE_URL/api/v1/tables/$VAULT/incidents/rows" \
  -H "Authorization: Bearer $PAT" \
  --data-urlencode "select=external_id,title,severity" \
  --data-urlencode "order=external_id.asc")
echo "$BODY" | python3 -c '
import sys,json
d=json.load(sys.stdin)
assert d["total"] == 1
assert d["items"][0]["external_id"] == "INC-1"
assert d["items"][0]["title"] == "A merged"
' >/dev/null 2>&1 &&
  pass "final row state" || fail "final row state" "$BODY"

echo ""
echo "Results: $PASS passed, $FAIL failed"
if [ "$FAIL" -ne 0 ]; then
  printf '%s\n' "${ERRORS[@]}"
  exit 1
fi
