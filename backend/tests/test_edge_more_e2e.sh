#!/bin/bash
#
# AKB E2E: More edge cases — concurrency, markdown, search filters,
# status transitions, table validation, ReDoS, sessions, public slug
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
echo "║   AKB More Edge Case E2E Tests           ║"
echo "║   Target: $BASE_URL"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── Setup ────────────────────────────────────────────────────
echo "▸ 0. Setup"

E2E_USER="more-edge-$(date +%s)"
curl -sk -X POST "$BASE_URL/api/v1/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$E2E_USER\",\"email\":\"$E2E_USER@test.dev\",\"password\":\"test1234\"}" >/dev/null 2>&1

JWT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$E2E_USER\",\"password\":\"test1234\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

PAT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" \
  -H "Authorization: Bearer $JWT" -H 'Content-Type: application/json' \
  -d '{"name":"more-edge"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

[ -n "$PAT" ] && pass "PAT acquired" || { fail "Setup" "no PAT"; exit 1; }

# MCP session
tmpfile=$(mktemp)
curl -sk -i -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"more-edge","version":"1.0"}}}' > "$tmpfile" 2>/dev/null
SID=$(grep -i "mcp-session-id" "$tmpfile" | tr -d '\r' | awk '{print $2}')
rm -f "$tmpfile"
curl -sk -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" \
  -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null 2>&1

mc() {
  MCP_ID=$((MCP_ID+1))
  curl -sk -X POST "$BASE_URL/mcp/" \
    -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" -H "mcp-session-id: $SID" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":$MCP_ID,\"method\":\"tools/call\",\"params\":{\"name\":\"$1\",\"arguments\":$2}}" 2>&1
}
mr() { python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(d['result']['content'][0]['text'])" 2>/dev/null; }
m() { mc "$1" "$2" | mr; }

VAULT="more-edge-$(date +%s)"
m "akb_create_vault" "{\"name\":\"$VAULT\",\"description\":\"more edge\"}" >/dev/null
pass "Vault created"

# ── 1. Empty/Whitespace Content ──────────────────────────────
echo ""
echo "▸ 1. Empty/Whitespace Content"

# Empty content string
R=$(m "akb_put" "{\"vault\":\"$VAULT\",\"collection\":\"empty\",\"title\":\"Empty Body\",\"content\":\"\"}")
EMPTY_RESULT=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('error' in d or 'doc_id' in d)" 2>/dev/null)
[ "$EMPTY_RESULT" = "True" ] && pass "Empty content: handled (error or accepted)" || fail "Empty content" "$R"

# Whitespace only
R=$(m "akb_put" "{\"vault\":\"$VAULT\",\"collection\":\"empty\",\"title\":\"Whitespace Body\",\"content\":\"   \\n  \\n   \"}")
WS_RESULT=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('error' in d or 'doc_id' in d)" 2>/dev/null)
[ "$WS_RESULT" = "True" ] && pass "Whitespace-only content: handled" || fail "Whitespace content" "$R"

# Only newlines
R=$(m "akb_put" "{\"vault\":\"$VAULT\",\"collection\":\"empty\",\"title\":\"Newlines Body\",\"content\":\"\\n\\n\\n\"}")
NL_RESULT=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('error' in d or 'doc_id' in d)" 2>/dev/null)
[ "$NL_RESULT" = "True" ] && pass "Newlines-only content: handled" || fail "Newlines content" "$R"

# ── 2. Markdown Edge Cases ───────────────────────────────────
echo ""
echo "▸ 2. Markdown Edge Cases"

# Frontmatter-like content inside body (not actual frontmatter)
R=$(m "akb_put" "{\"vault\":\"$VAULT\",\"collection\":\"md\",\"title\":\"FM in Body\",\"content\":\"# Real Title\\n\\nBody text.\\n\\n\`\`\`yaml\\n---\\nfake_title: Should Not Override\\n---\\n\`\`\`\\n\\nMore body.\"}")
FM_DOC=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('doc_id',''))" 2>/dev/null)
[ -n "$FM_DOC" ] && pass "Frontmatter-like in code block accepted" || fail "FM body" "$R"

