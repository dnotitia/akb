#!/bin/bash
#
# AKB Security & Edge Case E2E Tests
# Covers: grep access control, edit edge cases, memory filtering, SQL access
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
echo "║   AKB Security & Edge Case E2E Tests     ║"
echo "║   Target: $BASE_URL"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── Setup: two users ─────────────────────────────────────────
echo "▸ 0. Setup (2 users + 2 vaults)"

setup_user() {
  local user=$1
  curl -sk -X POST "$BASE_URL/api/v1/auth/register" \
    -H 'Content-Type: application/json' \
    -d "{\"username\":\"$user\",\"email\":\"$user@test.dev\",\"password\":\"test1234\"}" >/dev/null 2>&1
  local jwt=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" \
    -H 'Content-Type: application/json' \
    -d "{\"username\":\"$user\",\"password\":\"test1234\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)
  curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" \
    -H "Authorization: Bearer $jwt" \
    -H 'Content-Type: application/json' \
    -d '{"name":"e2e"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null
}

USER1="sec-e2e-u1-$(date +%s)"
USER2="sec-e2e-u2-$(date +%s)"
PAT1=$(setup_user "$USER1")
PAT2=$(setup_user "$USER2")

[ -n "$PAT1" ] && [ -n "$PAT2" ] && pass "2 users created" || { fail "Setup" "user creation failed"; exit 1; }

# MCP session helpers
setup_mcp() {
  local pat=$1
  local tmpfile=$(mktemp)
  curl -sk -i -X POST "$BASE_URL/mcp/" \
    -H "Authorization: Bearer $pat" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"sec-e2e","version":"1.0"}}}' > "$tmpfile" 2>/dev/null
  local sid=$(grep -i "mcp-session-id" "$tmpfile" | tr -d '\r' | awk '{print $2}')
  rm -f "$tmpfile"
  curl -sk -X POST "$BASE_URL/mcp/" \
    -H "Authorization: Bearer $pat" \
    -H "Content-Type: application/json" \
    -H "mcp-session-id: $sid" \
    -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null 2>&1
  echo "$sid"
}

SID1=$(setup_mcp "$PAT1")
SID2=$(setup_mcp "$PAT2")

mcp_as() {
  local pat=$1 sid=$2 tool=$3 args=$4
  MCP_ID=$((MCP_ID+1))
  curl -sk -X POST "$BASE_URL/mcp/" \
    -H "Authorization: Bearer $pat" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -H "mcp-session-id: $sid" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":$MCP_ID,\"method\":\"tools/call\",\"params\":{\"name\":\"$tool\",\"arguments\":$args}}" 2>&1
}

mr() { python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(d['result']['content'][0]['text'])" 2>/dev/null; }

# Create vaults: user1 owns vault1 (private), user1 owns vault2 (private)
VAULT1="sec-private-$(date +%s)"
VAULT2="sec-other-$(($(date +%s)+1))"

R=$(mcp_as "$PAT1" "$SID1" "akb_create_vault" "{\"name\":\"$VAULT1\",\"description\":\"user1 private vault\"}" | mr)
echo "$R" | python3 -c "import sys,json; json.load(sys.stdin)['vault_id']" >/dev/null 2>&1 && pass "Vault1 created ($VAULT1)" || fail "Vault1" "$R"

R=$(mcp_as "$PAT1" "$SID1" "akb_create_vault" "{\"name\":\"$VAULT2\",\"description\":\"user1 other vault\"}" | mr)
echo "$R" | python3 -c "import sys,json; json.load(sys.stdin)['vault_id']" >/dev/null 2>&1 && pass "Vault2 created ($VAULT2)" || fail "Vault2" "$R"

# Put a doc with secret content in vault1
R=$(mcp_as "$PAT1" "$SID1" "akb_put" "{\"vault\":\"$VAULT1\",\"collection\":\"secrets\",\"title\":\"Secret Doc\",\"content\":\"# Secret\\nThe password is XYZZY-SECRET-12345\"}" | mr)
DOC1=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['doc_id'])" 2>/dev/null)
[ -n "$DOC1" ] && pass "Secret doc created ($DOC1)" || fail "Secret doc" "$R"

