#!/bin/bash
#
# AKB Table CRUD + Envelope E2E
# Verifies the table REST contract: create/list/drop + alter via MCP,
# focusing on the standard envelope keys (kind, id, vault, items, total).
#
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
VAULT="tbl-envelope-$(date +%s)"
USER="tbl-envelope-$(date +%s)"
TABLE="cust"
PASS=0
FAIL=0
ERRORS=()

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

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
  -d '{"name":"tbl-envelope"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

[ -n "$PAT" ] || { echo "FATAL: could not get PAT"; exit 1; }

# Create vault for the test
curl -sk -X POST "$BASE_URL/api/v1/vaults" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"$VAULT\",\"description\":\"envelope test\"}" >/dev/null

# JSON-key assertion helper.
assert_keys() {
  local label="$1" body="$2"
  shift 2
  for key in "$@"; do
    if echo "$body" | python3 -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if '$key' in d else 1)" 2>/dev/null; then
      pass "$label: has key '$key'"
    else
      fail "$label" "missing key '$key' in $body"
    fi
  done
}

assert_value() {
  local label="$1" body="$2" path="$3" expected="$4"
  local got
  got=$(echo "$body" | python3 -c "import sys,json; d=json.load(sys.stdin); $path; print(v)" 2>/dev/null)
  if [ "$got" = "$expected" ]; then
    pass "$label: $path == '$expected'"
  else
    fail "$label" "expected '$expected' for $path, got '$got'"
  fi
}

echo ""
echo "▸ 1. Create table — envelope shape"

CREATE=$(curl -sk -X POST "$BASE_URL/api/v1/tables/$VAULT" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"$TABLE\",\"description\":\"customers\",\"columns\":[{\"name\":\"email\",\"type\":\"text\"},{\"name\":\"age\",\"type\":\"number\"}]}")
assert_keys "create" "$CREATE" kind id vault name columns
assert_value "create" "$CREATE" "v=d['kind']" "table"
assert_value "create" "$CREATE" "v=d['vault']" "$VAULT"
assert_value "create" "$CREATE" "v=d['name']" "$TABLE"

TABLE_ID=$(echo "$CREATE" | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')

echo ""
echo "▸ 2. List tables — envelope shape"

LIST=$(curl -sk "$BASE_URL/api/v1/tables/$VAULT" \
  -H "Authorization: Bearer $PAT")
assert_keys "list" "$LIST" kind vault items total
assert_value "list" "$LIST" "v=d['kind']" "table"
assert_value "list" "$LIST" "v=d['vault']" "$VAULT"
assert_value "list" "$LIST" "v=d['total']" "1"
assert_value "list" "$LIST" "v=d['items'][0]['kind']" "table"
assert_value "list" "$LIST" "v=d['items'][0]['name']" "$TABLE"
assert_value "list" "$LIST" "v=d['items'][0]['id']" "$TABLE_ID"

echo ""
echo "▸ 3. SQL SELECT — envelope shape (rows → items)"

INS=$(curl -sk -X POST "$BASE_URL/api/v1/tables/$VAULT/sql" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d "{\"sql\":\"INSERT INTO $TABLE (email, age) VALUES ('a@x', 30), ('b@y', 40)\"}")
assert_keys "sql.insert" "$INS" kind result vaults
assert_value "sql.insert" "$INS" "v=d['kind']" "table_sql"

SEL=$(curl -sk -X POST "$BASE_URL/api/v1/tables/$VAULT/sql" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d "{\"sql\":\"SELECT email, age FROM $TABLE ORDER BY age\"}")
assert_keys "sql.select" "$SEL" kind columns items total vaults
assert_value "sql.select" "$SEL" "v=d['kind']" "table_query"
assert_value "sql.select" "$SEL" "v=d['total']" "2"
assert_value "sql.select" "$SEL" "v=d['items'][0]['email']" "a@x"

echo ""
echo "▸ 4. Drop table — envelope shape"

DROP=$(curl -sk -X DELETE "$BASE_URL/api/v1/tables/$VAULT/$TABLE" \
  -H "Authorization: Bearer $PAT")
assert_keys "drop" "$DROP" kind id vault name deleted
assert_value "drop" "$DROP" "v=d['kind']" "table"
assert_value "drop" "$DROP" "v=d['vault']" "$VAULT"
assert_value "drop" "$DROP" "v=d['name']" "$TABLE"
assert_value "drop" "$DROP" "v=str(d['deleted']).lower()" "true"

echo ""
echo "▸ 5. Drop missing table — proper 4xx error"

MISS=$(curl -sk -o /dev/null -w "%{http_code}" -X DELETE "$BASE_URL/api/v1/tables/$VAULT/does-not-exist" \
  -H "Authorization: Bearer $PAT")
[ "$MISS" = "404" ] && pass "drop-missing: HTTP 404" || fail "drop-missing" "expected 404, got $MISS"

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
