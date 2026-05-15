#!/bin/bash
#
# AKB akb_edit E2E Test Suite
# Tests exact-text replacement editing via MCP Streamable HTTP transport
#
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
VAULT="edit-e2e-$(date +%s)"
E2E_USER="edit-user-$(date +%s)"
READER_USER="edit-reader-$(date +%s)"
PASS=0
FAIL=0
ERRORS=()

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

echo "╔══════════════════════════════════════════╗"
echo "║   AKB akb_edit E2E Test Suite            ║"
echo "║   Target: $BASE_URL/mcp/"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── 0. Setup: register users + get PATs + MCP sessions ────────
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
  -d '{"name":"edit-e2e"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

[ -n "$PAT" ] && pass "PAT acquired" || { fail "PAT" "could not get PAT"; exit 1; }

# Register a reader-only user for access-control test
curl -sk -X POST "$BASE_URL/api/v1/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$READER_USER\",\"email\":\"$READER_USER@test.dev\",\"password\":\"test1234\"}" >/dev/null 2>&1

READER_JWT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$READER_USER\",\"password\":\"test1234\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

READER_PAT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" \
  -H "Authorization: Bearer $READER_JWT" \
  -H 'Content-Type: application/json' \
  -d '{"name":"edit-reader"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

# MCP Initialize (writer)
INIT_RESP=$(curl -sk -i -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"edit-e2e","version":"1.0"}}}' 2>&1)

SID=$(echo "$INIT_RESP" | grep -i "mcp-session-id" | tr -d '\r' | awk '{print $2}')
[ -n "$SID" ] && pass "MCP session ($SID)" || { fail "MCP" "no session"; exit 1; }

curl -sk -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null 2>&1

# MCP Initialize (reader)
READER_INIT_RESP=$(curl -sk -i -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $READER_PAT" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"edit-e2e-reader","version":"1.0"}}}' 2>&1)

READER_SID=$(echo "$READER_INIT_RESP" | grep -i "mcp-session-id" | tr -d '\r' | awk '{print $2}')

curl -sk -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $READER_PAT" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: $READER_SID" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null 2>&1

# MCP helpers
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
mcp_call_as_reader() {
  local tool=$1 args=$2
  MCP_ID=$((MCP_ID+1))
  curl -sk -X POST "$BASE_URL/mcp/" \
    -H "Authorization: Bearer $READER_PAT" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -H "mcp-session-id: $READER_SID" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":$MCP_ID,\"method\":\"tools/call\",\"params\":{\"name\":\"$tool\",\"arguments\":$args}}" 2>&1
}
mcp_result() {
  python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(d['result']['content'][0]['text'])" 2>/dev/null
}

# Create vault
R=$(mcp_call akb_create_vault "{\"name\":\"$VAULT\",\"description\":\"Edit E2E test\"}" | mcp_result)
VAULT_ID=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['vault_id'])" 2>/dev/null)
[ -n "$VAULT_ID" ] && pass "Vault created ($VAULT)" || { fail "Vault" "no vault_id"; exit 1; }

# Grant reader access to the other user
R=$(mcp_call akb_grant "{\"vault\":\"$VAULT\",\"user\":\"$READER_USER\",\"role\":\"reader\"}" | mcp_result)
GRANT_OK=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('granted',False) or 'role' in d)" 2>/dev/null)
[ "$GRANT_OK" = "True" ] && pass "Reader access granted" || fail "Grant reader" "$R"

# ── 1. akb_edit in tools/list ──────────────────────────────────
echo ""
echo "▸ 1. Tool Discovery"