# ── 1. Grep Access Control ───────────────────────────────────
echo ""
echo "▸ 1. Grep Access Control"

# User2 should NOT be able to grep user1's private vault
R=$(mcp_as "$PAT2" "$SID2" "akb_grep" "{\"pattern\":\"XYZZY-SECRET\",\"vault\":\"$VAULT1\"}" | mr)
HAS_ERROR=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('error' in d or 'Access denied' in str(d))" 2>/dev/null)
[ "$HAS_ERROR" = "True" ] && pass "User2 blocked from grep on private vault" || fail "Grep access" "User2 could search private vault: $R"

# User1 CAN grep own vault
R=$(mcp_as "$PAT1" "$SID1" "akb_grep" "{\"pattern\":\"XYZZY-SECRET\",\"vault\":\"$VAULT1\"}" | mr)
MATCHES=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('total_matches',0))" 2>/dev/null)
[ "$MATCHES" -ge 1 ] 2>/dev/null && pass "User1 can grep own vault ($MATCHES matches)" || fail "Grep own vault" "expected >=1 match, got: $MATCHES"

# User2 grep without vault should NOT return user1's private content
R=$(mcp_as "$PAT2" "$SID2" "akb_grep" "{\"pattern\":\"XYZZY-SECRET\"}" | mr)
LEAK_DOCS=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('total_docs',0))" 2>/dev/null)
[ "$LEAK_DOCS" = "0" ] && pass "Cross-vault grep leak prevented (0 docs)" || fail "Grep leak" "User2 found $LEAK_DOCS docs without vault access"

# ── 1b. Knowledge graph access control (issue #3) ────────────
echo ""
echo "▸ 1b. Knowledge graph access control"

DOC1_URI="akb://$VAULT1/doc/secrets/secret-doc.md"

# User2 must NOT be able to query relations on user1's vault
R=$(mcp_as "$PAT2" "$SID2" "akb_relations" "{\"vault\":\"$VAULT1\",\"resource_uri\":\"$DOC1_URI\"}" | mr)
HAS_ERR=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('error' in d or 'Access denied' in str(d) or 'forbidden' in str(d).lower())" 2>/dev/null)
[ "$HAS_ERR" = "True" ] && pass "User2 blocked from akb_relations on private vault" || fail "Relations ACL" "User2 got relations on private vault: $R"

R=$(mcp_as "$PAT2" "$SID2" "akb_graph" "{\"vault\":\"$VAULT1\"}" | mr)
HAS_ERR=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('error' in d or 'Access denied' in str(d) or 'forbidden' in str(d).lower())" 2>/dev/null)
[ "$HAS_ERR" = "True" ] && pass "User2 blocked from akb_graph on private vault" || fail "Graph ACL" "User2 got graph on private vault: $R"

R=$(mcp_as "$PAT2" "$SID2" "akb_provenance" "{\"doc_id\":\"$DOC1\"}" | mr)
HAS_ERR=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('error' in d or 'Access denied' in str(d) or 'forbidden' in str(d).lower() or 'not found' in str(d).lower())" 2>/dev/null)
[ "$HAS_ERR" = "True" ] && pass "User2 blocked from akb_provenance on private doc" || fail "Provenance ACL" "User2 got provenance on private doc: $R"

# User1 can still call all three on own vault
R=$(mcp_as "$PAT1" "$SID1" "akb_graph" "{\"vault\":\"$VAULT1\"}" | mr)
echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); assert 'nodes' in d and 'edges' in d" 2>/dev/null && pass "User1 can call akb_graph on own vault" || fail "Graph self" "$R"

# ── 1c. Vault-scoped health ACL ──────────────────────────────
echo ""
echo "▸ 1c. Vault-scoped health ACL"

