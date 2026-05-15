#!/bin/bash
#
# AKB stdio MCP: akb_put/akb_update `file` param E2E Test
# Tests that local file path can be used instead of content param
#
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
PASS=0
FAIL=0
ERRORS=()
MSGID=0

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

echo "╔══════════════════════════════════════════╗"
echo "║   akb_put/akb_update file param E2E     ║"
echo "║   Target: $BASE_URL"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── 0. Setup ─────────────────────────────────────────────────
echo "▸ 0. Setup"

E2E_USER="put-file-$(date +%s)"
curl -sk -X POST "$BASE_URL/api/v1/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$E2E_USER\",\"email\":\"$E2E_USER@test.dev\",\"password\":\"test1234\"}" >/dev/null 2>&1

PAT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$E2E_USER\",\"password\":\"test1234\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

PAT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d '{"name":"put-file-e2e"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

[ -n "$PAT" ] && pass "PAT acquired" || { fail "PAT" "could not get PAT"; exit 1; }

# ── Start proxy ──────────────────────────────────────────────
AKB_MCP_BIN="$(dirname "$0")/../../packages/akb-mcp-client/bin/akb-mcp.mjs"
if [ ! -f "$AKB_MCP_BIN" ]; then
  AKB_MCP_BIN="$(which akb-mcp 2>/dev/null || echo "")"
fi
[ -z "$AKB_MCP_BIN" ] && { echo "ERROR: akb-mcp not found"; exit 1; }

FIFO_IN=$(mktemp -u /tmp/akb-mcp-in.XXXXXX)
FIFO_OUT=$(mktemp -u /tmp/akb-mcp-out.XXXXXX)
mkfifo "$FIFO_IN" "$FIFO_OUT"

NODE_TLS_REJECT_UNAUTHORIZED=0 node "$AKB_MCP_BIN" \
  --url "$BASE_URL/mcp/" --pat "$PAT" --insecure \
  < "$FIFO_IN" > "$FIFO_OUT" 2>/dev/null &
MCP_PID=$!

exec 3>"$FIFO_IN" 4<"$FIFO_OUT"

cleanup() {
  exec 3>&- 4<&- 2>/dev/null
  kill $MCP_PID 2>/dev/null; wait $MCP_PID 2>/dev/null
  rm -f "$FIFO_IN" "$FIFO_OUT" /tmp/akb-put-file-test*.md
}
trap cleanup EXIT

rpc_call() {
  MSGID=$((MSGID+1))
  local msg="{\"jsonrpc\":\"2.0\",\"id\":$MSGID,\"method\":\"$1\",\"params\":$2}"
  echo "$msg" >&3
  RESPONSE=""
  read -r -t 30 RESPONSE <&4 || true
  echo "$RESPONSE"
}

tool_result() {
  echo "$1" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["result"]["content"][0]["text"])' 2>/dev/null
}

sleep 1

# ── 1. Initialize ────────────────────────────────────────────
echo ""
echo "▸ 1. Initialize"
INIT=$(rpc_call "initialize" '{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"put-file-e2e","version":"1.0"}}')
echo "$INIT" | python3 -c 'import sys,json; json.load(sys.stdin)["result"]["protocolVersion"]' >/dev/null 2>&1 && pass "Initialized" || fail "Init" "failed"
echo '{"jsonrpc":"2.0","method":"notifications/initialized"}' >&3

# ── 2. Verify file param in tool schema ──────────────────────
echo ""
echo "▸ 2. Verify file param in tool schemas"

TOOLS=$(rpc_call "tools/list" '{}')

