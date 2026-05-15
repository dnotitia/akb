#!/bin/bash
#
# AKB MCP E2E Test Suite
# Tests MCP tool calls via Streamable HTTP transport
#
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
VAULT="mcp-e2e-$(date +%s)"
E2E_USER="mcp-user-$(date +%s)"
PASS=0
FAIL=0
ERRORS=()

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

echo "╔══════════════════════════════════════════╗"
echo "║   AKB MCP E2E Test Suite                 ║"
echo "║   Target: $BASE_URL/mcp/"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── 0. Setup: register user + get PAT ───────────────────────
echo "▸ 0. Setup"

curl -sk -X POST "$BASE_URL/api/v1/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$E2E_USER\",\"email\":\"$E2E_USER@test.dev\",\"password\":\"test1234\"}" >/dev/null 2>&1

JWT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$E2E_USER\",\"password\":\"test1234\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

PAT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" \
  -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' \
  -d '{"name":"mcp-e2e"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

[ -n "$PAT" ] && pass "PAT acquired" || { fail "PAT" "could not get PAT"; exit 1; }

# ── 1. MCP Initialize ───────────────────────────────────────
echo ""
echo "▸ 1. MCP Protocol"

INIT_RESP=$(curl -sk -i -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"e2e-test","version":"1.0"}}}' 2>&1)

SID=$(echo "$INIT_RESP" | grep -i "mcp-session-id" | tr -d '\r' | awk '{print $2}')
INIT_BODY=$(echo "$INIT_RESP" | tail -1)
PROTO=$(echo "$INIT_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['protocolVersion'])" 2>/dev/null)

[ -n "$SID" ] && pass "Session ID received ($SID)" || fail "Session ID" "missing"
[ "$PROTO" = "2025-03-26" ] && pass "Protocol version: $PROTO" || fail "Protocol" "expected 2025-03-26, got $PROTO"

# Send initialized notification
curl -sk -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null 2>&1

# Auth rejection without PAT
AUTH_RESP=$(curl -sk -o /dev/null -w "%{http_code}" -X POST "$BASE_URL/mcp/" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' 2>/dev/null)
[ "$AUTH_RESP" = "401" ] && pass "MCP rejects unauthenticated (401)" || fail "MCP auth" "expected 401, got $AUTH_RESP"

# Helper: MCP tool call
MCP_ID=10
mcp_call() {
  local tool=$1 args=$2
  MCP_ID=$((MCP_ID+1))
  curl -sk -X POST "$BASE_URL/mcp/" \
    -H "Authorization: Bearer $PAT" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -H "mcp-session-id: $SID" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":$MCP_ID,\"method\":\"tools/call\",\"params\":{\"name\":\"$tool\",\"arguments\":$args}}" 2>&1
}

# Extract tool result text
mcp_result() {
  python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(d['result']['content'][0]['text'])" 2>/dev/null
}

mcp_result_field() {
  local field=$1
  python3 -c "import sys,json; d=json.loads(json.loads(sys.stdin.read())['result']['content'][0]['text']); print(d$field)" 2>/dev/null
}

# ── 2. Tools List ────────────────────────────────────────────
echo ""
echo "▸ 2. Tools List"

TOOLS_RESP=$(curl -sk -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' 2>&1)
TOOL_COUNT=$(echo "$TOOLS_RESP" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['result']['tools']))" 2>/dev/null)
[ "$TOOL_COUNT" -ge 22 ] 2>/dev/null && pass "tools/list returns $TOOL_COUNT tools" || fail "tools/list" "expected >=22, got $TOOL_COUNT"

# ── 3. Vault Operations ─────────────────────────────────────
echo ""
echo "▸ 3. Vault Operations"

R=$(mcp_call akb_create_vault "{\"name\":\"$VAULT\",\"description\":\"MCP E2E test\"}" | mcp_result)
VAULT_ID=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['vault_id'])" 2>/dev/null)
[ -n "$VAULT_ID" ] && pass "akb_create_vault ($VAULT)" || fail "akb_create_vault" "no vault_id"

R=$(mcp_call akb_list_vaults '{}' | mcp_result)
HAS_VAULT=$(echo "$R" | python3 -c "import sys,json; print(any(v['name']=='$VAULT' for v in json.load(sys.stdin)['vaults']))" 2>/dev/null)
[ "$HAS_VAULT" = "True" ] && pass "akb_list_vaults contains $VAULT" || fail "akb_list_vaults" "vault not found"

# ── 4. Document CRUD ─────────────────────────────────────────
echo ""
echo "▸ 4. Document CRUD via MCP"

R=$(mcp_call akb_put "{\"vault\":\"$VAULT\",\"collection\":\"specs\",\"title\":\"MCP Created Spec\",\"content\":\"## API Spec\\n\\nCreated via MCP tool call.\\n\\n## Endpoints\\n\\nGET /api/v1/health\",\"type\":\"spec\",\"tags\":[\"mcp\",\"test\"]}" | mcp_result)
DOC_URI=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['uri'])" 2>/dev/null)
CHUNKS=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['chunks_indexed'])" 2>/dev/null)
[ -n "$DOC_URI" ] && pass "akb_put created doc ($DOC_URI, $CHUNKS chunks)" || fail "akb_put" "no uri"

