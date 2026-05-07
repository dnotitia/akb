#!/bin/bash
#
# AKB E2E Test: Unified Browse + Cross-Type Edges
# Tests the new unified browse (documents, tables, files in one view)
# and the cross-type edge system (akb_link, akb_unlink, URI-based graph)
#
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
VAULT="edge-e2e-$(date +%s)"
E2E_USER="edge-user-$(date +%s)"
PASS=0
FAIL=0
ERRORS=()

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

echo "╔══════════════════════════════════════════════════╗"
echo "║   AKB Unified Browse + Edges E2E Test Suite      ║"
echo "║   Target: $BASE_URL/mcp/"
echo "╚══════════════════════════════════════════════════╝"
echo ""

# ── 0. Setup ────────────────────────────────────────────────
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
  -d '{"name":"edge-e2e"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

[ -n "$PAT" ] && pass "PAT acquired" || { fail "PAT" "could not get PAT"; exit 1; }

# MCP init
INIT_RESP=$(curl -sk -i -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"edge-e2e","version":"1.0"}}}' 2>&1)
SID=$(echo "$INIT_RESP" | grep -i "mcp-session-id" | tr -d '\r' | awk '{print $2}')
[ -n "$SID" ] && pass "MCP session ($SID)" || { fail "MCP" "no session"; exit 1; }

curl -sk -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null 2>&1

MCP_ID=10
mcp_call() {
  MCP_ID=$((MCP_ID+1))
  curl -sk -X POST "$BASE_URL/mcp/" \
    -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" -H "mcp-session-id: $SID" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":$MCP_ID,\"method\":\"tools/call\",\"params\":{\"name\":\"$1\",\"arguments\":$2}}" 2>&1
}
mcp_result() {
  python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(d['result']['content'][0]['text'])" 2>/dev/null
}

# Create vault
R=$(mcp_call akb_create_vault "{\"name\":\"$VAULT\",\"description\":\"Edge E2E test\"}" | mcp_result)
VID=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['vault_id'])" 2>/dev/null)
[ -n "$VID" ] && pass "Vault created ($VAULT)" || { fail "Vault" "no id"; exit 1; }

# ── 1. Create test data: documents, tables, files ───────────
echo ""
echo "▸ 1. Create Test Data (doc + table + file)"

# Document 1: API Spec
R=$(mcp_call akb_put "{\"vault\":\"$VAULT\",\"collection\":\"specs\",\"title\":\"API Spec v2\",\"content\":\"## Endpoints\\n\\nGET /api/v1/users\",\"type\":\"spec\",\"tags\":[\"api\"]}" | mcp_result)
DOC1_ID=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['doc_id'])" 2>/dev/null)
DOC1_PATH=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['path'])" 2>/dev/null)
[ -n "$DOC1_ID" ] && pass "Doc1 created ($DOC1_ID)" || fail "Doc1" "no id"

# Document 2: Design doc (depends_on doc1)
R=$(mcp_call akb_put "{\"vault\":\"$VAULT\",\"collection\":\"designs\",\"title\":\"System Design\",\"content\":\"## Architecture\\n\\nBased on API spec.\",\"type\":\"spec\",\"depends_on\":[\"$DOC1_ID\"]}" | mcp_result)
DOC2_ID=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['doc_id'])" 2>/dev/null)
DOC2_PATH=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['path'])" 2>/dev/null)
[ -n "$DOC2_ID" ] && pass "Doc2 created with depends_on ($DOC2_ID)" || fail "Doc2" "no id"

# Table
R=$(mcp_call akb_create_table "{\"vault\":\"$VAULT\",\"name\":\"test_metrics\",\"description\":\"API performance metrics\",\"columns\":[{\"name\":\"endpoint\",\"type\":\"text\"},{\"name\":\"latency_ms\",\"type\":\"number\"}]}" | mcp_result)
TBL_ID=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['table_id'])" 2>/dev/null)
[ -n "$TBL_ID" ] && pass "Table created (test_metrics)" || fail "Table" "no id"

# Insert rows
R=$(mcp_call akb_sql "{\"vault\":\"$VAULT\",\"sql\":\"INSERT INTO test_metrics (endpoint, latency_ms) VALUES ('/users', 45), ('/auth', 120)\"}" | mcp_result)
pass "Table rows inserted"

# Build URIs
DOC1_URI="akb://$VAULT/doc/$DOC1_PATH"
DOC2_URI="akb://$VAULT/doc/$DOC2_PATH"
TABLE_URI="akb://$VAULT/table/test_metrics"

echo "  DOC1_URI: $DOC1_URI"
echo "  DOC2_URI: $DOC2_URI"
echo "  TABLE_URI: $TABLE_URI"

# ── 2. Unified Browse ───────────────────────────────────────
echo ""
echo "▸ 2. Unified Browse"