TOOLS_RESP=$(curl -sk -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' 2>&1)

HAS_EDIT=$(echo "$TOOLS_RESP" | python3 -c "import sys,json; tools=json.load(sys.stdin)['result']['tools']; print(any(t['name']=='akb_edit' for t in tools))" 2>/dev/null)
[ "$HAS_EDIT" = "True" ] && pass "akb_edit in tools/list" || fail "akb_edit discovery" "tool not found"

NO_PATCH=$(echo "$TOOLS_RESP" | python3 -c "import sys,json; tools=json.load(sys.stdin)['result']['tools']; print(not any(t['name']=='akb_patch' for t in tools))" 2>/dev/null)
[ "$NO_PATCH" = "True" ] && pass "akb_patch is removed" || fail "akb_patch still present" ""

EDIT_SCHEMA=$(echo "$TOOLS_RESP" | python3 -c "
import sys,json
tools=json.load(sys.stdin)['result']['tools']
t=[t for t in tools if t['name']=='akb_edit'][0]
schema=t['inputSchema']
required=schema.get('required',[])
props=sorted(schema['properties'].keys())
print(f'required={required} props={props}')
" 2>/dev/null)
echo "    Schema: $EDIT_SCHEMA"
HAS_REQUIRED=$(echo "$EDIT_SCHEMA" | grep -c "old_string" || true)
[ "$HAS_REQUIRED" -ge 1 ] && pass "akb_edit schema valid" || fail "akb_edit schema" "missing old_string"

# ── 2. Basic Edit: Single Replacement ────────────────────────
echo ""
echo "▸ 2. Basic Edit — Single Replacement"

R=$(mcp_call akb_put "{\"vault\":\"$VAULT\",\"collection\":\"edit-tests\",\"title\":\"Edit Target\",\"content\":\"# Introduction\\n\\nThis is the original introduction paragraph.\\n\\n## Section A\\n\\nContent of section A is here.\\n\\n## Section B\\n\\nContent of section B is here.\\n\\n## Conclusion\\n\\nOriginal conclusion.\",\"type\":\"spec\",\"tags\":[\"edit\",\"test\"]}" | mcp_result)
DOC_URI=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['uri'])" 2>/dev/null)
[ -n "$DOC_URI" ] && pass "Created target doc ($DOC_URI)" || { fail "Create doc" "no uri"; exit 1; }

# Simple exact-text replacement
R=$(mcp_call akb_edit "{\"uri\":\"$DOC_URI\",\"old_string\":\"Content of section A is here.\",\"new_string\":\"Content of section A has been updated.\",\"message\":\"Update section A\"}" | mcp_result)
COMMIT=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('commit_hash',''))" 2>/dev/null)
CHUNKS=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('chunks_indexed',0))" 2>/dev/null)
[ -n "$COMMIT" ] && pass "Basic edit applied (commit: ${COMMIT:0:8}, chunks: $CHUNKS)" || fail "Basic edit" "$R"

# Verify content changed
R=$(mcp_call akb_get "{\"uri\":\"$DOC_URI\"}" | mcp_result)
HAS_UPDATE=$(echo "$R" | python3 -c "import sys,json; c=json.load(sys.stdin)['content']; print('has been updated' in c and 'is here.' not in c.split('Section A')[1].split('Section B')[0])" 2>/dev/null)
[ "$HAS_UPDATE" = "True" ] && pass "Edit persisted in content" || fail "Edit content check" "$R"

# chunks_indexed > 0
[ "$CHUNKS" -gt 0 ] 2>/dev/null && pass "Re-indexed ($CHUNKS chunks)" || fail "Re-index" "chunks=$CHUNKS"

# ── 3. Not-Found Error ──────────────────────────────────────────
echo ""
echo "▸ 3. Not-Found Error"

R=$(mcp_call akb_edit "{\"uri\":\"$DOC_URI\",\"old_string\":\"this string definitely does not exist in document xyz\",\"new_string\":\"replacement\"}" | mcp_result)
NOT_FOUND_ERR=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('error','')=='edit_failed' and 'not found' in d.get('message','').lower())" 2>/dev/null)
[ "$NOT_FOUND_ERR" = "True" ] && pass "Not-found old_string returns edit_failed" || fail "Not found" "$R"

HAS_HINT=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('hint' in d)" 2>/dev/null)
[ "$HAS_HINT" = "True" ] && pass "Error includes hint" || fail "Hint" "$R"

# ── 4. Not-Unique Error ─────────────────────────────────────────
echo ""
echo "▸ 4. Not-Unique Error"