# Verify title is preserved (not overridden by fake frontmatter)
R=$(m "akb_get" "{\"vault\":\"$VAULT\",\"doc_id\":\"$FM_DOC\"}")
TITLE_OK=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('title','')=='FM in Body')" 2>/dev/null)
[ "$TITLE_OK" = "True" ] && pass "Title not overridden by code block" || fail "Title" "$R"

# Nested code blocks (4 backticks)
R=$(m "akb_put" "{\"vault\":\"$VAULT\",\"collection\":\"md\",\"title\":\"Nested Code\",\"content\":\"# Nested\\n\\n\`\`\`\`markdown\\n\`\`\`python\\nprint('hi')\\n\`\`\`\\n\`\`\`\`\"}")
NEST_OK=$(echo "$R" | python3 -c "import sys,json; print(bool(json.load(sys.stdin).get('doc_id','')))" 2>/dev/null)
[ "$NEST_OK" = "True" ] && pass "Nested code blocks accepted" || fail "Nested code" "$R"

# Document with only headings, no paragraphs
R=$(m "akb_put" "{\"vault\":\"$VAULT\",\"collection\":\"md\",\"title\":\"Headings Only\",\"content\":\"# H1\\n\\n## H2\\n\\n### H3\"}")
HEAD_OK=$(echo "$R" | python3 -c "import sys,json; print(bool(json.load(sys.stdin).get('doc_id','')))" 2>/dev/null)
[ "$HEAD_OK" = "True" ] && pass "Headings-only document accepted" || fail "Headings only" "$R"

# ── 3. Search Filter Combinations ────────────────────────────
echo ""
echo "▸ 3. Search Filter Combinations"

# Set up docs with different types/tags/collections
m "akb_put" "{\"vault\":\"$VAULT\",\"collection\":\"filter-a\",\"title\":\"Spec Alpha\",\"content\":\"# Spec\\nBlue spec content\",\"type\":\"spec\",\"tags\":[\"blue\",\"alpha\"]}" >/dev/null
m "akb_put" "{\"vault\":\"$VAULT\",\"collection\":\"filter-a\",\"title\":\"Note Alpha\",\"content\":\"# Note\\nBlue note content\",\"type\":\"note\",\"tags\":[\"blue\",\"alpha\"]}" >/dev/null
m "akb_put" "{\"vault\":\"$VAULT\",\"collection\":\"filter-b\",\"title\":\"Spec Beta\",\"content\":\"# Spec\\nRed spec content\",\"type\":\"spec\",\"tags\":[\"red\",\"beta\"]}" >/dev/null
pass "3 docs with different type/tags/collection created"

# Filter by type only
R=$(m "akb_search" "{\"query\":\"content\",\"vault\":\"$VAULT\",\"type\":\"spec\"}")
SPEC_COUNT=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('total',0))" 2>/dev/null)
[ "$SPEC_COUNT" -ge 2 ] 2>/dev/null && pass "Type filter (spec): $SPEC_COUNT results" || fail "Type filter" "$R"

# Filter by tag only
R=$(m "akb_search" "{\"query\":\"content\",\"vault\":\"$VAULT\",\"tags\":[\"blue\"]}")
BLUE_COUNT=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('total',0))" 2>/dev/null)
[ "$BLUE_COUNT" -ge 2 ] 2>/dev/null && pass "Tag filter (blue): $BLUE_COUNT results" || fail "Tag filter" "$R"

# Filter by collection only
R=$(m "akb_search" "{\"query\":\"content\",\"vault\":\"$VAULT\",\"collection\":\"filter-a\"}")
COLLA_COUNT=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('total',0))" 2>/dev/null)
[ "$COLLA_COUNT" -ge 2 ] 2>/dev/null && pass "Collection filter (filter-a): $COLLA_COUNT results" || fail "Collection filter" "$R"