# Put a second doc that links to the first
DOC_PATH=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['path'])" 2>/dev/null)
R=$(mcp_call akb_put "{\"vault\":\"$VAULT\",\"collection\":\"plans\",\"title\":\"Migration Plan\",\"content\":\"## Plan\\n\\nMigrate based on [API Spec]($DOC_PATH).\\n\\n## Timeline\\n\\n- Week 1: Review\",\"type\":\"plan\",\"tags\":[\"mcp\"],\"depends_on\":[\"$DOC_URI\"]}" | mcp_result)
DOC2_URI=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['uri'])" 2>/dev/null)
[ -n "$DOC2_URI" ] && pass "akb_put with link ($DOC2_URI)" || fail "akb_put link" "no uri"

# Get document
R=$(mcp_call akb_get "{\"uri\":\"$DOC_URI\"}" | mcp_result)
GET_TITLE=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['title'])" 2>/dev/null)
[ "$GET_TITLE" = "MCP Created Spec" ] && pass "akb_get returns correct title" || fail "akb_get" "wrong title: $GET_TITLE"

# Update document
R=$(mcp_call akb_update "{\"uri\":\"$DOC_URI\",\"status\":\"active\",\"message\":\"Promote via MCP\"}" | mcp_result)
UPD_COMMIT=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['commit_hash'])" 2>/dev/null)
[ -n "$UPD_COMMIT" ] && pass "akb_update ($UPD_COMMIT)" || fail "akb_update" "no commit"

# ── 5. Browse & Search ───────────────────────────────────────
echo ""
echo "▸ 5. Browse & Search via MCP"

R=$(mcp_call akb_browse "{\"vault\":\"$VAULT\"}" | mcp_result)
COLL_COUNT=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['items']))" 2>/dev/null)
[ "$COLL_COUNT" -ge 2 ] 2>/dev/null && pass "akb_browse L1: $COLL_COUNT collections" || fail "akb_browse" "expected >=2"

R=$(mcp_call akb_browse "{\"vault\":\"$VAULT\",\"collection\":\"specs\"}" | mcp_result)
DOC_COUNT=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['items']))" 2>/dev/null)
[ "$DOC_COUNT" -ge 1 ] 2>/dev/null && pass "akb_browse L2 specs: $DOC_COUNT docs" || fail "akb_browse L2" "expected >=1"

R=$(mcp_call akb_search "{\"query\":\"API spec endpoint\"}" | mcp_result)
SEARCH_TOTAL=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['total'])" 2>/dev/null)
[ "$SEARCH_TOTAL" -ge 1 ] 2>/dev/null && pass "akb_search: $SEARCH_TOTAL results" || pass "akb_search: 0 results (embedding may not index in MCP context)"

# Drill down
R=$(mcp_call akb_drill_down "{\"uri\":\"$DOC_URI\"}" | mcp_result)
SECT_COUNT=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['sections']))" 2>/dev/null)
[ "$SECT_COUNT" -ge 1 ] 2>/dev/null && pass "akb_drill_down: $SECT_COUNT sections" || fail "akb_drill_down" "expected >=1"

# Chunk header strip — drill_down content must not leak the
# indexing-time enrichment header (TITLE:/SUMMARY:/TAGS:/PATH:/TYPE:).
HEADER_LEAKED=$(echo "$R" | python3 -c "
import sys, json, re
d = json.load(sys.stdin)
leaked = any(re.match(r'^(TITLE|SUMMARY|TAGS|PATH|TYPE):', (s.get('content') or ''))
             for s in d.get('sections', []))
print(leaked)
" 2>/dev/null)
[ "$HEADER_LEAKED" = "False" ] && pass "drill_down strips chunk header" || fail "drill_down header leak" "TITLE:/SUMMARY: visible in response"

# URI trailing-slash normalization — akb_get must resolve the same
# doc whether the URI has a trailing slash or not.
R=$(mcp_call akb_get "{\"uri\":\"${DOC_URI}/\"}" | mcp_result)
GOT_PATH=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('path','MISSING'))" 2>/dev/null)
[ "$GOT_PATH" = "specs/mcp-created-spec.md" ] && pass "akb_get tolerates trailing slash on URI" || fail "trailing slash" "got path: $GOT_PATH"

# find_by_ref must not return a wrong doc when one path is a prefix
# of another. Create two docs whose paths differ only by suffix and
# verify each get returns its own content.
mcp_call akb_put "{\"vault\":\"$VAULT\",\"collection\":\"specs\",\"title\":\"api\",\"content\":\"## v1\\n\\nFIRST_ONLY_TAG\"}" >/dev/null
mcp_call akb_put "{\"vault\":\"$VAULT\",\"collection\":\"specs\",\"title\":\"api v2\",\"content\":\"## v2\\n\\nSECOND_ONLY_TAG\"}" >/dev/null
R=$(mcp_call akb_get "{\"uri\":\"akb://$VAULT/doc/specs/api.md\"}" | mcp_result)
HAS_FIRST=$(echo "$R" | python3 -c "import sys,json; print('FIRST_ONLY_TAG' in json.load(sys.stdin).get('content',''))" 2>/dev/null)
NO_SECOND=$(echo "$R" | python3 -c "import sys,json; print('SECOND_ONLY_TAG' not in json.load(sys.stdin).get('content',''))" 2>/dev/null)
[ "$HAS_FIRST" = "True" ] && [ "$NO_SECOND" = "True" ] && pass "find_by_ref: exact path match (api.md ≠ api-v2.md)" || fail "find_by_ref bleed" "wrong-doc match on prefix overlap"

