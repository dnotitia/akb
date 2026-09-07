#!/bin/bash
#
# MCP transport and REST-boundary checks.
# Authenticated MCP product behavior lives in backend/tests/mcp_e2e and is
# executed through the official Python SDK by the repository-owned runtime.
#
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
VAULT="mcp-boundary-$(date +%s)"
E2E_USER="mcp-boundary-user-$(date +%s)"
PASS=0
FAIL=0
ERRORS=()

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

echo "╔══════════════════════════════════════════╗"
echo "║   AKB MCP Boundary E2E Suite             ║"
echo "║   Target: $BASE_URL/mcp/"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── 0. REST auth setup ───────────────────────────────────────
echo "▸ REST authentication"

curl -sk -X POST "$BASE_URL/api/v1/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$E2E_USER\",\"email\":\"$E2E_USER@test.dev\",\"password\":\"test1234\"}" >/dev/null 2>&1

JWT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$E2E_USER\",\"password\":\"test1234\"}" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

PAT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" \
  -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' \
  -d '{"name":"mcp-boundary"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

[ -n "$PAT" ] && pass "REST PAT acquired" || { fail "REST PAT" "could not get PAT"; exit 1; }

# ── 1. MCP initialize/session boundary ───────────────────────
echo ""
echo "▸ MCP transport boundary"

INIT_RESP=$(curl -sk -i -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"boundary-test","version":"1.0"}}}' 2>&1)

SID=$(echo "$INIT_RESP" | grep -i "mcp-session-id" | tr -d '\r' | awk '{print $2}')
INIT_BODY=$(echo "$INIT_RESP" | tail -1)
PROTO=$(echo "$INIT_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['protocolVersion'])" 2>/dev/null)

[ -n "$SID" ] && pass "MCP session ID received" || fail "MCP session ID" "missing"
[ "$PROTO" = "2025-03-26" ] && pass "MCP protocol version negotiated" || fail "MCP protocol" "expected 2025-03-26, got $PROTO"

curl -sk -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null 2>&1

AUTH_RESP=$(curl -sk -o /dev/null -w "%{http_code}" -X POST "$BASE_URL/mcp/" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' 2>/dev/null)
[ "$AUTH_RESP" = "401" ] && pass "MCP rejects unauthenticated requests" || fail "MCP auth" "expected 401, got $AUTH_RESP"

TOOLS_RESP=$(curl -sk -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' 2>&1)
TOOL_COUNT=$(echo "$TOOLS_RESP" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['result']['tools']))" 2>/dev/null)
[ "$TOOL_COUNT" -ge 22 ] 2>/dev/null && pass "MCP tools/list exposes $TOOL_COUNT tools" || fail "MCP tools/list" "expected >=22, got $TOOL_COUNT"

# ── 2. Direct REST shape checks retained from the mixed suite ─
echo ""
echo "▸ REST response boundaries"

VAULT_RESP=$(curl -sk -X POST "$BASE_URL/api/v1/vaults?name=$VAULT&description=MCP+boundary+test" \
  -H "Authorization: Bearer $PAT")
VAULT_ID=$(echo "$VAULT_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("vault_id",""))' 2>/dev/null)
[ -n "$VAULT_ID" ] && pass "REST vault setup" || fail "REST vault setup" "no vault_id"

TABLE_RESP=$(curl -sk -X POST "$BASE_URL/api/v1/tables/$VAULT" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d '{"name":"mcp_items","description":"MCP boundary table","columns":[{"name":"product","type":"text"}]}' 2>/dev/null)
TABLE_ID=$(echo "$TABLE_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("id",""))' 2>/dev/null)
[ -n "$TABLE_ID" ] && pass "REST table setup" || fail "REST table setup" "no id"

REST_SQL=$(curl -sk "$BASE_URL/api/v1/tables/$VAULT" \
  -H "Authorization: Bearer $PAT" \
  | python3 -c 'import sys,json; print(next((t.get("sql_name","MISSING") for t in json.load(sys.stdin).get("items",[]) if t.get("name")=="mcp_items"), "MISSING"))' 2>/dev/null)
[ "$REST_SQL" = "mcp_items" ] && pass "REST /tables exposes sql_name" || fail "REST sql_name" "got: $REST_SQL"

DOC_RESP=$(curl -sk -X POST "$BASE_URL/api/v1/documents" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d "{\"vault\":\"$VAULT\",\"collection\":\"specs\",\"title\":\"REST Boundary Document\",\"content\":\"## Public\\n\\nREST boundary check.\",\"type\":\"note\",\"tags\":[]}" 2>/dev/null)
DOC_ID=$(echo "$DOC_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("doc_id",""))' 2>/dev/null)
[ -n "$DOC_ID" ] && pass "REST document setup" || fail "REST document setup" "no doc_id"

PROFILE=$(curl -sk -X PATCH "$BASE_URL/api/v1/auth/me" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d '{"display_name":"MCP Boundary User"}')
PROFILE_OK=$(echo "$PROFILE" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("updated",False))' 2>/dev/null)
[ "$PROFILE_OK" = "True" ] && pass "REST profile update" || fail "REST profile update" "not updated"

PUBLISH_RESP=$(curl -sk -X POST "$BASE_URL/api/v1/documents/$VAULT/$DOC_ID/publish" \
  -H "Authorization: Bearer $PAT")
PUB_SLUG=$(echo "$PUBLISH_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("slug",""))' 2>/dev/null)
[ -n "$PUB_SLUG" ] && pass "REST publish" || fail "REST publish" "no slug"

PUB_TITLE=$(curl -sk "$BASE_URL/api/v1/public/$PUB_SLUG" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin).get("title",""))' 2>/dev/null)
[ "$PUB_TITLE" = "REST Boundary Document" ] && pass "REST public access" || fail "REST public access" "wrong title: $PUB_TITLE"

curl -sk -X POST "$BASE_URL/api/v1/documents/$VAULT/$DOC_ID/unpublish" \
  -H "Authorization: Bearer $PAT" >/dev/null 2>&1
PUBLIC_STATUS=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$PUB_SLUG" 2>/dev/null)
[ "$PUBLIC_STATUS" = "404" ] && pass "REST unpublish revokes public access" || fail "REST unpublish" "expected 404, got $PUBLIC_STATUS"

# ── 3. Session termination ───────────────────────────────────
echo ""
echo "▸ MCP session termination"

TERM_RESP=$(curl -sk -X DELETE "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $PAT" \
  -H "mcp-session-id: $SID" 2>&1)
TERMINATED=$(echo "$TERM_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("terminated",False))' 2>/dev/null)
[ "$TERMINATED" = "True" ] && pass "MCP session terminated" || fail "MCP session termination" "failed"

# Best-effort REST cleanup. The suite result is determined by assertions above.
curl -sk -X DELETE "$BASE_URL/api/v1/vaults/$VAULT" \
  -H "Authorization: Bearer $PAT" >/dev/null 2>&1 || true

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   Results: $PASS passed, $FAIL failed"
echo "╚══════════════════════════════════════════╝"

if [ "$FAIL" -gt 0 ]; then
  echo ""
  echo "Failures:"
  for error in "${ERRORS[@]}"; do echo "  ✗ $error"; done
  exit 1
fi
exit 0