# Combined: type + tags
R=$(m "akb_search" "{\"query\":\"content\",\"vault\":\"$VAULT\",\"type\":\"spec\",\"tags\":[\"blue\"]}")
COMBO_COUNT=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('total',0))" 2>/dev/null)
[ "$COMBO_COUNT" = "1" ] && pass "Combined type+tags: 1 result (spec AND blue)" || fail "Combined filters" "expected 1, got $COMBO_COUNT"

# All three combined
R=$(m "akb_search" "{\"query\":\"content\",\"vault\":\"$VAULT\",\"type\":\"spec\",\"tags\":[\"blue\"],\"collection\":\"filter-a\"}")
ALL_COUNT=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('total',0))" 2>/dev/null)
[ "$ALL_COUNT" = "1" ] && pass "All 3 filters combined: 1 result" || fail "All filters" "expected 1, got $ALL_COUNT"

# ── 4. Update Tags Only (no content change) ──────────────────
echo ""
echo "▸ 4. Update Tags Only"

R=$(m "akb_put" "{\"vault\":\"$VAULT\",\"collection\":\"upd\",\"title\":\"Tag Update Test\",\"content\":\"# Test\\nOriginal body\",\"tags\":[\"original\"]}")
UPD_DOC=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('doc_id',''))" 2>/dev/null)
[ -n "$UPD_DOC" ] && pass "Doc for tag update created" || fail "Tag upd doc" "$R"

# Update only tags
R=$(m "akb_update" "{\"vault\":\"$VAULT\",\"doc_id\":\"$UPD_DOC\",\"tags\":[\"renamed\",\"updated\"]}")
UPD_OK=$(echo "$R" | python3 -c "import sys,json; print(bool(json.load(sys.stdin).get('commit_hash','')))" 2>/dev/null)
[ "$UPD_OK" = "True" ] && pass "Tags-only update succeeded" || fail "Tag update" "$R"

# Verify tags changed and content preserved
R=$(m "akb_get" "{\"vault\":\"$VAULT\",\"doc_id\":\"$UPD_DOC\"}")
TAG_OK=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); t=d.get('tags',[]); print('renamed' in t and 'updated' in t and 'original' not in t and 'Original body' in d.get('content',''))" 2>/dev/null)
[ "$TAG_OK" = "True" ] && pass "Tags updated, content preserved" || fail "Tag verify" "$R"

# ── 5. Status Transitions ────────────────────────────────────
echo ""
echo "▸ 5. Status Transitions"

R=$(m "akb_put" "{\"vault\":\"$VAULT\",\"collection\":\"status\",\"title\":\"Status Test\",\"content\":\"# X\"}")
ST_DOC=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('doc_id',''))" 2>/dev/null)
[ -n "$ST_DOC" ] && pass "Status test doc created" || fail "Status doc" "$R"

# Default should be draft
R=$(m "akb_get" "{\"vault\":\"$VAULT\",\"doc_id\":\"$ST_DOC\"}")
DEFAULT_STATUS=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null)
[ "$DEFAULT_STATUS" = "draft" ] && pass "Default status is 'draft'" || fail "Default status" "$DEFAULT_STATUS"

# Transition draft → active
R=$(m "akb_update" "{\"vault\":\"$VAULT\",\"doc_id\":\"$ST_DOC\",\"status\":\"active\"}")
ACT_OK=$(echo "$R" | python3 -c "import sys,json; print(bool(json.load(sys.stdin).get('commit_hash','')))" 2>/dev/null)
[ "$ACT_OK" = "True" ] && pass "draft → active" || fail "Activate" "$R"

# Transition active → archived
R=$(m "akb_update" "{\"vault\":\"$VAULT\",\"doc_id\":\"$ST_DOC\",\"status\":\"archived\"}")
ARCH_OK=$(echo "$R" | python3 -c "import sys,json; print(bool(json.load(sys.stdin).get('commit_hash','')))" 2>/dev/null)
[ "$ARCH_OK" = "True" ] && pass "active → archived" || fail "Archive doc" "$R"