# Create doc with duplicate content
R=$(mcp_call akb_put "{\"vault\":\"$VAULT\",\"collection\":\"edit-tests\",\"title\":\"Duplicate Content\",\"content\":\"# Doc\\n\\nDUPLICATE LINE\\nother stuff\\nDUPLICATE LINE\\nmore stuff\\nDUPLICATE LINE\"}" | mcp_result)
DUP_DOC_URI=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['uri'])" 2>/dev/null)
[ -n "$DUP_DOC_URI" ] && pass "Duplicate doc created" || fail "Dup doc" "$R"

R=$(mcp_call akb_edit "{\"uri\":\"$DUP_DOC_URI\",\"old_string\":\"DUPLICATE LINE\",\"new_string\":\"X\"}" | mcp_result)
NOT_UNIQUE_ERR=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); m=d.get('message',''); print(d.get('error','')=='edit_failed' and 'appears 3 times' in m)" 2>/dev/null)
[ "$NOT_UNIQUE_ERR" = "True" ] && pass "Non-unique old_string rejected (3 occurrences)" || fail "Not unique" "$R"

# ── 5. replace_all ──────────────────────────────────────────────
echo ""
echo "▸ 5. replace_all"

R=$(mcp_call akb_edit "{\"uri\":\"$DUP_DOC_URI\",\"old_string\":\"DUPLICATE LINE\",\"new_string\":\"REPLACED\",\"replace_all\":true}" | mcp_result)
REPL_COMMIT=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('commit_hash',''))" 2>/dev/null)
[ -n "$REPL_COMMIT" ] && pass "replace_all succeeded (commit: ${REPL_COMMIT:0:8})" || fail "replace_all" "$R"

R=$(mcp_call akb_get "{\"uri\":\"$DUP_DOC_URI\"}" | mcp_result)
ALL_REPLACED=$(echo "$R" | python3 -c "import sys,json; c=json.load(sys.stdin)['content']; print(c.count('REPLACED')==3 and 'DUPLICATE LINE' not in c)" 2>/dev/null)
[ "$ALL_REPLACED" = "True" ] && pass "All 3 occurrences replaced" || fail "replace_all count" "$R"

# ── 6. Empty old_string ─────────────────────────────────────────
echo ""
echo "▸ 6. Empty old_string"

R=$(mcp_call akb_edit "{\"uri\":\"$DOC_URI\",\"old_string\":\"\",\"new_string\":\"anything\"}" | mcp_result)
EMPTY_ERR=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('error','')=='edit_failed' and 'empty' in d.get('message','').lower())" 2>/dev/null)
[ "$EMPTY_ERR" = "True" ] && pass "Empty old_string rejected" || fail "Empty old_string" "$R"

# ── 7. No-op edit (old == new) ──────────────────────────────────
echo ""
echo "▸ 7. No-op Edit"

R=$(mcp_call akb_edit "{\"uri\":\"$DOC_URI\",\"old_string\":\"Original conclusion.\",\"new_string\":\"Original conclusion.\"}" | mcp_result)
NOOP_CHUNKS=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('chunks_indexed','-1'))" 2>/dev/null)
[ "$NOOP_CHUNKS" = "0" ] && pass "No-op edit skipped re-indexing (0 chunks)" || fail "No-op edit" "chunks=$NOOP_CHUNKS, response=$R"

# ── 8. Empty new_string (deletion) ──────────────────────────────
echo ""
echo "▸ 8. Empty new_string (Deletion)"

R=$(mcp_call akb_put "{\"vault\":\"$VAULT\",\"collection\":\"edit-tests\",\"title\":\"Deletion Test\",\"content\":\"# Start\\n\\nkeep this\\nDELETE_ME_LINE\\nkeep that\"}" | mcp_result)
DEL_DOC_URI=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['uri'])" 2>/dev/null)
[ -n "$DEL_DOC_URI" ] && pass "Deletion target doc created" || fail "Del doc" "$R"

R=$(mcp_call akb_edit "{\"uri\":\"$DEL_DOC_URI\",\"old_string\":\"DELETE_ME_LINE\\n\",\"new_string\":\"\"}" | mcp_result)
DEL_COMMIT=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('commit_hash',''))" 2>/dev/null)
[ -n "$DEL_COMMIT" ] && pass "Deletion edit succeeded" || fail "Delete edit" "$R"

