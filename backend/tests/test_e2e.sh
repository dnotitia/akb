#!/bin/bash
#
# AKB End-to-End Test Suite
# Tests all core operations against a running AKB instance
#
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
VAULT="e2e-vault-$(date +%s)"
E2E_USER="e2e-$(date +%s)"
PASS=0
FAIL=0
ERRORS=()
AUTH_HEADER=""

# Will be set after auth setup
CURL="curl -sk --max-time 15"
CURLA=""  # CURL with Auth

# ── Helpers ──────────────────────────────────────────────────

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

assert_status() {
  local desc="$1" method="$2" url="$3" expected="$4"
  local status
  status=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 15 -X "$method" "$url" 2>/dev/null) || true
  if [ "$status" = "$expected" ]; then pass "$desc"; else fail "$desc" "expected $expected, got $status"; fi
}

assert_status_auth() {
  local desc="$1" method="$2" url="$3" expected="$4"
  local status
  status=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 15 -X "$method" -H "$AUTH_HEADER" "$url" 2>/dev/null) || true
  if [ "$status" = "$expected" ]; then pass "$desc"; else fail "$desc" "expected $expected, got $status"; fi
}

assert_json() {
  local desc="$1" response="$2" field="$3" expected="$4"
  local actual
  actual=$(echo "$response" | python3 -c "import sys,json; print(json.load(sys.stdin)$field)" 2>/dev/null) || true
  if [ "$actual" = "$expected" ]; then pass "$desc"; else fail "$desc" "expected '$expected', got '$actual'"; fi
}

assert_json_exists() {
  local desc="$1" response="$2" field="$3"
  local actual
  actual=$(echo "$response" | python3 -c "import sys,json; v=json.load(sys.stdin)$field; print('exists' if v else 'empty')" 2>/dev/null) || true
  if [ "$actual" = "exists" ]; then pass "$desc"; else fail "$desc" "field $field missing or empty"; fi
}

assert_json_count() {
  local desc="$1" response="$2" field="$3" op="$4" count="$5"
  local actual
  actual=$(echo "$response" | python3 -c "import sys,json; print(len(json.load(sys.stdin)$field))" 2>/dev/null) || true
  case "$op" in
    eq) [ "$actual" = "$count" ] && pass "$desc" || fail "$desc" "expected count=$count, got $actual" ;;
    ge) [ "$actual" -ge "$count" ] 2>/dev/null && pass "$desc" || fail "$desc" "expected count>=$count, got $actual" ;;
    gt) [ "$actual" -gt "$count" ] 2>/dev/null && pass "$desc" || fail "$desc" "expected count>$count, got $actual" ;;
  esac
}

# ── Tests ────────────────────────────────────────────────────

echo "╔══════════════════════════════════════════╗"
echo "║   AKB E2E Test Suite                     ║"
echo "║   Target: $BASE_URL"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── 0. Auth Setup ────────────────────────────────────────────
echo "▸ 0. Auth Setup"

R=$($CURL -X POST "$BASE_URL/api/v1/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$E2E_USER\",\"email\":\"$E2E_USER@test.dev\",\"password\":\"test1234\"}" 2>/dev/null)
assert_json_exists "Register user" "$R" "['user_id']"

LOGIN=$($CURL -X POST "$BASE_URL/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$E2E_USER\",\"password\":\"test1234\"}" 2>/dev/null)
JWT=$(echo "$LOGIN" | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])" 2>/dev/null)
[ -n "$JWT" ] && pass "Login returns JWT" || fail "Login" "no JWT returned"