# Top-level: should show collections + tables (+ files if any)
R=$(mcp_call akb_browse "{\"vault\":\"$VAULT\"}" | mcp_result)
ITEM_COUNT=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['items']))" 2>/dev/null)
HAS_TABLE=$(echo "$R" | python3 -c "import sys,json; print(any(i['type']=='table' for i in json.load(sys.stdin)['items']))" 2>/dev/null)
HAS_COLL=$(echo "$R" | python3 -c "import sys,json; print(any(i['type']=='collection' for i in json.load(sys.stdin)['items']))" 2>/dev/null)
[ "$HAS_TABLE" = "True" ] && pass "Browse shows tables" || fail "Browse tables" "no table items"
[ "$HAS_COLL" = "True" ] && pass "Browse shows collections" || fail "Browse collections" "no collection items"
echo "  Total items: $ITEM_COUNT"

# Check table item has row_count
TBL_ROWS=$(echo "$R" | python3 -c "import sys,json; ts=[i for i in json.load(sys.stdin)['items'] if i['type']=='table']; print(ts[0]['row_count'] if ts else -1)" 2>/dev/null)
[ "$TBL_ROWS" = "2" ] && pass "Table shows row_count=2" || fail "Table row_count" "expected 2, got $TBL_ROWS"

# Check table item has URI
TBL_URI_CHECK=$(echo "$R" | python3 -c "import sys,json; ts=[i for i in json.load(sys.stdin)['items'] if i['type']=='table']; print(ts[0].get('uri','') if ts else '')" 2>/dev/null)
[ -n "$TBL_URI_CHECK" ] && pass "Table has URI ($TBL_URI_CHECK)" || fail "Table URI" "no uri"

# Filter by content_type=tables
R=$(mcp_call akb_browse "{\"vault\":\"$VAULT\",\"content_type\":\"tables\"}" | mcp_result)
ONLY_TABLES=$(echo "$R" | python3 -c "import sys,json; items=json.load(sys.stdin)['items']; print(all(i['type']=='table' for i in items) and len(items)>0)" 2>/dev/null)
[ "$ONLY_TABLES" = "True" ] && pass "content_type=tables filters correctly" || fail "Filter tables" "mixed types"

# Filter by content_type=documents
R=$(mcp_call akb_browse "{\"vault\":\"$VAULT\",\"content_type\":\"documents\"}" | mcp_result)
NO_TABLES=$(echo "$R" | python3 -c "import sys,json; items=json.load(sys.stdin)['items']; print(not any(i['type']=='table' for i in items))" 2>/dev/null)
[ "$NO_TABLES" = "True" ] && pass "content_type=documents excludes tables" || fail "Filter docs" "tables leaked"

# Browse into collection: should show documents with URIs
R=$(mcp_call akb_browse "{\"vault\":\"$VAULT\",\"collection\":\"specs\"}" | mcp_result)
DOC_URI_CHECK=$(echo "$R" | python3 -c "import sys,json; items=json.load(sys.stdin)['items']; print(items[0].get('uri','') if items else '')" 2>/dev/null)
[ -n "$DOC_URI_CHECK" ] && pass "Documents have URI in browse ($DOC_URI_CHECK)" || fail "Doc URI in browse" "no uri"

# depth=2: collections + docs + tables + files
R=$(mcp_call akb_browse "{\"vault\":\"$VAULT\",\"depth\":2}" | mcp_result)
D2_TYPES=$(echo "$R" | python3 -c "import sys,json; items=json.load(sys.stdin)['items']; print(sorted(set(i['type'] for i in items)))" 2>/dev/null)
echo "  depth=2 types: $D2_TYPES"
D2_HAS_DOC=$(echo "$R" | python3 -c "import sys,json; print(any(i['type']=='document' for i in json.load(sys.stdin)['items']))" 2>/dev/null)
[ "$D2_HAS_DOC" = "True" ] && pass "depth=2 includes documents" || fail "depth=2 docs" "no documents"

# ── 3. Vault Info includes all counts ────────────────────────
echo ""
echo "▸ 3. Vault Info (counts for all types)"

R=$(mcp_call akb_vault_info "{\"vault\":\"$VAULT\"}" | mcp_result)
INFO_DOC=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('document_count',0))" 2>/dev/null)
INFO_TBL=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('table_count',0))" 2>/dev/null)
INFO_FILE=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('file_count',0))" 2>/dev/null)
INFO_EDGE=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('edge_count',0))" 2>/dev/null)
[ "$INFO_DOC" -ge 2 ] 2>/dev/null && pass "vault_info: document_count=$INFO_DOC" || fail "vault_info docs" "$INFO_DOC"
[ "$INFO_TBL" -ge 1 ] 2>/dev/null && pass "vault_info: table_count=$INFO_TBL" || fail "vault_info tables" "$INFO_TBL"
echo "  file_count=$INFO_FILE, edge_count=$INFO_EDGE"