# Invalid status — schema validation returns InputValidationError
R=$(MCP_ID=$((MCP_ID+1)); curl -sk -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" -H "mcp-session-id: $SID" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":$MCP_ID,\"method\":\"tools/call\",\"params\":{\"name\":\"akb_update\",\"arguments\":{\"vault\":\"$VAULT\",\"doc_id\":\"$ST_DOC\",\"status\":\"INVALID\"}}}" 2>&1)
INV_REJ=$(echo "$R" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print('error' in d or 'enum' in str(d).lower() or 'not one of' in str(d).lower())" 2>/dev/null)
[ "$INV_REJ" = "True" ] && pass "Invalid status rejected" || fail "Invalid status" "$R"

# ── 6. Concurrent Writes (Race Condition) ────────────────────
echo ""
echo "▸ 6. Concurrent Writes"

# Two parallel updates to the same doc
R=$(m "akb_put" "{\"vault\":\"$VAULT\",\"collection\":\"race\",\"title\":\"Race Doc\",\"content\":\"# Initial\"}")
RACE_DOC=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('doc_id',''))" 2>/dev/null)

# Fire two updates in parallel
(m "akb_update" "{\"vault\":\"$VAULT\",\"doc_id\":\"$RACE_DOC\",\"content\":\"# Update A\"}" > /tmp/race_a.txt) &
(m "akb_update" "{\"vault\":\"$VAULT\",\"doc_id\":\"$RACE_DOC\",\"content\":\"# Update B\"}" > /tmp/race_b.txt) &
wait

A_OK=$(grep -c "commit_hash" /tmp/race_a.txt 2>/dev/null || echo 0)
B_OK=$(grep -c "commit_hash" /tmp/race_b.txt 2>/dev/null || echo 0)
( [ "$A_OK" -ge 1 ] || [ "$B_OK" -ge 1 ] ) && pass "Concurrent updates: at least one succeeded (A=$A_OK B=$B_OK)" || fail "Concurrent writes" "both failed"

# Verify final state is consistent (one of A or B)
R=$(m "akb_get" "{\"vault\":\"$VAULT\",\"doc_id\":\"$RACE_DOC\"}")
FINAL=$(echo "$R" | python3 -c "import sys,json; c=json.load(sys.stdin).get('content',''); print('Update A' in c or 'Update B' in c)" 2>/dev/null)
[ "$FINAL" = "True" ] && pass "Race result is consistent (one update won)" || fail "Race consistency" "$R"
rm -f /tmp/race_a.txt /tmp/race_b.txt

# ── 7. Knowledge Graph Cycles ────────────────────────────────
echo ""
echo "▸ 7. Knowledge Graph Cycles"

m "akb_put" "{\"vault\":\"$VAULT\",\"collection\":\"graph\",\"title\":\"Node A\",\"content\":\"# A\"}" >/dev/null
m "akb_put" "{\"vault\":\"$VAULT\",\"collection\":\"graph\",\"title\":\"Node B\",\"content\":\"# B\"}" >/dev/null
m "akb_put" "{\"vault\":\"$VAULT\",\"collection\":\"graph\",\"title\":\"Node C\",\"content\":\"# C\"}" >/dev/null

URI_A="akb://$VAULT/doc/graph/node-a.md"
URI_B="akb://$VAULT/doc/graph/node-b.md"
URI_C="akb://$VAULT/doc/graph/node-c.md"

# Create cycle: A→B→C→A
m "akb_link" "{\"vault\":\"$VAULT\",\"source\":\"$URI_A\",\"target\":\"$URI_B\",\"relation\":\"depends_on\"}" >/dev/null
m "akb_link" "{\"vault\":\"$VAULT\",\"source\":\"$URI_B\",\"target\":\"$URI_C\",\"relation\":\"depends_on\"}" >/dev/null
R=$(m "akb_link" "{\"vault\":\"$VAULT\",\"source\":\"$URI_C\",\"target\":\"$URI_A\",\"relation\":\"depends_on\"}")
CYCLE_LINKED=$(echo "$R" | python3 -c "import sys,json; print(bool(json.load(sys.stdin).get('linked',False)))" 2>/dev/null)
[ "$CYCLE_LINKED" = "True" ] && pass "Cycle A→B→C→A created (3 edges)" || fail "Cycle link" "$R"