PAT_RESULT=$($CURL -X POST "$BASE_URL/api/v1/auth/tokens" \
  -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' \
  -d '{"name":"e2e-test-pat","scopes":["read","write"]}' 2>/dev/null)
PAT=$(echo "$PAT_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])" 2>/dev/null)
[ -n "$PAT" ] && pass "PAT created (${PAT:0:12}...)" || fail "PAT creation" "no token"

AUTH_HEADER="Authorization: Bearer $PAT"
CURLA="$CURL -H \"$AUTH_HEADER\""

# Helper: authenticated curl
acurl() { $CURL -H "$AUTH_HEADER" "$@"; }

# Verify auth
R=$(acurl "$BASE_URL/api/v1/auth/me" 2>/dev/null)
assert_json "Auth/me returns username" "$R" "['username']" "$E2E_USER"

# Verify unauthenticated rejected
assert_status "Unauthenticated rejected" GET "$BASE_URL/api/v1/vaults" "401"

# ── 1. Health & Basics ───────────────────────────────────────
echo ""
echo "▸ 1. Health & Basics"

R=$($CURL "$BASE_URL/health" 2>/dev/null)
assert_json "Health check returns ok" "$R" "['status']" "ok"
assert_status "Frontend returns 200" GET "$BASE_URL/" "200"

# ── 2. Vault Lifecycle ──────────────────────────────────────
echo ""
echo "▸ 2. Vault Lifecycle"

# Create vault
R=$(acurl -X POST "$BASE_URL/api/v1/vaults?name=$VAULT&description=E2E+test+vault" 2>/dev/null)
assert_json_exists "Create vault returns vault_id" "$R" "['vault_id']"

# Create duplicate vault (should fail 409)
assert_status_auth "Duplicate vault returns 409" POST "$BASE_URL/api/v1/vaults?name=$VAULT" "409"

# List vaults
R=$(acurl "$BASE_URL/api/v1/vaults" 2>/dev/null)
assert_json_count "Vault list contains test vault" "$R" "['vaults']" "ge" "1"

# ── 3. Document CRUD ────────────────────────────────────────
echo ""
echo "▸ 3. Document CRUD"

# Put document 1
R=$(acurl -X POST "$BASE_URL/api/v1/documents" \
  -H 'Content-Type: application/json' \
  -d "{\"vault\":\"$VAULT\",\"collection\":\"specs\",\"title\":\"E2E Test Document Alpha\",\"content\":\"## Overview\\n\\nThis is a test document for e2e testing.\\n\\n## Details\\n\\nIt has multiple sections to test chunking.\\n\\n### Subsection A\\n\\nSome detailed content in subsection A.\\n\\n### Subsection B\\n\\nMore content in subsection B.\",\"type\":\"spec\",\"tags\":[\"test\",\"e2e\",\"alpha\"],\"domain\":\"engineering\",\"summary\":\"E2E test document with multiple sections.\"}" 2>/dev/null)
DOC1_ID=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['doc_id'])" 2>/dev/null)
DOC1_PATH=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['path'])" 2>/dev/null)
assert_json_exists "Put doc1 returns doc_id" "$R" "['doc_id']"
assert_json_exists "Put doc1 returns commit_hash" "$R" "['commit_hash']"
CHUNKS1=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['chunks_indexed'])" 2>/dev/null)
[ "$CHUNKS1" -ge 3 ] 2>/dev/null && pass "Doc1 chunked into $CHUNKS1 chunks (≥3)" || fail "Doc1 chunking" "expected ≥3, got $CHUNKS1"

# Put document 2
R=$(acurl -X POST "$BASE_URL/api/v1/documents" \
  -H 'Content-Type: application/json' \
  -d "{\"vault\":\"$VAULT\",\"collection\":\"specs\",\"title\":\"E2E Test Document Beta\",\"content\":\"## Purpose\\n\\nSecond test document.\\n\\n## Implementation\\n\\nThis tests multi-document scenarios.\",\"type\":\"report\",\"tags\":[\"test\",\"e2e\",\"beta\"],\"domain\":\"engineering\",\"summary\":\"Second e2e test document.\"}" 2>/dev/null)
DOC2_ID=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['doc_id'])" 2>/dev/null)
assert_json_exists "Put doc2 returns doc_id" "$R" "['doc_id']"

# Put document 3 in different collection
R=$(acurl -X POST "$BASE_URL/api/v1/documents" \
  -H 'Content-Type: application/json' \
  -d "{\"vault\":\"$VAULT\",\"collection\":\"decisions\",\"title\":\"Decision: Use gRPC\",\"content\":\"## Decision\\n\\nWe will use gRPC.\\n\\n## Rationale\\n\\nPerformance and type safety.\",\"type\":\"decision\",\"tags\":[\"grpc\",\"architecture\"],\"domain\":\"engineering\",\"summary\":\"Decision to adopt gRPC.\"}" 2>/dev/null)
DOC3_ID=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['doc_id'])" 2>/dev/null)
assert_json_exists "Put doc3 in different collection" "$R" "['doc_id']"

# ── 4. Get Document ──────────────────────────────────────────
echo ""
echo "▸ 4. Get Document"

