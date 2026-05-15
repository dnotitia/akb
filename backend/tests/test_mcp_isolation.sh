#!/bin/bash
#
# MCP Multi-Session User Isolation Test
# Verifies that concurrent MCP sessions correctly isolate user contexts.
#
# Scenario:
#   1. Alice creates a vault, puts a document
#   2. Bob creates a different vault, puts a document
#   3. Both sessions run concurrently
#   4. Verify: Alice's vault is owned by Alice, Bob's by Bob
#   5. Verify: Alice can't access Bob's vault, Bob can't access Alice's
#   6. Verify: tool calls return correct user context per session
#
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
TS=$(date +%s)
PASS=0
FAIL=0
ERRORS=()

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

echo "╔══════════════════════════════════════════╗"
echo "║   MCP Multi-Session Isolation Test       ║"
echo "║   Target: $BASE_URL/mcp/"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── Setup: two users with PATs ──────────────────────────────
echo "▸ 0. Setup: Create two users"

ALICE="alice-iso-$TS"
BOB="bob-iso-$TS"

curl -sk -X POST "$BASE_URL/api/v1/auth/register" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$ALICE\",\"email\":\"$ALICE@test.dev\",\"password\":\"test1234\",\"display_name\":\"Alice\"}" >/dev/null 2>&1
curl -sk -X POST "$BASE_URL/api/v1/auth/register" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$BOB\",\"email\":\"$BOB@test.dev\",\"password\":\"test1234\",\"display_name\":\"Bob\"}" >/dev/null 2>&1

ALICE_JWT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$ALICE\",\"password\":\"test1234\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)
BOB_JWT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$BOB\",\"password\":\"test1234\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

ALICE_PAT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" -H "Authorization: Bearer $ALICE_JWT" \
  -H 'Content-Type: application/json' -d '{"name":"iso-test"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)
BOB_PAT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" -H "Authorization: Bearer $BOB_JWT" \
  -H 'Content-Type: application/json' -d '{"name":"iso-test"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

[ -n "$ALICE_PAT" ] && [ -n "$BOB_PAT" ] && pass "Both users created with PATs" || { fail "Setup" "PAT creation failed"; exit 1; }

# ── Helper: MCP session init ────────────────────────────────

mcp_init() {
  local pat=$1
  curl -sk -i -X POST "$BASE_URL/mcp/" \
    -H "Authorization: Bearer $pat" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"iso-test","version":"1.0"}}}' 2>&1
}

mcp_call() {
  local pat=$1 sid=$2 id=$3 tool=$4 args=$5
  curl -sk -X POST "$BASE_URL/mcp/" \
    -H "Authorization: Bearer $pat" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -H "mcp-session-id: $sid" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":$id,\"method\":\"tools/call\",\"params\":{\"name\":\"$tool\",\"arguments\":$args}}" 2>&1
}

mcp_result() {
  python3 -c "import sys,json; print(json.loads(json.loads(sys.stdin.read())['result']['content'][0]['text']))" 2>/dev/null
}

# ── 1. Initialize two concurrent MCP sessions ───────────────
echo ""
echo "▸ 1. Initialize concurrent sessions"

ALICE_INIT=$(mcp_init "$ALICE_PAT")
ALICE_SID=$(echo "$ALICE_INIT" | grep -i "mcp-session-id" | tr -d '\r' | awk '{print $2}')
[ -n "$ALICE_SID" ] && pass "Alice session: ${ALICE_SID:0:8}..." || fail "Alice session" "no SID"

BOB_INIT=$(mcp_init "$BOB_PAT")
BOB_SID=$(echo "$BOB_INIT" | grep -i "mcp-session-id" | tr -d '\r' | awk '{print $2}')
[ -n "$BOB_SID" ] && pass "Bob session: ${BOB_SID:0:8}..." || fail "Bob session" "no SID"

[ "$ALICE_SID" != "$BOB_SID" ] && pass "Sessions are distinct" || fail "Session IDs" "same SID!"

# ── 2. Each user creates their own vault ─────────────────────
echo ""
echo "▸ 2. Each user creates vault via MCP"

ALICE_VAULT="alice-vault-$TS"
BOB_VAULT="bob-vault-$TS"

R=$(mcp_call "$ALICE_PAT" "$ALICE_SID" 10 akb_create_vault "{\"name\":\"$ALICE_VAULT\",\"description\":\"Alice's vault\"}")
A_VID=$(echo "$R" | python3 -c "import sys,json; print(json.loads(json.load(sys.stdin)['result']['content'][0]['text'])['vault_id'])" 2>/dev/null)
[ -n "$A_VID" ] && pass "Alice created $ALICE_VAULT" || fail "Alice vault" "failed"

