#!/bin/bash
#
# AKB MCP: akb_put `slug` param E2E Test
# Tests that an explicit slug controls the document path/uri filename
# (independent of the title), and that omitting it falls back to the title.
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
echo "║   akb_put slug param E2E                 ║"
echo "║   Target: $BASE_URL"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── 0. Setup ─────────────────────────────────────────────────
echo "▸ 0. Setup"

E2E_USER="put-slug-$(date +%s)"
curl -sk -X POST "$BASE_URL/api/v1/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$E2E_USER\",\"email\":\"$E2E_USER@test.dev\",\"password\":\"test1234\"}" >/dev/null 2>&1

PAT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$E2E_USER\",\"password\":\"test1234\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

PAT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d '{"name":"put-slug-e2e"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

[ -n "$PAT" ] && pass "PAT acquired" || { fail "PAT" "could not get PAT"; exit 1; }

# ── Start proxy ──────────────────────────────────────────────
AKB_MCP_BIN="$(dirname "$0")/../../packages/akb-mcp-client/bin/akb-mcp.mjs"
if [ ! -f "$AKB_MCP_BIN" ]; then
  AKB_MCP_BIN="$(which akb-mcp 2>/dev/null || echo "")"
fi
[ -z "$AKB_MCP_BIN" ] && { echo "ERROR: akb-mcp not found"; exit 1; }

FIFO_IN=$(mktemp -u /tmp/akb-mcp-slug-in.XXXXXX)
FIFO_OUT=$(mktemp -u /tmp/akb-mcp-slug-out.XXXXXX)
mkfifo "$FIFO_IN" "$FIFO_OUT"

NODE_TLS_REJECT_UNAUTHORIZED=0 node "$AKB_MCP_BIN" \
  --url "$BASE_URL/mcp/" --pat "$PAT" --insecure \
  < "$FIFO_IN" > "$FIFO_OUT" 2>/dev/null &
MCP_PID=$!

exec 3>"$FIFO_IN" 4<"$FIFO_OUT"

cleanup() {
  exec 3>&- 4<&- 2>/dev/null
  kill $MCP_PID 2>/dev/null; wait $MCP_PID 2>/dev/null
  rm -f "$FIFO_IN" "$FIFO_OUT"
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
INIT=$(rpc_call "initialize" '{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"put-slug-e2e","version":"1.0"}}')
echo "$INIT" | python3 -c 'import sys,json; json.load(sys.stdin)["result"]["protocolVersion"]' >/dev/null 2>&1 && pass "Initialized" || fail "Init" "failed"
echo '{"jsonrpc":"2.0","method":"notifications/initialized"}' >&3

# ── 2. Verify slug param in akb_put schema ───────────────────
echo ""
echo "▸ 2. Verify slug param in akb_put schema"
TOOLS=$(rpc_call "tools/list" '{}')
PUT_HAS_SLUG=$(echo "$TOOLS" | python3 -c '
import sys, json
d = json.load(sys.stdin)
for t in d["result"]["tools"]:
    if t["name"] == "akb_put":
        print("slug" in t["inputSchema"]["properties"])
        break
' 2>/dev/null)
[ "$PUT_HAS_SLUG" = "True" ] && pass "akb_put advertises slug param" || fail "akb_put schema" "slug param missing"

# ── 3. Create vault ──────────────────────────────────────────
echo ""
echo "▸ 3. Create vault"
VAULT="put-slug-e2e-$(date +%s)"
VAULT_RESP=$(rpc_call "tools/call" "{\"name\":\"akb_create_vault\",\"arguments\":{\"name\":\"$VAULT\",\"description\":\"put slug param e2e\"}}")
VAULT_ID=$(tool_result "$VAULT_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("vault_id",""))' 2>/dev/null)
[ -n "$VAULT_ID" ] && pass "Created vault $VAULT" || fail "Create vault" "no vault_id"

# ── 4. Explicit slug controls the path filename ──────────────
echo ""
echo "▸ 4. Explicit slug controls path (independent of title)"
# Title slugifies to 'slug-path-probe-title'; the explicit slug must win.
PUT_RESP=$(rpc_call "tools/call" "{\"name\":\"akb_put\",\"arguments\":{\"vault\":\"$VAULT\",\"collection\":\"probe\",\"title\":\"Slug Path Probe Title\",\"slug\":\"fixed-slug-xyz\",\"content\":\"# body\\n\\nslug probe\"}}")
DOC_URI=$(tool_result "$PUT_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("uri",""))' 2>/dev/null)
DOC_PATH=$(tool_result "$PUT_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("path",""))' 2>/dev/null)
echo "$DOC_URI" | grep -q "/fixed-slug-xyz\.md$" \
  && pass "slug controls filename: uri=$DOC_URI" \
  || fail "slug path" "expected uri ending /fixed-slug-xyz.md, got '$DOC_URI' (path='$DOC_PATH')"
# Negative: the title slug must NOT be the path.
echo "$DOC_URI" | grep -q "slug-path-probe-title" \
  && fail "slug overrides title" "path fell back to the title slug: $DOC_URI" \
  || pass "title slug not used when slug given"

# ── 5. Title is preserved (slug only affects the path) ───────
echo ""
echo "▸ 5. Title independent of slug"
GET_RESP=$(rpc_call "tools/call" "{\"name\":\"akb_get\",\"arguments\":{\"uri\":\"$DOC_URI\"}}")
GOT_TITLE=$(tool_result "$GET_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("title",""))' 2>/dev/null)
[ "$GOT_TITLE" = "Slug Path Probe Title" ] \
  && pass "title preserved: '$GOT_TITLE'" \
  || fail "title" "expected 'Slug Path Probe Title', got '$GOT_TITLE'"

# ── 6. Omitting slug falls back to the title (control) ───────
echo ""
echo "▸ 6. Default — no slug derives the path from the title"
PUT_RESP2=$(rpc_call "tools/call" "{\"name\":\"akb_put\",\"arguments\":{\"vault\":\"$VAULT\",\"collection\":\"probe\",\"title\":\"Title Derived Path\",\"content\":\"# body\\n\\nno slug\"}}")
DOC_URI2=$(tool_result "$PUT_RESP2" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("uri",""))' 2>/dev/null)
echo "$DOC_URI2" | grep -q "/title-derived-path\.md$" \
  && pass "default path from title: uri=$DOC_URI2" \
  || fail "default path" "expected uri ending /title-derived-path.md, got '$DOC_URI2'"

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