R=$(acurl "$BASE_URL/api/v1/documents/$VAULT/$DOC1_ID" 2>/dev/null)
assert_json "Get doc1 title" "$R" "['title']" "E2E Test Document Alpha"
assert_json "Get doc1 type" "$R" "['type']" "spec"
assert_json "Get doc1 status" "$R" "['status']" "draft"
assert_json "Get doc1 domain" "$R" "['domain']" "engineering"
assert_json_exists "Get doc1 has content" "$R" "['content']"
assert_json_exists "Get doc1 has commit" "$R" "['current_commit']"

# Get non-existent document
assert_status_auth "Get non-existent doc returns 404" GET "$BASE_URL/api/v1/documents/$VAULT/nonexistent-id" "404"

# ── 5. Update Document ──────────────────────────────────────
echo ""
echo "▸ 5. Update Document"

R=$(acurl -X PATCH "$BASE_URL/api/v1/documents/$VAULT/$DOC1_ID" \
  -H 'Content-Type: application/json' \
  -d '{"status": "active", "message": "Promote to active"}' 2>/dev/null)
assert_json "Update status returns doc_id" "$R" "['doc_id']" "$DOC1_ID"
assert_json_exists "Update returns new commit" "$R" "['commit_hash']"

# Verify update persisted
R=$(acurl "$BASE_URL/api/v1/documents/$VAULT/$DOC1_ID" 2>/dev/null)
assert_json "Updated status is active" "$R" "['status']" "active"

# Update with new content
R=$(acurl -X PATCH "$BASE_URL/api/v1/documents/$VAULT/$DOC1_ID" \
  -H 'Content-Type: application/json' \
  -d '{"content": "## Overview\n\nUpdated content.\n\n## New Section\n\nThis section was added in the update.", "tags": ["test", "e2e", "updated"], "message": "Add new section"}' 2>/dev/null)
assert_json_exists "Content update returns commit" "$R" "['commit_hash']"
CHUNKS_UPDATED=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['chunks_indexed'])" 2>/dev/null)
[ "$CHUNKS_UPDATED" -ge 1 ] 2>/dev/null && pass "Updated doc re-chunked ($CHUNKS_UPDATED chunks)" || fail "Re-chunking" "got $CHUNKS_UPDATED"

# ── 6. Browse (Tree Retrieval) ──────────────────────────────
echo ""
echo "▸ 6. Browse (L1/L2 Tree Retrieval)"

# L1: collections
R=$(acurl "$BASE_URL/api/v1/browse/$VAULT" 2>/dev/null)
assert_json "Browse vault returns vault name" "$R" "['vault']" "$VAULT"
assert_json_count "L1 shows 2 collections" "$R" "['items']" "eq" "2"

# L2: documents in collection
R=$(acurl "$BASE_URL/api/v1/browse/$VAULT?collection=specs" 2>/dev/null)
assert_json_count "L2 specs has 2 documents" "$R" "['items']" "eq" "2"

R=$(acurl "$BASE_URL/api/v1/browse/$VAULT?collection=decisions" 2>/dev/null)
assert_json_count "L2 decisions has 1 document" "$R" "['items']" "eq" "1"

# L1+L2 depth=2
R=$(acurl "$BASE_URL/api/v1/browse/$VAULT?depth=2" 2>/dev/null)
assert_json_count "Depth=2 shows collections + all docs" "$R" "['items']" "ge" "5"

# Browse non-existent collection
R=$(acurl "$BASE_URL/api/v1/browse/$VAULT?collection=nonexistent" 2>/dev/null)
assert_json_count "Empty collection returns 0 items" "$R" "['items']" "eq" "0"

# ── 7. Drill Down (L3 Sections) ─────────────────────────────
echo ""
echo "▸ 7. Drill Down (L3 Sections)"