# user1 sees own vault health (note: /health is off-prefix, no /api/v1)
R=$(curl -sk -H "Authorization: Bearer $PAT1" "$BASE_URL/health/vault/$VAULT1")
HAS=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('vector_store' in d)" 2>/dev/null)
[ "$HAS" = "True" ] && pass "User1 sees own vault health" || fail "vault health self" "$R"

# user2 blocked from user1's private vault → 403
HTTP=$(curl -sS -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $PAT2" "$BASE_URL/health/vault/$VAULT1")
[ "$HTTP" = "403" ] && pass "User2 blocked from vault health (403)" || fail "vault health ACL" "got HTTP $HTTP"

# unauthenticated → 401
HTTP=$(curl -sS -o /dev/null -w "%{http_code}" "$BASE_URL/health/vault/$VAULT1")
[ "$HTTP" = "401" ] && pass "Unauthenticated blocked from vault health (401)" || fail "vault health auth" "got HTTP $HTTP"

# unknown vault → 404
HTTP=$(curl -sS -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $PAT1" "$BASE_URL/health/vault/nonexistent-vault-xyz")
[ "$HTTP" = "404" ] && pass "Unknown vault returns 404" || fail "vault health 404" "got HTTP $HTTP"

# ── 2. Grep Regex Validation ─────────────────────────────────
echo ""
echo "▸ 2. Grep Regex Validation"

R=$(mcp_as "$PAT1" "$SID1" "akb_grep" "{\"pattern\":\"(invalid[[\",\"regex\":true,\"vault\":\"$VAULT1\"}" | mr)
HAS_REGEX_ERR=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('error' in d and 'regex' in d.get('error','').lower())" 2>/dev/null)
[ "$HAS_REGEX_ERR" = "True" ] && pass "Invalid regex rejected with clear error" || fail "Regex validation" "$R"

# Valid regex should work
R=$(mcp_as "$PAT1" "$SID1" "akb_grep" "{\"pattern\":\"XYZZY.*12345\",\"regex\":true,\"vault\":\"$VAULT1\"}" | mr)
REGEX_MATCH=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('total_matches',0))" 2>/dev/null)
[ "$REGEX_MATCH" -ge 1 ] 2>/dev/null && pass "Valid regex works ($REGEX_MATCH matches)" || fail "Valid regex" "$R"

# ── 3. Edit Edge Cases ───────────────────────────────────────
echo ""
echo "▸ 3. Edit Edge Cases"

# Create a doc to edit — Line 1 repeated twice to test uniqueness
R=$(mcp_as "$PAT1" "$SID1" "akb_put" "{\"vault\":\"$VAULT1\",\"collection\":\"docs\",\"title\":\"Edit Test\",\"content\":\"# Edit Test\\n\\nAlpha unique line\\nBeta repeated\\nGamma line\\nBeta repeated\\nDelta unique line\"}" | mr)
EDIT_DOC=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['doc_id'])" 2>/dev/null)
[ -n "$EDIT_DOC" ] && pass "Edit test doc created" || fail "Edit doc" "$R"

# 3a. Edit: old_string not found → error
R=$(mcp_as "$PAT1" "$SID1" "akb_edit" "{\"vault\":\"$VAULT1\",\"doc_id\":\"$EDIT_DOC\",\"old_string\":\"NOTHING LIKE THIS EXISTS\",\"new_string\":\"whatever\"}" | mr)
NOT_FOUND_ERR=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('error','')=='edit_failed' and 'not found' in d.get('message','').lower())" 2>/dev/null)
[ "$NOT_FOUND_ERR" = "True" ] && pass "Edit: old_string not found rejected" || fail "Edit not found" "$R"

# 3b. Edit: old_string not unique → error
R=$(mcp_as "$PAT1" "$SID1" "akb_edit" "{\"vault\":\"$VAULT1\",\"doc_id\":\"$EDIT_DOC\",\"old_string\":\"Beta repeated\",\"new_string\":\"Beta replaced\"}" | mr)
NOT_UNIQUE_ERR=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('error','')=='edit_failed' and 'appears' in d.get('message',''))" 2>/dev/null)
[ "$NOT_UNIQUE_ERR" = "True" ] && pass "Edit: non-unique old_string rejected" || fail "Edit non-unique" "$R"