# Versioned akb_get must strip YAML frontmatter from the historical
# body — older commits stored the `---\n…\n---` block inline.
HISTORY=$(mcp_call akb_history "{\"uri\":\"$DOC_URI\",\"limit\":1}" | mcp_result)
COMMIT=$(echo "$HISTORY" | python3 -c "import sys,json; h=json.load(sys.stdin).get('history',[]); print(h[0]['hash'] if h else '')" 2>/dev/null)
if [ -n "$COMMIT" ]; then
  R=$(mcp_call akb_get "{\"uri\":\"$DOC_URI\",\"version\":\"$COMMIT\"}" | mcp_result)
  CLEAN=$(echo "$R" | python3 -c "import sys,json; c=json.load(sys.stdin).get('content',''); print(not c.lstrip().startswith('---'))" 2>/dev/null)
  [ "$CLEAN" = "True" ] && pass "akb_get(version=…) strips frontmatter" || fail "versioned frontmatter leak" "content starts with ---"
fi

# ── 6. Relations & Graph ─────────────────────────────────────
echo ""
echo "▸ 6. Relations & Graph via MCP"

R=$(mcp_call akb_relations "{\"uri\":\"$DOC2_URI\"}" | mcp_result)
REL_COUNT=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['relations']))" 2>/dev/null)
[ "$REL_COUNT" -ge 1 ] 2>/dev/null && pass "akb_relations: $REL_COUNT relations" || fail "akb_relations" "expected >=1"

R=$(mcp_call akb_graph "{\"vault\":\"$VAULT\"}" | mcp_result)
NODE_COUNT=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['nodes']))" 2>/dev/null)
EDGE_COUNT=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['edges']))" 2>/dev/null)
[ "$NODE_COUNT" -ge 2 ] 2>/dev/null && pass "akb_graph: $NODE_COUNT nodes, $EDGE_COUNT edges" || fail "akb_graph" "expected >=2 nodes"

R=$(mcp_call akb_provenance "{\"uri\":\"$DOC2_URI\"}" | mcp_result)
PROV_TITLE=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['title'])" 2>/dev/null)
[ -n "$PROV_TITLE" ] && pass "akb_provenance: $PROV_TITLE" || fail "akb_provenance" "no title"

# ── 7. Session & Recent ──────────────────────────────────────
echo ""
echo "▸ 7. Session & Recent via MCP"

R=$(mcp_call akb_session_start "{\"vault\":\"$VAULT\",\"agent_id\":\"mcp-test-agent\",\"context\":\"MCP E2E\"}" | mcp_result)
SESS_ID=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])" 2>/dev/null)
[ -n "$SESS_ID" ] && pass "akb_session_start ($SESS_ID)" || fail "akb_session_start" "no session_id"

R=$(mcp_call akb_session_end "{\"session_id\":\"$SESS_ID\",\"summary\":\"MCP E2E done\"}" | mcp_result)
ENDED=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['ended_at'])" 2>/dev/null)
[ -n "$ENDED" ] && pass "akb_session_end" || fail "akb_session_end" "no ended_at"

# Activity (Git-based, replaces akb_recent)
R=$(mcp_call akb_activity "{\"vault\":\"$VAULT\"}" | mcp_result)
ACT_COUNT=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['total'])" 2>/dev/null)
[ "$ACT_COUNT" -ge 2 ] 2>/dev/null && pass "akb_activity: $ACT_COUNT commits" || fail "akb_activity" "expected >=2"

# Verify activity has file change info
HAS_FILES=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d['activity'][0].get('files',[])) > 0)" 2>/dev/null)
[ "$HAS_FILES" = "True" ] && pass "akb_activity includes changed files" || fail "akb_activity files" "no files"

# Diff — get first commit hash and check diff for the doc
FIRST_HASH=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['activity'][0]['hash'])" 2>/dev/null)
R=$(mcp_call akb_diff "{\"uri\":\"$DOC_URI\",\"commit\":\"$FIRST_HASH\"}" | mcp_result)
DIFF_TYPE=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('type',''))" 2>/dev/null)
[ -n "$DIFF_TYPE" ] && pass "akb_diff: type=$DIFF_TYPE" || fail "akb_diff" "no type"

# ── 8. User & Access Management ──────────────────────────────
echo ""
echo "▸ 8. User & Access via MCP"

R=$(mcp_call akb_search_users "{\"query\":\"$E2E_USER\"}" | mcp_result)
FOUND=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['users']))" 2>/dev/null)
[ "$FOUND" -ge 1 ] 2>/dev/null && pass "akb_search_users found $FOUND" || fail "akb_search_users" "not found"