R=$(mcp_call akb_get "{\"uri\":\"$DEL_DOC_URI\"}" | mcp_result)
GONE=$(echo "$R" | python3 -c "import sys,json; c=json.load(sys.stdin)['content']; print('DELETE_ME_LINE' not in c and 'keep this' in c and 'keep that' in c)" 2>/dev/null)
[ "$GONE" = "True" ] && pass "Deleted line removed, surrounding content intact" || fail "Delete verify" "$R"

# ── 9. Multi-line old_string ────────────────────────────────────
echo ""
echo "▸ 9. Multi-line Edit"

R=$(mcp_call akb_put "{\"vault\":\"$VAULT\",\"collection\":\"edit-tests\",\"title\":\"Multiline Test\",\"content\":\"# Doc\\n\\n## Old Section\\n\\nLine one\\nLine two\\nLine three\\n\\n## Keep\\n\\nkeep me\"}" | mcp_result)
ML_DOC_URI=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['uri'])" 2>/dev/null)
[ -n "$ML_DOC_URI" ] && pass "Multiline doc created" || fail "ML doc" "$R"

R=$(mcp_call akb_edit "{\"uri\":\"$ML_DOC_URI\",\"old_string\":\"## Old Section\\n\\nLine one\\nLine two\\nLine three\",\"new_string\":\"## New Section\\n\\nRewritten entirely.\",\"message\":\"Rewrite section\"}" | mcp_result)
ML_COMMIT=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('commit_hash',''))" 2>/dev/null)
[ -n "$ML_COMMIT" ] && pass "Multi-line edit applied" || fail "Multi-line edit" "$R"

R=$(mcp_call akb_get "{\"uri\":\"$ML_DOC_URI\"}" | mcp_result)
ML_OK=$(echo "$R" | python3 -c "import sys,json; c=json.load(sys.stdin)['content']; print('New Section' in c and 'Line one' not in c and 'keep me' in c)" 2>/dev/null)
[ "$ML_OK" = "True" ] && pass "Multi-line replacement correct" || fail "Multi-line verify" "$R"

# ── 9b. Regex metacharacters treated literally ──────────────────
echo ""
echo "▸ 9b. Regex Metacharacters Literal"

R=$(mcp_call akb_put "{\"vault\":\"$VAULT\",\"collection\":\"edit-tests\",\"title\":\"Regex Literal Test\",\"content\":\"# Regex Test\\n\\nVersion check: foo.*bar\\nPattern: a[bc]+d\\nDollar: \$HOME and \$USER\\nGroup ref: \\\\1 not a backreference\"}" | mcp_result)
RX_DOC_URI=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['uri'])" 2>/dev/null)
[ -n "$RX_DOC_URI" ] && pass "Regex-literal doc created" || fail "Regex literal doc" "$R"

# Replace literal foo.*bar (not a regex match)
R=$(mcp_call akb_edit "{\"uri\":\"$RX_DOC_URI\",\"old_string\":\"foo.*bar\",\"new_string\":\"REPLACED_FOO_BAR\"}" | mcp_result)
RX_COMMIT=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('commit_hash',''))" 2>/dev/null)
[ -n "$RX_COMMIT" ] && pass "Regex metachar literal: foo.*bar replaced" || fail "Regex literal edit" "$R"

# Verify it didn't regex-match anything else
R=$(mcp_call akb_get "{\"uri\":\"$RX_DOC_URI\"}" | mcp_result)
RX_OK=$(echo "$R" | python3 -c "import sys,json; c=json.load(sys.stdin)['content']; print('REPLACED_FOO_BAR' in c and 'a[bc]+d' in c and 'foo.*bar' not in c)" 2>/dev/null)
[ "$RX_OK" = "True" ] && pass "Other metachar lines untouched" || fail "Regex literal verify" "$R"

# Try replacing literal char class — must not regex-match
R=$(mcp_call akb_edit "{\"uri\":\"$RX_DOC_URI\",\"old_string\":\"a[bc]+d\",\"new_string\":\"CHARCLASS_GONE\"}" | mcp_result)
CC_COMMIT=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('commit_hash',''))" 2>/dev/null)
[ -n "$CC_COMMIT" ] && pass "Char class literal a[bc]+d replaced" || fail "Char class edit" "$R"

