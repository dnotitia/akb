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

# The legacy transport is stateless, so no session is minted. That is the
# property worth asserting: a session is what binds a client to the replica
# that answered `initialize`, and behind a load balancer that costs it roughly
# half its calls.
[ -z "$SID" ] && pass "MCP mints no session (any replica can answer)" || fail "MCP session" "a session id was issued: $SID"
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
CATALOG_STATUS=$(echo "$TOOLS_RESP" | python3 -c '
import json, sys

tools = json.load(sys.stdin)["result"]["tools"]
by_name = {tool["name"]: tool for tool in tools}
expected_groups = {
    "akb_discover": {"list_vaults", "vault_info", "browse", "search", "grep"},
    "akb_document_read": {"get", "section", "activity", "history", "diff", "provenance"},
    "akb_relationships": {"relations", "graph"},
    "akb_vault_access": {"members", "explain"},
    "akb_identity": {"whoami", "search_users"},
}
expected_standalone = {
    "akb_publications", "akb_export",
    "akb_put", "akb_update", "akb_edit", "akb_move", "akb_delete", "akb_grep_replace",
    "akb_create_collection", "akb_delete_collection",
    "akb_link", "akb_unlink",
    "akb_grant", "akb_revoke", "akb_transfer_ownership", "akb_set_public",
    "akb_publish", "akb_publication_snapshot", "akb_unpublish",
    "akb_create_vault", "akb_archive_vault", "akb_delete_vault",
    "akb_create_table", "akb_alter_table", "akb_drop_table",
    "akb_import",
}
expected_names = set(expected_groups) | expected_standalone | {"akb_help", "akb_sql"}
actual_names = set(by_name)
action_diff = {}
for name, wanted in expected_groups.items():
    schema = by_name.get(name, {}).get("inputSchema", {})
    actual = {
        branch.get("properties", {}).get("action", {}).get("const")
        for branch in schema.get("oneOf", [])
    }
    if actual != wanted:
        action_diff[name] = {"missing": sorted(wanted - actual), "unexpected": sorted(actual - wanted)}
if actual_names == expected_names and not action_diff:
    print("True")
else:
    print(json.dumps({
        "missing_tools": sorted(expected_names - actual_names),
        "unexpected_tools": sorted(actual_names - expected_names),
        "action_diff": action_diff,
    }, sort_keys=True))
' 2>/dev/null)
[ "$CATALOG_STATUS" = "True" ] && pass "MCP tools/list exposes the candidate capability/action catalog" || fail "MCP tools/list" "candidate catalog mismatch: $CATALOG_STATUS"

# Repeat the call: behind two replicas a stateful transport failed roughly half
# of these with `Session not found`, and a single sample would have missed it.
MCP_OK=0
for _ in $(seq 1 12); do
  RC=$(curl -sk -o /dev/null -w "%{http_code}" -X POST "$BASE_URL/mcp/" \
    -H "Authorization: Bearer $PAT" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -H "mcp-protocol-version: 2025-03-26" \
    -d '{"jsonrpc":"2.0","id":3,"method":"tools/list"}' 2>/dev/null)
  [ "$RC" = "200" ] && MCP_OK=$((MCP_OK+1))
done
[ "$MCP_OK" = "12" ] && pass "MCP legacy tools/list 12/12 across replicas" || fail "MCP replica spread" "$MCP_OK/12 succeeded"

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
TABLE_URI=$(echo "$TABLE_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("uri",""))' 2>/dev/null)
[ -n "$TABLE_URI" ] && pass "REST table setup" || fail "REST table setup" "no uri"

REST_SQL=$(curl -sk "$BASE_URL/api/v1/tables/$VAULT" \
  -H "Authorization: Bearer $PAT" \
  | python3 -c 'import sys,json; print(next((t.get("sql_name","MISSING") for t in json.load(sys.stdin).get("items",[]) if t.get("name")=="mcp_items"), "MISSING"))' 2>/dev/null)
[ "$REST_SQL" = "mcp_items" ] && pass "REST /tables exposes sql_name" || fail "REST sql_name" "got: $REST_SQL"

DOC_RESP=$(curl -sk -X POST "$BASE_URL/api/v1/documents" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d "{\"vault\":\"$VAULT\",\"collection\":\"specs\",\"title\":\"REST Boundary Document\",\"content\":\"## Public\\n\\nREST boundary check.\",\"type\":\"note\",\"tags\":[]}" 2>/dev/null)
DOC_URI=$(echo "$DOC_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("uri",""))' 2>/dev/null)
[ -n "$DOC_URI" ] && pass "REST document setup" || fail "REST document setup" "no uri"

PROFILE=$(curl -sk -X PATCH "$BASE_URL/api/v1/auth/me" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d '{"display_name":"MCP Boundary User"}')
PROFILE_OK=$(echo "$PROFILE" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("updated",False))' 2>/dev/null)
[ "$PROFILE_OK" = "True" ] && pass "REST profile update" || fail "REST profile update" "not updated"

PUBLISH_RESP=$(curl -sk -X POST "$BASE_URL/api/v1/publications/$VAULT/create" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d "{\"uri\":\"$DOC_URI\"}")
PUB_SLUG=$(echo "$PUBLISH_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("slug",""))' 2>/dev/null)
[ -n "$PUB_SLUG" ] && pass "REST publish" || fail "REST publish" "no slug"

PUB_TITLE=$(curl -sk "$BASE_URL/api/v1/public/$PUB_SLUG" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin).get("title",""))' 2>/dev/null)
[ "$PUB_TITLE" = "REST Boundary Document" ] && pass "REST public access" || fail "REST public access" "wrong title: $PUB_TITLE"

curl -sk -X DELETE "$BASE_URL/api/v1/publications/$VAULT/$PUB_SLUG" \
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
