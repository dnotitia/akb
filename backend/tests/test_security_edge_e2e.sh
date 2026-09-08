#!/bin/bash
#
# REST and health boundary checks for security-sensitive surfaces.
# Detailed MCP security behavior lives in backend/tests/mcp_e2e and runs
# through the official Python SDK. The MCP calls below only prepare data for
# the retained REST assertions.
#
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
PASS=0
FAIL=0
ERRORS=()

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

echo "╔══════════════════════════════════════════╗"
echo "║   AKB REST Security Boundary E2E Tests   ║"
echo "║   Target: $BASE_URL"
echo "╚══════════════════════════════════════════╝"
echo ""

setup_user() {
  local user=$1
  curl -sk -X POST "$BASE_URL/api/v1/auth/register" \
    -H 'Content-Type: application/json' \
    -d "{\"username\":\"$user\",\"email\":\"$user@test.dev\",\"password\":\"test1234\"}" >/dev/null 2>&1
  local jwt
  jwt=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" \
    -H 'Content-Type: application/json' \
    -d "{\"username\":\"$user\",\"password\":\"test1234\"}" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)
  curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" \
    -H "Authorization: Bearer $jwt" \
    -H 'Content-Type: application/json' \
    -d '{"name":"security-rest-e2e"}' \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null
}

USER1="sec-rest-u1-$(date +%s)"
USER2="sec-rest-u2-$(date +%s)"
PAT1=$(setup_user "$USER1")
PAT2=$(setup_user "$USER2")
[ -n "$PAT1" ] && [ -n "$PAT2" ] && pass "2 users created" || { fail "Setup" "user creation failed"; exit 1; }

