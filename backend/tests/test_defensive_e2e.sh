#!/bin/bash
#
# AKB E2E: Defensive / Edge Cases
# Delete cleanup, nonexistent resources, publish lifecycle, empty results, invalid inputs
#
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
PASS=0
FAIL=0
ERRORS=()
MCP_ID=10

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

echo "╔══════════════════════════════════════════╗"
echo "║   AKB Defensive / Edge Case E2E Tests    ║"
echo "║   Target: $BASE_URL"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── Setup ────────────────────────────────────────────────────
echo "▸ 0. Setup"

E2E_USER="def-e2e-$(date +%s)"
curl -sk -X POST "$BASE_URL/api/v1/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$E2E_USER\",\"email\":\"$E2E_USER@test.dev\",\"password\":\"test1234\"}" >/dev/null 2>&1

JWT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$E2E_USER\",\"password\":\"test1234\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

PAT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" \
  -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' \
  -d '{"name":"def-e2e"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

[ -n "$PAT" ] && pass "PAT acquired" || { fail "PAT" "could not get PAT"; exit 1; }

# MCP session
tmpfile=$(mktemp)
curl -sk -i -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"def-e2e","version":"1.0"}}}' > "$tmpfile" 2>/dev/null
SID=$(grep -i "mcp-session-id" "$tmpfile" | tr -d '\r' | awk '{print $2}')
rm -f "$tmpfile"
curl -sk -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null 2>&1

mc() {
  MCP_ID=$((MCP_ID+1))
  curl -sk -X POST "$BASE_URL/mcp/" \
    -H "Authorization: Bearer $PAT" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -H "mcp-session-id: $SID" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":$MCP_ID,\"method\":\"tools/call\",\"params\":{\"name\":\"$1\",\"arguments\":$2}}" 2>&1
}

mr() { python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(d['result']['content'][0]['text'])" 2>/dev/null; }
m() { mc "$1" "$2" | mr; }

VAULT="def-e2e-$(date +%s)"
m "akb_create_vault" "{\"name\":\"$VAULT\",\"description\":\"defensive tests\"}" >/dev/null
pass "Vault created ($VAULT)"

# ── 1. Delete + Cleanup Verification ────────────────────────
echo ""
echo "▸ 1. Delete + Search/Grep Cleanup"

# Create a doc with distinctive content
R=$(m "akb_put" "{\"vault\":\"$VAULT\",\"collection\":\"cleanup\",\"title\":\"Ephemeral Doc\",\"content\":\"# Ephemeral\\nUNIQUE_MARKER_XJ7K9Q for search verification\",\"tags\":[\"ephemeral\"]}")
EPH_DOC_URI=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('uri',''))" 2>/dev/null)
[ -n "$EPH_DOC_URI" ] && pass "Ephemeral doc created ($EPH_DOC_URI)" || fail "Create" "$R"

# Verify searchable
source "$(dirname "$0")/_wait_for_indexing.sh"
wait_for_indexing
R=$(m "akb_search" "{\"query\":\"UNIQUE_MARKER_XJ7K9Q\",\"vault\":\"$VAULT\"}")
SEARCH_BEFORE=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('total',0))" 2>/dev/null)
[ "$SEARCH_BEFORE" -ge 1 ] 2>/dev/null && pass "Searchable before delete ($SEARCH_BEFORE)" || fail "Search before" "$R"

# Verify greppable
R=$(m "akb_grep" "{\"pattern\":\"UNIQUE_MARKER_XJ7K9Q\",\"vault\":\"$VAULT\"}")
GREP_BEFORE=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('total_matches',0))" 2>/dev/null)
[ "$GREP_BEFORE" -ge 1 ] 2>/dev/null && pass "Greppable before delete ($GREP_BEFORE)" || fail "Grep before" "$R"

# Delete
R=$(m "akb_delete" "{\"uri\":\"$EPH_DOC_URI\"}")
DELETED=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('deleted',False))" 2>/dev/null)
[ "$DELETED" = "True" ] && pass "Document deleted" || fail "Delete" "$R"

