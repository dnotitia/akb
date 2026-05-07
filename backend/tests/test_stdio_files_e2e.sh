#!/bin/bash
#
# AKB stdio MCP File Tools E2E Test
# Tests file upload/download/list/delete via akb-mcp stdio proxy
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
echo "║   AKB stdio File Tools E2E Test          ║"
echo "║   Target: $BASE_URL"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── 0. Setup: register user + get PAT ───────────────────────
echo "▸ 0. Setup"

E2E_USER="stdio-file-$(date +%s)"
curl -sk -X POST "$BASE_URL/api/v1/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$E2E_USER\",\"email\":\"$E2E_USER@test.dev\",\"password\":\"test1234\"}" >/dev/null 2>&1

PAT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$E2E_USER\",\"password\":\"test1234\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

PAT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d '{"name":"stdio-e2e"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

[ -n "$PAT" ] && pass "PAT acquired" || { fail "PAT" "could not get PAT"; exit 1; }

# ── Start akb-mcp stdio proxy as a coprocess ─────────────────
AKB_MCP_BIN="$(dirname "$0")/../../packages/akb-mcp-client/bin/akb-mcp.mjs"
if [ ! -f "$AKB_MCP_BIN" ]; then
  AKB_MCP_BIN="$(which akb-mcp 2>/dev/null || echo "")"
fi
if [ -z "$AKB_MCP_BIN" ]; then
  echo "ERROR: akb-mcp binary not found"
  exit 1
fi

# Create fifos for communication
FIFO_IN=$(mktemp -u /tmp/akb-mcp-in.XXXXXX)
FIFO_OUT=$(mktemp -u /tmp/akb-mcp-out.XXXXXX)
mkfifo "$FIFO_IN" "$FIFO_OUT"

NODE_TLS_REJECT_UNAUTHORIZED=0 node "$AKB_MCP_BIN" \
  --url "$BASE_URL/mcp/" --pat "$PAT" --insecure \
  < "$FIFO_IN" > "$FIFO_OUT" 2>/dev/null &
MCP_PID=$!

# Open file descriptors
exec 3>"$FIFO_IN" 4<"$FIFO_OUT"

cleanup() {
  exec 3>&- 4<&- 2>/dev/null
  kill $MCP_PID 2>/dev/null; wait $MCP_PID 2>/dev/null
  rm -f "$FIFO_IN" "$FIFO_OUT"
}
trap cleanup EXIT

# Helper: send JSON-RPC and read response
rpc() {
  MSGID=$((MSGID+1))
  echo "$1" >&3
  read -r -t 15 RESPONSE <&4
  echo "$RESPONSE"
}

rpc_call() {
  local method="$1"
  local params="$2"
  MSGID=$((MSGID+1))
  local msg="{\"jsonrpc\":\"2.0\",\"id\":$MSGID,\"method\":\"$method\",\"params\":$params}"
  echo "$msg" >&3
  read -r -t 15 RESPONSE <&4
  echo "$RESPONSE"
}

# Extract tool result text
tool_result() {
  echo "$1" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["result"]["content"][0]["text"])' 2>/dev/null
}

sleep 1

# ── 1. Initialize ────────────────────────────────────────────
echo ""
echo "▸ 1. MCP Initialize"

INIT=$(rpc_call "initialize" '{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"stdio-e2e","version":"1.0"}}')
echo "$INIT" | python3 -c 'import sys,json; d=json.load(sys.stdin); print("  Protocol:", d["result"].get("protocolVersion","?"))' 2>/dev/null && pass "Initialized" || fail "Init" "failed"

# Send notifications/initialized
echo '{"jsonrpc":"2.0","method":"notifications/initialized"}' >&3

# ── 2. Verify file tools in tools/list ───────────────────────
echo ""
echo "▸ 2. File tools in tools/list"