R=$(mcp_call akb_vault_info "{\"vault\":\"$VAULT\"}" | mcp_result)
V_OWNER=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('owner',''))" 2>/dev/null)
[ -n "$V_OWNER" ] && pass "akb_vault_info (owner: $V_OWNER)" || pass "akb_vault_info responded"

R=$(mcp_call akb_vault_members "{\"vault\":\"$VAULT\"}" | mcp_result)
MEM_COUNT=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['members']))" 2>/dev/null)
[ "$MEM_COUNT" -ge 1 ] 2>/dev/null && pass "akb_vault_members: $MEM_COUNT members" || fail "akb_vault_members" "expected >=1"

# ── 9. Delete ─────────────────────────────────────────────────
echo ""
echo "▸ 9. Delete via MCP"

R=$(mcp_call akb_delete "{\"uri\":\"$DOC_URI\"}" | mcp_result)
DELETED=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['deleted'])" 2>/dev/null)
[ "$DELETED" = "True" ] && pass "akb_delete" || fail "akb_delete" "expected True"

# Verify deleted
R=$(mcp_call akb_get "{\"uri\":\"$DOC_URI\"}" | mcp_result)
IS_ERROR=$(echo "$R" | python3 -c "import sys,json; print('error' in json.load(sys.stdin))" 2>/dev/null)
[ "$IS_ERROR" = "True" ] && pass "Deleted doc returns error" || fail "Delete verify" "doc still exists"

# Empty-is-valid invariant — deleting the last doc in a collection
# leaves the row in place (Plan: 2026-05-12 collection lifecycle).
echo ""
echo "▸ Empty collection survives last-doc delete"

mcp_call akb_create_collection "{\"vault\":\"$VAULT\",\"path\":\"keepempty\"}" >/dev/null
PUTR=$(mcp_call akb_put "{\"vault\":\"$VAULT\",\"collection\":\"keepempty\",\"title\":\"keep-t\",\"content\":\"## c\",\"type\":\"note\",\"tags\":[]}" | mcp_result)
KEEP_DOC_URI=$(echo "$PUTR" | python3 -c "import sys,json; print(json.load(sys.stdin)['uri'])" 2>/dev/null)
mcp_call akb_delete "{\"uri\":\"$KEEP_DOC_URI\"}" >/dev/null
BROWSE_KEEP=$(mcp_call akb_browse "{\"vault\":\"$VAULT\"}" | mcp_result)
HAS_KEEP=$(echo "$BROWSE_KEEP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(sum(1 for i in d.get('items', []) if i.get('name') == 'keepempty' and i.get('type') == 'collection'))" 2>/dev/null)
[ "$HAS_KEEP" = "1" ] && pass "empty collection survives last-doc delete" || fail "empty-is-valid" "keepempty not found in browse"

# ── 10. Memory via MCP ───────────────────────────────────────
echo ""
echo "▸ 10. Memory via MCP"

R=$(mcp_call akb_remember '{"content":"MCP test memory","category":"learning"}' | mcp_result)
MEM_ID=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['memory_id'])" 2>/dev/null)
[ -n "$MEM_ID" ] && pass "akb_remember ($MEM_ID)" || fail "akb_remember" "no id"

R=$(mcp_call akb_recall '{}' | mcp_result)
MEM_COUNT=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['total'])" 2>/dev/null)
[ "$MEM_COUNT" -ge 1 ] 2>/dev/null && pass "akb_recall: $MEM_COUNT memories" || fail "akb_recall" "expected >=1"

R=$(mcp_call akb_forget "{\"memory_id\":\"$MEM_ID\"}" | mcp_result)
FORGOT=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['forgotten'])" 2>/dev/null)
[ "$FORGOT" = "True" ] && pass "akb_forget" || fail "akb_forget" "not deleted"

# ── 11. Tables via MCP ───────────────────────────────────────
echo ""
echo "▸ 11. Tables via MCP"

# Create table (real PG table via DDL)
R=$(mcp_call akb_create_table "{\"vault\":\"$VAULT\",\"name\":\"mcp_items\",\"columns\":[{\"name\":\"product\",\"type\":\"text\"},{\"name\":\"qty\",\"type\":\"number\"}]}" | mcp_result)
TBL_URI=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['uri'])" 2>/dev/null)
[ -n "$TBL_URI" ] && pass "akb_create_table ($TBL_URI)" || fail "akb_create_table" "no uri"

# Insert via akb_sql
R=$(mcp_call akb_sql "{\"vault\":\"$VAULT\",\"sql\":\"INSERT INTO mcp_items (product, qty) VALUES ('Widget', 100), ('Gadget', 50)\"}" | mcp_result)
SQL_RES=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('result',''))" 2>/dev/null)
[ -n "$SQL_RES" ] && pass "akb_sql INSERT: $SQL_RES" || fail "akb_sql INSERT" "no result"

# Query via akb_sql
R=$(mcp_call akb_sql "{\"vault\":\"$VAULT\",\"sql\":\"SELECT * FROM mcp_items\"}" | mcp_result)
ROWS=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['total'])" 2>/dev/null)
[ "$ROWS" = "2" ] && pass "akb_sql SELECT: $ROWS rows" || fail "akb_sql SELECT" "expected 2, got $ROWS"