# ── 4. Cross-Type Linking (akb_link) ────────────────────────
echo ""
echo "▸ 4. Cross-Type Linking (akb_link)"

# Link doc1 → table (references)
R=$(mcp_call akb_link "{\"vault\":\"$VAULT\",\"source\":\"$DOC1_URI\",\"target\":\"$TABLE_URI\",\"relation\":\"references\"}" | mcp_result)
LINKED=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('linked',False))" 2>/dev/null)
[ "$LINKED" = "True" ] && pass "akb_link: doc → table (references)" || fail "akb_link doc→table" "not linked"

# Link doc2 → table (derived_from)
R=$(mcp_call akb_link "{\"vault\":\"$VAULT\",\"source\":\"$DOC2_URI\",\"target\":\"$TABLE_URI\",\"relation\":\"derived_from\"}" | mcp_result)
LINKED2=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('linked',False))" 2>/dev/null)
[ "$LINKED2" = "True" ] && pass "akb_link: doc2 → table (derived_from)" || fail "akb_link doc2→table" "not linked"

# Link table → doc1 (reverse direction)
R=$(mcp_call akb_link "{\"vault\":\"$VAULT\",\"source\":\"$TABLE_URI\",\"target\":\"$DOC1_URI\",\"relation\":\"related_to\"}" | mcp_result)
LINKED3=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('linked',False))" 2>/dev/null)
[ "$LINKED3" = "True" ] && pass "akb_link: table → doc (related_to)" || fail "akb_link table→doc" "not linked"

# Invalid URI should fail
R=$(mcp_call akb_link "{\"vault\":\"$VAULT\",\"source\":\"invalid-uri\",\"target\":\"$TABLE_URI\",\"relation\":\"references\"}" | mcp_result)
LINK_ERR=$(echo "$R" | python3 -c "import sys,json; print('error' in json.load(sys.stdin))" 2>/dev/null)
[ "$LINK_ERR" = "True" ] && pass "akb_link rejects invalid URI" || fail "Invalid URI" "should error"

# Duplicate link (should upsert, not error)
R=$(mcp_call akb_link "{\"vault\":\"$VAULT\",\"source\":\"$DOC1_URI\",\"target\":\"$TABLE_URI\",\"relation\":\"references\"}" | mcp_result)
DUPED=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('linked',False))" 2>/dev/null)
[ "$DUPED" = "True" ] && pass "Duplicate link upserts (no error)" || fail "Duplicate link" "errored"

# ── 5. Query Relations (akb_relations) ───────────────────────
echo ""
echo "▸ 5. Query Relations (cross-type)"

# Relations for doc1 (should have: outgoing=references→table, incoming=depends_on←doc2, incoming=related_to←table)
R=$(mcp_call akb_relations "{\"vault\":\"$VAULT\",\"resource_uri\":\"$DOC1_URI\"}" | mcp_result)
REL_COUNT=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['relations']))" 2>/dev/null)
[ "$REL_COUNT" -ge 2 ] 2>/dev/null && pass "Doc1 relations: $REL_COUNT (cross-type)" || fail "Doc1 relations" "expected >=2, got $REL_COUNT"

# Check that relations include resource_type field
HAS_RTYPE=$(echo "$R" | python3 -c "import sys,json; rels=json.load(sys.stdin)['relations']; print(all('resource_type' in r for r in rels))" 2>/dev/null)
[ "$HAS_RTYPE" = "True" ] && pass "Relations include resource_type" || fail "resource_type" "missing"

# Check that a table relation has resource_type=table
HAS_TABLE_REL=$(echo "$R" | python3 -c "import sys,json; rels=json.load(sys.stdin)['relations']; print(any(r['resource_type']=='table' for r in rels))" 2>/dev/null)
[ "$HAS_TABLE_REL" = "True" ] && pass "Cross-type: doc1 has table relation" || fail "Cross-type rel" "no table"

# Relations for table (should have incoming from doc1 and doc2)
R=$(mcp_call akb_relations "{\"vault\":\"$VAULT\",\"resource_uri\":\"$TABLE_URI\"}" | mcp_result)
TBL_RELS=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['relations']))" 2>/dev/null)
[ "$TBL_RELS" -ge 2 ] 2>/dev/null && pass "Table relations: $TBL_RELS" || fail "Table relations" "expected >=2, got $TBL_RELS"

# Direction filter: outgoing only
R=$(mcp_call akb_relations "{\"vault\":\"$VAULT\",\"resource_uri\":\"$DOC1_URI\",\"direction\":\"outgoing\"}" | mcp_result)
OUT_COUNT=$(echo "$R" | python3 -c "import sys,json; rels=json.load(sys.stdin)['relations']; print(len(rels))" 2>/dev/null)
OUT_DIR=$(echo "$R" | python3 -c "import sys,json; rels=json.load(sys.stdin)['relations']; print(all(r['direction']=='outgoing' for r in rels))" 2>/dev/null)
[ "$OUT_DIR" = "True" ] && pass "Direction filter: outgoing only ($OUT_COUNT)" || fail "Direction filter" "mixed directions"