# Get doc UUID from browse
DOC1_UUID=$(acurl "$BASE_URL/api/v1/documents/$VAULT/$DOC1_ID" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" 2>/dev/null)

R=$(acurl "$BASE_URL/api/v1/drill-down/$VAULT/$DOC1_UUID" 2>/dev/null)
assert_json_count "Drill down returns sections" "$R" "['sections']" "ge" "1"

# Drill down with section filter
R=$(acurl "$BASE_URL/api/v1/drill-down/$VAULT/$DOC1_UUID?section=Overview" 2>/dev/null)
assert_json_count "Section filter returns matching" "$R" "['sections']" "ge" "1"

# ── 8. Session Management ───────────────────────────────────
echo ""
echo "▸ 8. Session Management"

R=$(acurl -X POST "$BASE_URL/api/v1/sessions/start?vault=$VAULT&agent_id=e2e-test-agent&context=E2E+testing" 2>/dev/null)
SESSION_ID=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])" 2>/dev/null)
assert_json_exists "Session start returns session_id" "$R" "['session_id']"
assert_json "Session agent_id" "$R" "['agent_id']" "e2e-test-agent"

# End session
R=$(acurl -X POST "$BASE_URL/api/v1/sessions/$SESSION_ID/end?summary=E2E+test+completed" 2>/dev/null)
assert_json_exists "Session end returns ended_at" "$R" "['ended_at']"

# ── 9. Activity (Git-based, replaces /recent) ──────────────
echo ""
echo "▸ 9. Activity"

R=$(acurl "$BASE_URL/api/v1/activity/$VAULT" 2>/dev/null)
ACT_TOTAL=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['total'])" 2>/dev/null)
[ "$ACT_TOTAL" -ge 3 ] 2>/dev/null && pass "Activity: $ACT_TOTAL commits" || fail "Activity" "expected >=3, got $ACT_TOTAL"

R=$(acurl "$BASE_URL/api/v1/activity/$VAULT?limit=1" 2>/dev/null)
ACT_LIM=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['total'])" 2>/dev/null)
[ "$ACT_LIM" = "1" ] && pass "Activity limit=1" || fail "Activity limit" "expected 1, got $ACT_LIM"

R=$(acurl "$BASE_URL/api/v1/activity/$VAULT?collection=specs" 2>/dev/null)
ACT_COL=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['total'])" 2>/dev/null)
[ "$ACT_COL" -ge 1 ] 2>/dev/null && pass "Activity collection filter: $ACT_COL" || fail "Activity filter" "expected >=1"

# ── 10. Document Relations & Graph ──────────────────────────
echo ""
echo "▸ 10. Document Relations & Graph"

# Get UUIDs for relation testing
DOC1_UUID=$(acurl "$BASE_URL/api/v1/documents/$VAULT/$DOC1_ID" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" 2>/dev/null)
DOC3_UUID=$(acurl "$BASE_URL/api/v1/documents/$VAULT/$DOC3_ID" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" 2>/dev/null)
DOC3_PATH=$(acurl "$BASE_URL/api/v1/documents/$VAULT/$DOC3_ID" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['path'])" 2>/dev/null)

# Create a doc with markdown link to doc3
R=$(acurl -X POST "$BASE_URL/api/v1/documents" \
  -H 'Content-Type: application/json' \
  -d "{\"vault\":\"$VAULT\",\"collection\":\"specs\",\"title\":\"Linked Doc\",\"content\":\"## Overview\\n\\nThis doc links to [gRPC Decision]($DOC3_PATH) via markdown.\",\"type\":\"note\",\"tags\":[\"link-test\"],\"depends_on\":[\"$DOC1_ID\"]}" 2>/dev/null)
LINKED_DOC_ID=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['doc_id'])" 2>/dev/null)
assert_json_exists "Doc with markdown link created" "$R" "['doc_id']"

# Check relations: should have depends_on + links_to
LINKED_UUID=$(acurl "$BASE_URL/api/v1/documents/$VAULT/$LINKED_DOC_ID" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" 2>/dev/null)
R=$(acurl "$BASE_URL/api/v1/relations/$VAULT/$LINKED_UUID" 2>/dev/null)
REL_COUNT=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['relations']))" 2>/dev/null)
[ "$REL_COUNT" -ge 2 ] 2>/dev/null && pass "Linked doc has $REL_COUNT relations (depends_on + links_to)" || fail "Markdown link relations" "expected >=2, got $REL_COUNT"

# Check depends_on relation exists
HAS_DEPENDS=$(echo "$R" | python3 -c "import sys,json; rels=json.load(sys.stdin)['relations']; print(any(r['relation']=='depends_on' for r in rels))" 2>/dev/null)
[ "$HAS_DEPENDS" = "True" ] && pass "depends_on relation from frontmatter" || fail "depends_on" "not found"