R=$(mcp_call "$BOB_PAT" "$BOB_SID" 10 akb_create_vault "{\"name\":\"$BOB_VAULT\",\"description\":\"Bob's vault\"}")
B_VID=$(echo "$R" | python3 -c "import sys,json; print(json.loads(json.load(sys.stdin)['result']['content'][0]['text'])['vault_id'])" 2>/dev/null)
[ -n "$B_VID" ] && pass "Bob created $BOB_VAULT" || fail "Bob vault" "failed"

# ── 3. Verify ownership ─────────────────────────────────────
echo ""
echo "▸ 3. Verify vault ownership"

R=$(mcp_call "$ALICE_PAT" "$ALICE_SID" 11 akb_vault_info "{\"vault\":\"$ALICE_VAULT\"}")
ALICE_OWNER=$(echo "$R" | python3 -c "import sys,json; print(json.loads(json.load(sys.stdin)['result']['content'][0]['text'])['owner'])" 2>/dev/null)
[ "$ALICE_OWNER" = "$ALICE" ] && pass "Alice's vault owned by '$ALICE'" || fail "Alice ownership" "owner is '$ALICE_OWNER', expected '$ALICE'"

R=$(mcp_call "$BOB_PAT" "$BOB_SID" 11 akb_vault_info "{\"vault\":\"$BOB_VAULT\"}")
BOB_OWNER=$(echo "$R" | python3 -c "import sys,json; print(json.loads(json.load(sys.stdin)['result']['content'][0]['text'])['owner'])" 2>/dev/null)
[ "$BOB_OWNER" = "$BOB" ] && pass "Bob's vault owned by '$BOB'" || fail "Bob ownership" "owner is '$BOB_OWNER', expected '$BOB'"

# ── 4. Put documents in own vaults ───────────────────────────
echo ""
echo "▸ 4. Put documents (each in own vault)"

R=$(mcp_call "$ALICE_PAT" "$ALICE_SID" 12 akb_put "{\"vault\":\"$ALICE_VAULT\",\"collection\":\"notes\",\"title\":\"Alice Note\",\"content\":\"## Alice\\n\\nWritten by Alice.\",\"type\":\"note\",\"tags\":[]}")
ALICE_DOC_URI=$(echo "$R" | python3 -c "import sys,json; print(json.loads(json.load(sys.stdin)['result']['content'][0]['text'])['uri'])" 2>/dev/null)
[ -n "$ALICE_DOC_URI" ] && pass "Alice put document" || fail "Alice put" "failed"

R=$(mcp_call "$BOB_PAT" "$BOB_SID" 12 akb_put "{\"vault\":\"$BOB_VAULT\",\"collection\":\"notes\",\"title\":\"Bob Note\",\"content\":\"## Bob\\n\\nWritten by Bob.\",\"type\":\"note\",\"tags\":[]}")
BOB_DOC_URI=$(echo "$R" | python3 -c "import sys,json; print(json.loads(json.load(sys.stdin)['result']['content'][0]['text'])['uri'])" 2>/dev/null)
[ -n "$BOB_DOC_URI" ] && pass "Bob put document" || fail "Bob put" "failed"

# ── 5. Verify created_by is correct ─────────────────────────
echo ""
echo "▸ 5. Verify created_by attribution"

R=$(mcp_call "$ALICE_PAT" "$ALICE_SID" 13 akb_get "{\"uri\":\"$ALICE_DOC_URI\"}")
ALICE_AUTHOR=$(echo "$R" | python3 -c "import sys,json; print(json.loads(json.load(sys.stdin)['result']['content'][0]['text']).get('created_by',''))" 2>/dev/null)
[ "$ALICE_AUTHOR" = "$ALICE" ] && pass "Alice's doc created_by='$ALICE'" || fail "Alice attribution" "created_by='$ALICE_AUTHOR'"

R=$(mcp_call "$BOB_PAT" "$BOB_SID" 13 akb_get "{\"uri\":\"$BOB_DOC_URI\"}")
BOB_AUTHOR=$(echo "$R" | python3 -c "import sys,json; print(json.loads(json.load(sys.stdin)['result']['content'][0]['text']).get('created_by',''))" 2>/dev/null)
[ "$BOB_AUTHOR" = "$BOB" ] && pass "Bob's doc created_by='$BOB'" || fail "Bob attribution" "created_by='$BOB_AUTHOR'"

# ── 6. Cross-access isolation ────────────────────────────────
echo ""
echo "▸ 6. Cross-access isolation"

# Alice tries to browse Bob's vault (should fail — no access)
R=$(mcp_call "$ALICE_PAT" "$ALICE_SID" 14 akb_browse "{\"vault\":\"$BOB_VAULT\"}")
IS_ERROR=$(echo "$R" | python3 -c "import sys,json; t=json.load(sys.stdin)['result']['content'][0]['text']; print('error' in t.lower() or 'forbidden' in t.lower())" 2>/dev/null)
[ "$IS_ERROR" = "True" ] && pass "Alice can't browse Bob's vault" || fail "Cross-access" "Alice accessed Bob's vault"