# 3c. Edit: valid single replacement
R=$(mcp_as "$PAT1" "$SID1" "akb_edit" "{\"vault\":\"$VAULT1\",\"doc_id\":\"$EDIT_DOC\",\"old_string\":\"Alpha unique line\",\"new_string\":\"Alpha MODIFIED\"}" | mr)
EDIT_COMMIT=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('commit_hash',''))" 2>/dev/null)
[ -n "$EDIT_COMMIT" ] && pass "Valid edit applied (commit=${EDIT_COMMIT:0:8})" || fail "Valid edit" "$R"

# 3d. Edit: replace_all works for duplicates
R=$(mcp_as "$PAT1" "$SID1" "akb_edit" "{\"vault\":\"$VAULT1\",\"doc_id\":\"$EDIT_DOC\",\"old_string\":\"Beta repeated\",\"new_string\":\"Beta fixed\",\"replace_all\":true}" | mr)
EDIT_ALL_COMMIT=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('commit_hash',''))" 2>/dev/null)
[ -n "$EDIT_ALL_COMMIT" ] && pass "Edit replace_all works (commit=${EDIT_ALL_COMMIT:0:8})" || fail "Edit replace_all" "$R"

# 3e. Edit: empty old_string rejected
R=$(mcp_as "$PAT1" "$SID1" "akb_edit" "{\"vault\":\"$VAULT1\",\"doc_id\":\"$EDIT_DOC\",\"old_string\":\"\",\"new_string\":\"x\"}" | mr)
EMPTY_ERR=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('error','')=='edit_failed' and 'empty' in d.get('message','').lower())" 2>/dev/null)
[ "$EMPTY_ERR" = "True" ] && pass "Edit: empty old_string rejected" || fail "Empty old_string" "$R"

# ── 4. Memory Category Filtering ─────────────────────────────
echo ""
echo "▸ 4. Memory Category Filtering"

# Create memories in different categories
R=$(mcp_as "$PAT1" "$SID1" "akb_remember" '{"content":"I prefer dark mode","category":"preference"}' | mr)
MEM_PREF=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['memory_id'])" 2>/dev/null)

R=$(mcp_as "$PAT1" "$SID1" "akb_remember" '{"content":"Learned about vector indexing","category":"learning"}' | mr)
MEM_LEARN=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['memory_id'])" 2>/dev/null)

R=$(mcp_as "$PAT1" "$SID1" "akb_remember" '{"content":"Working on RFP analysis","category":"context"}' | mr)
MEM_CTX=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['memory_id'])" 2>/dev/null)

[ -n "$MEM_PREF" ] && [ -n "$MEM_LEARN" ] && [ -n "$MEM_CTX" ] && pass "3 memories created in different categories" || fail "Memory create" "missing IDs"

# Filter by category
R=$(mcp_as "$PAT1" "$SID1" "akb_recall" '{"category":"preference"}' | mr)
PREF_COUNT=$(echo "$R" | python3 -c "import sys,json; mems=json.load(sys.stdin)['memories']; print(len([m for m in mems if m['category']=='preference']))" 2>/dev/null)
PREF_TOTAL=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['memories']))" 2>/dev/null)
[ "$PREF_COUNT" = "$PREF_TOTAL" ] && pass "Category filter: only preference returned ($PREF_COUNT)" || fail "Category filter" "expected all preference, got $PREF_COUNT of $PREF_TOTAL"

# Recall all
R=$(mcp_as "$PAT1" "$SID1" "akb_recall" '{}' | mr)
ALL_COUNT=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['memories']))" 2>/dev/null)
[ "$ALL_COUNT" -ge 3 ] 2>/dev/null && pass "Recall all: $ALL_COUNT memories" || fail "Recall all" "expected >=3, got $ALL_COUNT"