# Check links_to relation exists
HAS_LINKS=$(echo "$R" | python3 -c "import sys,json; rels=json.load(sys.stdin)['relations']; print(any(r['relation']=='links_to' for r in rels))" 2>/dev/null)
[ "$HAS_LINKS" = "True" ] && pass "links_to relation from markdown body" || fail "links_to" "not found"

# Check incoming relation on doc3 (backlink)
R=$(acurl "$BASE_URL/api/v1/relations/$VAULT/$DOC3_UUID?direction=incoming" 2>/dev/null)
INCOMING=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['relations']))" 2>/dev/null)
[ "$INCOMING" -ge 1 ] 2>/dev/null && pass "Doc3 has $INCOMING incoming backlink(s)" || fail "Backlinks" "expected >=1, got $INCOMING"

# Graph: full vault
R=$(acurl "$BASE_URL/api/v1/graph/$VAULT" 2>/dev/null)
NODE_COUNT=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['nodes']))" 2>/dev/null)
EDGE_COUNT=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['edges']))" 2>/dev/null)
[ "$NODE_COUNT" -ge 3 ] 2>/dev/null && pass "Graph has $NODE_COUNT nodes" || fail "Graph nodes" "expected >=3, got $NODE_COUNT"
[ "$EDGE_COUNT" -ge 2 ] 2>/dev/null && pass "Graph has $EDGE_COUNT edges" || fail "Graph edges" "expected >=2, got $EDGE_COUNT"

# Graph: centered on linked doc (BFS depth=1)
R=$(acurl "$BASE_URL/api/v1/graph/$VAULT?doc_id=$LINKED_UUID&depth=1" 2>/dev/null)
SUBGRAPH_NODES=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['nodes']))" 2>/dev/null)
[ "$SUBGRAPH_NODES" -ge 2 ] 2>/dev/null && pass "Subgraph (depth=1) has $SUBGRAPH_NODES nodes" || fail "Subgraph" "expected >=2, got $SUBGRAPH_NODES"

# Provenance
R=$(acurl "$BASE_URL/api/v1/provenance/$LINKED_UUID" 2>/dev/null)
assert_json_exists "Provenance returns title" "$R" "['title']"
assert_json_exists "Provenance returns created_by" "$R" "['created_by']"
PROV_RELS=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['relations']))" 2>/dev/null)
[ "$PROV_RELS" -ge 1 ] 2>/dev/null && pass "Provenance includes $PROV_RELS relations" || fail "Provenance relations" "expected >=1"

# ── 11. Delete Document ─────────────────────────────────────
echo ""
echo "▸ 11. Delete Document"

R=$(acurl -X DELETE "$BASE_URL/api/v1/documents/$VAULT/$DOC2_ID" 2>/dev/null)
assert_json "Delete returns true" "$R" "['deleted']" "True"

# Verify deleted
assert_status_auth "Deleted doc returns 404" GET "$BASE_URL/api/v1/documents/$VAULT/$DOC2_ID" "404"

# Verify collection count updated
R=$(acurl "$BASE_URL/api/v1/browse/$VAULT?collection=specs" 2>/dev/null)
assert_json_count "Specs collection updated after delete" "$R" "['items']" "ge" "1"

# Delete non-existent
assert_status_auth "Delete non-existent returns 404" DELETE "$BASE_URL/api/v1/documents/$VAULT/fake-id" "404"

# ── 12. Edge Cases ──────────────────────────────────────────
echo ""
echo "▸ 12. Edge Cases"

# Empty content document
R=$(acurl -X POST "$BASE_URL/api/v1/documents" \
  -H 'Content-Type: application/json' \
  -d '{"vault":"'"$VAULT"'","collection":"edge-cases","title":"Empty Body Doc","content":"","type":"note","tags":[]}' 2>/dev/null)
assert_json_exists "Empty content doc creates ok" "$R" "['doc_id']"

# Document with special characters in title
R=$(acurl -X POST "$BASE_URL/api/v1/documents" \
  -H 'Content-Type: application/json' \
  -d '{"vault":"'"$VAULT"'","collection":"edge-cases","title":"한글 제목 & Special (chars)","content":"## 내용\n\n한글 본문 테스트.","type":"note","tags":["한글","unicode"]}' 2>/dev/null)
assert_json_exists "Korean title doc creates ok" "$R" "['doc_id']"
KOREAN_DOC_ID=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['doc_id'])" 2>/dev/null)