# ── 10. Frontmatter Not Affected ────────────────────────────────
echo ""
echo "▸ 10. Frontmatter Safety"

# Try editing something that exists only in frontmatter → should not match
R=$(mcp_call akb_edit "{\"uri\":\"$DOC_URI\",\"old_string\":\"Edit Target\",\"new_string\":\"Hacked Title\"}" | mcp_result)
FM_SAFE=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('error','')=='edit_failed')" 2>/dev/null)
[ "$FM_SAFE" = "True" ] && pass "Frontmatter-only text not editable (body-only scope)" || fail "Frontmatter safety" "$R"

R=$(mcp_call akb_get "{\"uri\":\"$DOC_URI\"}" | mcp_result)
TITLE_OK=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('title','')=='Edit Target')" 2>/dev/null)
[ "$TITLE_OK" = "True" ] && pass "Title unchanged after failed frontmatter edit" || fail "Title check" "$R"

# ── 11. Access Control: Reader Cannot Edit ──────────────────────
echo ""
echo "▸ 11. Access Control"

if [ -n "$READER_SID" ]; then
  R=$(mcp_call_as_reader akb_edit "{\"uri\":\"$DOC_URI\",\"old_string\":\"Content of section B is here.\",\"new_string\":\"reader attempted edit\"}" | mcp_result)
  DENIED=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); err=str(d.get('error',''))+str(d.get('message','')); print('403' in err or 'forbidden' in err.lower() or 'permission' in err.lower() or 'writer' in err.lower() or 'role' in err.lower())" 2>/dev/null)
  [ "$DENIED" = "True" ] && pass "Reader cannot edit (access denied)" || fail "Reader edit" "$R"
else
  fail "Reader MCP session" "no READER_SID"
fi

# ── 12. History Reflects Edit Commits ───────────────────────────
echo ""
echo "▸ 12. Git History"

R=$(mcp_call akb_history "{\"uri\":\"$DOC_URI\"}" | mcp_result)
HAS_EDIT_MSG=$(echo "$R" | python3 -c "import sys,json; h=json.load(sys.stdin).get('history',[]); print(any('edit' in str(e).lower() for e in h))" 2>/dev/null)
[ "$HAS_EDIT_MSG" = "True" ] && pass "History contains edit commit" || fail "History" "$R"

# ── 13. Nonexistent Document ────────────────────────────────────
echo ""
echo "▸ 13. Nonexistent Document"

R=$(mcp_call akb_edit "{\"uri\":\"akb://$VAULT/doc/nonexistent/missing.md\",\"old_string\":\"a\",\"new_string\":\"b\"}" | mcp_result)
NF=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('error' in d or 'not found' in str(d).lower())" 2>/dev/null)
[ "$NF" = "True" ] && pass "Nonexistent doc returns error" || fail "Nonexistent" "$R"

# ── 14. Help Entry ──────────────────────────────────────────────
echo ""
echo "▸ 14. Help System"

R=$(mcp_call akb_help '{"topic":"akb_edit"}' | mcp_result)
HELP_OK=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); h=d.get('help',''); print('akb_edit' in h and 'old_string' in h)" 2>/dev/null)
[ "$HELP_OK" = "True" ] && pass "akb_help(topic=akb_edit) works" || fail "Help entry" "$R"

R=$(mcp_call akb_help '{"topic":"documents"}' | mcp_result)
DOCS_HELP=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); h=d.get('help',''); print('akb_edit' in h and 'akb_patch' not in h)" 2>/dev/null)
[ "$DOCS_HELP" = "True" ] && pass "akb_edit listed in documents help (akb_patch removed)" || fail "Documents help" "$R"

# ── 15. Cleanup ─────────────────────────────────────────────────
echo ""
echo "▸ 15. Cleanup"

R=$(mcp_call akb_delete_vault "{\"vault\":\"$VAULT\"}" | mcp_result)
DEL_OK=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('deleted',False))" 2>/dev/null)
[ "$DEL_OK" = "True" ] && pass "Vault deleted" || fail "Cleanup" "vault not deleted"

# ── Results ─────────────────────────────────────────────────────
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
