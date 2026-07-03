#!/bin/bash
#
# AKB row-read REST E2E tests.
#
# Verifies GET /api/v1/tables/{vault}/{table}/rows and POST .../query compile
# read filters into parameterized SQL while preserving the existing table ACL path.
#
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
PASS=0
FAIL=0
ERRORS=()

pass() { PASS=$((PASS+1)); echo "  OK $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  FAIL $1 - $2"; }

echo "AKB row-read REST E2E"
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
    -d '{"name":"row-read-e2e"}' |
    python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null
}

USER="row-read-$(date +%s)"
VAULT="row-read-vault-$(date +%s)"
PAT=$(setup_user "$USER")
[ -n "$PAT" ] && pass "user + PAT created" || { fail "setup" "user/PAT creation failed"; exit 1; }

cleanup() {
  curl -sk -X DELETE "$BASE_URL/api/v1/vaults/$VAULT" \
    -H "Authorization: Bearer $PAT" >/dev/null 2>&1 || true
}
trap cleanup EXIT

R=$(curl -sk -X POST "$BASE_URL/api/v1/vaults?name=$VAULT&description=row-read-e2e" \
  -H "Authorization: Bearer $PAT")
echo "$R" | python3 -c 'import sys,json; json.load(sys.stdin)["vault_id"]' >/dev/null 2>&1 &&
  pass "vault created" || fail "vault create" "$R"

R=$(curl -sk -X POST "$BASE_URL/api/v1/tables/$VAULT" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d '{"name":"incidents","columns":[{"name":"title","type":"text","required":true},{"name":"severity","type":"text"},{"name":"score","type":"number"},{"name":"metadata","type":"json"}]}')
echo "$R" | python3 -c 'import sys,json; d=json.load(sys.stdin); assert d["name"] == "incidents"' >/dev/null 2>&1 &&
  pass "table created" || fail "table create" "$R"

SQL="INSERT INTO incidents (title, severity, score, metadata) VALUES ('API outage', 'high', 0.95, '{\"tier\":\"gold\",\"stats\":{\"count\":7}}'::jsonb), ('Minor typo', 'low', 0.10, '{\"tier\":\"bronze\",\"stats\":{\"count\":1}}'::jsonb), ('DB pressure', 'critical', 0.90, '{\"tier\":\"gold\",\"stats\":{\"count\":9}}'::jsonb)"
SQL_PAYLOAD=$(SQL="$SQL" python3 -c 'import json,os; print(json.dumps({"sql": os.environ["SQL"]}))')
R=$(curl -sk -X POST "$BASE_URL/api/v1/tables/$VAULT/sql" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d "$SQL_PAYLOAD")
echo "$R" | python3 -c 'import sys,json; assert json.load(sys.stdin)["kind"] == "table_sql"' >/dev/null 2>&1 &&
  pass "rows inserted" || fail "row insert" "$R"

HDR=$(mktemp)
BODY=$(curl -sk -D "$HDR" -G "$BASE_URL/api/v1/tables/$VAULT/incidents/rows" \
  -H "Authorization: Bearer $PAT" \
  -H "Prefer: count=exact" \
  --data-urlencode "select=title,severity,metadata->>tier" \
  --data-urlencode "severity=in.(high,critical)" \
  --data-urlencode "order=created_at.asc" \
  --data-urlencode "limit=2")
grep -qi '^Content-Range: 0-1/2' "$HDR" &&
  pass "Content-Range exact count" || fail "Content-Range" "$(cat "$HDR")"
echo "$BODY" | python3 -c '
import sys,json
d=json.load(sys.stdin)
assert d["kind"] == "table_query"
assert "vaults" not in d
assert d["columns"] == ["title","severity","metadata->>tier"]
assert d["total"] == 2
assert [r["severity"] for r in d["items"]] == ["high","critical"]
assert [r["metadata->>tier"] for r in d["items"]] == ["gold","gold"]
' >/dev/null 2>&1 &&
  pass "select/filter/order/count body" || fail "select/filter/order/count body" "$BODY"
rm -f "$HDR"

HDR=$(mktemp)
BODY=$(curl -sk -D "$HDR" -G "$BASE_URL/api/v1/tables/$VAULT/incidents/rows" \
  -H "Authorization: Bearer $PAT" \
  -H "Prefer: count=exact" \
  --data-urlencode "severity=in.(high,critical)" \
  --data-urlencode "offset=100" \
  --data-urlencode "limit=2")