TOOLS=$(rpc_call "tools/list" '{}')
FILE_TOOL_COUNT=$(echo "$TOOLS" | python3 -c '
import sys,json
d = json.load(sys.stdin)
tools = d["result"]["tools"]
file_tools = [t["name"] for t in tools if t["name"] in ("akb_put_file","akb_get_file","akb_delete_file")]
print(len(file_tools))
' 2>/dev/null)

[ "$FILE_TOOL_COUNT" = "3" ] && pass "3 file tools found (list_files replaced by browse)" || fail "File tools" "expected 3, got $FILE_TOOL_COUNT"

TOTAL_TOOLS=$(echo "$TOOLS" | python3 -c 'import sys,json; print(len(json.load(sys.stdin)["result"]["tools"]))' 2>/dev/null)
[ "$TOTAL_TOOLS" -ge 40 ] 2>/dev/null && pass "Total tools: $TOTAL_TOOLS" || fail "Total tools" "expected >=40, got $TOTAL_TOOLS"

# ── 3. Create vault for file tests ──────────────────────────
echo ""
echo "▸ 3. Create vault"

VAULT="stdio-file-e2e-$(date +%s)"
VAULT_RESP=$(rpc_call "tools/call" "{\"name\":\"akb_create_vault\",\"arguments\":{\"name\":\"$VAULT\",\"description\":\"stdio file e2e test\"}}")
VAULT_ID=$(tool_result "$VAULT_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("vault_id",""))' 2>/dev/null)
[ -n "$VAULT_ID" ] && pass "Created vault $VAULT" || fail "Create vault" "no vault_id"

# ── 4. Upload file ───────────────────────────────────────────
echo ""
echo "▸ 4. Upload file"

# Create test files
TEST_FILE1="/tmp/akb-stdio-test1.txt"
TEST_FILE2="/tmp/akb-stdio-test2.pdf"
echo "Hello from stdio E2E test — file 1" > "$TEST_FILE1"
dd if=/dev/urandom bs=1024 count=10 of="$TEST_FILE2" 2>/dev/null  # 10KB binary

UPLOAD1=$(rpc_call "tools/call" "{\"name\":\"akb_put_file\",\"arguments\":{\"vault\":\"$VAULT\",\"file_path\":\"$TEST_FILE1\",\"collection\":\"docs\",\"description\":\"Test text file\"}}")
FILE1_ID=$(tool_result "$UPLOAD1" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("id",""))' 2>/dev/null)
[ -n "$FILE1_ID" ] && pass "Uploaded text file ($FILE1_ID)" || fail "Upload text" "no id"

UPLOAD2=$(rpc_call "tools/call" "{\"name\":\"akb_put_file\",\"arguments\":{\"vault\":\"$VAULT\",\"file_path\":\"$TEST_FILE2\",\"collection\":\"data\",\"description\":\"Test binary file\"}}")
FILE2_ID=$(tool_result "$UPLOAD2" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("id",""))' 2>/dev/null)
FILE2_SIZE=$(tool_result "$UPLOAD2" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("size_bytes",0))' 2>/dev/null)
[ -n "$FILE2_ID" ] && pass "Uploaded binary file ($FILE2_SIZE bytes)" || fail "Upload binary" "no id"

# ── 5. Browse files (replaces akb_list_files) ────────────────
echo ""
echo "▸ 5. Browse files"

BROWSE_ALL=$(rpc_call "tools/call" "{\"name\":\"akb_browse\",\"arguments\":{\"vault\":\"$VAULT\",\"content_type\":\"files\"}}")
TOTAL=$(tool_result "$BROWSE_ALL" | python3 -c 'import sys,json; print(len([i for i in json.load(sys.stdin).get("items",[]) if i["type"]=="file"]))' 2>/dev/null)
[ "$TOTAL" = "2" ] && pass "Total files: 2" || fail "Browse all files" "expected 2, got $TOTAL"

BROWSE_DOCS=$(rpc_call "tools/call" "{\"name\":\"akb_browse\",\"arguments\":{\"vault\":\"$VAULT\",\"collection\":\"docs\",\"content_type\":\"files\"}}")
DOCS_COUNT=$(tool_result "$BROWSE_DOCS" | python3 -c 'import sys,json; print(len([i for i in json.load(sys.stdin).get("items",[]) if i["type"]=="file"]))' 2>/dev/null)
[ "$DOCS_COUNT" = "1" ] && pass "Docs collection: 1 file" || fail "Browse docs files" "expected 1, got $DOCS_COUNT"

# ── 6. Download file ─────────────────────────────────────────
echo ""
echo "▸ 6. Download file"

DL_PATH="/tmp/akb-stdio-dl"
mkdir -p "$DL_PATH"
DL1=$(rpc_call "tools/call" "{\"name\":\"akb_get_file\",\"arguments\":{\"vault\":\"$VAULT\",\"file_id\":\"$FILE1_ID\",\"save_to\":\"$DL_PATH\"}}")
DL1_PATH=$(tool_result "$DL1" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("save_to",""))' 2>/dev/null)

if [ -f "$DL1_PATH" ]; then
  diff "$TEST_FILE1" "$DL1_PATH" > /dev/null 2>&1 && pass "Text file content matches" || fail "Download text" "content mismatch"
else
  fail "Download text" "file not saved to $DL1_PATH"
fi

DL2=$(rpc_call "tools/call" "{\"name\":\"akb_get_file\",\"arguments\":{\"vault\":\"$VAULT\",\"file_id\":\"$FILE2_ID\",\"save_to\":\"$DL_PATH\"}}")
DL2_PATH=$(tool_result "$DL2" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("save_to",""))' 2>/dev/null)

if [ -f "$DL2_PATH" ]; then
  diff "$TEST_FILE2" "$DL2_PATH" > /dev/null 2>&1 && pass "Binary file content matches" || fail "Download binary" "content mismatch"
else
  fail "Download binary" "file not saved to $DL2_PATH"
fi

# ── 7. Delete file ───────────────────────────────────────────
echo ""
echo "▸ 7. Delete file"

DEL1=$(rpc_call "tools/call" "{\"name\":\"akb_delete_file\",\"arguments\":{\"vault\":\"$VAULT\",\"file_id\":\"$FILE1_ID\"}}")
DELETED=$(tool_result "$DEL1" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("deleted",False))' 2>/dev/null)
[ "$DELETED" = "True" ] && pass "Deleted text file" || fail "Delete" "expected True, got $DELETED"

BROWSE_AFTER=$(rpc_call "tools/call" "{\"name\":\"akb_browse\",\"arguments\":{\"vault\":\"$VAULT\",\"content_type\":\"files\"}}")
REMAINING=$(tool_result "$BROWSE_AFTER" | python3 -c 'import sys,json; print(len([i for i in json.load(sys.stdin).get("items",[]) if i["type"]=="file"]))' 2>/dev/null)
[ "$REMAINING" = "1" ] && pass "1 file remaining after delete" || fail "Post-delete browse" "expected 1, got $REMAINING"

# ── 8. Error cases ───────────────────────────────────────────
echo ""
echo "▸ 8. Error cases"

ERR_UPLOAD=$(rpc_call "tools/call" "{\"name\":\"akb_put_file\",\"arguments\":{\"vault\":\"$VAULT\",\"file_path\":\"/nonexistent/file.txt\"}}")
ERR_MSG=$(tool_result "$ERR_UPLOAD" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("error",""))' 2>/dev/null)
[ -n "$ERR_MSG" ] && pass "Upload nonexistent file returns error" || fail "Error case" "no error for missing file"

ERR_DL=$(rpc_call "tools/call" "{\"name\":\"akb_get_file\",\"arguments\":{\"vault\":\"$VAULT\",\"file_id\":\"00000000-0000-0000-0000-000000000000\",\"save_to\":\"/tmp\"}}")
ERR_DL_MSG=$(tool_result "$ERR_DL" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("error",""))' 2>/dev/null)
[ -n "$ERR_DL_MSG" ] && pass "Download nonexistent file returns error" || fail "Error case" "no error for missing file"

# ── 9. Vault delete cleans up S3 files ──────────────────────
echo ""
echo "▸ 9. Vault delete cleans S3 files"

# Upload a file, then delete the vault — file should be gone from S3
VAULT2="stdio-file-e2e-cleanup-$(date +%s)"
rpc_call "tools/call" "{\"name\":\"akb_create_vault\",\"arguments\":{\"name\":\"$VAULT2\",\"description\":\"cleanup test\"}}" >/dev/null

CLEANUP_FILE="/tmp/akb-stdio-cleanup.txt"
echo "cleanup test file" > "$CLEANUP_FILE"
UPLOAD_CL=$(rpc_call "tools/call" "{\"name\":\"akb_put_file\",\"arguments\":{\"vault\":\"$VAULT2\",\"file_path\":\"$CLEANUP_FILE\",\"collection\":\"test\"}}")
CL_FILE_ID=$(tool_result "$UPLOAD_CL" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("id",""))' 2>/dev/null)
[ -n "$CL_FILE_ID" ] && pass "Uploaded file to cleanup vault" || fail "Cleanup upload" "no id"

# Delete the vault (should cascade-delete S3 files too)
rpc_call "tools/call" "{\"name\":\"akb_delete_vault\",\"arguments\":{\"vault\":\"$VAULT2\"}}" >/dev/null

# Try to download the deleted file's presigned URL — should fail
ERR_GONE=$(rpc_call "tools/call" "{\"name\":\"akb_get_file\",\"arguments\":{\"vault\":\"$VAULT2\",\"file_id\":\"$CL_FILE_ID\",\"save_to\":\"/tmp\"}}")
ERR_GONE_MSG=$(tool_result "$ERR_GONE" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("error",""))' 2>/dev/null)
[ -n "$ERR_GONE_MSG" ] && pass "Deleted vault's files are inaccessible" || fail "Vault cleanup" "file still accessible after vault delete"
rm -f "$CLEANUP_FILE"

# ── 10. Cleanup ──────────────────────────────────────────────
echo ""
echo "▸ 10. Cleanup"

# Delete remaining file from original vault
rpc_call "tools/call" "{\"name\":\"akb_delete_file\",\"arguments\":{\"vault\":\"$VAULT\",\"file_id\":\"$FILE2_ID\"}}" >/dev/null
# Delete vault
rpc_call "tools/call" "{\"name\":\"akb_delete_vault\",\"arguments\":{\"vault\":\"$VAULT\"}}" >/dev/null
pass "Cleanup done"

rm -f "$TEST_FILE1" "$TEST_FILE2" "$DL_PATH"/*

# ── Results ──────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   Results: $PASS passed, $FAIL failed"
echo "╚══════════════════════════════════════════╝"

if [ $FAIL -gt 0 ]; then
  echo ""
  echo "Failures:"
  for err in "${ERRORS[@]}"; do
    echo "  ✗ $err"
  done
  exit 1
fi
