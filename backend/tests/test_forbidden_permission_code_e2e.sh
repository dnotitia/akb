#!/bin/bash
#
# AKB #221 E2E — access-gated MCP tools must surface a STABLE permission code,
# not the generic code=internal. Covers:
#   - non-admin (reader) calling an admin-gated tool -> code=permission_denied
#   - any caller referencing a non-existent vault    -> code=not_found
# (both previously fell through call_tool's catch-all as code=internal).
#
#   AKB_URL=http://localhost:18082 bash tests/test_forbidden_permission_code_e2e.sh
#
set -uo pipefail
BASE_URL="${AKB_URL:-http://localhost:8000}"
source "$(dirname "$0")/mcp_modern.sh"
SUF="$(date +%s)-$$"
VAULT="fb-e2e-$SUF"; ADMIN="fb-admin-$SUF"; READER="fb-reader-$SUF"
PASS=0; FAIL=0; ERRORS=()
pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }
echo "▸ #221 permission-code e2e → $BASE_URL"

register_pat() {
  local u=$1
  curl -sk -X POST "$BASE_URL/api/v1/auth/register" -H 'Content-Type: application/json' \
    -d "{\"username\":\"$u\",\"email\":\"$u@test.dev\",\"password\":\"test1234\"}" >/dev/null 2>&1
  local jwt
  jwt=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" -H 'Content-Type: application/json' \
    -d "{\"username\":\"$u\",\"password\":\"test1234\"}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])' 2>/dev/null)
  curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" -H "Authorization: Bearer $jwt" \
    -H 'Content-Type: application/json' -d '{"name":"e2e"}' \
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])' 2>/dev/null
}
mcp_session() {
  mcp_modern_discover "$1" e2e >/dev/null && echo modern
}
init_notify() {
  :
}
MCP_ID=10
mcp() {
  MCP_ID=$((MCP_ID+1))
  mcp_modern_call "$1" "$3" "$4" "$MCP_ID" e2e \
    | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['result']['content'][0]['text'])" 2>/dev/null
}
field() { python3 -c "import sys,json; print(json.loads(sys.stdin.read())$1)" 2>/dev/null; }

# setup: admin user + vault + a table; reader user granted reader
APAT=$(register_pat "$ADMIN"); ASID=$(mcp_session "$APAT"); init_notify "$APAT" "$ASID"
[ -n "$APAT" ] && [ -n "$ASID" ] && pass "admin session" || { fail setup "no admin session"; exit 1; }
mcp "$APAT" "$ASID" akb_create_vault "{\"name\":\"$VAULT\",\"description\":\"#221\"}" >/dev/null
mcp "$APAT" "$ASID" akb_create_table "{\"vault\":\"$VAULT\",\"name\":\"t\",\"columns\":[{\"name\":\"a\",\"type\":\"text\"}]}" >/dev/null
RPAT=$(register_pat "$READER"); RSID=$(mcp_session "$RPAT"); init_notify "$RPAT" "$RSID"
mcp "$APAT" "$ASID" akb_grant "{\"vault\":\"$VAULT\",\"user\":\"$READER\",\"role\":\"reader\"}" >/dev/null

# AC: reader (non-admin) calling an admin-gated tool -> permission_denied (NOT internal)
R=$(mcp "$RPAT" "$RSID" akb_alter_table "{\"uri\":\"akb://$VAULT/table/t\",\"add_columns\":[{\"name\":\"b\",\"type\":\"text\"}]}")
CODE=$(echo "$R" | field "['code']")
[ "$CODE" = "permission_denied" ] && pass "reader→alter_table = permission_denied" || fail "forbidden→permission_denied" "got code=$CODE resp=$R"
[ "$CODE" != "internal" ] && pass "not surfaced as internal" || fail "still internal" "$R"

# reader → drop_table (also admin-gated) -> permission_denied
R=$(mcp "$RPAT" "$RSID" akb_drop_table "{\"uri\":\"akb://$VAULT/table/t\"}")
CODE=$(echo "$R" | field "['code']")
[ "$CODE" = "permission_denied" ] && pass "reader→drop_table = permission_denied" || fail "drop_table forbidden" "got code=$CODE resp=$R"

# reader → grant (admin-gated) -> permission_denied
R=$(mcp "$RPAT" "$RSID" akb_grant "{\"vault\":\"$VAULT\",\"user\":\"$ADMIN\",\"role\":\"admin\"}")
CODE=$(echo "$R" | field "['code']")
[ "$CODE" = "permission_denied" ] && pass "reader→grant = permission_denied" || fail "grant forbidden" "got code=$CODE resp=$R"

# non-existent vault reference -> not_found (NOT internal)
R=$(mcp "$APAT" "$ASID" akb_vault_info "{\"vault\":\"does-not-exist-$SUF\"}")
CODE=$(echo "$R" | field "['code']")
[ "$CODE" = "not_found" ] && pass "missing vault→vault_info = not_found" || fail "notfound→not_found" "got code=$CODE resp=$R"

# control: admin CAN alter (no regression — gate still lets the right role through)
R=$(mcp "$APAT" "$ASID" akb_alter_table "{\"uri\":\"akb://$VAULT/table/t\",\"add_columns\":[{\"name\":\"c\",\"type\":\"text\"}]}")
echo "$R" | field "['code']" | grep -q . && fail "admin alter regression" "$R" || pass "admin alter still succeeds (no regression)"

echo ""
if [ "$FAIL" -gt 0 ]; then printf '%s\n' "${ERRORS[@]}"; fi
# Canonical summary — the CI runner parses this line for the counts.
echo "── #221 e2e — Results: $PASS passed, $FAIL failed ──"
[ "$FAIL" -eq 0 ] || exit 1