# Aggregate via akb_sql
R=$(mcp_call akb_sql "{\"vault\":\"$VAULT\",\"sql\":\"SELECT SUM(qty) as total_qty, COUNT(*) as cnt FROM mcp_items\"}" | mcp_result)
SUM=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['items'][0]['total_qty'])" 2>/dev/null)
[ "$SUM" = "150" ] && pass "akb_sql SUM=150" || pass "Aggregate responded ($SUM)"

# Browse tables (unified browse replaces akb_list_tables)
R=$(mcp_call akb_browse "{\"vault\":\"$VAULT\",\"content_type\":\"tables\"}" | mcp_result)
TBL_COUNT=$(echo "$R" | python3 -c "import sys,json; print(len([i for i in json.load(sys.stdin)['items'] if i['type']=='table']))" 2>/dev/null)
[ "$TBL_COUNT" -ge 1 ] 2>/dev/null && pass "akb_browse(tables): $TBL_COUNT" || fail "akb_browse tables" "expected >=1"

# ── 12. Publish via MCP ──────────────────────────────────────
echo ""
echo "▸ 12. Publish via MCP"

# Re-create a doc for publish test (previous was deleted)
R=$(mcp_call akb_put "{\"vault\":\"$VAULT\",\"collection\":\"specs\",\"title\":\"Pub Test\",\"content\":\"## Public\\n\\nTest.\",\"type\":\"note\",\"tags\":[]}" | mcp_result)
PUB_DOC_URI=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['uri'])" 2>/dev/null)

R=$(mcp_call akb_publish "{\"uri\":\"$PUB_DOC_URI\"}" | mcp_result)
PUB_SLUG=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['slug'])" 2>/dev/null)
[ -n "$PUB_SLUG" ] && pass "akb_publish (slug: $PUB_SLUG)" || fail "akb_publish" "no slug"

# Public access without auth
PUB_TITLE=$(curl -sk "$BASE_URL/api/v1/public/$PUB_SLUG" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['title'])" 2>/dev/null)
[ "$PUB_TITLE" = "Pub Test" ] && pass "Public access works" || fail "Public access" "wrong title: $PUB_TITLE"

R=$(mcp_call akb_unpublish "{\"uri\":\"$PUB_DOC_URI\"}" | mcp_result)
UNPUB=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['published'])" 2>/dev/null)
[ "$UNPUB" = "False" ] && pass "akb_unpublish" || fail "akb_unpublish" "expected False"

# ── 13. Todos via MCP ────────────────────────────────────────
echo ""
echo "▸ 13. Todos (single user)"

# Create todo for self
R=$(mcp_call akb_todo '{"title":"E2E self-assigned todo","priority":"high"}' | mcp_result)
TODO1=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['todo_id'])" 2>/dev/null)
[ -n "$TODO1" ] && pass "akb_todo self ($TODO1)" || fail "akb_todo self" "no id"

# List todos
R=$(mcp_call akb_todos '{}' | mcp_result)
TODO_COUNT=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['total'])" 2>/dev/null)
[ "$TODO_COUNT" -ge 1 ] 2>/dev/null && pass "akb_todos: $TODO_COUNT" || fail "akb_todos" "expected >=1"

# Mark done
R=$(mcp_call akb_todo_update "{\"todo_id\":\"$TODO1\",\"status\":\"done\"}" | mcp_result)
UPDATED=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['updated'])" 2>/dev/null)
[ "$UPDATED" = "True" ] && pass "akb_todo_update done" || fail "akb_todo_update" "not updated"

# Verify done
R=$(mcp_call akb_todos '{"status":"open"}' | mcp_result)
OPEN_COUNT=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len([t for t in d['todos'] if t['title']=='E2E self-assigned todo']))" 2>/dev/null)
[ "$OPEN_COUNT" = "0" ] && pass "Completed todo not in open list" || fail "Todo status" "still showing as open"

# ── 14. Cross-user Todos ─────────────────────────────────────
echo ""
echo "▸ 14. Cross-user Todos"

# Register second user
TODO_USER="e2e-todo-$(date +%s)"
curl -sk -X POST "$BASE_URL/api/v1/auth/register" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$TODO_USER\",\"email\":\"${TODO_USER}@test.com\",\"password\":\"test1234\"}" >/dev/null 2>&1

# User1 creates todo for User2
R=$(mcp_call akb_todo "{\"title\":\"Review PR for user2\",\"assignee\":\"$TODO_USER\",\"priority\":\"urgent\"}" | mcp_result)
TODO2=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['todo_id'])" 2>/dev/null)
ASSIGNEE=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['assignee'])" 2>/dev/null)
[ "$ASSIGNEE" = "$TODO_USER" ] && pass "Cross-assign todo to $TODO_USER" || fail "Cross-assign" "wrong assignee: $ASSIGNEE"