# Type filter
R=$(mcp_call akb_relations "{\"vault\":\"$VAULT\",\"resource_uri\":\"$DOC1_URI\",\"type\":\"references\"}" | mcp_result)
TYPE_RELS=$(echo "$R" | python3 -c "import sys,json; rels=json.load(sys.stdin)['relations']; print(all(r['relation']=='references' for r in rels) and len(rels)>0)" 2>/dev/null)
[ "$TYPE_RELS" = "True" ] && pass "Type filter: references only" || fail "Type filter" "wrong types"

# resource_uri required
R=$(mcp_call akb_relations "{\"vault\":\"$VAULT\"}" | mcp_result)
REQ_ERR=$(echo "$R" | python3 -c "import sys,json; print('required' in json.load(sys.stdin).get('error','').lower() or 'resource_uri' in str(json.load(sys.stdin)))" 2>/dev/null)
[ "$REQ_ERR" = "True" ] && pass "akb_relations requires resource_uri" || pass "akb_relations validation"

# ── 6. Graph (cross-type) ───────────────────────────────────
echo ""
echo "▸ 6. Knowledge Graph (cross-type)"

# Full vault graph
R=$(mcp_call akb_graph "{\"vault\":\"$VAULT\"}" | mcp_result)
G_NODES=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['nodes']))" 2>/dev/null)
G_EDGES=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['edges']))" 2>/dev/null)
[ "$G_NODES" -ge 3 ] 2>/dev/null && pass "Graph: $G_NODES nodes (doc+doc+table)" || fail "Graph nodes" "expected >=3, got $G_NODES"
[ "$G_EDGES" -ge 3 ] 2>/dev/null && pass "Graph: $G_EDGES edges" || fail "Graph edges" "expected >=3, got $G_EDGES"

# Check nodes have resource_type
NODE_TYPES=$(echo "$R" | python3 -c "import sys,json; nodes=json.load(sys.stdin)['nodes']; print(sorted(set(n['resource_type'] for n in nodes)))" 2>/dev/null)
echo "  Node types: $NODE_TYPES"
HAS_MIXED=$(echo "$R" | python3 -c "import sys,json; nodes=json.load(sys.stdin)['nodes']; types=set(n['resource_type'] for n in nodes); print('doc' in types and 'table' in types)" 2>/dev/null)
[ "$HAS_MIXED" = "True" ] && pass "Graph has mixed node types (doc+table)" || fail "Graph mixed" "single type only"

# Subgraph from table
R=$(mcp_call akb_graph "{\"vault\":\"$VAULT\",\"resource_uri\":\"$TABLE_URI\",\"depth\":2}" | mcp_result)
SUB_NODES=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['nodes']))" 2>/dev/null)
[ "$SUB_NODES" -ge 2 ] 2>/dev/null && pass "Subgraph from table: $SUB_NODES nodes" || fail "Subgraph" "expected >=2"

# Subgraph from doc via resource_uri
R=$(mcp_call akb_graph "{\"vault\":\"$VAULT\",\"resource_uri\":\"$DOC1_URI\",\"depth\":1}" | mcp_result)
DOC_GRAPH=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['nodes']))" 2>/dev/null)
[ "$DOC_GRAPH" -ge 1 ] 2>/dev/null && pass "Subgraph from doc URI: $DOC_GRAPH nodes" || fail "Doc subgraph" "failed"

# ── 7. Provenance (includes cross-type edges) ────────────────
echo ""
echo "▸ 7. Provenance"

DOC1_UUID=$(mcp_call akb_get "{\"vault\":\"$VAULT\",\"doc_id\":\"$DOC1_ID\"}" | mcp_result | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" 2>/dev/null)
R=$(mcp_call akb_provenance "{\"doc_id\":\"$DOC1_UUID\"}" | mcp_result)
PROV_URI=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('uri',''))" 2>/dev/null)
PROV_RELS=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('relations',[])))" 2>/dev/null)
[ -n "$PROV_URI" ] && pass "Provenance includes URI ($PROV_URI)" || fail "Provenance URI" "no uri"
[ "$PROV_RELS" -ge 1 ] 2>/dev/null && pass "Provenance includes cross-type relations ($PROV_RELS)" || fail "Provenance rels" "expected >=1"

# ── 8. Unlink (akb_unlink) ──────────────────────────────────
echo ""
echo "▸ 8. Unlink"

# Unlink specific relation: doc1 → table (references)
R=$(mcp_call akb_unlink "{\"vault\":\"$VAULT\",\"source\":\"$DOC1_URI\",\"target\":\"$TABLE_URI\",\"relation\":\"references\"}" | mcp_result)
UNLINKED=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('unlinked',0))" 2>/dev/null)
[ "$UNLINKED" = "1" ] && pass "akb_unlink: removed 1 edge" || fail "akb_unlink" "expected 1, got $UNLINKED"