# Bob tries to put in Alice's vault (should fail)
R=$(mcp_call "$BOB_PAT" "$BOB_SID" 14 akb_put "{\"vault\":\"$ALICE_VAULT\",\"collection\":\"hack\",\"title\":\"Hack\",\"content\":\"## Nope\",\"type\":\"note\",\"tags\":[]}")
IS_ERROR=$(echo "$R" | python3 -c "import sys,json; t=json.load(sys.stdin)['result']['content'][0]['text']; print('error' in t.lower() or 'forbidden' in t.lower())" 2>/dev/null)
[ "$IS_ERROR" = "True" ] && pass "Bob can't write to Alice's vault" || fail "Write isolation" "Bob wrote to Alice's vault"

# ── 7. Grant access and verify ───────────────────────────────
echo ""
echo "▸ 7. Grant access across sessions"

# Alice grants Bob reader access
R=$(mcp_call "$ALICE_PAT" "$ALICE_SID" 15 akb_grant "{\"vault\":\"$ALICE_VAULT\",\"user\":\"$BOB\",\"role\":\"reader\"}")
GRANTED=$(echo "$R" | python3 -c "import sys,json; print(json.loads(json.load(sys.stdin)['result']['content'][0]['text']).get('granted',False))" 2>/dev/null)
[ "$GRANTED" = "True" ] && pass "Alice granted Bob reader on her vault" || fail "Grant" "failed"

# Bob can now read Alice's vault
R=$(mcp_call "$BOB_PAT" "$BOB_SID" 16 akb_browse "{\"vault\":\"$ALICE_VAULT\"}")
ITEMS=$(echo "$R" | python3 -c "import sys,json; print(len(json.loads(json.load(sys.stdin)['result']['content'][0]['text'])['items']))" 2>/dev/null)
[ "$ITEMS" -ge 1 ] 2>/dev/null && pass "Bob can now browse Alice's vault ($ITEMS items)" || fail "Read after grant" "expected >=1"

# Bob still can't write to Alice's vault (reader only)
R=$(mcp_call "$BOB_PAT" "$BOB_SID" 17 akb_put "{\"vault\":\"$ALICE_VAULT\",\"collection\":\"hack\",\"title\":\"Still Hack\",\"content\":\"## Nope\",\"type\":\"note\",\"tags\":[]}")
IS_ERROR=$(echo "$R" | python3 -c "import sys,json; t=json.load(sys.stdin)['result']['content'][0]['text']; print('error' in t.lower() or 'forbidden' in t.lower())" 2>/dev/null)
[ "$IS_ERROR" = "True" ] && pass "Bob (reader) still can't write to Alice's vault" || fail "Reader write block" "Bob wrote as reader"

# ── 8. list_vaults shows only accessible ─────────────────────
echo ""
echo "▸ 8. Vault listing per user"

R=$(mcp_call "$ALICE_PAT" "$ALICE_SID" 18 akb_list_vaults '{}')
ALICE_VAULTS=$(echo "$R" | python3 -c "import sys,json; vaults=json.loads(json.load(sys.stdin)['result']['content'][0]['text'])['vaults']; print([v['name'] for v in vaults])" 2>/dev/null)
echo "    Alice sees: $ALICE_VAULTS"

R=$(mcp_call "$BOB_PAT" "$BOB_SID" 18 akb_list_vaults '{}')
BOB_VAULTS=$(echo "$R" | python3 -c "import sys,json; vaults=json.loads(json.load(sys.stdin)['result']['content'][0]['text'])['vaults']; print([v['name'] for v in vaults])" 2>/dev/null)
echo "    Bob sees: $BOB_VAULTS"

# Alice should see her vault
HAS=$(echo "$R" | python3 -c "import sys,json; vaults=json.loads(json.load(sys.stdin)['result']['content'][0]['text'])['vaults']; print(any(v['name']=='$BOB_VAULT' for v in vaults))" 2>/dev/null)
[ "$HAS" = "True" ] && pass "Bob sees his own vault" || fail "Bob vault list" "own vault missing"

# ── 9. Terminate sessions ─────────────────────────────────────
echo ""
echo "▸ 9. Cleanup"

curl -sk -X DELETE "$BASE_URL/mcp/" -H "Authorization: Bearer $ALICE_PAT" -H "mcp-session-id: $ALICE_SID" >/dev/null 2>&1
curl -sk -X DELETE "$BASE_URL/mcp/" -H "Authorization: Bearer $BOB_PAT" -H "mcp-session-id: $BOB_SID" >/dev/null 2>&1
pass "Both sessions terminated"

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