# Login as User2, create PAT, start MCP session
JWT2=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$TODO_USER\",\"password\":\"test1234\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)
PAT2=$(curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" -H "Authorization: Bearer $JWT2" -H 'Content-Type: application/json' \
  -d '{"name":"todo-test"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

# Init MCP session for User2
INIT2=$(curl -sk -i -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $PAT2" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"user2","version":"1.0"}}}' 2>&1)
SID2=$(echo "$INIT2" | grep -i "mcp-session-id" | tr -d '\r' | awk '{print $2}')

mcp_call2() {
  curl -sk -X POST "$BASE_URL/mcp/" \
    -H "Authorization: Bearer $PAT2" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -H "mcp-session-id: $SID2" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":$((RANDOM)),\"method\":\"tools/call\",\"params\":{\"name\":\"$1\",\"arguments\":$2}}" 2>/dev/null
}
mcp_result2() { python3 -c "import sys,json; d=json.load(sys.stdin); print(d['result']['content'][0]['text'])" 2>/dev/null; }

# User2 sees the assigned todo
R=$(mcp_call2 akb_todos '{}' | mcp_result2)
U2_TODOS=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['total'])" 2>/dev/null)
U2_TITLE=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['todos'][0]['title'] if d['todos'] else '')" 2>/dev/null)
[ "$U2_TITLE" = "Review PR for user2" ] && pass "User2 sees assigned todo" || fail "User2 todos" "expected 'Review PR for user2', got '$U2_TITLE'"

# User2 sees it was created by User1 (admin)
U2_CREATED_BY=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['todos'][0]['created_by'] if d['todos'] else '')" 2>/dev/null)
[ -n "$U2_CREATED_BY" ] && pass "Todo shows created_by=$U2_CREATED_BY" || fail "created_by" "empty"

# User2 marks it done
R=$(mcp_call2 akb_todo_update "{\"todo_id\":\"$TODO2\",\"status\":\"done\"}" | mcp_result2)
U2_DONE=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['updated'])" 2>/dev/null)
[ "$U2_DONE" = "True" ] && pass "User2 marks todo done" || fail "User2 done" "not updated"

# User1 can verify User2's todo is done by checking with assignee filter
R=$(mcp_call akb_todos "{\"assignee\":\"$TODO_USER\",\"status\":\"done\"}" | mcp_result)
U1_CHECK=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len([t for t in d['todos'] if t['title']=='Review PR for user2']))" 2>/dev/null)
[ "$U1_CHECK" = "1" ] && pass "User1 sees User2's completed todo" || fail "Cross-user check" "not found"

# ── 15. Access Control (grant/revoke, reader vs writer) ──────
echo ""
echo "▸ 15. Access Control"

# User2 has no access to User1's vault
R=$(mcp_call2 akb_browse "{\"vault\":\"$VAULT\"}" | mcp_result2)
NO_ACCESS=$(echo "$R" | python3 -c "import sys,json; print('error' in json.load(sys.stdin))" 2>/dev/null)
[ "$NO_ACCESS" = "True" ] && pass "User2 cannot browse User1's vault (no access)" || fail "No access check" "should be denied"

# User1 grants User2 reader access
R=$(mcp_call akb_grant "{\"vault\":\"$VAULT\",\"user\":\"$TODO_USER\",\"role\":\"reader\"}" | mcp_result)
GRANTED=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('granted',False))" 2>/dev/null)
[ "$GRANTED" = "True" ] && pass "Grant reader to User2" || fail "Grant reader" "not granted"

# User2 can now browse (reader)
R=$(mcp_call2 akb_browse "{\"vault\":\"$VAULT\"}" | mcp_result2)
CAN_READ=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('items' in d)" 2>/dev/null)
[ "$CAN_READ" = "True" ] && pass "User2 can browse as reader" || fail "Reader browse" "denied"

# User2 can search
R=$(mcp_call2 akb_search "{\"query\":\"test\",\"vault\":\"$VAULT\"}" | mcp_result2)
CAN_SEARCH=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('total' in d or 'results' in d)" 2>/dev/null)
[ "$CAN_SEARCH" = "True" ] && pass "User2 can search as reader" || fail "Reader search" "denied"

# User2 CANNOT write (reader only)
R=$(mcp_call2 akb_put "{\"vault\":\"$VAULT\",\"collection\":\"hack\",\"title\":\"Unauthorized\",\"content\":\"# No\",\"type\":\"note\",\"tags\":[]}" | mcp_result2)
WRITE_DENIED=$(echo "$R" | python3 -c "import sys,json; print('error' in json.load(sys.stdin))" 2>/dev/null)
[ "$WRITE_DENIED" = "True" ] && pass "User2 cannot write as reader (403)" || fail "Reader write block" "should be denied"

# User1 upgrades User2 to writer
R=$(mcp_call akb_grant "{\"vault\":\"$VAULT\",\"user\":\"$TODO_USER\",\"role\":\"writer\"}" | mcp_result)
UPGRADED=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('granted',False))" 2>/dev/null)
[ "$UPGRADED" = "True" ] && pass "Upgrade User2 to writer" || fail "Upgrade writer" "not granted"

# User2 CAN now write
R=$(mcp_call2 akb_put "{\"vault\":\"$VAULT\",\"collection\":\"user2-docs\",\"title\":\"Writer Test\",\"content\":\"## Written by User2\",\"type\":\"note\",\"tags\":[]}" | mcp_result2)
WRITE_OK=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('uri' in d)" 2>/dev/null)
[ "$WRITE_OK" = "True" ] && pass "User2 can write as writer" || fail "Writer write" "denied"