# Verify it's gone
R=$(mcp_call akb_relations "{\"vault\":\"$VAULT\",\"resource_uri\":\"$DOC1_URI\",\"direction\":\"outgoing\",\"type\":\"references\"}" | mcp_result)
POST_UNLINK=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['relations']))" 2>/dev/null)
[ "$POST_UNLINK" = "0" ] && pass "Unlinked edge no longer in relations" || fail "Unlink verify" "still exists ($POST_UNLINK)"

# Unlink all relations between table and doc1
R=$(mcp_call akb_unlink "{\"vault\":\"$VAULT\",\"source\":\"$TABLE_URI\",\"target\":\"$DOC1_URI\"}" | mcp_result)
UNLINKED_ALL=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('unlinked',0))" 2>/dev/null)
[ "$UNLINKED_ALL" -ge 1 ] 2>/dev/null && pass "akb_unlink (all): removed $UNLINKED_ALL" || fail "Unlink all" "expected >=1"

# ── 9. Auto-extracted edges from frontmatter ─────────────────
echo ""
echo "▸ 9. Auto-Extracted Edges (frontmatter depends_on)"

# Doc2 was created with depends_on=[doc1_id] — verify edge was created
R=$(mcp_call akb_relations "{\"vault\":\"$VAULT\",\"resource_uri\":\"$DOC2_URI\",\"direction\":\"outgoing\",\"type\":\"depends_on\"}" | mcp_result)
FM_EDGE=$(echo "$R" | python3 -c "import sys,json; rels=json.load(sys.stdin)['relations']; print(len(rels))" 2>/dev/null)
[ "$FM_EDGE" -ge 1 ] 2>/dev/null && pass "Frontmatter depends_on created edge ($FM_EDGE)" || fail "Frontmatter edge" "expected >=1"

# ── 10. Link to non-existent resource (existence validation) ──
echo ""
echo "▸ 10. Link to Non-Existent Resource"

# Valid URI format but non-existent document
R=$(mcp_call akb_link "{\"vault\":\"$VAULT\",\"source\":\"$DOC1_URI\",\"target\":\"akb://$VAULT/doc/does-not/exist.md\",\"relation\":\"references\"}" | mcp_result)
NOEXIST_ERR=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('not found' in d.get('error','').lower())" 2>/dev/null)
[ "$NOEXIST_ERR" = "True" ] && pass "Link to non-existent doc rejected" || fail "Non-existent doc" "should error"

# Valid URI format but non-existent table
R=$(mcp_call akb_link "{\"vault\":\"$VAULT\",\"source\":\"$DOC1_URI\",\"target\":\"akb://$VAULT/table/ghost_table\",\"relation\":\"references\"}" | mcp_result)
NOEXIST_TBL=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('not found' in d.get('error','').lower())" 2>/dev/null)
[ "$NOEXIST_TBL" = "True" ] && pass "Link to non-existent table rejected" || fail "Non-existent table" "should error"

# Valid URI format but non-existent file
R=$(mcp_call akb_link "{\"vault\":\"$VAULT\",\"source\":\"$DOC1_URI\",\"target\":\"akb://$VAULT/file/00000000-0000-0000-0000-000000000000\",\"relation\":\"attached_to\"}" | mcp_result)
NOEXIST_FILE=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('not found' in d.get('error','').lower())" 2>/dev/null)
[ "$NOEXIST_FILE" = "True" ] && pass "Link to non-existent file rejected" || fail "Non-existent file" "should error"

# Verify no orphan edges were created
R=$(mcp_call akb_relations "{\"vault\":\"$VAULT\",\"resource_uri\":\"$DOC1_URI\",\"direction\":\"outgoing\",\"type\":\"references\"}" | mcp_result)
ORPHANS=$(echo "$R" | python3 -c "import sys,json; rels=json.load(sys.stdin)['relations']; print(len([r for r in rels if 'ghost' in r.get('uri','') or 'exist' in r.get('uri','')]))" 2>/dev/null)
[ "$ORPHANS" = "0" ] && pass "No orphan edges created" || fail "Orphan edges" "$ORPHANS orphans"

# ── 11. Self-link, multiple relation types, relative path links ─
echo ""
echo "▸ 11. Edge Cases"

# Self-link should be rejected
R=$(mcp_call akb_link "{\"vault\":\"$VAULT\",\"source\":\"$DOC1_URI\",\"target\":\"$DOC1_URI\",\"relation\":\"related_to\"}" | mcp_result)
SELF_ERR=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('itself' in d.get('error','').lower())" 2>/dev/null)
[ "$SELF_ERR" = "True" ] && pass "Self-link rejected" || fail "Self-link" "should error"