INIT_RESP=$(curl -sk -i -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $PAT1" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"security-rest-e2e","version":"1.0"}}}' 2>&1)
SID=$(echo "$INIT_RESP" | grep -i "mcp-session-id" | tr -d '\r' | awk '{print $2}')
[ -n "$SID" ] && pass "MCP setup session acquired" || { fail "MCP setup" "missing session id"; exit 1; }
curl -sk -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $PAT1" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null 2>&1

mcp_call() {
  local tool=$1 args=$2
  curl -sk -X POST "$BASE_URL/mcp/" \
    -H "Authorization: Bearer $PAT1" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -H "mcp-session-id: $SID" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":10,\"method\":\"tools/call\",\"params\":{\"name\":\"$tool\",\"arguments\":$args}}" \
    | python3 -c 'import sys,json; print(json.loads(sys.stdin.read())["result"]["content"][0]["text"])' 2>/dev/null
}

VAULT="sec-rest-private-$(date +%s)"
R=$(mcp_call akb_create_vault "{\"name\":\"$VAULT\",\"description\":\"REST security boundary\"}")
echo "$R" | python3 -c 'import sys,json; assert json.load(sys.stdin).get("vault_id")' >/dev/null 2>&1 \
  && pass "private vault prepared" || { fail "Vault setup" "MCP setup failed"; exit 1; }
R=$(mcp_call akb_put "{\"vault\":\"$VAULT\",\"collection\":\"secrets\",\"title\":\"REST Security Document\",\"content\":\"# Private\\nREST_SECURITY_MARKER\"}")
DOC_URI=$(echo "$R" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("uri", ""))' 2>/dev/null)
[ -n "$DOC_URI" ] && pass "REST drill-down document prepared" || fail "Document setup" "missing URI"

echo ""
echo "▸ REST drill-down ACL"
HTTP=$(curl -sS -k -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer $PAT2" \
  --get --data-urlencode "uri=$DOC_URI" \
  "$BASE_URL/api/v1/drill-down")
[ "$HTTP" = "403" ] && pass "unauthorized REST drill-down → 403" || fail "REST drill-down ACL" "got HTTP $HTTP"

echo ""
echo "▸ Vault-scoped health ACL"
R=$(curl -sk -H "Authorization: Bearer $PAT1" "$BASE_URL/health/vault/$VAULT")
HAS=$(echo "$R" | python3 -c "import sys,json; print('vector_store' in json.load(sys.stdin))" 2>/dev/null)
[ "$HAS" = "True" ] && pass "owner sees own vault health" || fail "vault health self" "$R"
HTTP=$(curl -sS -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $PAT2" "$BASE_URL/health/vault/$VAULT")
[ "$HTTP" = "403" ] && pass "non-member vault health → 403" || fail "vault health ACL" "got HTTP $HTTP"
HTTP=$(curl -sS -o /dev/null -w "%{http_code}" "$BASE_URL/health/vault/$VAULT")
[ "$HTTP" = "401" ] && pass "unauthenticated vault health → 401" || fail "vault health auth" "got HTTP $HTTP"
HTTP=$(curl -sS -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $PAT1" "$BASE_URL/health/vault/nonexistent-vault-xyz")
[ "$HTTP" = "404" ] && pass "unknown vault health → 404" || fail "vault health 404" "got HTTP $HTTP"

echo ""
echo "▸ Global health disclosure boundary"
ANON=$(curl -sk "$BASE_URL/health")
HTTP=$(curl -sS -o /dev/null -w "%{http_code}" "$BASE_URL/health")
[ "$HTTP" = "200" ] && pass "anonymous /health → 200" || fail "anonymous health code" "got HTTP $HTTP"
ANON_OK=$(echo "$ANON" | python3 -c "import sys,json; d=json.load(sys.stdin); print('vector_store' in d and 'rbac' not in d and 'audit' not in d)" 2>/dev/null)
[ "$ANON_OK" = "True" ] && pass "anonymous health withholds rbac and audit" || fail "anonymous health body" "$ANON"
AUTHED=$(curl -sk -H "Authorization: Bearer $PAT1" "$BASE_URL/health")
AUTH_OK=$(echo "$AUTHED" | python3 -c "import sys,json; d=json.load(sys.stdin); print('vector_store' in d and 'rbac' in d and 'audit' in d)" 2>/dev/null)
[ "$AUTH_OK" = "True" ] && pass "authenticated health includes rbac and audit" || fail "authenticated health body" "$AUTHED"

echo ""
echo "▸ Vault role-source disclosure"
INFO=$(curl -sk -H "Authorization: Bearer $PAT1" "$BASE_URL/api/v1/vaults/$VAULT/info")
ROLE_SOURCE=$(echo "$INFO" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("role_source", "MISSING"))' 2>/dev/null)
[ "$ROLE_SOURCE" = "member" ] && pass "owner role_source=member" || fail "member role_source" "got $ROLE_SOURCE"

PUBLIC_VAULT="sec-rest-public-$(date +%s)-$$"
curl -sk -X POST "$BASE_URL/api/v1/vaults?name=$PUBLIC_VAULT&description=REST+public+role+source&public_access=writer" \
  -H "Authorization: Bearer $PAT1" >/dev/null
INFO=$(curl -sk -H "Authorization: Bearer $PAT2" "$BASE_URL/api/v1/vaults/$PUBLIC_VAULT/info")
ROLE_SOURCE=$(echo "$INFO" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("role_source", "MISSING"))' 2>/dev/null)
[ "$ROLE_SOURCE" = "public" ] && pass "non-member role_source=public" || fail "public role_source" "got $ROLE_SOURCE"
curl -sk -X DELETE "$BASE_URL/api/v1/vaults/$PUBLIC_VAULT" -H "Authorization: Bearer $PAT1" >/dev/null

R=$(mcp_call akb_delete_vault "{\"vault\":\"$VAULT\"}")
DELETED=$(echo "$R" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("deleted", False))' 2>/dev/null)
[ "$DELETED" = "True" ] && pass "private setup vault cleaned" || fail "cleanup" "deleted=$DELETED"

echo ""
echo "Results: $PASS passed, $FAIL failed"
if [ "$FAIL" -gt 0 ]; then
  printf ' - %s\n' "${ERRORS[@]}"
  exit 1
fi