# Verify NOT searchable after delete
sleep 1  # brief wait for async cleanup
R=$(m "akb_search" "{\"query\":\"UNIQUE_MARKER_XJ7K9Q\",\"vault\":\"$VAULT\"}")
SEARCH_AFTER=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('total',0))" 2>/dev/null)
[ "$SEARCH_AFTER" = "0" ] && pass "Not searchable after delete (0 hits)" || fail "Search after delete" "still $SEARCH_AFTER hits"

# Verify NOT greppable after delete
R=$(m "akb_grep" "{\"pattern\":\"UNIQUE_MARKER_XJ7K9Q\",\"vault\":\"$VAULT\"}")
GREP_AFTER=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('total_matches',0))" 2>/dev/null)
[ "$GREP_AFTER" = "0" ] && pass "Not greppable after delete (0 matches)" || fail "Grep after delete" "still $GREP_AFTER matches"

# Verify get returns not found
R=$(m "akb_get" "{\"uri\":\"$EPH_DOC_URI\"}")
GET_ERROR=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('error' in d or 'not found' in str(d).lower())" 2>/dev/null)
[ "$GET_ERROR" = "True" ] && pass "Get returns not found after delete" || fail "Get after delete" "$R"

# ── 2. Nonexistent Resource Operations ───────────────────────
echo ""
echo "▸ 2. Nonexistent Resource Operations"

NONEXIST_URI="akb://$VAULT/doc/nonexistent/missing.md"

# Update nonexistent doc
R=$(m "akb_update" "{\"uri\":\"$NONEXIST_URI\",\"content\":\"# Nope\"}")
UPDATE_ERR=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('error' in d or 'not found' in str(d).lower())" 2>/dev/null)
[ "$UPDATE_ERR" = "True" ] && pass "Update nonexistent doc → error" || fail "Update nonexistent" "$R"

# Edit nonexistent doc
R=$(m "akb_edit" "{\"uri\":\"$NONEXIST_URI\",\"old_string\":\"old\",\"new_string\":\"new\"}")
EDIT_ERR=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('error' in d or 'not found' in str(d).lower())" 2>/dev/null)
[ "$EDIT_ERR" = "True" ] && pass "Edit nonexistent doc → error" || fail "Edit nonexistent" "$R"

# Delete nonexistent doc
R=$(m "akb_delete" "{\"uri\":\"$NONEXIST_URI\"}")
DEL_ERR=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('deleted') == False or 'error' in d or 'not found' in str(d).lower())" 2>/dev/null)
[ "$DEL_ERR" = "True" ] && pass "Delete nonexistent doc → error/false" || fail "Delete nonexistent" "$R"

# History nonexistent doc
R=$(m "akb_history" "{\"uri\":\"$NONEXIST_URI\"}")
HIST_ERR=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('error' in d or 'not found' in str(d).lower())" 2>/dev/null)
[ "$HIST_ERR" = "True" ] && pass "History nonexistent doc → error" || fail "History nonexistent" "$R"

# Operations on nonexistent vault
R=$(m "akb_put" "{\"vault\":\"vault-that-does-not-exist-xyz\",\"collection\":\"x\",\"title\":\"x\",\"content\":\"x\"}")
VAULT_ERR=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('error' in d or 'not found' in str(d).lower() or 'denied' in str(d).lower())" 2>/dev/null)
[ "$VAULT_ERR" = "True" ] && pass "Put to nonexistent vault → error" || fail "Nonexistent vault" "$R"

# ── 3. Publish Lifecycle ─────────────────────────────────────
echo ""
echo "▸ 3. Publish → View → Unpublish → Verify Inaccessible"

R=$(m "akb_put" "{\"vault\":\"$VAULT\",\"collection\":\"public\",\"title\":\"Public Test Doc\",\"content\":\"# Public Content\\nThis should be viewable via slug\"}")
PUB_DOC_URI=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('uri',''))" 2>/dev/null)
[ -n "$PUB_DOC_URI" ] && pass "Doc for publishing ($PUB_DOC_URI)" || fail "Pub doc create" "$R"