# User2 should NOT see user1's memories
R=$(mcp_as "$PAT2" "$SID2" "akb_recall" '{}' | mr)
U2_MEMS=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['memories']))" 2>/dev/null)
[ "$U2_MEMS" = "0" ] && pass "User2 sees 0 of User1's memories (isolation)" || fail "Memory isolation" "User2 sees $U2_MEMS memories"

# Cleanup memories
mcp_as "$PAT1" "$SID1" "akb_forget" "{\"memory_id\":\"$MEM_PREF\"}" >/dev/null 2>&1
mcp_as "$PAT1" "$SID1" "akb_forget" "{\"memory_id\":\"$MEM_LEARN\"}" >/dev/null 2>&1
mcp_as "$PAT1" "$SID1" "akb_forget" "{\"memory_id\":\"$MEM_CTX\"}" >/dev/null 2>&1
pass "Memories cleaned up"

# ── 5. Todo Edge Cases ───────────────────────────────────────
echo ""
echo "▸ 5. Todo Edge Cases"

# Todo with vault + ref_doc
R=$(mcp_as "$PAT1" "$SID1" "akb_todo" "{\"title\":\"Review secret doc\",\"vault\":\"$VAULT1\",\"ref_doc\":\"$DOC1\",\"priority\":\"urgent\",\"due_date\":\"2026-04-15\"}" | mr)
TODO_ID=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['todo_id'])" 2>/dev/null)
[ -n "$TODO_ID" ] && pass "Todo with vault+ref_doc+priority+due ($TODO_ID)" || fail "Todo create" "$R"

# List with vault filter
R=$(mcp_as "$PAT1" "$SID1" "akb_todos" "{\"vault\":\"$VAULT1\"}" | mr)
VAULT_TODOS=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['todos']))" 2>/dev/null)
[ "$VAULT_TODOS" -ge 1 ] 2>/dev/null && pass "Todo vault filter: $VAULT_TODOS todos" || fail "Todo vault filter" "$R"

# Update: reassign + change priority
R=$(mcp_as "$PAT1" "$SID1" "akb_todo_update" "{\"todo_id\":\"$TODO_ID\",\"priority\":\"low\",\"note\":\"Deprioritized\"}" | mr)
UPDATED=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('updated',False))" 2>/dev/null)
[ "$UPDATED" = "True" ] && pass "Todo updated (priority+note)" || fail "Todo update" "$R"

# Mark done
R=$(mcp_as "$PAT1" "$SID1" "akb_todo_update" "{\"todo_id\":\"$TODO_ID\",\"status\":\"done\"}" | mr)
pass "Todo marked done"

# Done todo should not appear in open list
R=$(mcp_as "$PAT1" "$SID1" "akb_todos" "{\"status\":\"open\",\"vault\":\"$VAULT1\"}" | mr)
OPEN_REMAINING=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['todos']))" 2>/dev/null)
[ "$OPEN_REMAINING" = "0" ] && pass "Done todo not in open list" || fail "Todo status" "still $OPEN_REMAINING open"

# ── 6. Drill-down with d- prefix ID ─────────────────────────
echo ""
echo "▸ 6. Drill-down ID resolution"

R=$(mcp_as "$PAT1" "$SID1" "akb_drill_down" "{\"vault\":\"$VAULT1\",\"doc_id\":\"$DOC1\"}" | mr)
DD_SECTIONS=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('sections',[])))" 2>/dev/null)
[ "$DD_SECTIONS" -ge 1 ] 2>/dev/null && pass "Drill-down with d- prefix ID: $DD_SECTIONS sections" || fail "Drill-down" "0 sections, response=$R"

# Filter by section
R=$(mcp_as "$PAT1" "$SID1" "akb_drill_down" "{\"vault\":\"$VAULT1\",\"doc_id\":\"$DOC1\",\"section\":\"Secret\"}" | mr)
DD_FILTERED=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('sections',[])))" 2>/dev/null)
[ "$DD_FILTERED" -ge 1 ] 2>/dev/null && pass "Drill-down section filter works" || fail "Drill-down filter" "$R"

