#!/bin/bash
#
# AKB Table CRUD + Envelope E2E
# Verifies the table REST contract: create/list/alter/drop,
# focusing on the standard envelope keys (kind, id, vault, items, total).
#
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
VAULT="tbl-envelope-$(date +%s)"
USER="tbl-envelope-$(date +%s)"
READER="tbl-envelope-reader-$(date +%s)"
OUTSIDER="tbl-envelope-outsider-$(date +%s)"
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

curl -sk -X POST "$BASE_URL/api/v1/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$READER\",\"email\":\"$READER@test.dev\",\"password\":\"test1234\"}" >/dev/null 2>&1

READER_JWT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$READER\",\"password\":\"test1234\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

READER_PAT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" \
  -H "Authorization: Bearer $READER_JWT" \
  -H 'Content-Type: application/json' \
  -d '{"name":"tbl-envelope-reader"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

curl -sk -X POST "$BASE_URL/api/v1/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$OUTSIDER\",\"email\":\"$OUTSIDER@test.dev\",\"password\":\"test1234\"}" >/dev/null 2>&1

OUTSIDER_JWT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$OUTSIDER\",\"password\":\"test1234\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

OUTSIDER_PAT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" \
  -H "Authorization: Bearer $OUTSIDER_JWT" \
  -H 'Content-Type: application/json' \
  -d '{"name":"tbl-envelope-outsider"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

# Create vault for the test (POST /vaults uses query params, not JSON body)
curl -sk -X POST "$BASE_URL/api/v1/vaults?name=$VAULT&description=envelope%20test" \
  -H "Authorization: Bearer $PAT" >/dev/null

curl -sk -X POST "$BASE_URL/api/v1/vaults/$VAULT/grant" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d "{\"user\":\"$READER\",\"role\":\"reader\"}" >/dev/null

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
  -d "{\"name\":\"$TABLE\",\"description\":\"customers\",\"columns\":[{\"name\":\"email\",\"type\":\"text\",\"required\":true},{\"name\":\"age\",\"type\":\"number\"}],\"unique_keys\":[{\"columns\":[\"email\"]}],\"indexes\":[{\"columns\":[{\"name\":\"age\",\"order\":\"desc\"}]}]}")
# Tables are URI-addressed (no `d-` id like documents) — the envelope key
# is `uri`, not `id`.
assert_keys "create" "$CREATE" kind uri vault name columns
assert_value "create" "$CREATE" "v=d['kind']" "table"
assert_value "create" "$CREATE" "v=d['vault']" "$VAULT"
assert_value "create" "$CREATE" "v=d['name']" "$TABLE"

TABLE_URI=$(echo "$CREATE" | python3 -c 'import sys,json; print(json.load(sys.stdin)["uri"])')

echo ""
echo "▸ 2. REST alter — envelope shape + permission gate"

ALTER_DENY=$(curl -sk -o /dev/null -w "%{http_code}" -X PATCH "$BASE_URL/api/v1/tables/$VAULT/$TABLE" \
  -H "Authorization: Bearer $READER_PAT" \
  -H 'Content-Type: application/json' \
  -d '{"add_columns":[{"name":"status","type":"text"}]}')
[ "$ALTER_DENY" = "403" ] && pass "alter.reader: HTTP 403" || fail "alter.reader" "expected 403, got $ALTER_DENY"

ALTER=$(curl -sk -X PATCH "$BASE_URL/api/v1/tables/$VAULT/$TABLE" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d '{"add_columns":[{"name":"status","type":"text","default":"active","check":{"op":"in","values":["active","inactive"]},"enum":["active","inactive"],"references":{"table":"statuses","column":"code"},"on_delete":"restrict","index":true}],"rename_columns":{"age":"age_years"}}')
assert_keys "alter" "$ALTER" kind uri vault name columns
assert_value "alter" "$ALTER" "v=d['kind']" "table"
assert_value "alter" "$ALTER" "v=d['vault']" "$VAULT"
assert_value "alter" "$ALTER" "v=any(c.get('name') == 'status' and c.get('type') == 'text' for c in d['columns'])" "True"
assert_value "alter" "$ALTER" "v=any(c.get('name') == 'age_years' for c in d['columns'])" "True"