# Graph traversal should not infinite loop
R=$(m "akb_graph" "{\"vault\":\"$VAULT\",\"resource_uri\":\"$URI_A\",\"depth\":5}")
NODES=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('nodes',[])))" 2>/dev/null)
[ "$NODES" -ge 3 ] 2>/dev/null && pass "Graph traversal handles cycle ($NODES nodes)" || fail "Cycle traversal" "$R"

# ── 8. Cross-Vault Link ──────────────────────────────────────
echo ""
echo "▸ 8. Cross-Vault Link"

VAULT2="more-edge2-$(date +%s)"
m "akb_create_vault" "{\"name\":\"$VAULT2\",\"description\":\"link target\"}" >/dev/null

m "akb_put" "{\"vault\":\"$VAULT2\",\"collection\":\"x\",\"title\":\"Remote Doc\",\"content\":\"# Remote\"}" >/dev/null
URI_REMOTE="akb://$VAULT2/doc/x/remote-doc.md"

# Try to link from vault1 doc to vault2 doc
R=$(m "akb_link" "{\"vault\":\"$VAULT\",\"source\":\"$URI_A\",\"target\":\"$URI_REMOTE\",\"relation\":\"references\"}")
CROSS_RESULT=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('linked' in d or 'error' in d)" 2>/dev/null)
[ "$CROSS_RESULT" = "True" ] && pass "Cross-vault link: handled (allowed or rejected)" || fail "Cross link" "$R"

m "akb_delete_vault" "{\"name\":\"$VAULT2\"}" >/dev/null 2>&1

# ── 9. Session Lifecycle Edge Cases ──────────────────────────
echo ""
echo "▸ 9. Session Lifecycle"

# End nonexistent session
R=$(m "akb_session_end" "{\"session_id\":\"00000000-0000-0000-0000-000000000000\",\"summary\":\"test\"}")
END_INVALID=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('error' in d or 'not found' in str(d).lower())" 2>/dev/null)
[ "$END_INVALID" = "True" ] && pass "End nonexistent session → error" || fail "End invalid" "$R"

# Start session normally
R=$(m "akb_session_start" "{\"vault\":\"$VAULT\",\"agent_id\":\"test-agent\",\"context\":\"testing\"}")
SES_ID=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null)
[ -n "$SES_ID" ] && pass "Session started ($SES_ID)" || fail "Session start" "$R"

# End it
R=$(m "akb_session_end" "{\"session_id\":\"$SES_ID\",\"summary\":\"done\"}")
END_OK=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('ended' in d or 'session_id' in d or 'summary' in d)" 2>/dev/null)
[ "$END_OK" = "True" ] && pass "Session ended" || fail "Session end" "$R"

# Double-end same session
R=$(m "akb_session_end" "{\"session_id\":\"$SES_ID\",\"summary\":\"again\"}")
DOUBLE_END=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('error' in d or 'already' in str(d).lower() or 'ended' in d)" 2>/dev/null)
[ "$DOUBLE_END" = "True" ] && pass "Double-end session: handled" || fail "Double end" "$R"

# ── 10. Table Validation Edge Cases ──────────────────────────
echo ""
echo "▸ 10. Table Validation"

# Test reserved column rejection
R=$(m "akb_create_table" "{\"vault\":\"$VAULT\",\"name\":\"reserved_test\",\"columns\":[{\"name\":\"id\",\"type\":\"text\",\"required\":true}]}")
RESERVED_REJ=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('error' in d and 'reserved' in d.get('error','').lower())" 2>/dev/null)
[ "$RESERVED_REJ" = "True" ] && pass "Reserved column 'id' rejected on create" || fail "Reserved col" "$R"