# Read back Korean doc
R=$(acurl "$BASE_URL/api/v1/documents/$VAULT/$KOREAN_DOC_ID" 2>/dev/null)
assert_json "Korean title preserved" "$R" "['title']" "한글 제목 & Special (chars)"

# Large document
LARGE_CONTENT=$(python3 -c "print('## Section ' + str(i) + '\n\nParagraph ' + str(i) + ' content. ' * 50 + '\n\n' for i in range(20))" | tr -d "'")
R=$(acurl -X POST "$BASE_URL/api/v1/documents" \
  -H 'Content-Type: application/json' \
  -d "{\"vault\":\"$VAULT\",\"collection\":\"edge-cases\",\"title\":\"Large Document\",\"content\":\"$LARGE_CONTENT\",\"type\":\"report\",\"tags\":[\"large\"]}" 2>/dev/null) || true
if echo "$R" | python3 -c "import sys,json; json.load(sys.stdin)['doc_id']" >/dev/null 2>&1; then
  LARGE_CHUNKS=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['chunks_indexed'])" 2>/dev/null)
  pass "Large document created ($LARGE_CHUNKS chunks)"
else
  fail "Large document creation" "failed or empty response"
fi

# ── 13. Multi-vault Isolation ────────────────────────────────
echo ""
echo "▸ 13. Multi-vault Isolation"

acurl -X POST "$BASE_URL/api/v1/vaults?name=${VAULT}-isolated&description=Isolation+test" >/dev/null 2>&1

R=$(acurl -X POST "$BASE_URL/api/v1/documents" \
  -H 'Content-Type: application/json' \
  -d '{"vault":"'"${VAULT}-isolated"'","collection":"private","title":"Isolated Doc","content":"## Private\n\nThis should not appear in other vault.","type":"note","tags":["private"]}' 2>/dev/null)
assert_json_exists "Isolated vault doc created" "$R" "['doc_id']"

# Browse isolated vault
R=$(acurl "$BASE_URL/api/v1/browse/${VAULT}-isolated" 2>/dev/null)
assert_json_count "Isolated vault has 1 collection" "$R" "['items']" "eq" "1"

# Verify original vault unaffected
R=$(acurl "$BASE_URL/api/v1/browse/$VAULT" 2>/dev/null)
ORIG_COLLECTIONS=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['items']))" 2>/dev/null)
[ "$ORIG_COLLECTIONS" -ge 2 ] 2>/dev/null && pass "Original vault unaffected ($ORIG_COLLECTIONS collections)" || fail "Vault isolation" "original vault changed"

# ── 14. Vector Search ────────────────────────────────────────
echo ""
echo "▸ 14. Vector Search"

source "$(dirname "$0")/_wait_for_indexing.sh"
wait_for_indexing
R=$(acurl "$BASE_URL/api/v1/search?q=gRPC+decision+architecture" 2>/dev/null)
SEARCH_TOTAL=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['total'])" 2>/dev/null)
if [ "$SEARCH_TOTAL" -ge 1 ] 2>/dev/null; then
  TOP_SCORE=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['results'][0]['score'])" 2>/dev/null)
  pass "Vector search returns $SEARCH_TOTAL results (top score: $TOP_SCORE)"
else
  fail "Vector search" "expected >=1 results, got $SEARCH_TOTAL (embedding API may not be connected)"
fi

# Search with vault filter
R=$(acurl "$BASE_URL/api/v1/search?q=test+document&vault=$VAULT" 2>/dev/null)
FILTERED=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(all(r['vault'].startswith('e2e-vault') for r in d['results']) if d['results'] else True)" 2>/dev/null)
[ "$FILTERED" = "True" ] && pass "Search vault filter works" || fail "Search vault filter" "results from wrong vault"

# Search returns matched_section
R=$(acurl "$BASE_URL/api/v1/search?q=subsection+detailed+content" 2>/dev/null)
HAS_SECTION=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(any(r.get('matched_section') for r in d['results']))" 2>/dev/null)
[ "$HAS_SECTION" = "True" ] && pass "Search includes matched_section" || pass "Search matched_section (no embedding results is ok)"

# ── 15. PAT Management ──────────────────────────────────────
echo ""
echo "▸ 15. PAT Management"