# Check akb_put has file param
PUT_HAS_FILE=$(echo "$TOOLS" | python3 -c '
import sys, json
d = json.load(sys.stdin)
for t in d["result"]["tools"]:
    if t["name"] == "akb_put":
        print("file" in t["inputSchema"]["properties"])
        break
' 2>/dev/null)
[ "$PUT_HAS_FILE" = "True" ] && pass "akb_put has file param" || fail "akb_put schema" "file param missing"

# Check akb_update has file param
UPDATE_HAS_FILE=$(echo "$TOOLS" | python3 -c '
import sys, json
d = json.load(sys.stdin)
for t in d["result"]["tools"]:
    if t["name"] == "akb_update":
        print("file" in t["inputSchema"]["properties"])
        break
' 2>/dev/null)
[ "$UPDATE_HAS_FILE" = "True" ] && pass "akb_update has file param" || fail "akb_update schema" "file param missing"

# Check akb_search does NOT have file param (control)
SEARCH_HAS_FILE=$(echo "$TOOLS" | python3 -c '
import sys, json
d = json.load(sys.stdin)
for t in d["result"]["tools"]:
    if t["name"] == "akb_search":
        print("file" in t["inputSchema"].get("properties",{}))
        break
' 2>/dev/null)
[ "$SEARCH_HAS_FILE" = "False" ] && pass "akb_search has no file param (control)" || fail "akb_search schema" "unexpected file param"

# ── 3. Create vault ──────────────────────────────────────────
echo ""
echo "▸ 3. Create vault"

VAULT="put-file-e2e-$(date +%s)"
VAULT_RESP=$(rpc_call "tools/call" "{\"name\":\"akb_create_vault\",\"arguments\":{\"name\":\"$VAULT\",\"description\":\"put file param e2e\"}}")
VAULT_ID=$(tool_result "$VAULT_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("vault_id",""))' 2>/dev/null)
[ -n "$VAULT_ID" ] && pass "Created vault $VAULT" || fail "Create vault" "no vault_id"

# ── 4. akb_put with file param ───────────────────────────────
echo ""
echo "▸ 4. akb_put with file param"

# Create local markdown file
TEST_MD="/tmp/akb-put-file-test1.md"
cat > "$TEST_MD" << 'CONTENT'
# Test Document from File

This document was created using the `file` parameter instead of `content`.

## Section 1
- Item A
- Item B

## Section 2
Some detailed content here.
CONTENT

PUT_RESP=$(rpc_call "tools/call" "{\"name\":\"akb_put\",\"arguments\":{\"vault\":\"$VAULT\",\"collection\":\"test-docs\",\"title\":\"File Param Test\",\"file\":\"$TEST_MD\",\"tags\":[\"e2e\",\"file-param\"]}}")
DOC_URI=$(tool_result "$PUT_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("uri",""))' 2>/dev/null)
[ -n "$DOC_URI" ] && pass "akb_put with file: uri=$DOC_URI" || fail "akb_put file" "no uri — $(tool_result "$PUT_RESP")"

# Verify content matches
GET_RESP=$(rpc_call "tools/call" "{\"name\":\"akb_get\",\"arguments\":{\"uri\":\"$DOC_URI\"}}")
GOT_TITLE=$(tool_result "$GET_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("title",""))' 2>/dev/null)
GOT_CONTENT=$(tool_result "$GET_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("content","")[:50])' 2>/dev/null)
[ "$GOT_TITLE" = "File Param Test" ] && pass "Title matches" || fail "Title" "expected 'File Param Test', got '$GOT_TITLE'"
echo "$GOT_CONTENT" | grep -q "Test Document from File" && pass "Content from file verified" || fail "Content" "content mismatch: $GOT_CONTENT"

# ── 5. akb_update with file param ────────────────────────────
echo ""
echo "▸ 5. akb_update with file param"

TEST_MD2="/tmp/akb-put-file-test2.md"
cat > "$TEST_MD2" << 'CONTENT'
# Updated Document from File

Content has been completely replaced via file param on akb_update.

## New Section
- Updated Item X
- Updated Item Y
CONTENT

UPDATE_RESP=$(rpc_call "tools/call" "{\"name\":\"akb_update\",\"arguments\":{\"uri\":\"$DOC_URI\",\"file\":\"$TEST_MD2\",\"message\":\"Update via file param\"}}")
UPDATE_COMMIT=$(tool_result "$UPDATE_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("commit_hash",""))' 2>/dev/null)
[ -n "$UPDATE_COMMIT" ] && pass "akb_update with file: commit=$UPDATE_COMMIT" || fail "akb_update file" "no commit — $(tool_result "$UPDATE_RESP")"

# Verify updated content
GET_RESP2=$(rpc_call "tools/call" "{\"name\":\"akb_get\",\"arguments\":{\"uri\":\"$DOC_URI\"}}")
GOT_CONTENT2=$(tool_result "$GET_RESP2" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("content","")[:60])' 2>/dev/null)
echo "$GOT_CONTENT2" | grep -q "Updated Document from File" && pass "Updated content from file verified" || fail "Update content" "mismatch: $GOT_CONTENT2"

# ── 6. Error: both file and content ──────────────────────────
echo ""
echo "▸ 6. Error cases"

ERR_RESP=$(rpc_call "tools/call" "{\"name\":\"akb_put\",\"arguments\":{\"vault\":\"$VAULT\",\"collection\":\"test-docs\",\"title\":\"Both\",\"content\":\"inline\",\"file\":\"$TEST_MD\"}}")
ERR_MSG=$(tool_result "$ERR_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("error",""))' 2>/dev/null)
echo "$ERR_MSG" | grep -qi "both" && pass "Rejected both file+content" || fail "Both params" "expected error about both, got: $ERR_MSG"

# Error: file not found
ERR_RESP2=$(rpc_call "tools/call" "{\"name\":\"akb_put\",\"arguments\":{\"vault\":\"$VAULT\",\"collection\":\"test-docs\",\"title\":\"Missing\",\"file\":\"/tmp/nonexistent-file-12345.md\"}}")
ERR_MSG2=$(tool_result "$ERR_RESP2" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("error",""))' 2>/dev/null)
echo "$ERR_MSG2" | grep -qi "read\|found\|ENOENT" && pass "Rejected nonexistent file" || fail "Missing file" "expected error, got: $ERR_MSG2"

# ── 7. Large file test ───────────────────────────────────────
echo ""
echo "▸ 7. Large file test"

TEST_LARGE="/tmp/akb-put-file-test-large.md"
python3 -c "
print('# Large Document Test')
print()
for i in range(50):
    print(f'## Section {i+1}')
    print(f'Content for section {i+1}. ' * 20)
    print()
" > "$TEST_LARGE"
LARGE_SIZE=$(wc -c < "$TEST_LARGE" | tr -d ' ')

# Large files need longer timeout for server-side chunking/embedding
LARGE_RESP=$(MSGID=$((MSGID+1)); echo "{\"jsonrpc\":\"2.0\",\"id\":$MSGID,\"method\":\"tools/call\",\"params\":{\"name\":\"akb_put\",\"arguments\":{\"vault\":\"$VAULT\",\"collection\":\"test-docs\",\"title\":\"Large File Test\",\"file\":\"$TEST_LARGE\"}}}" >&3; RESPONSE=""; read -r -t 90 RESPONSE <&4 || true; echo "$RESPONSE")
LARGE_DOC_URI=$(tool_result "$LARGE_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("uri",""))' 2>/dev/null)
LARGE_CHUNKS=$(tool_result "$LARGE_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("chunks_indexed",0))' 2>/dev/null)
[ -n "$LARGE_DOC_URI" ] && pass "Large file ($LARGE_SIZE bytes): uri=$LARGE_DOC_URI, chunks=$LARGE_CHUNKS" || fail "Large file" "$(tool_result "$LARGE_RESP")"

# ── Cleanup vault ─────────────────────────────────────────────
echo ""
echo "▸ Cleanup"
rpc_call "tools/call" "{\"name\":\"akb_delete_vault\",\"arguments\":{\"name\":\"$VAULT\"}}" >/dev/null 2>&1
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