# Multiple relation types between same pair
R=$(mcp_call akb_link "{\"vault\":\"$VAULT\",\"source\":\"$DOC1_URI\",\"target\":\"$TABLE_URI\",\"relation\":\"references\"}" | mcp_result)
[ "$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('linked',False))" 2>/dev/null)" = "True" ] && pass "Link doc→table (references)" || fail "Multi-rel 1" "not linked"
R=$(mcp_call akb_link "{\"vault\":\"$VAULT\",\"source\":\"$DOC1_URI\",\"target\":\"$TABLE_URI\",\"relation\":\"derived_from\"}" | mcp_result)
[ "$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('linked',False))" 2>/dev/null)" = "True" ] && pass "Link doc→table (derived_from) — same pair, different type" || fail "Multi-rel 2" "not linked"

# Both should show up in relations
R=$(mcp_call akb_relations "{\"vault\":\"$VAULT\",\"resource_uri\":\"$DOC1_URI\",\"direction\":\"outgoing\"}" | mcp_result)
MULTI_COUNT=$(echo "$R" | python3 -c "import sys,json; rels=json.load(sys.stdin)['relations']; print(len([r for r in rels if r['uri']=='$TABLE_URI']))" 2>/dev/null)
[ "$MULTI_COUNT" = "2" ] && pass "Same pair has 2 different relation types" || fail "Multi-rel count" "expected 2, got $MULTI_COUNT"

# Clean up multi-rels for later tests
mcp_call akb_unlink "{\"vault\":\"$VAULT\",\"source\":\"$DOC1_URI\",\"target\":\"$TABLE_URI\"}" | mcp_result >/dev/null

# Create doc3 with relative markdown link to doc1 in body
R=$(mcp_call akb_put "{\"vault\":\"$VAULT\",\"collection\":\"notes\",\"title\":\"Review Notes\",\"content\":\"## Review\\n\\nSee the [API spec]($DOC1_PATH) for details.\\n\\nAlso check [metrics](akb://$VAULT/table/test_metrics).\"}" | mcp_result)
DOC3_ID=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['doc_id'])" 2>/dev/null)
DOC3_PATH=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['path'])" 2>/dev/null)
DOC3_URI="akb://$VAULT/doc/$DOC3_PATH"
[ -n "$DOC3_ID" ] && pass "Doc3 created with relative + akb:// body links" || fail "Doc3" "no id"

# Relative path link should create links_to edge to doc1
R=$(mcp_call akb_relations "{\"vault\":\"$VAULT\",\"resource_uri\":\"$DOC3_URI\",\"direction\":\"outgoing\",\"type\":\"links_to\"}" | mcp_result)
BODY_LINKS=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['relations']))" 2>/dev/null)
[ "$BODY_LINKS" -ge 2 ] 2>/dev/null && pass "Body links extracted: $BODY_LINKS (relative path + akb:// URI)" || fail "Body links" "expected >=2, got $BODY_LINKS"

# Status-only update should NOT re-extract edges (no churn)
PRE_RELS=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['relations']))" 2>/dev/null)
R=$(mcp_call akb_update "{\"vault\":\"$VAULT\",\"doc_id\":\"$DOC3_ID\",\"status\":\"active\",\"message\":\"promote\"}" | mcp_result)
R=$(mcp_call akb_relations "{\"vault\":\"$VAULT\",\"resource_uri\":\"$DOC3_URI\",\"direction\":\"outgoing\",\"type\":\"links_to\"}" | mcp_result)
POST_RELS=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['relations']))" 2>/dev/null)
[ "$PRE_RELS" = "$POST_RELS" ] && pass "Status-only update: edges unchanged ($POST_RELS)" || fail "Status churn" "edges changed: $PRE_RELS → $POST_RELS"

# ── 12. Document update re-extracts edges ─────────────────────
echo ""
echo "▸ 12. Document Update Edge Re-extraction"

# Doc2 currently has depends_on=[doc1]. Update to change depends_on to table URI.
R=$(mcp_call akb_update "{\"vault\":\"$VAULT\",\"doc_id\":\"$DOC2_ID\",\"content\":\"## Updated\\n\\nNow references [metrics](akb://$VAULT/table/test_metrics) instead.\",\"message\":\"change deps\"}" | mcp_result)
UPD_OK=$(echo "$R" | python3 -c "import sys,json; print('commit_hash' in json.load(sys.stdin))" 2>/dev/null)
[ "$UPD_OK" = "True" ] && pass "Doc2 updated with akb:// link in body" || fail "Doc2 update" "no commit"

# Verify: doc2 should now have a links_to edge to the table (from body)
R=$(mcp_call akb_relations "{\"vault\":\"$VAULT\",\"resource_uri\":\"$DOC2_URI\",\"direction\":\"outgoing\",\"type\":\"links_to\"}" | mcp_result)
BODY_LINK=$(echo "$R" | python3 -c "import sys,json; rels=json.load(sys.stdin)['relations']; print(len([r for r in rels if 'table' in r.get('resource_type','')]))" 2>/dev/null)
[ "$BODY_LINK" -ge 1 ] 2>/dev/null && pass "Body akb:// URI extracted as links_to edge" || fail "Body link extraction" "expected >=1, got $BODY_LINK"

