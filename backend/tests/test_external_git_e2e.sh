#!/bin/bash
#
# AKB External-Git Mirror E2E Test Suite
# Tests the full read-only mirror flow via MCP.
#
# Uses github.com/octocat/Spoon-Knife (stable, public, ~4 files with 1 README.md)
# as the upstream under test — small enough that clone + reconcile
# completes well inside the 120s poll window.
#
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
TS=$(date +%s)
VAULT="egit-e2e-$TS"
E2E_USER="egit-user-$TS"
MIRROR_URL="https://github.com/octocat/Spoon-Knife"
MIRROR_BRANCH="main"
PASS=0
FAIL=0
ERRORS=()

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

echo "╔══════════════════════════════════════════╗"
echo "║   AKB External-Git Mirror E2E            ║"
echo "║   Target: $BASE_URL/mcp/"
echo "║   Mirror: $MIRROR_URL"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── 0. Setup: register user + get PAT ───────────────────────
echo "▸ 0. Setup"

curl -sk -X POST "$BASE_URL/api/v1/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$E2E_USER\",\"email\":\"$E2E_USER@test.dev\",\"password\":\"test1234\"}" >/dev/null 2>&1

JWT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$E2E_USER\",\"password\":\"test1234\"}" | \
  python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

PAT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" \
  -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' \
  -d '{"name":"egit-e2e"}' | \
  python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

[ -n "$PAT" ] && pass "PAT acquired" || { fail "PAT" "could not get PAT"; exit 1; }

# ── 1. MCP session ──────────────────────────────────────────
INIT_RESP=$(curl -sk -i -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"egit-e2e","version":"1.0"}}}' 2>&1)
SID=$(echo "$INIT_RESP" | grep -i "mcp-session-id" | tr -d '\r' | awk '{print $2}')
[ -n "$SID" ] && pass "MCP session ($SID)" || { fail "MCP session" "no session id"; exit 1; }

curl -sk -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null 2>&1

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
mcp_result() {
  python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(d['result']['content'][0]['text'])" 2>/dev/null
}
mcp_result_field() {
  local field=$1
  python3 -c "import sys,json; d=json.loads(json.loads(sys.stdin.read())['result']['content'][0]['text']); print(d$field)" 2>/dev/null
}

# ── 2. Create mirror vault — must return immediately ────────
echo ""
echo "▸ 2. Create mirror vault"

START=$(date +%s)
R=$(mcp_call akb_create_vault \
  "{\"name\":\"$VAULT\",\"description\":\"E2E mirror test\",\"external_git\":{\"url\":\"$MIRROR_URL\",\"branch\":\"$MIRROR_BRANCH\"}}")
ELAPSED=$(( $(date +%s) - START ))
VAULT_ID=$(echo "$R" | mcp_result_field "['vault_id']")
MIRROR_FLAG=$(echo "$R" | mcp_result_field "['external_git']['read_only']")
[ -n "$VAULT_ID" ] && pass "akb_create_vault returned vault_id" || fail "create_vault" "no vault_id"
[ "$MIRROR_FLAG" = "True" ] && pass "external_git.read_only=True in response" || fail "mirror flag" "got '$MIRROR_FLAG'"
[ "$ELAPSED" -le 10 ] && pass "create returned fast (${ELAPSED}s ≤ 10s — clone deferred)" || fail "create speed" "took ${ELAPSED}s"

# ── 3. Write guard — every mutation must 403 ────────────────
echo ""
echo "▸ 3. Write guard (must reject with 'read-only external git mirror')"

R=$(mcp_call akb_put \
  "{\"vault\":\"$VAULT\",\"collection\":\"x\",\"title\":\"should fail\",\"content\":\"no\"}")
ERR=$(echo "$R" | mcp_result_field "['error']")
echo "$ERR" | grep -q "read-only" && pass "akb_put rejected" || fail "akb_put guard" "got '$ERR'"

# akb_update needs a doc_id; the guard triggers before find_by_ref resolves,
# so any made-up id is fine for asserting the block.
R=$(mcp_call akb_update \
  "{\"vault\":\"$VAULT\",\"doc_id\":\"d-deadbeef\",\"content\":\"edit\"}")
ERR=$(echo "$R" | mcp_result_field "['error']")
echo "$ERR" | grep -q "read-only" && pass "akb_update rejected" || fail "akb_update guard" "got '$ERR'"

R=$(mcp_call akb_delete "{\"vault\":\"$VAULT\",\"doc_id\":\"d-deadbeef\"}")
ERR=$(echo "$R" | mcp_result_field "['error']")
echo "$ERR" | grep -q "read-only" && pass "akb_delete rejected" || fail "akb_delete guard" "got '$ERR'"

# ── 4. Wait for poller to bootstrap + reconcile ─────────────
echo ""
echo "▸ 4. Poll until reconcile completes (up to 120s)"