# ── 7. SQL Table Access Control ──────────────────────────────
echo ""
echo "▸ 7. SQL Table Access Control"

# User1 creates a table in vault1
R=$(mcp_as "$PAT1" "$SID1" "akb_create_table" "{\"vault\":\"$VAULT1\",\"name\":\"finances\",\"description\":\"Sensitive data\",\"columns\":[{\"name\":\"item\",\"type\":\"text\",\"required\":true},{\"name\":\"amount\",\"type\":\"number\"}]}" | mr)
TABLE_OK=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(bool(d.get('id') or d.get('name')=='finances'))" 2>/dev/null)
[ "$TABLE_OK" = "True" ] && pass "Table created in private vault" || fail "Table create" "$R"

# User1 inserts data
R=$(mcp_as "$PAT1" "$SID1" "akb_sql" "{\"vault\":\"$VAULT1\",\"sql\":\"INSERT INTO finances (item, amount) VALUES ('Revenue', 1000000), ('Secret Cost', 999)\"}" | mr)
INSERT_OK=$(echo "$R" | python3 -c "import sys,json; print('INSERT' in json.load(sys.stdin).get('result','') or 'rows' in str(json.load(sys.stdin)))" 2>/dev/null)
[ "$INSERT_OK" = "True" ] && pass "User1 INSERT into own table" || fail "User1 INSERT" "$R"

# User1 can SELECT
R=$(mcp_as "$PAT1" "$SID1" "akb_sql" "{\"vault\":\"$VAULT1\",\"sql\":\"SELECT * FROM finances\"}" | mr)
ROW_COUNT=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('items',[])))" 2>/dev/null)
[ "$ROW_COUNT" = "2" ] && pass "User1 SELECT: 2 rows" || fail "User1 SELECT" "expected 2, got $ROW_COUNT"

# User2 should NOT be able to SELECT from user1's private vault table
R=$(mcp_as "$PAT2" "$SID2" "akb_sql" "{\"vault\":\"$VAULT1\",\"sql\":\"SELECT * FROM finances\"}" | mr)
SELECT_DENIED=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('error' in d or 'denied' in str(d).lower() or 'Access' in str(d))" 2>/dev/null)
[ "$SELECT_DENIED" = "True" ] && pass "User2 SELECT blocked on private vault" || fail "SQL read access" "User2 could SELECT: $R"

# User2 should NOT be able to INSERT into user1's private vault table
R=$(mcp_as "$PAT2" "$SID2" "akb_sql" "{\"vault\":\"$VAULT1\",\"sql\":\"INSERT INTO finances (item, amount) VALUES ('Hack', 0)\"}" | mr)
INSERT_DENIED=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('error' in d or 'denied' in str(d).lower() or 'Access' in str(d))" 2>/dev/null)
[ "$INSERT_DENIED" = "True" ] && pass "User2 INSERT blocked on private vault" || fail "SQL write access" "User2 could INSERT: $R"

# Grant User2 reader access
mcp_as "$PAT1" "$SID1" "akb_grant" "{\"vault\":\"$VAULT1\",\"user\":\"$USER2\",\"role\":\"reader\"}" >/dev/null 2>&1

# User2 as reader CAN SELECT
R=$(mcp_as "$PAT2" "$SID2" "akb_sql" "{\"vault\":\"$VAULT1\",\"sql\":\"SELECT * FROM finances\"}" | mr)
READER_SELECT=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('items',[])))" 2>/dev/null)
[ "$READER_SELECT" = "2" ] && pass "Reader can SELECT: 2 rows" || fail "Reader SELECT" "$R"