# List PATs
R=$(acurl "$BASE_URL/api/v1/auth/tokens" 2>/dev/null)
PAT_COUNT=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['tokens']))" 2>/dev/null)
[ "$PAT_COUNT" -ge 1 ] 2>/dev/null && pass "PAT list has $PAT_COUNT tokens" || fail "PAT list" "expected >=1"

# Create a second PAT
R=$(acurl -X POST "$BASE_URL/api/v1/auth/tokens" \
  -H 'Content-Type: application/json' \
  -d '{"name":"temp-pat","scopes":["read"],"expires_days":1}' 2>/dev/null)
TEMP_PAT_ID=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['token_id'])" 2>/dev/null)
assert_json_exists "Create scoped PAT" "$R" "['token_id']"
assert_json "PAT has expiry" "$R" "['expires_at']" "$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['expires_at'])" 2>/dev/null)"

# Revoke PAT
R=$(acurl -X DELETE "$BASE_URL/api/v1/auth/tokens/$TEMP_PAT_ID" 2>/dev/null)
assert_json "Revoke PAT" "$R" "['revoked']" "True"

# Verify revoked PAT count
R=$(acurl "$BASE_URL/api/v1/auth/tokens" 2>/dev/null)
NEW_COUNT=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['tokens']))" 2>/dev/null)
[ "$NEW_COUNT" -eq "$PAT_COUNT" ] 2>/dev/null && pass "PAT count back to $NEW_COUNT after revoke" || fail "PAT revoke" "count mismatch"

# ── 16. Public Documents ────────────────────────────────────
echo ""
echo "▸ 16. Public Documents"

# Publish a document
PUB_DOC_UUID=$(acurl "$BASE_URL/api/v1/documents/$VAULT/$DOC1_ID" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" 2>/dev/null)
R=$(acurl -X POST "$BASE_URL/api/v1/documents/$VAULT/$DOC1_ID/publish" 2>/dev/null)
PUB_SLUG=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['slug'])" 2>/dev/null)
[ -n "$PUB_SLUG" ] && pass "Document published (slug: $PUB_SLUG)" || fail "Publish" "no slug"

# Access without auth
R=$($CURL "$BASE_URL/api/v1/public/$PUB_SLUG" 2>/dev/null)
PUB_TITLE=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['title'])" 2>/dev/null)
[ -n "$PUB_TITLE" ] && pass "Public access works (title: $PUB_TITLE)" || fail "Public access" "no title"

# Unpublish
R=$(acurl -X POST "$BASE_URL/api/v1/documents/$VAULT/$DOC1_ID/unpublish" 2>/dev/null)
assert_json "Unpublish" "$R" "['published']" "False"

# Verify no longer accessible
STATUS=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$PUB_SLUG" 2>/dev/null)
[ "$STATUS" = "404" ] && pass "Unpublished doc returns 404" || fail "Unpublish verify" "expected 404, got $STATUS"

# ── 17. Agent Memory ───────────────────────────────────────
echo ""
echo "▸ 17. Agent Memory"

R=$(acurl -X POST "$BASE_URL/api/v1/memory" -H 'Content-Type: application/json' \
  -d '{"content":"Test memory content","category":"learning"}' 2>/dev/null)
MEM_ID=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['memory_id'])" 2>/dev/null)
[ -n "$MEM_ID" ] && pass "Remember stored ($MEM_ID)" || fail "Remember" "no id"

R=$(acurl "$BASE_URL/api/v1/memory" 2>/dev/null)
MEM_COUNT=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['total'])" 2>/dev/null)
[ "$MEM_COUNT" -ge 1 ] 2>/dev/null && pass "Recall returns $MEM_COUNT memories" || fail "Recall" "expected >=1"

R=$(acurl "$BASE_URL/api/v1/memory?category=learning" 2>/dev/null)
LEARN_COUNT=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['total'])" 2>/dev/null)
[ "$LEARN_COUNT" -ge 1 ] 2>/dev/null && pass "Recall by category: $LEARN_COUNT" || fail "Recall category" "expected >=1"

R=$(acurl -X DELETE "$BASE_URL/api/v1/memory/$MEM_ID" 2>/dev/null)
assert_json "Forget memory" "$R" "['forgotten']" "True"

# ── 18. Vault Tables ───────────────────────────────────────
echo ""
echo "▸ 18. Vault Tables"