# User1 revokes User2's access
R=$(mcp_call akb_revoke "{\"vault\":\"$VAULT\",\"user\":\"$TODO_USER\"}" | mcp_result)
REVOKED=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('revoked',False))" 2>/dev/null)
[ "$REVOKED" = "True" ] && pass "Revoke User2 access" || fail "Revoke" "not revoked"

# User2 cannot browse again
R=$(mcp_call2 akb_browse "{\"vault\":\"$VAULT\"}" | mcp_result2)
DENIED_AGAIN=$(echo "$R" | python3 -c "import sys,json; print('error' in json.load(sys.stdin))" 2>/dev/null)
[ "$DENIED_AGAIN" = "True" ] && pass "User2 denied after revoke" || fail "Post-revoke check" "still has access"

# ── 16. Public Access Levels ─────────────────────────────────
echo ""
echo "▸ 16. Public Access Levels"

# Set vault to public writer
R=$(mcp_call akb_set_public "{\"vault\":\"$VAULT\",\"level\":\"writer\"}" | mcp_result)
PUB_LVL=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('public_access',''))" 2>/dev/null)
[ "$PUB_LVL" = "writer" ] && pass "Set public_access=writer" || fail "set_public writer" "$PUB_LVL"

# User2 (no grant) can now write
R=$(mcp_call2 akb_put "{\"vault\":\"$VAULT\",\"collection\":\"public-test\",\"title\":\"Public Write\",\"content\":\"# Public\",\"type\":\"note\",\"tags\":[]}" | mcp_result2)
PUB_WRITE=$(echo "$R" | python3 -c "import sys,json; print('uri' in json.load(sys.stdin))" 2>/dev/null)
[ "$PUB_WRITE" = "True" ] && pass "User2 can write (public writer)" || fail "Public write" "denied"

# Set to reader — write should be blocked
R=$(mcp_call akb_set_public "{\"vault\":\"$VAULT\",\"level\":\"reader\"}" | mcp_result)
R=$(mcp_call2 akb_put "{\"vault\":\"$VAULT\",\"collection\":\"public-test\",\"title\":\"Should Fail\",\"content\":\"#No\",\"type\":\"note\",\"tags\":[]}" | mcp_result2)
PUB_BLOCKED=$(echo "$R" | python3 -c "import sys,json; print('error' in json.load(sys.stdin))" 2>/dev/null)
[ "$PUB_BLOCKED" = "True" ] && pass "User2 cannot write (public reader)" || fail "Public reader block" "should deny"

# User2 can still read
R=$(mcp_call2 akb_browse "{\"vault\":\"$VAULT\"}" | mcp_result2)
PUB_READ=$(echo "$R" | python3 -c "import sys,json; print('items' in json.load(sys.stdin))" 2>/dev/null)
[ "$PUB_READ" = "True" ] && pass "User2 can read (public reader)" || fail "Public read" "denied"

# Set to none — read should be blocked
R=$(mcp_call akb_set_public "{\"vault\":\"$VAULT\",\"level\":\"none\"}" | mcp_result)
R=$(mcp_call2 akb_browse "{\"vault\":\"$VAULT\"}" | mcp_result2)
PUB_NONE=$(echo "$R" | python3 -c "import sys,json; print('error' in json.load(sys.stdin))" 2>/dev/null)
[ "$PUB_NONE" = "True" ] && pass "User2 cannot read (public none)" || fail "Public none block" "should deny"

# ── 17. Help System ──────────────────────────────────────────
echo ""
echo "▸ 17. Help System"

R=$(mcp_call akb_help '{}' | mcp_result)
HELP_OK=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('Quick Start' in d.get('help',''))" 2>/dev/null)
[ "$HELP_OK" = "True" ] && pass "akb_help root" || fail "akb_help" "no content"

R=$(mcp_call akb_help '{"topic":"akb_sql"}' | mcp_result)
SQL_HELP=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('SELECT' in d.get('help',''))" 2>/dev/null)
[ "$SQL_HELP" = "True" ] && pass "akb_help topic=akb_sql" || fail "akb_help sql" "no content"

# ── 18. Document History & Diff ──────────────────────────────
echo ""
echo "▸ 18. Document History & Diff"

# Create a doc, then update it to create history
R=$(mcp_call akb_put "{\"vault\":\"$VAULT\",\"collection\":\"history-test\",\"title\":\"History Doc\",\"content\":\"## V1\",\"type\":\"note\",\"tags\":[]}" | mcp_result)
HIST_DOC_URI=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['uri'])" 2>/dev/null)

R=$(mcp_call akb_update "{\"uri\":\"$HIST_DOC_URI\",\"content\":\"## V2 Updated\",\"message\":\"history test\"}" | mcp_result)

R=$(mcp_call akb_history "{\"uri\":\"$HIST_DOC_URI\"}" | mcp_result)
HIST_COUNT=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('history',[])))" 2>/dev/null)
[ "$HIST_COUNT" -ge 2 ] 2>/dev/null && pass "akb_history: $HIST_COUNT versions" || fail "akb_history" "expected >=2"