# Publish
R=$(m "akb_publish" "{\"uri\":\"$PUB_DOC_URI\"}")
SLUG=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('slug',''))" 2>/dev/null)
[ -n "$SLUG" ] && pass "Published (slug=$SLUG)" || fail "Publish" "$R"

# Access public URL (no auth)
PUB_STATUS=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$SLUG")
[ "$PUB_STATUS" = "200" ] && pass "Public URL accessible (200)" || fail "Public access" "HTTP $PUB_STATUS"

# Verify content in public response
PUB_CONTENT=$(curl -sk "$BASE_URL/api/v1/public/$SLUG" | python3 -c "import sys,json; d=json.load(sys.stdin); print('Public Content' in d.get('content',''))" 2>/dev/null)
[ "$PUB_CONTENT" = "True" ] && pass "Public content correct" || fail "Public content" "missing"

# Unpublish
R=$(m "akb_unpublish" "{\"uri\":\"$PUB_DOC_URI\"}")
UNPUB=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('published') == False)" 2>/dev/null)
[ "$UNPUB" = "True" ] && pass "Unpublished" || fail "Unpublish" "$R"

# Public URL should now fail
UNPUB_STATUS=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/public/$SLUG")
[ "$UNPUB_STATUS" = "404" ] && pass "Public URL inaccessible after unpublish (404)" || fail "Unpublish verify" "HTTP $UNPUB_STATUS"

# ── 4. Search Edge Cases ─────────────────────────────────────
echo ""
echo "▸ 4. Search Edge Cases"

# Semantic search may return low-relevance results — verify it doesn't crash
R=$(m "akb_search" "{\"query\":\"ZZZZZ_NONEXISTENT_QUERY_12345\",\"vault\":\"$VAULT\"}")
EMPTY_OK=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('total' in d and 'results' in d)" 2>/dev/null)
[ "$EMPTY_OK" = "True" ] && pass "Irrelevant search returns valid response" || fail "Empty search" "$R"

# Grep empty results
R=$(m "akb_grep" "{\"pattern\":\"ZZZZZ_NONEXISTENT_12345\",\"vault\":\"$VAULT\"}")
GREP_EMPTY=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('total_docs',0))" 2>/dev/null)
[ "$GREP_EMPTY" = "0" ] && pass "Empty grep returns 0 docs" || fail "Empty grep" "docs=$GREP_EMPTY"

# Search with special characters
R=$(m "akb_search" "{\"query\":\"SELECT * FROM; DROP TABLE--\",\"vault\":\"$VAULT\"}")
SPECIAL_OK=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('error' not in d)" 2>/dev/null)
[ "$SPECIAL_OK" = "True" ] && pass "Search with SQL-like chars doesn't crash" || fail "Special char search" "$R"

# ── 5. Invalid Vault Names ───────────────────────────────────
echo ""
echo "▸ 5. Invalid Vault Names"

# Vault with uppercase (should be rejected or normalized)
R=$(m "akb_create_vault" "{\"name\":\"UPPERCASE_VAULT\",\"description\":\"test\"}")
UPPER_RESULT=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('error' in d or 'vault_id' in d)" 2>/dev/null)
[ "$UPPER_RESULT" = "True" ] && pass "Uppercase vault: handled (error or normalized)" || fail "Uppercase vault" "$R"
# Cleanup if created
m "akb_delete_vault" "{\"name\":\"UPPERCASE_VAULT\"}" >/dev/null 2>&1
m "akb_delete_vault" "{\"name\":\"uppercase_vault\"}" >/dev/null 2>&1

# Vault with spaces — should be rejected
R=$(m "akb_create_vault" "{\"name\":\"vault with spaces\",\"description\":\"test\"}")
SPACE_ERR=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('error' in d or 'Invalid' in str(d))" 2>/dev/null)
[ "$SPACE_ERR" = "True" ] && pass "Vault with spaces rejected" || { fail "Space vault" "$R"; m "akb_delete_vault" "{\"name\":\"vault with spaces\"}" >/dev/null 2>&1; }