BAD_RENAME=$(curl -sk -o /dev/null -w "%{http_code}" -X PATCH "$BASE_URL/api/v1/tables/$VAULT/$TABLE" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d '{"rename_columns":{"missing":"new_name"}}')
[ "$BAD_RENAME" = "422" ] && pass "alter.bad-rename: HTTP 422" || fail "alter.bad-rename" "expected 422, got $BAD_RENAME"

BAD_RENAME_CASE=$(curl -sk -o /dev/null -w "%{http_code}" -X PATCH "$BASE_URL/api/v1/tables/$VAULT/$TABLE" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d '{"rename_columns":{"AGE_YEARS":"age_again"}}')
[ "$BAD_RENAME_CASE" = "422" ] && pass "alter.bad-rename-case: HTTP 422" || fail "alter.bad-rename-case" "expected 422, got $BAD_RENAME_CASE"

BAD_RENAME_DUP=$(curl -sk -o /dev/null -w "%{http_code}" -X PATCH "$BASE_URL/api/v1/tables/$VAULT/$TABLE" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d '{"rename_columns":{"email":"dup_name","status":"dup_name"}}')
[ "$BAD_RENAME_DUP" = "422" ] && pass "alter.bad-rename-dup: HTTP 422" || fail "alter.bad-rename-dup" "expected 422, got $BAD_RENAME_DUP"

echo ""
echo "▸ 3. Schema introspection — registry/live merge + reader gate"

SCHEMA_DENY=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/tables/$VAULT/$TABLE/schema" \
  -H "Authorization: Bearer $OUTSIDER_PAT")
[ "$SCHEMA_DENY" = "403" ] && pass "schema.outsider: HTTP 403" || fail "schema.outsider" "expected 403, got $SCHEMA_DENY"

SCHEMA=$(curl -sk "$BASE_URL/api/v1/tables/$VAULT/$TABLE/schema" \
  -H "Authorization: Bearer $READER_PAT")
assert_keys "schema.table" "$SCHEMA" kind uri vault name table sql_name columns unique_keys indexes pg_types system_columns drift
assert_value "schema.table" "$SCHEMA" "v=d['kind']" "table_schema"
assert_value "schema.table" "$SCHEMA" "v=d['vault']" "$VAULT"
assert_value "schema.table" "$SCHEMA" "v=d['name']" "$TABLE"
assert_value "schema.table" "$SCHEMA" "v=next(c for c in d['columns'] if c.get('name') == 'email')['unique']" "True"
assert_value "schema.table" "$SCHEMA" "v=next(c for c in d['columns'] if c.get('name') == 'age_years')['index']" "True"
assert_value "schema.table" "$SCHEMA" "v=next(c for c in d['columns'] if c.get('name') == 'status')['default']" "active"
assert_value "schema.table" "$SCHEMA" "v=next(c for c in d['columns'] if c.get('name') == 'status')['check']['op']" "in"
assert_value "schema.table" "$SCHEMA" "v=next(c for c in d['columns'] if c.get('name') == 'status')['enum'][0]" "active"
assert_value "schema.table" "$SCHEMA" "v=next(c for c in d['columns'] if c.get('name') == 'status')['references']['table']" "statuses"
assert_value "schema.table" "$SCHEMA" "v=next(c for c in d['columns'] if c.get('name') == 'status')['on_delete']" "restrict"
assert_value "schema.table" "$SCHEMA" "v=next(c for c in d['columns'] if c.get('name') == 'status')['pg_type']" "text"
assert_value "schema.table" "$SCHEMA" "v=d['drift']['has_drift']" "False"

VAULT_SCHEMA=$(curl -sk "$BASE_URL/api/v1/tables/$VAULT/schema" \
  -H "Authorization: Bearer $READER_PAT")
assert_keys "schema.vault" "$VAULT_SCHEMA" kind vault tables total
assert_value "schema.vault" "$VAULT_SCHEMA" "v=d['kind']" "vault_table_schema"
assert_value "schema.vault" "$VAULT_SCHEMA" "v=d['total']" "1"
assert_value "schema.vault" "$VAULT_SCHEMA" "v=d['tables'][0]['name']" "$TABLE"

echo ""
echo "▸ 4. List tables — envelope shape"

LIST=$(curl -sk "$BASE_URL/api/v1/tables/$VAULT" \
  -H "Authorization: Bearer $PAT")