grep -qi '^Content-Range: \*/2' "$HDR" &&
  pass "empty page keeps exact count" || fail "empty page Content-Range" "$(cat "$HDR")"
echo "$BODY" | python3 -c '
import sys,json
d=json.load(sys.stdin)
assert d["total"] == 2
assert d["items"] == []
' >/dev/null 2>&1 &&
  pass "empty page body keeps exact count" || fail "empty page body keeps exact count" "$BODY"
rm -f "$HDR"

BODY=$(curl -sk -G "$BASE_URL/api/v1/tables/$VAULT/incidents/rows" \
  -H "Authorization: Bearer $PAT" \
  --data-urlencode "select=title" \
  --data-urlencode "metadata%23%3E%3E%7Bstats%2Ccount%7D%3A%3Aint=gt.5" \
  --data-urlencode "order=title.asc")
echo "$BODY" | python3 -c '
import sys,json
d=json.load(sys.stdin)
assert [r["title"] for r in d["items"]] == ["API outage","DB pressure"]
' >/dev/null 2>&1 &&
  pass "json path cast filter" || fail "json path cast filter" "$BODY"

BODY=$(curl -sk -G "$BASE_URL/api/v1/tables/$VAULT/incidents/rows" \
  -H "Authorization: Bearer $PAT" \
  --data-urlencode "select=title" \
  --data-urlencode 'metadata=cs.{"tier":"gold"}' \
  --data-urlencode "order=title.asc")
echo "$BODY" | python3 -c '
import sys,json
d=json.load(sys.stdin)
assert [r["title"] for r in d["items"]] == ["API outage","DB pressure"]
' >/dev/null 2>&1 &&
  pass "json containment object filter" || fail "json containment object filter" "$BODY"

QUERY_PAYLOAD='{"select":["title","severity","metadata->>tier"],"and":[{"col":"severity","op":"in","val":["high","critical"]},{"jsonb":{"col":"metadata","path":["tier"],"cast":null},"op":"eq","val":"gold"}],"order":[{"col":"created_at","dir":"asc"}],"limit":2,"count":"exact"}'
HDR=$(mktemp)
BODY=$(curl -sk -D "$HDR" -X POST "$BASE_URL/api/v1/tables/$VAULT/incidents/query" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d "$QUERY_PAYLOAD")
grep -qi '^Content-Range: 0-1/2' "$HDR" &&
  pass "query AST Content-Range exact count" || fail "query AST Content-Range" "$(cat "$HDR")"
echo "$BODY" | python3 -c '
import sys,json
d=json.load(sys.stdin)
assert d["kind"] == "table_query"
assert "vaults" not in d
assert d["columns"] == ["title","severity","metadata->>tier"]
assert d["total"] == 2
assert [r["title"] for r in d["items"]] == ["API outage","DB pressure"]
assert [r["metadata->>tier"] for r in d["items"]] == ["gold","gold"]
' >/dev/null 2>&1 &&
  pass "query AST select/filter/order/count body" || fail "query AST body" "$BODY"
rm -f "$HDR"

TMP=$(mktemp)
STATUS=$(curl -sk -o "$TMP" -w "%{http_code}" -G "$BASE_URL/api/v1/tables/$VAULT/incidents/rows" \
  -H "Authorization: Bearer $PAT" \
  --data-urlencode "sevverity=eq.high")
[ "$STATUS" = "400" ] &&
  python3 -c 'import sys,json; assert json.load(open(sys.argv[1]))["code"] == "undefined_column"' "$TMP" >/dev/null 2>&1 &&
  pass "unknown column rejected" || fail "unknown column rejected" "status=$STATUS body=$(cat "$TMP")"
rm -f "$TMP"

TMP=$(mktemp)
STATUS=$(curl -sk -o "$TMP" -w "%{http_code}" -X POST "$BASE_URL/api/v1/tables/$VAULT/incidents/query" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d '{"filter":{"col":"sevverity","op":"eq","val":"high"}}')
[ "$STATUS" = "400" ] &&
  python3 -c 'import sys,json; assert json.load(open(sys.argv[1]))["code"] == "undefined_column"' "$TMP" >/dev/null 2>&1 &&
  pass "query AST unknown column rejected" || fail "query AST unknown column rejected" "status=$STATUS body=$(cat "$TMP")"
rm -f "$TMP"

echo ""
echo "Results: $PASS passed, $FAIL failed"
if [ "$FAIL" -ne 0 ]; then
  printf '%s\n' "${ERRORS[@]}"
  exit 1
fi