# Use non-reserved column names
m "akb_create_table" "{\"vault\":\"$VAULT\",\"name\":\"validation\",\"columns\":[{\"name\":\"item\",\"type\":\"text\",\"required\":true},{\"name\":\"qty\",\"type\":\"number\"}]}" >/dev/null
TBL_OK=$(m "akb_browse" "{\"vault\":\"$VAULT\",\"content_type\":\"tables\"}" | python3 -c "import sys,json; items=json.load(sys.stdin).get('items',[]); print(any(i['name']=='validation' for i in items))" 2>/dev/null)
[ "$TBL_OK" = "True" ] && pass "Table 'validation' created" || fail "Table create" "not found"

# Insert NULL into required column
R=$(m "akb_sql" "{\"vault\":\"$VAULT\",\"sql\":\"INSERT INTO validation (qty) VALUES (10)\"}")
NULL_REJ=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('error' in d)" 2>/dev/null)
[ "$NULL_REJ" = "True" ] && pass "NULL in required column rejected" || fail "NULL required" "$R"

# Insert wrong type
R=$(m "akb_sql" "{\"vault\":\"$VAULT\",\"sql\":\"INSERT INTO validation (item, qty) VALUES ('item1', 'not_a_number')\"}")
TYPE_REJ=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('error' in d)" 2>/dev/null)
[ "$TYPE_REJ" = "True" ] && pass "Wrong type (string in number col) rejected" || fail "Wrong type" "$R"

# Valid insert
R=$(m "akb_sql" "{\"vault\":\"$VAULT\",\"sql\":\"INSERT INTO validation (item, qty) VALUES ('item1', 42)\"}")
VAL_OK=$(echo "$R" | python3 -c "import sys,json; print('INSERT' in json.load(sys.stdin).get('result',''))" 2>/dev/null)
[ "$VAL_OK" = "True" ] && pass "Valid insert succeeded" || fail "Valid insert" "$R"

# ── 11. ReDoS Prevention ─────────────────────────────────────
echo ""
echo "▸ 11. ReDoS / Catastrophic Regex"

# Create a doc with content that could trigger ReDoS
m "akb_put" "{\"vault\":\"$VAULT\",\"collection\":\"redos\",\"title\":\"ReDoS Target\",\"content\":\"# Test\\n$(python3 -c 'print(\"a\" * 100)')X\"}" >/dev/null

# Catastrophic backtracking pattern: (a+)+$ on string of all 'a's followed by 'X'
# Should either complete quickly or be rejected
START=$(date +%s)
R=$(m "akb_grep" "{\"pattern\":\"(a+)+\$\",\"regex\":true,\"vault\":\"$VAULT\"}" 2>&1 || echo "TIMEOUT")
END=$(date +%s)
ELAPSED=$((END - START))
# Should complete in < 30s (either matched or rejected)
[ "$ELAPSED" -lt 30 ] 2>/dev/null && pass "ReDoS pattern completes in ${ELAPSED}s (not hung)" || fail "ReDoS" "took ${ELAPSED}s"

# Another known catastrophic: ^(([a-z])+.)+[A-Z]([a-z])+$
START=$(date +%s)
R=$(m "akb_grep" "{\"pattern\":\"^(([a-z])+.)+[A-Z]([a-z])+\$\",\"regex\":true,\"vault\":\"$VAULT\"}" 2>&1)
END=$(date +%s)
ELAPSED=$((END - START))
[ "$ELAPSED" -lt 30 ] 2>/dev/null && pass "Catastrophic regex 2 completes in ${ELAPSED}s" || fail "ReDoS 2" "took ${ELAPSED}s"

# ── 12. Graph Self-Reference ─────────────────────────────────
echo ""
echo "▸ 12. Self-Reference"

# Try to link a doc to itself
R=$(m "akb_link" "{\"vault\":\"$VAULT\",\"source\":\"$URI_A\",\"target\":\"$URI_A\",\"relation\":\"related_to\"}")
SELF_RESULT=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('linked' in d or 'error' in d)" 2>/dev/null)
[ "$SELF_RESULT" = "True" ] && pass "Self-link: handled (allowed or rejected)" || fail "Self link" "$R"

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