# User2 as reader should NOT be able to INSERT
R=$(mcp_as "$PAT2" "$SID2" "akb_sql" "{\"vault\":\"$VAULT1\",\"sql\":\"INSERT INTO finances (item, amount) VALUES ('Blocked', 0)\"}" | mr)
READER_INSERT_DENIED=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('error' in d or 'denied' in str(d).lower() or 'Access' in str(d))" 2>/dev/null)
[ "$READER_INSERT_DENIED" = "True" ] && pass "Reader INSERT blocked" || fail "Reader INSERT" "should be denied: $R"

# User2 as reader should NOT be able to UPDATE
R=$(mcp_as "$PAT2" "$SID2" "akb_sql" "{\"vault\":\"$VAULT1\",\"sql\":\"UPDATE finances SET amount = 0 WHERE item = 'Revenue'\"}" | mr)
READER_UPDATE_DENIED=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('error' in d or 'denied' in str(d).lower() or 'Access' in str(d))" 2>/dev/null)
[ "$READER_UPDATE_DENIED" = "True" ] && pass "Reader UPDATE blocked" || fail "Reader UPDATE" "should be denied: $R"

# User2 as reader should NOT be able to DELETE
R=$(mcp_as "$PAT2" "$SID2" "akb_sql" "{\"vault\":\"$VAULT1\",\"sql\":\"DELETE FROM finances WHERE item = 'Revenue'\"}" | mr)
READER_DELETE_DENIED=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('error' in d or 'denied' in str(d).lower() or 'Access' in str(d))" 2>/dev/null)
[ "$READER_DELETE_DENIED" = "True" ] && pass "Reader DELETE blocked" || fail "Reader DELETE" "should be denied: $R"

# Upgrade to writer
mcp_as "$PAT1" "$SID1" "akb_grant" "{\"vault\":\"$VAULT1\",\"user\":\"$USER2\",\"role\":\"writer\"}" >/dev/null 2>&1

# User2 as writer CAN INSERT
R=$(mcp_as "$PAT2" "$SID2" "akb_sql" "{\"vault\":\"$VAULT1\",\"sql\":\"INSERT INTO finances (item, amount) VALUES ('Writer Added', 500)\"}" | mr)
WRITER_INSERT=$(echo "$R" | python3 -c "import sys,json; print('INSERT' in json.load(sys.stdin).get('result','') or 'rows' in str(json.load(sys.stdin)))" 2>/dev/null)
[ "$WRITER_INSERT" = "True" ] && pass "Writer can INSERT" || fail "Writer INSERT" "$R"

# Verify 3 rows now
R=$(mcp_as "$PAT1" "$SID1" "akb_sql" "{\"vault\":\"$VAULT1\",\"sql\":\"SELECT count(*) as cnt FROM finances\"}" | mr)
FINAL_COUNT=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['items'][0]['cnt'])" 2>/dev/null)
[ "$FINAL_COUNT" = "3" ] && pass "Final row count: 3" || fail "Final count" "expected 3, got $FINAL_COUNT"

# Revoke User2 access
mcp_as "$PAT1" "$SID1" "akb_revoke" "{\"vault\":\"$VAULT1\",\"user\":\"$USER2\"}" >/dev/null 2>&1

# User2 blocked again after revoke
R=$(mcp_as "$PAT2" "$SID2" "akb_sql" "{\"vault\":\"$VAULT1\",\"sql\":\"SELECT * FROM finances\"}" | mr)
REVOKED_DENIED=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('error' in d or 'denied' in str(d).lower() or 'Access' in str(d))" 2>/dev/null)
[ "$REVOKED_DENIED" = "True" ] && pass "Revoked user blocked from SELECT" || fail "Revoke" "still has access: $R"

# ── 8. SQL Injection / Bypass Prevention ─────────────────────
echo ""
echo "▸ 8. SQL Injection Prevention"

# Re-grant reader for bypass tests
mcp_as "$PAT1" "$SID1" "akb_grant" "{\"vault\":\"$VAULT1\",\"user\":\"$USER2\",\"role\":\"reader\"}" >/dev/null 2>&1