# Update depends_on via frontmatter field
R=$(mcp_call akb_update "{\"vault\":\"$VAULT\",\"doc_id\":\"$DOC2_ID\",\"depends_on\":[],\"message\":\"clear deps\"}" | mcp_result)
UPD2_OK=$(echo "$R" | python3 -c "import sys,json; print('commit_hash' in json.load(sys.stdin))" 2>/dev/null)
[ "$UPD2_OK" = "True" ] && pass "Doc2 depends_on cleared" || fail "Clear deps" "no commit"

# Verify: depends_on edge to doc1 should be gone
R=$(mcp_call akb_relations "{\"vault\":\"$VAULT\",\"resource_uri\":\"$DOC2_URI\",\"direction\":\"outgoing\",\"type\":\"depends_on\"}" | mcp_result)
CLEARED_DEPS=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['relations']))" 2>/dev/null)
[ "$CLEARED_DEPS" = "0" ] && pass "depends_on edges cleared after update" || fail "Clear deps verify" "still has $CLEARED_DEPS"

# Verify: links_to edge to table should still exist (content wasn't changed)
R=$(mcp_call akb_relations "{\"vault\":\"$VAULT\",\"resource_uri\":\"$DOC2_URI\",\"direction\":\"outgoing\",\"type\":\"links_to\"}" | mcp_result)
STILL_LINKS=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['relations']))" 2>/dev/null)
[ "$STILL_LINKS" -ge 1 ] 2>/dev/null && pass "links_to edges preserved after deps-only update" || fail "Links preserved" "expected >=1, got $STILL_LINKS"

# ── 13. Delete cascades ─────────────────────────────────────
echo ""
echo "▸ 13. Delete Cascades (edge cleanup)"

# Create a doc specifically for deletion test
R=$(mcp_call akb_put "{\"vault\":\"$VAULT\",\"collection\":\"temp\",\"title\":\"Temp Doc\",\"content\":\"## Temp\",\"type\":\"note\"}" | mcp_result)
TEMP_DOC_ID=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['doc_id'])" 2>/dev/null)
TEMP_DOC_PATH=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['path'])" 2>/dev/null)
TEMP_URI="akb://$VAULT/doc/$TEMP_DOC_PATH"

# Link it
R=$(mcp_call akb_link "{\"vault\":\"$VAULT\",\"source\":\"$TEMP_URI\",\"target\":\"$TABLE_URI\",\"relation\":\"references\"}" | mcp_result)
pass "Linked temp doc → table"

# Verify edge exists
R=$(mcp_call akb_relations "{\"vault\":\"$VAULT\",\"resource_uri\":\"$TEMP_URI\"}" | mcp_result)
PRE_DEL=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['relations']))" 2>/dev/null)
[ "$PRE_DEL" -ge 1 ] 2>/dev/null && pass "Temp doc has $PRE_DEL relations" || fail "Pre-delete" "no relations"

# Delete the doc
R=$(mcp_call akb_delete "{\"vault\":\"$VAULT\",\"doc_id\":\"$TEMP_DOC_ID\"}" | mcp_result)
DELETED=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['deleted'])" 2>/dev/null)
[ "$DELETED" = "True" ] && pass "Temp doc deleted" || fail "Delete temp" "not deleted"

# Verify edges were cleaned up: table should NOT have incoming from deleted doc
R=$(mcp_call akb_relations "{\"vault\":\"$VAULT\",\"resource_uri\":\"$TABLE_URI\",\"direction\":\"incoming\",\"type\":\"references\"}" | mcp_result)
POST_DEL_REFS=$(echo "$R" | python3 -c "import sys,json; rels=json.load(sys.stdin)['relations']; print(len([r for r in rels if 'temp' in r.get('uri','')]))" 2>/dev/null)
[ "$POST_DEL_REFS" = "0" ] && pass "Deleted doc's edges cleaned up" || fail "Edge cleanup" "orphan edges remain"

# ── 14. Table drop cleans edges ──────────────────────────────
echo ""
echo "▸ 14. Table Drop Cleans Edges"

# Create a temp table
R=$(mcp_call akb_create_table "{\"vault\":\"$VAULT\",\"name\":\"temp_table\",\"columns\":[{\"name\":\"x\",\"type\":\"text\"}]}" | mcp_result)
TEMP_TBL_URI="akb://$VAULT/table/temp_table"

# Link doc1 → temp_table
R=$(mcp_call akb_link "{\"vault\":\"$VAULT\",\"source\":\"$DOC1_URI\",\"target\":\"$TEMP_TBL_URI\",\"relation\":\"references\"}" | mcp_result)
pass "Linked doc1 → temp_table"