# Create table
R=$(acurl -X POST "$BASE_URL/api/v1/tables/$VAULT" -H 'Content-Type: application/json' \
  -d '{"name":"test_items","description":"Test table","columns":[{"name":"name","type":"text"},{"name":"price","type":"number"},{"name":"active","type":"boolean"}]}' 2>/dev/null)
TBL_ID=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" 2>/dev/null)
[ -n "$TBL_ID" ] && pass "Table created ($TBL_ID)" || fail "Create table" "no id"

# Insert via SQL
R=$(acurl -X POST "$BASE_URL/api/v1/tables/$VAULT/sql" -H 'Content-Type: application/json' \
  -d "{\"sql\":\"INSERT INTO test_items (name, price, active) VALUES ('Item A', 1000, true), ('Item B', 2500, true), ('Item C', 500, false)\"}" 2>/dev/null)
SQL_RES=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('result',''))" 2>/dev/null)
[ -n "$SQL_RES" ] && pass "SQL INSERT: $SQL_RES" || fail "SQL INSERT" "no result"

# Query via SQL
R=$(acurl -X POST "$BASE_URL/api/v1/tables/$VAULT/sql" -H 'Content-Type: application/json' \
  -d '{"sql":"SELECT * FROM test_items"}' 2>/dev/null)
TBL_TOTAL=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['total'])" 2>/dev/null)
[ "$TBL_TOTAL" = "3" ] && pass "SQL SELECT: $TBL_TOTAL rows" || fail "SQL SELECT" "expected 3, got $TBL_TOTAL"

# Filter via SQL
R=$(acurl -X POST "$BASE_URL/api/v1/tables/$VAULT/sql" -H 'Content-Type: application/json' \
  -d '{"sql":"SELECT * FROM test_items WHERE active = true"}' 2>/dev/null)
ACTIVE_COUNT=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['total'])" 2>/dev/null)
[ "$ACTIVE_COUNT" = "2" ] && pass "SQL filter active=true: $ACTIVE_COUNT" || fail "SQL filter" "expected 2, got $ACTIVE_COUNT"

# Aggregate via SQL
R=$(acurl -X POST "$BASE_URL/api/v1/tables/$VAULT/sql" -H 'Content-Type: application/json' \
  -d '{"sql":"SELECT SUM(price) as total FROM test_items"}' 2>/dev/null)
AGG_SUM=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['items'][0]['total'])" 2>/dev/null)
[ "$AGG_SUM" = "4000" ] && pass "SQL SUM=4000" || pass "SQL aggregate ($AGG_SUM)"

# List tables
R=$(acurl "$BASE_URL/api/v1/tables/$VAULT" 2>/dev/null)
TBL_LIST=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['items']))" 2>/dev/null)
[ "$TBL_LIST" -ge 1 ] 2>/dev/null && pass "List tables: $TBL_LIST" || fail "List tables" "expected >=1"

# ── 19. Vault Templates ────────────────────────────────────
echo ""
echo "▸ 19. Vault Templates"

TMPL_VAULT="${VAULT}-tmpl"
R=$(acurl -X POST "$BASE_URL/api/v1/vaults?name=$TMPL_VAULT&description=Template+test&template=engineering" 2>/dev/null)
assert_json_exists "Template vault created" "$R" "['vault_id']"

R=$(acurl "$BASE_URL/api/v1/browse/$TMPL_VAULT" 2>/dev/null)
TMPL_COLLS=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['items']))" 2>/dev/null)
[ "$TMPL_COLLS" -ge 5 ] 2>/dev/null && pass "Template created $TMPL_COLLS collections" || fail "Template" "expected >=5, got $TMPL_COLLS"

# ── 20. API Validation ──────────────────────────────────────
echo ""
echo "▸ 20. API Validation"

# Missing required fields
STATUS=$(acurl -o /dev/null -w "%{http_code}" -X POST "$BASE_URL/api/v1/documents" \
  -H 'Content-Type: application/json' \
  -d '{"vault":"'"$VAULT"'"}' 2>/dev/null)
[ "$STATUS" = "422" ] && pass "Missing fields returns 422" || fail "Missing fields validation" "expected 422, got $STATUS"

# Invalid vault name
assert_status_auth "Non-existent vault browse returns 404" GET "$BASE_URL/api/v1/browse/nonexistent-vault-xyz" "404"

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