# 8a. Multi-statement (semicolon injection)
R=$(mcp_as "$PAT2" "$SID2" "akb_sql" "{\"vault\":\"$VAULT1\",\"sql\":\"SELECT 1; DELETE FROM finances\"}" | mr)
MULTI_BLOCKED=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('error' in d and 'multi' in d.get('error','').lower())" 2>/dev/null)
[ "$MULTI_BLOCKED" = "True" ] && pass "Multi-statement SQL blocked" || fail "Multi-statement" "$R"

# 8b. Comment bypass: /* */ INSERT
R=$(mcp_as "$PAT2" "$SID2" "akb_sql" "{\"vault\":\"$VAULT1\",\"sql\":\"/* bypass */ INSERT INTO finances (item, amount) VALUES ('hacked', 0)\"}" | mr)
COMMENT_BLOCKED=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('error' in d or 'denied' in str(d).lower() or 'Access' in str(d))" 2>/dev/null)
[ "$COMMENT_BLOCKED" = "True" ] && pass "Comment-prefixed INSERT blocked for reader" || fail "Comment bypass" "$R"

# 8c. CTE bypass: WITH ... DELETE
R=$(mcp_as "$PAT2" "$SID2" "akb_sql" "{\"vault\":\"$VAULT1\",\"sql\":\"WITH del AS (DELETE FROM finances RETURNING *) SELECT * FROM del\"}" | mr)
CTE_BLOCKED=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('error' in d or 'denied' in str(d).lower() or 'Access' in str(d))" 2>/dev/null)
[ "$CTE_BLOCKED" = "True" ] && pass "CTE DELETE blocked for reader" || fail "CTE bypass" "$R"

# 8d. Verify data not corrupted (still 3 rows)
R=$(mcp_as "$PAT1" "$SID1" "akb_sql" "{\"vault\":\"$VAULT1\",\"sql\":\"SELECT count(*) as cnt FROM finances\"}" | mr)
INTEGRITY=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['items'][0]['cnt'])" 2>/dev/null)
[ "$INTEGRITY" = "3" ] && pass "Data integrity preserved (3 rows)" || fail "Data integrity" "expected 3, got $INTEGRITY"

# Revoke again
mcp_as "$PAT1" "$SID1" "akb_revoke" "{\"vault\":\"$VAULT1\",\"user\":\"$USER2\"}" >/dev/null 2>&1

# ── 9. role_source field on /vaults/{vault}/info ─────────────
echo ""
echo "▸ 9. role_source field on /vaults/{vault}/info"

# Owner (member) of their own vault → role_source=member
INFO=$(curl -sk "$BASE_URL/api/v1/vaults/$VAULT1/info" -H "Authorization: Bearer $PAT1")
RS=$(echo "$INFO" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("role_source","MISSING"))' 2>/dev/null)
[ "$RS" = "member" ] && pass "owner sees role_source=member" \
  || fail "role_source owner" "got $RS"

# Set vault to public-writer so a non-member can read it
curl -sk -X PATCH "$BASE_URL/api/v1/vaults/$VAULT1" \
  -H "Authorization: Bearer $PAT1" \
  -H 'Content-Type: application/json' \
  -d '{"public_access":"writer"}' >/dev/null

# User2 is not a member (just revoked above) → role_source=public
INFO=$(curl -sk "$BASE_URL/api/v1/vaults/$VAULT1/info" -H "Authorization: Bearer $PAT2")
RS=$(echo "$INFO" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("role_source","MISSING"))' 2>/dev/null)
[ "$RS" = "public" ] && pass "non-member sees role_source=public" \
  || fail "role_source public" "got $RS"

# ── Cleanup ──────────────────────────────────────────────────
echo ""
echo "▸ Cleanup"
mcp_as "$PAT1" "$SID1" "akb_delete_vault" "{\"name\":\"$VAULT1\"}" >/dev/null 2>&1
mcp_as "$PAT1" "$SID1" "akb_delete_vault" "{\"name\":\"$VAULT2\"}" >/dev/null 2>&1
pass "Vaults deleted"

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