assert_keys "list" "$LIST" kind vault items total
assert_value "list" "$LIST" "v=d['kind']" "table"
assert_value "list" "$LIST" "v=d['vault']" "$VAULT"
assert_value "list" "$LIST" "v=d['total']" "1"
assert_value "list" "$LIST" "v=d['items'][0]['kind']" "table"
assert_value "list" "$LIST" "v=d['items'][0]['name']" "$TABLE"
assert_value "list" "$LIST" "v=d['items'][0]['uri']" "$TABLE_URI"

echo ""
echo "▸ 5. Rich column DDL — defaults, unique, check, index"

RICH="rich"
RICH_CREATE=$(curl -sk -X POST "$BASE_URL/api/v1/tables/$VAULT" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"$RICH\",\"description\":\"rich columns\",\"columns\":[{\"name\":\"email\",\"type\":\"text\",\"required\":true,\"unique\":true,\"check\":{\"op\":\"len_lte\",\"value\":80}},{\"name\":\"status\",\"type\":\"text\",\"default\":\"todo\",\"check\":{\"op\":\"in\",\"values\":[\"todo\",\"done\"]},\"index\":true},{\"name\":\"state\",\"type\":\"enum\",\"enum\":[\"draft\",\"active\"],\"default\":\"draft\"},{\"name\":\"qty\",\"type\":\"int\",\"default\":1,\"check\":{\"op\":\"gte\",\"value\":0}},{\"name\":\"rating\",\"type\":\"float\",\"default\":1.5},{\"name\":\"public_id\",\"type\":\"uuid\",\"default\":\"gen_random_uuid()\"},{\"name\":\"seen_at\",\"type\":\"timestamp\",\"default\":\"now()\"},{\"name\":\"tags\",\"type\":\"text[]\",\"default\":[\"new\"]}]}")
assert_keys "rich.create" "$RICH_CREATE" kind uri vault name columns unique_keys indexes
assert_value "rich.create" "$RICH_CREATE" "v=next(c for c in d['columns'] if c.get('name') == 'qty')['type']" "int"
assert_value "rich.create" "$RICH_CREATE" "v=next(c for c in d['columns'] if c.get('name') == 'state')['type']" "enum"
assert_value "rich.create" "$RICH_CREATE" "v=next(c for c in d['columns'] if c.get('name') == 'state')['enum'][0]" "draft"
assert_value "rich.create" "$RICH_CREATE" "v=next(c for c in d['columns'] if c.get('name') == 'status')['index']" "True"
assert_value "rich.create" "$RICH_CREATE" "v=len(d['unique_keys'])" "1"
assert_value "rich.create" "$RICH_CREATE" "v=len(d['indexes'])" "1"

BAD_TYPE=$(curl -sk -X POST "$BASE_URL/api/v1/tables/$VAULT" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d '{"name":"badtype","columns":[{"name":"x","type":"varchar(20)"}]}')
assert_value "rich.bad-type" "$BAD_TYPE" "v=d['code']" "invalid_column_type"

BAD_DEFAULT=$(curl -sk -o /dev/null -w "%{http_code}" -X POST "$BASE_URL/api/v1/tables/$VAULT" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d '{"name":"baddefault","columns":[{"name":"x","type":"timestamp","default":"now(); DROP"}]}')
[ "$BAD_DEFAULT" = "422" ] && pass "rich.bad-default: HTTP 422" || fail "rich.bad-default" "expected 422, got $BAD_DEFAULT"

