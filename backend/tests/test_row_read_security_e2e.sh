#!/bin/bash
#
# AKB row-read security E2E tests.
#
# Exercises adversarial /rows and /query requests against a live backend and
# verifies the compiler rejects malformed identifiers while bound-value attacks
# neither leak nor mutate data.
#
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
PASS=0
FAIL=0
ERRORS=()

pass() { PASS=$((PASS+1)); echo "  OK $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  FAIL $1 - $2"; }

echo "AKB row-read security E2E"
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
    -d '{"name":"row-read-security-e2e"}' |
    python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null
}

USER="row-read-security-$(date +%s)"
VAULT="row-read-security-$(date +%s)"
PAT=$(setup_user "$USER")
[ -n "$PAT" ] && pass "user + PAT created" || { fail "setup" "user/PAT creation failed"; exit 1; }

cleanup() {
  curl -sk -X DELETE "$BASE_URL/api/v1/vaults/$VAULT" \
    -H "Authorization: Bearer $PAT" >/dev/null 2>&1 || true
}
trap cleanup EXIT

R=$(curl -sk -X POST "$BASE_URL/api/v1/vaults?name=$VAULT&description=row-read-security-e2e" \
  -H "Authorization: Bearer $PAT")
echo "$R" | python3 -c 'import sys,json; json.load(sys.stdin)["vault_id"]' >/dev/null 2>&1 &&
  pass "vault created" || fail "vault create" "$R"

R=$(curl -sk -X POST "$BASE_URL/api/v1/tables/$VAULT" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d '{"name":"incidents","columns":[{"name":"title","type":"text"},{"name":"severity","type":"text"},{"name":"metadata","type":"json"}]}')
echo "$R" | python3 -c 'import sys,json; d=json.load(sys.stdin); assert d["name"] == "incidents"' >/dev/null 2>&1 &&
  pass "table created" || fail "table create" "$R"

SQL="INSERT INTO incidents (title, severity, metadata) VALUES ('safe row', 'high', '{\"tier\":\"gold\"}'::jsonb)"
SQL_PAYLOAD=$(SQL="$SQL" python3 -c 'import json,os; print(json.dumps({"sql": os.environ["SQL"]}))')
R=$(curl -sk -X POST "$BASE_URL/api/v1/tables/$VAULT/sql" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d "$SQL_PAYLOAD")
echo "$R" | python3 -c 'import sys,json; assert json.load(sys.stdin)["kind"] == "table_sql"' >/dev/null 2>&1 &&
  pass "row inserted" || fail "row insert" "$R"

TMP=$(mktemp)
STATUS=$(curl -sk -o "$TMP" -w "%{http_code}" -G "$BASE_URL/api/v1/tables/$VAULT/incidents/rows" \
  -H "Authorization: Bearer $PAT" \
  --data-urlencode "title%3BDROP%20TABLE%20users%3B--=eq.safe")
[ "$STATUS" = "400" ] &&
  python3 -c 'import sys,json; assert json.load(open(sys.argv[1]))["code"] == "undefined_column"' "$TMP" >/dev/null 2>&1 &&
  pass "rows identifier injection rejected" || fail "rows identifier injection rejected" "status=$STATUS body=$(cat "$TMP")"
rm -f "$TMP"

TMP=$(mktemp)
STATUS=$(curl -sk -o "$TMP" -w "%{http_code}" -X POST "$BASE_URL/api/v1/tables/$VAULT/incidents/query" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d '{"filter":{"jsonb":{"col":"metadata","path":["tier'\''); DROP TABLE users;--"],"cast":"money"},"op":"eq","val":"gold"}}')
[ "$STATUS" = "400" ] &&
  python3 -c 'import sys,json; assert json.load(open(sys.argv[1]))["code"] == "invalid_cast"' "$TMP" >/dev/null 2>&1 &&
  pass "query cast injection rejected" || fail "query cast injection rejected" "status=$STATUS body=$(cat "$TMP")"
rm -f "$TMP"

BODY=$(curl -sk -X POST "$BASE_URL/api/v1/tables/$VAULT/incidents/query" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d '{"filter":{"jsonb":{"col":"metadata","path":["tier'\''); DROP TABLE users;--"],"cast":null},"op":"eq","val":"gold"}}')
echo "$BODY" | python3 -c '
import sys,json
d=json.load(sys.stdin)
assert d["kind"] == "table_query"
assert d["items"] == []
assert d["total"] == 0
' >/dev/null 2>&1 &&
  pass "query JSON path injection stays bound" || fail "query JSON path injection stays bound" "$BODY"

TMP=$(mktemp)
STATUS=$(curl -sk -o "$TMP" -w "%{http_code}" -X POST "$BASE_URL/api/v1/tables/$VAULT/incidents/query" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d '{"delete":{"filter":{"col":"title","op":"eq","val":"safe row"}}}')
[ "$STATUS" = "400" ] &&
  python3 -c 'import sys,json; assert json.load(open(sys.argv[1]))["code"] == "method_not_allowed"' "$TMP" >/dev/null 2>&1 &&
  pass "query write AST rejected" || fail "query write AST rejected" "status=$STATUS body=$(cat "$TMP")"
rm -f "$TMP"

BODY=$(curl -sk -G "$BASE_URL/api/v1/tables/$VAULT/incidents/rows" \
  -H "Authorization: Bearer $PAT" \
  --data-urlencode "select=title,severity")
echo "$BODY" | python3 -c '
import sys,json
d=json.load(sys.stdin)
assert d["total"] == 1
assert d["items"] == [{"title":"safe row","severity":"high"}]
' >/dev/null 2>&1 &&
  pass "data unchanged after adversarial requests" || fail "data unchanged after adversarial requests" "$BODY"

echo ""
echo "Results: $PASS passed, $FAIL failed"
if [ "$FAIL" -ne 0 ]; then
  printf '%s\n' "${ERRORS[@]}"
  exit 1
fi
