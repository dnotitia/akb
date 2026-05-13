#!/usr/bin/env bash
# E2E for vault template selection.
#   1. GET /vaults/templates (authenticated) returns expected shape.
#   2. GET /vaults/templates without auth → 401.
#   3. POST /vaults?template=engineering creates seeded collections.
#   4. POST /vaults?template=does-not-exist → 400.
#   5. POST /vaults with no template → empty vault (regression).
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
PASS=0
FAIL=0
ERRORS=()
pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

echo "╔══════════════════════════════════════════╗"
echo "║   Vault Template Selection E2E           ║"
echo "║   Target: $BASE_URL"
echo "╚══════════════════════════════════════════╝"

# 0. Bootstrap user + JWT
USER="tpl-e2e-$(date +%s)"
curl -sk -X POST "$BASE_URL/api/v1/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER\",\"email\":\"$USER@t.dev\",\"password\":\"test1234\"}" >/dev/null
JWT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER\",\"password\":\"test1234\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
[ -n "$JWT" ] && pass "bootstrap user" || { fail "bootstrap" "no JWT"; exit 1; }

# 1. GET /vaults/templates (authenticated)
echo ""
echo "▸ 1. List templates"
R=$(curl -sk "$BASE_URL/api/v1/vaults/templates" -H "Authorization: Bearer $JWT")
COUNT=$(echo "$R" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)))')
[ "$COUNT" -ge 1 ] 2>/dev/null && pass "GET /vaults/templates returns $COUNT templates" \
  || fail "GET /vaults/templates" "got $COUNT items"

HAS_ENG=$(echo "$R" | python3 -c \
  'import sys,json; ts=json.load(sys.stdin); print(any(t["name"]=="engineering" for t in ts))')
[ "$HAS_ENG" = "True" ] && pass "engineering template listed" \
  || fail "engineering missing" "$R"

ENG_COLLS=$(echo "$R" | python3 -c \
  'import sys,json; ts=json.load(sys.stdin); \
   t=next(x for x in ts if x["name"]=="engineering"); \
   print(t["collection_count"])')
[ "$ENG_COLLS" -ge 5 ] 2>/dev/null \
  && pass "engineering has $ENG_COLLS collections" \
  || fail "engineering collection_count" "got $ENG_COLLS"

ENG_FIRST_COLL=$(echo "$R" | python3 -c \
  'import sys,json; ts=json.load(sys.stdin); \
   t=next(x for x in ts if x["name"]=="engineering"); \
   print(t["collections"][0]["path"])')
[ -n "$ENG_FIRST_COLL" ] \
  && pass "engineering.collections[0].path = $ENG_FIRST_COLL" \
  || fail "collection summary missing path"

# 2. GET /vaults/templates without auth → 401
echo ""
echo "▸ 2. ACL"
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/vaults/templates")
[ "$HTTP" = "401" ] && pass "unauthenticated → 401" || fail "no-auth check" "got $HTTP"

# 3. POST /vaults?template=engineering creates seeded collections
echo ""
echo "▸ 3. Apply template"
VAULT="tpl-eng-$(date +%s)"
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" -X POST \
  "$BASE_URL/api/v1/vaults?name=$VAULT&template=engineering" \
  -H "Authorization: Bearer $JWT")
[ "$HTTP" = "200" ] && pass "POST /vaults?template=engineering" \
  || fail "create with template" "got $HTTP"

# Browse and confirm engineering collections present
R=$(curl -sk "$BASE_URL/api/v1/browse/$VAULT" -H "Authorization: Bearer $JWT")
HAS_SPECS=$(echo "$R" | python3 -c \
  'import sys,json; d=json.load(sys.stdin); \
   print(any(i.get("name")=="specs" for i in d.get("items",[])))')
[ "$HAS_SPECS" = "True" ] && pass "engineering collections seeded (specs visible)" \
  || fail "seed check" "$R"

# 4. POST /vaults?template=garbage → 400
echo ""
echo "▸ 4. Unknown template rejected"
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" -X POST \
  "$BASE_URL/api/v1/vaults?name=tpl-bad-$(date +%s)&template=does-not-exist" \
  -H "Authorization: Bearer $JWT")
[ "$HTTP" = "400" ] && pass "unknown template → 400" \
  || fail "validation" "got $HTTP"

# 5. POST /vaults with no template → empty vault (regression)
echo ""
echo "▸ 5. No-template regression"
VAULT2="tpl-empty-$(date +%s)"
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" -X POST \
  "$BASE_URL/api/v1/vaults?name=$VAULT2" \
  -H "Authorization: Bearer $JWT")
[ "$HTTP" = "200" ] && pass "no-template POST /vaults" \
  || fail "no-template" "got $HTTP"

# Summary
echo ""
echo "═══════════════════════════════════"
if [ $FAIL -eq 0 ]; then
  echo "✓ All $PASS tests passed"
  exit 0
else
  echo "✗ $FAIL failures (of $((PASS+FAIL)) total)"
  printf '  - %s\n' "${ERRORS[@]}"
  exit 1
fi