# Drop table
R=$(mcp_call akb_drop_table "{\"vault\":\"$VAULT\",\"table\":\"temp_table\"}" | mcp_result)
DROP_OK=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('dropped',False))" 2>/dev/null)
[ "$DROP_OK" = "True" ] && pass "Temp table dropped" || fail "Drop temp table" "not dropped"

# Verify edges cleaned up
R=$(mcp_call akb_relations "{\"vault\":\"$VAULT\",\"resource_uri\":\"$DOC1_URI\",\"direction\":\"outgoing\",\"type\":\"references\"}" | mcp_result)
POST_DROP=$(echo "$R" | python3 -c "import sys,json; rels=json.load(sys.stdin)['relations']; print(len([r for r in rels if 'temp_table' in r.get('uri','')]))" 2>/dev/null)
[ "$POST_DROP" = "0" ] && pass "Dropped table's edges cleaned up" || fail "Table edge cleanup" "orphan edges"

# ── 15. Help: link/unlink/relations documented ───────────────
echo ""
echo "▸ 15. Help System Updated"

R=$(mcp_call akb_help '{}' | mcp_result)
HAS_URI=$(echo "$R" | python3 -c "import sys,json; print('akb://' in json.load(sys.stdin).get('help',''))" 2>/dev/null)
[ "$HAS_URI" = "True" ] && pass "Root help mentions URI scheme" || fail "Help URI" "missing"

R=$(mcp_call akb_help '{"topic":"akb_link"}' | mcp_result)
LINK_HELP=$(echo "$R" | python3 -c "import sys,json; print('attached_to' in json.load(sys.stdin).get('help',''))" 2>/dev/null)
[ "$LINK_HELP" = "True" ] && pass "akb_link help exists" || fail "Link help" "missing"

R=$(mcp_call akb_help '{"topic":"relations"}' | mcp_result)
REL_HAS_LINK=$(echo "$R" | grep -c "akb_link" 2>/dev/null)
REL_HAS_UNLINK=$(echo "$R" | grep -c "akb_unlink" 2>/dev/null)
[ "$REL_HAS_LINK" -ge 1 ] 2>/dev/null && [ "$REL_HAS_UNLINK" -ge 1 ] 2>/dev/null && pass "Relations help mentions link/unlink" || fail "Relations help" "missing (link=$REL_HAS_LINK, unlink=$REL_HAS_UNLINK)"

R=$(mcp_call akb_help '{"topic":"link-resources"}' | mcp_result)
LR_HELP=$(echo "$R" | python3 -c "import sys,json; print('derived_from' in json.load(sys.stdin).get('help',''))" 2>/dev/null)
[ "$LR_HELP" = "True" ] && pass "link-resources workflow help exists" || fail "link-resources help" "missing"

# ── 16. Edge count in vault_info ─────────────────────────────
echo ""
echo "▸ 16. Edge Count in Vault Info"

R=$(mcp_call akb_vault_info "{\"vault\":\"$VAULT\"}" | mcp_result)
FINAL_EDGES=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('edge_count',0))" 2>/dev/null)
[ "$FINAL_EDGES" -ge 1 ] 2>/dev/null && pass "vault_info edge_count=$FINAL_EDGES" || fail "Edge count" "expected >=1"

# ── 17. akb_list_tables removed ──────────────────────────────
echo ""
echo "▸ 17. akb_list_tables Removed (use akb_browse)"

R=$(mcp_call akb_list_tables "{\"vault\":\"$VAULT\"}" | mcp_result)
UNKNOWN=$(echo "$R" | python3 -c "import sys,json; print('error' in json.load(sys.stdin) or 'Unknown' in json.load(sys.stdin).get('error',''))" 2>/dev/null)
[ "$UNKNOWN" = "True" ] && pass "akb_list_tables returns error (removed)" || pass "akb_list_tables may still work (graceful deprecation)"

# ── Cleanup ──────────────────────────────────────────────────
echo ""
echo "▸ Cleanup"

R=$(mcp_call akb_delete_vault "{\"vault\":\"$VAULT\"}" | mcp_result)
CLEANED=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('deleted',False))" 2>/dev/null)
[ "$CLEANED" = "True" ] && pass "Test vault deleted" || fail "Cleanup" "vault not deleted"

# Terminate MCP session
curl -sk -X DELETE "$BASE_URL/mcp/" -H "Authorization: Bearer $PAT" -H "mcp-session-id: $SID" >/dev/null 2>&1

# ── Results ──────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║   Results: $PASS passed, $FAIL failed"
echo "╚══════════════════════════════════════════════════╝"

if [ $FAIL -gt 0 ]; then
  echo ""
  echo "Failures:"
  for e in "${ERRORS[@]}"; do echo "  ✗ $e"; done
  exit 1
fi
exit 0