BAD_CHECK=$(curl -sk -o /dev/null -w "%{http_code}" -X POST "$BASE_URL/api/v1/tables/$VAULT" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d '{"name":"badcheck","columns":[{"name":"x","type":"text","check":"x <> ''''"}]}')
[ "$BAD_CHECK" = "422" ] && pass "rich.bad-check: HTTP 422" || fail "rich.bad-check" "expected 422, got $BAD_CHECK"

RICH_INSERT=$(curl -sk -X POST "$BASE_URL/api/v1/tables/$VAULT/sql" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d "{\"sql\":\"INSERT INTO $RICH (email) VALUES ('one@test.dev')\"}")
assert_keys "rich.insert" "$RICH_INSERT" kind result vaults

RICH_SELECT=$(curl -sk -X POST "$BASE_URL/api/v1/tables/$VAULT/sql" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d "{\"sql\":\"SELECT email, status, state, qty, rating, public_id IS NOT NULL AS has_uuid, seen_at IS NOT NULL AS has_seen, tags[1] AS first_tag FROM $RICH\"}")
assert_value "rich.defaults" "$RICH_SELECT" "v=d['items'][0]['status']" "todo"
assert_value "rich.defaults" "$RICH_SELECT" "v=d['items'][0]['state']" "draft"
assert_value "rich.defaults" "$RICH_SELECT" "v=d['items'][0]['qty']" "1"
assert_value "rich.defaults" "$RICH_SELECT" "v=d['items'][0]['rating']" "1.5"
assert_value "rich.defaults" "$RICH_SELECT" "v=d['items'][0]['has_uuid']" "True"
assert_value "rich.defaults" "$RICH_SELECT" "v=d['items'][0]['has_seen']" "True"
assert_value "rich.defaults" "$RICH_SELECT" "v=d['items'][0]['first_tag']" "new"

RICH_DUP=$(curl -sk -o /dev/null -w "%{http_code}" -X POST "$BASE_URL/api/v1/tables/$VAULT/sql" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d "{\"sql\":\"INSERT INTO $RICH (email) VALUES ('one@test.dev')\"}")
[ "$RICH_DUP" = "409" ] && pass "rich.unique: HTTP 409" || fail "rich.unique" "expected 409, got $RICH_DUP"

RICH_CHECK=$(curl -sk -o /dev/null -w "%{http_code}" -X POST "$BASE_URL/api/v1/tables/$VAULT/sql" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d "{\"sql\":\"INSERT INTO $RICH (email, status) VALUES ('two@test.dev', 'blocked')\"}")
[ "$RICH_CHECK" = "400" ] && pass "rich.check: HTTP 400" || fail "rich.check" "expected 400, got $RICH_CHECK"

RICH_ENUM_CHECK=$(curl -sk -o /dev/null -w "%{http_code}" -X POST "$BASE_URL/api/v1/tables/$VAULT/sql" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d "{\"sql\":\"INSERT INTO $RICH (email, state) VALUES ('bad-state@test.dev', 'blocked')\"}")
[ "$RICH_ENUM_CHECK" = "400" ] && pass "rich.enum-check: HTTP 400" || fail "rich.enum-check" "expected 400, got $RICH_ENUM_CHECK"

RICH_ENUM_ALTER=$(curl -sk -X PATCH "$BASE_URL/api/v1/tables/$VAULT/$RICH" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d '{"alter_columns":[{"name":"state","enum":["draft","active","archived"]}]}')
assert_value "rich.enum-add" "$RICH_ENUM_ALTER" "v=next(c for c in d['columns'] if c.get('name') == 'state')['enum'][2]" "archived"

RICH_ENUM_DEFAULT=$(curl -sk -X PATCH "$BASE_URL/api/v1/tables/$VAULT/$RICH" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d '{"alter_columns":[{"name":"state","enum":["draft","active","archived"],"default":"active"}]}')
assert_value "rich.enum-default" "$RICH_ENUM_DEFAULT" "v=next(c for c in d['columns'] if c.get('name') == 'state')['default']" "active"

RICH_DEFAULT_INSERT=$(curl -sk -X POST "$BASE_URL/api/v1/tables/$VAULT/sql" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d "{\"sql\":\"INSERT INTO $RICH (email) VALUES ('default-state@test.dev')\"}")
assert_keys "rich.enum-default-insert" "$RICH_DEFAULT_INSERT" kind result vaults

RICH_DEFAULT_SELECT=$(curl -sk -X POST "$BASE_URL/api/v1/tables/$VAULT/sql" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d "{\"sql\":\"SELECT state FROM $RICH WHERE email = 'default-state@test.dev'\"}")
assert_value "rich.enum-default-row" "$RICH_DEFAULT_SELECT" "v=d['items'][0]['state']" "active"

RICH_DEFAULT_DELETE=$(curl -sk -X POST "$BASE_URL/api/v1/tables/$VAULT/sql" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d "{\"sql\":\"DELETE FROM $RICH WHERE email = 'default-state@test.dev'\"}")
assert_keys "rich.enum-default-delete" "$RICH_DEFAULT_DELETE" kind result vaults

RICH_ARCHIVED_INSERT=$(curl -sk -X POST "$BASE_URL/api/v1/tables/$VAULT/sql" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d "{\"sql\":\"INSERT INTO $RICH (email, state) VALUES ('archived@test.dev', 'archived')\"}")
assert_keys "rich.enum-insert" "$RICH_ARCHIVED_INSERT" kind result vaults

RICH_ENUM_RENAME=$(curl -sk -X PATCH "$BASE_URL/api/v1/tables/$VAULT/$RICH" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d '{"alter_columns":[{"name":"state","enum":["draft","active","closed"],"rename_enum_values":{"archived":"closed"}}]}')
assert_value "rich.enum-rename" "$RICH_ENUM_RENAME" "v=next(c for c in d['columns'] if c.get('name') == 'state')['enum'][2]" "closed"

RICH_RENAME_SELECT=$(curl -sk -X POST "$BASE_URL/api/v1/tables/$VAULT/sql" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d "{\"sql\":\"SELECT state FROM $RICH WHERE email = 'archived@test.dev'\"}")
assert_value "rich.enum-renamed-row" "$RICH_RENAME_SELECT" "v=d['items'][0]['state']" "closed"

RICH_ENUM_REMOVE=$(curl -sk -X PATCH "$BASE_URL/api/v1/tables/$VAULT/$RICH" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d '{"alter_columns":[{"name":"state","enum":["draft","closed"],"default":"draft"}]}')
assert_value "rich.enum-remove" "$RICH_ENUM_REMOVE" "v=len(next(c for c in d['columns'] if c.get('name') == 'state')['enum'])" "2"

RICH_ACTIVE_REJECT=$(curl -sk -o /dev/null -w "%{http_code}" -X POST "$BASE_URL/api/v1/tables/$VAULT/sql" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d "{\"sql\":\"INSERT INTO $RICH (email, state) VALUES ('active@test.dev', 'active')\"}")
[ "$RICH_ACTIVE_REJECT" = "400" ] && pass "rich.enum-remove-check: HTTP 400" || fail "rich.enum-remove-check" "expected 400, got $RICH_ACTIVE_REJECT"

RICH_ALTER=$(curl -sk -X PATCH "$BASE_URL/api/v1/tables/$VAULT/$RICH" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d '{"add_columns":[{"name":"score","type":"numeric","default":0,"check":{"op":"gte","value":0},"index":true}]}')
assert_value "rich.alter" "$RICH_ALTER" "v=next(c for c in d['columns'] if c.get('name') == 'score')['type']" "numeric"
assert_value "rich.alter" "$RICH_ALTER" "v=len(d['indexes'])" "2"

echo ""
echo "▸ 6. SQL SELECT — envelope shape (rows → items)"

INS=$(curl -sk -X POST "$BASE_URL/api/v1/tables/$VAULT/sql" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d "{\"sql\":\"INSERT INTO $TABLE (email, age_years, status) VALUES ('a@x', 30, 'active'), ('b@y', 40, 'inactive')\"}")
assert_keys "sql.insert" "$INS" kind result vaults
assert_value "sql.insert" "$INS" "v=d['kind']" "table_sql"

SEL=$(curl -sk -X POST "$BASE_URL/api/v1/tables/$VAULT/sql" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d "{\"sql\":\"SELECT email, age_years, status FROM $TABLE ORDER BY age_years\"}")
assert_keys "sql.select" "$SEL" kind columns items total vaults
assert_value "sql.select" "$SEL" "v=d['kind']" "table_query"
assert_value "sql.select" "$SEL" "v=d['total']" "2"
assert_value "sql.select" "$SEL" "v=d['items'][0]['email']" "a@x"
assert_value "sql.select" "$SEL" "v=d['items'][0]['status']" "active"

echo ""
echo "▸ 7. Drop table — envelope shape"

DROP=$(curl -sk -X DELETE "$BASE_URL/api/v1/tables/$VAULT/$TABLE" \
  -H "Authorization: Bearer $PAT")
assert_keys "drop" "$DROP" kind uri vault name deleted
assert_value "drop" "$DROP" "v=d['kind']" "table"
assert_value "drop" "$DROP" "v=d['vault']" "$VAULT"
assert_value "drop" "$DROP" "v=d['name']" "$TABLE"
assert_value "drop" "$DROP" "v=str(d['deleted']).lower()" "true"

echo ""
echo "▸ 8. Drop missing table — proper 4xx error"

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