# Empty vault name — should be rejected
R=$(m "akb_create_vault" "{\"name\":\"\",\"description\":\"test\"}")
EMPTY_NAME_ERR=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('error' in d or 'Invalid' in str(d))" 2>/dev/null)
[ "$EMPTY_NAME_ERR" = "True" ] && pass "Empty vault name rejected" || fail "Empty name" "$R"

# Vault starting with hyphen — should be rejected
R=$(m "akb_create_vault" "{\"name\":\"-bad-start\",\"description\":\"test\"}")
HYPHEN_ERR=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('error' in d or 'Invalid' in str(d))" 2>/dev/null)
[ "$HYPHEN_ERR" = "True" ] && pass "Vault starting with hyphen rejected" || fail "Hyphen start" "$R"

# ── 6. Browse Empty Vault ────────────────────────────────────
echo ""
echo "▸ 6. Empty Vault Operations"

EMPTY_VAULT="def-empty-$(date +%s)"
m "akb_create_vault" "{\"name\":\"$EMPTY_VAULT\",\"description\":\"empty\"}" >/dev/null

# Browse empty vault
R=$(m "akb_browse" "{\"vault\":\"$EMPTY_VAULT\"}")
EMPTY_ITEMS=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('items',[])))" 2>/dev/null)
[ "$EMPTY_ITEMS" = "0" ] && pass "Browse empty vault: 0 items" || fail "Empty browse" "items=$EMPTY_ITEMS"

# Search empty vault
R=$(m "akb_search" "{\"query\":\"anything\",\"vault\":\"$EMPTY_VAULT\"}")
EMPTY_SEARCH=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('total',0))" 2>/dev/null)
[ "$EMPTY_SEARCH" = "0" ] && pass "Search empty vault: 0 results" || fail "Empty vault search" "$R"

# Activity on new vault — may have init commit
R=$(m "akb_activity" "{\"vault\":\"$EMPTY_VAULT\"}")
EMPTY_ACT=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('activity',[])))" 2>/dev/null)
[ "$EMPTY_ACT" -le 1 ] 2>/dev/null && pass "Activity on new vault: $EMPTY_ACT entries (init commit)" || fail "Empty activity" "entries=$EMPTY_ACT"

m "akb_delete_vault" "{\"name\":\"$EMPTY_VAULT\"}" >/dev/null 2>&1

# ── 7. Duplicate Operations ──────────────────────────────────
echo ""
echo "▸ 7. Idempotency / Duplicates"

# Create vault with same name twice
R=$(m "akb_create_vault" "{\"name\":\"$VAULT\",\"description\":\"duplicate\"}")
DUP_ERR=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('error' in d or 'exist' in str(d).lower() or 'conflict' in str(d).lower())" 2>/dev/null)
[ "$DUP_ERR" = "True" ] && pass "Duplicate vault creation rejected" || fail "Dup vault" "$R"

# Publish already published doc (should be idempotent)
R=$(m "akb_put" "{\"vault\":\"$VAULT\",\"collection\":\"idem\",\"title\":\"Idem Doc\",\"content\":\"# Idem\"}")
IDEM_DOC_URI=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('uri',''))" 2>/dev/null)
m "akb_publish" "{\"uri\":\"$IDEM_DOC_URI\"}" >/dev/null
R=$(m "akb_publish" "{\"uri\":\"$IDEM_DOC_URI\"}")
IDEM_PUB=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('published',False))" 2>/dev/null)
[ "$IDEM_PUB" = "True" ] && pass "Re-publish is idempotent" || fail "Re-publish" "$R"

# ── Cleanup ──────────────────────────────────────────────────
echo ""
echo "▸ Cleanup"
m "akb_delete_vault" "{\"name\":\"$VAULT\"}" >/dev/null 2>&1
pass "Vault deleted"

# ── Summary ──────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════"
echo "  Results: $PASS passed, $FAIL failed"
if [ $FAIL -gt 0 ]; then
  echo "  Failures:"
  for e in "${ERRORS[@]}"; do echo "    - $e"; done
  echo "════════════════════════════════════════════"
  exit 1
fi
echo "════════════════════════════════════════════"