SYNCED=0
for i in $(seq 1 60); do
  R=$(mcp_call akb_vault_info "{\"vault\":\"$VAULT\"}")
  DOC_COUNT=$(echo "$R" | mcp_result_field "['document_count']")
  if [ -n "$DOC_COUNT" ] && [ "$DOC_COUNT" -ge 1 ] 2>/dev/null; then
    SYNCED=1
    break
  fi
  sleep 2
done
[ "$SYNCED" = "1" ] && pass "Reconcile populated documents (count=$DOC_COUNT)" || fail "reconcile" "no docs after 120s"

# Bail out of read-path tests if the reconcile never completed — rest
# of the suite depends on documents existing.
if [ "$SYNCED" != "1" ]; then
  echo ""
  echo "FAIL: giving up early; review poller logs"
  exit 1
fi

# ── 5. Read path: browse / get / history / diff ─────────────
echo ""
echo "▸ 5. Read path"

# Top-level browse returns collections + files. Spoon-Knife's README.md
# lives at repo root (NULL collection_id by design — parent path is ""),
# so the top-level listing is expected to be empty items with a "Vault
# is empty" hint. Just assert the call returned a well-formed response.
R=$(mcp_call akb_browse "{\"vault\":\"$VAULT\"}")
VAULT_ECHO=$(echo "$R" | mcp_result_field "['vault']")
[ "$VAULT_ECHO" = "$VAULT" ] && pass "akb_browse responds (root empty is expected for root-only repos)" || fail "akb_browse" "got '$VAULT_ECHO'"

# Spoon-Knife's README.md is the canonical indexed file.
R=$(mcp_call akb_get "{\"vault\":\"$VAULT\",\"doc_id\":\"README.md\"}")
TITLE=$(echo "$R" | mcp_result_field "['title']")
[ -n "$TITLE" ] && pass "akb_get README.md (title='$TITLE')" || fail "akb_get" "no title"

R=$(mcp_call akb_history "{\"vault\":\"$VAULT\",\"doc_id\":\"README.md\",\"limit\":5}")
HIST_COUNT=$(echo "$R" | python3 -c "import sys,json; d=json.loads(json.loads(sys.stdin.read())['result']['content'][0]['text']); print(len(d.get('history',[])))" 2>/dev/null)
[ -n "$HIST_COUNT" ] && [ "$HIST_COUNT" -ge 1 ] 2>/dev/null && pass "akb_history returns $HIST_COUNT commit(s)" || fail "akb_history" "no commits"

# Grab the first commit and diff it.
FIRST_COMMIT=$(echo "$R" | python3 -c "import sys,json; d=json.loads(json.loads(sys.stdin.read())['result']['content'][0]['text']); print(d['history'][0]['hash'])" 2>/dev/null)
if [ -n "$FIRST_COMMIT" ]; then
  R=$(mcp_call akb_diff "{\"vault\":\"$VAULT\",\"doc_id\":\"README.md\",\"commit\":\"$FIRST_COMMIT\"}")
  DIFF=$(echo "$R" | mcp_result_field "['diff']")
  [ -n "$DIFF" ] && pass "akb_diff returns patch for $FIRST_COMMIT" || fail "akb_diff" "empty diff"
fi

# ── 6. Health surface — new workers visible ─────────────────
echo ""
echo "▸ 6. /health surfaces external_git + metadata workers"

HEALTH=$(curl -sk "$BASE_URL/health" 2>/dev/null)
EG=$(echo "$HEALTH" | python3 -c "import sys,json; d=json.load(sys.stdin); print('ok' if 'external_git' in d else 'missing')" 2>/dev/null)
MD=$(echo "$HEALTH" | python3 -c "import sys,json; d=json.load(sys.stdin); print('ok' if 'metadata_backfill' in d else 'missing')" 2>/dev/null)
[ "$EG" = "ok" ] && pass "/health: external_git key" || fail "/health external_git" "missing"
[ "$MD" = "ok" ] && pass "/health: metadata_backfill key" || fail "/health metadata_backfill" "missing"

# ── 7. Cleanup ──────────────────────────────────────────────
echo ""
echo "▸ 7. Cleanup"

R=$(mcp_call akb_delete_vault "{\"vault\":\"$VAULT\"}")
DELETED=$(echo "$R" | mcp_result_field "['deleted']")
[ "$DELETED" = "True" ] && pass "akb_delete_vault ($VAULT)" || fail "delete_vault" "got '$DELETED'"

# ── Summary ─────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Results: $PASS passed, $FAIL failed"
if [ "$FAIL" -gt 0 ]; then
  echo ""
  echo "Failures:"
  for e in "${ERRORS[@]}"; do echo "  - $e"; done
  exit 1
fi
echo "All external-git mirror checks passed."