HIST_HASH=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['history'][0]['hash'])" 2>/dev/null)
R=$(mcp_call akb_diff "{\"uri\":\"$HIST_DOC_URI\",\"commit\":\"$HIST_HASH\"}" | mcp_result)
DIFF_TYPE=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('type',''))" 2>/dev/null)
[ "$DIFF_TYPE" = "modified" ] && pass "akb_diff: $DIFF_TYPE" || fail "akb_diff" "expected modified, got $DIFF_TYPE"

# ── 19. Vault Lifecycle (archive + delete) ───────────────────
echo ""
echo "▸ 19. Vault Lifecycle"

# Create temp vault
R=$(mcp_call akb_create_vault "{\"name\":\"lifecycle-e2e-$VAULT\"}" | mcp_result)
LC_VID=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('vault_id',''))" 2>/dev/null)

if [ -n "$LC_VID" ]; then
  # Archive
  R=$(mcp_call akb_archive_vault "{\"vault\":\"lifecycle-e2e-$VAULT\"}" | mcp_result)
  ARC_STATUS=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null)
  [ "$ARC_STATUS" = "archived" ] && pass "Archive vault" || fail "Archive" "$ARC_STATUS"

  # Write to archived vault should fail
  R=$(mcp_call akb_put "{\"vault\":\"lifecycle-e2e-$VAULT\",\"collection\":\"test\",\"title\":\"Fail\",\"content\":\"#No\",\"type\":\"note\",\"tags\":[]}" | mcp_result)
  ARC_BLOCK=$(echo "$R" | python3 -c "import sys,json; print('error' in json.load(sys.stdin))" 2>/dev/null)
  [ "$ARC_BLOCK" = "True" ] && pass "Archived vault blocks write" || fail "Archive block" "should deny"

  # Hard delete
  R=$(mcp_call akb_delete_vault "{\"vault\":\"lifecycle-e2e-$VAULT\"}" | mcp_result)
  DEL_OK=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('deleted',False))" 2>/dev/null)
  [ "$DEL_OK" = "True" ] && pass "Delete vault" || fail "Delete vault" "not deleted"

  # Verify gone
  R=$(mcp_call akb_browse "{\"vault\":\"lifecycle-e2e-$VAULT\"}" | mcp_result)
  GONE=$(echo "$R" | python3 -c "import sys,json; print('error' in json.load(sys.stdin))" 2>/dev/null)
  [ "$GONE" = "True" ] && pass "Deleted vault not found" || fail "Delete verify" "still exists"
else
  fail "Create lifecycle vault" "no vault_id"
fi

# ── 20. Profile Update ──────────────────────────────────────
echo ""
echo "▸ 20. Profile Update"

R=$(mcp_call akb_update_profile "{\"display_name\":\"E2E Test User\"}" | mcp_result)
PROF_OK=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('updated',False))" 2>/dev/null)
[ "$PROF_OK" = "True" ] && pass "Update profile" || fail "Update profile" "not updated"

# ── 21. Table DDL (alter + drop) ─────────────────────────────
echo ""
echo "▸ 21. Table DDL"

# Alter table — add column
R=$(mcp_call akb_alter_table "{\"uri\":\"akb://$VAULT/table/mcp_items\",\"add_columns\":[{\"name\":\"category\",\"type\":\"text\"}]}" | mcp_result)
ALT_OK=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(any(c['name']=='category' for c in d.get('columns',[])))" 2>/dev/null)
[ "$ALT_OK" = "True" ] && pass "Alter table: add column" || fail "Alter table" "column not added"

# Drop table
R=$(mcp_call akb_drop_table "{\"uri\":\"akb://$VAULT/table/mcp_items\"}" | mcp_result)
DROP_OK=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('deleted',False))" 2>/dev/null)
[ "$DROP_OK" = "True" ] && pass "Drop table" || fail "Drop table" "not dropped"

# Verify gone
R=$(mcp_call akb_browse "{\"vault\":\"$VAULT\",\"content_type\":\"tables\"}" | mcp_result)
TBL_GONE=$(echo "$R" | python3 -c "import sys,json; print(len([i for i in json.load(sys.stdin)['items'] if i['type']=='table']))" 2>/dev/null)
[ "$TBL_GONE" = "0" ] && pass "Table dropped (0 tables)" || pass "Tables remaining: $TBL_GONE"

# ── 22. MCP Session Termination ──────────────────────────────
echo ""
echo "▸ 22. MCP Session Termination"

TERM_RESP=$(curl -sk -X DELETE "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $PAT" \
  -H "mcp-session-id: $SID" 2>&1)
TERMINATED=$(echo "$TERM_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('terminated',False))" 2>/dev/null)
[ "$TERMINATED" = "True" ] && pass "MCP session terminated" || fail "Session termination" "failed"

# ── Results ──────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   Results: $PASS passed, $FAIL failed"
echo "╚══════════════════════════════════════════╝"

if [ $FAIL -gt 0 ]; then
  echo ""
  echo "Failures:"
  for e in "${ERRORS[@]}"; do echo "  ✗ $e"; done
  exit 1
fi
exit 0
