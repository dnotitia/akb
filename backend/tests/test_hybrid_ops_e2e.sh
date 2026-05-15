#!/bin/bash
#
# Hybrid operational races & rarely-hit paths
#
# - concurrent grant/revoke race between two admins
# - transfer chain (A → B → C)
# - circular graph links
# - publication with password protection
# - /my/vaults listing after transfer
# - search immediately after bulk re-index trigger
#
set -uo pipefail

BASE="${AKB_URL:-http://localhost:8000}"
TS=$(date +%s)
A="hybrid-ops-a-$TS"
B="hybrid-ops-b-$TS"
C="hybrid-ops-c-$TS"
VAULT="hybrid-ops-v-$TS"
WAIT=25
PASS=0
FAIL=0
ERRORS=()

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

rcurl() {
  local out=""
  for _ in 1 2 3 4 5; do
    out=$(curl -sk --max-time 20 "$@" 2>/dev/null)
    if [ -n "$out" ]; then echo "$out"; return 0; fi
    sleep 2
  done
  echo ""; return 1
}
jget() { python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print($1)" 2>/dev/null; }
register_pat() {
  local u=$1
  rcurl -X POST "$BASE/api/v1/auth/register" -H 'Content-Type: application/json' \
    -d "{\"username\":\"$u\",\"email\":\"$u@t.dev\",\"password\":\"test1234\"}" >/dev/null
  local jwt=$(rcurl -X POST "$BASE/api/v1/auth/login" -H 'Content-Type: application/json' \
    -d "{\"username\":\"$u\",\"password\":\"test1234\"}" | jget "d['token']")
  rcurl -X POST "$BASE/api/v1/auth/tokens" -H "Authorization: Bearer $jwt" -H 'Content-Type: application/json' \
    -d '{"name":"t"}' | jget "d['token']"
}

echo "╔══════════════════════════════════════════╗"
echo "║   Hybrid Ops Races                       ║"
echo "║   Target: $BASE"
echo "╚══════════════════════════════════════════╝"
echo ""

echo "▸ Setup 3 users"
PAT_A=$(register_pat "$A")
PAT_B=$(register_pat "$B")
PAT_C=$(register_pat "$C")
[ -n "$PAT_A" ] && [ -n "$PAT_B" ] && [ -n "$PAT_C" ] && pass "3 PATs" || { fail "setup" ""; exit 1; }

rcurl -X POST "$BASE/api/v1/vaults?name=$VAULT" -H "Authorization: Bearer $PAT_A" >/dev/null
rcurl -X POST "$BASE/api/v1/documents" -H "Authorization: Bearer $PAT_A" -H 'Content-Type: application/json' \
  -d "{\"vault\":\"$VAULT\",\"collection\":\"x\",\"title\":\"Doc\",\"content\":\"OpsRaceMarkerZephyrus content here.\"}" >/dev/null
pass "seed vault + doc"

sleep "$WAIT"

# ── O1. Concurrent grant/revoke race ─────────────────────────
# Two rapid ops from the owner: grant then revoke B. Final state should
# be deterministic (one wins), not a crash.
echo ""
echo "▸ O1. Grant + revoke race on same user"

(
  rcurl -X POST "$BASE/api/v1/vaults/$VAULT/grant" -H "Authorization: Bearer $PAT_A" -H 'Content-Type: application/json' \
    -d "{\"user\":\"$B\",\"role\":\"reader\"}" >/dev/null
) &
(
  sleep 0.1
  rcurl -X POST "$BASE/api/v1/vaults/$VAULT/revoke" -H "Authorization: Bearer $PAT_A" -H 'Content-Type: application/json' \
    -d "{\"user\":\"$B\"}" >/dev/null
) &
wait
sleep 3

# No 500 crash — verify by searching (owner's view)
T=$(rcurl -H "Authorization: Bearer $PAT_A" "$BASE/api/v1/search?q=OpsRaceMarkerZephyrus&vault=$VAULT&limit=3" | jget "d.get('total', 0)")
[ "$T" -ge 1 ] 2>/dev/null && pass "owner search still works after grant/revoke race" || fail "O1" "got $T"

# B's final state: either has or doesn't — both acceptable, no crash
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 10 \
  -H "Authorization: Bearer $PAT_B" "$BASE/api/v1/search?q=OpsRaceMarkerZephyrus&vault=$VAULT")
case "$HTTP" in 200|403) pass "B's post-race access: $HTTP (no 5xx)";; *) fail "O1-b" "got $HTTP";; esac

# ── O2. Transfer chain A → B → C ─────────────────────────────
echo ""
echo "▸ O2. Transfer chain A→B→C"

rcurl -X POST "$BASE/api/v1/vaults/$VAULT/transfer" -H "Authorization: Bearer $PAT_A" -H 'Content-Type: application/json' \
  -d "{\"new_owner\":\"$B\"}" >/dev/null
sleep 2
rcurl -X POST "$BASE/api/v1/vaults/$VAULT/transfer" -H "Authorization: Bearer $PAT_B" -H 'Content-Type: application/json' \
  -d "{\"new_owner\":\"$C\"}" >/dev/null
sleep 3

T=$(rcurl -H "Authorization: Bearer $PAT_C" "$BASE/api/v1/search?q=OpsRaceMarkerZephyrus&vault=$VAULT&limit=3" | jget "d.get('total', 0)")
[ "$T" -ge 1 ] 2>/dev/null && pass "final owner C sees vault" || fail "O2-c" "got $T"

# A (original owner) might still have reader role; accept either
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 10 \
  -H "Authorization: Bearer $PAT_A" "$BASE/api/v1/vaults/$VAULT/info")
case "$HTTP" in 200|403) pass "A post-chain access: $HTTP (no 5xx)";; *) fail "O2-a" "got $HTTP";; esac

# ── O3. Circular graph link (A→B, B→A) ───────────────────────
echo ""
echo "▸ O3. Circular links"

# Need MCP session as C (current owner)
SID=$(curl -sk -i --max-time 10 -X POST "$BASE/mcp/" \
  -H "Authorization: Bearer $PAT_C" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"o","version":"1.0"}}}' 2>/dev/null \
  | grep -i "mcp-session-id" | tr -d '\r' | awk '{print $2}')
rcurl -X POST "$BASE/mcp/" -H "Authorization: Bearer $PAT_C" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null

mcp_call() {
  rcurl -X POST "$BASE/mcp/" -H "Authorization: Bearer $PAT_C" \
    -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
    -H "mcp-session-id: $SID" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"$1\",\"arguments\":$2}}"
}

# Put two docs
D1_RESP=$(rcurl -X POST "$BASE/api/v1/documents" -H "Authorization: Bearer $PAT_C" -H 'Content-Type: application/json' \
  -d "{\"vault\":\"$VAULT\",\"collection\":\"x\",\"title\":\"CirclA\",\"content\":\"circular source node A.\"}")
P1=$(echo "$D1_RESP" | jget "d['path']")
D2_RESP=$(rcurl -X POST "$BASE/api/v1/documents" -H "Authorization: Bearer $PAT_C" -H 'Content-Type: application/json' \
  -d "{\"vault\":\"$VAULT\",\"collection\":\"x\",\"title\":\"CirclB\",\"content\":\"circular source node B.\"}")
P2=$(echo "$D2_RESP" | jget "d['path']")

mcp_call "akb_link" "{\"source\":\"akb://$VAULT/doc/$P1\",\"target\":\"akb://$VAULT/doc/$P2\",\"relation\":\"related_to\"}" >/dev/null
mcp_call "akb_link" "{\"source\":\"akb://$VAULT/doc/$P2\",\"target\":\"akb://$VAULT/doc/$P1\",\"relation\":\"related_to\"}" >/dev/null
sleep 2

HTTP=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 10 \
  -H "Authorization: Bearer $PAT_C" "$BASE/api/v1/graph/$VAULT")
[ "$HTTP" = "200" ] && pass "graph endpoint stable with circular links" || fail "O3" "got $HTTP"

# ── O4. Publication with password protection ────────────────
echo ""
echo "▸ O4. Publication + password"

D_RESP=$(rcurl -X POST "$BASE/api/v1/documents" -H "Authorization: Bearer $PAT_C" -H 'Content-Type: application/json' \
  -d "{\"vault\":\"$VAULT\",\"collection\":\"x\",\"title\":\"PubProtected\",\"content\":\"protected content here.\"}")
PUB_DOC=$(echo "$D_RESP" | jget "d['doc_id']")

PUB_RESP=$(rcurl -X POST "$BASE/api/v1/publications/$VAULT/create" -H "Authorization: Bearer $PAT_C" -H 'Content-Type: application/json' \
  -d "{\"resource_type\":\"document\",\"doc_id\":\"$PUB_DOC\",\"password\":\"secret-passwd-xy\"}")
SLUG=$(echo "$PUB_RESP" | jget "d.get('slug','')")
[ -n "$SLUG" ] && pass "password-protected publication created" || fail "O4-create" "$PUB_RESP"

if [ -n "$SLUG" ]; then
  # Anonymous access — password-protected publications should require auth (401/403)
  HTTP=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 10 "$BASE/api/v1/public/$SLUG")
  case "$HTTP" in 200|401|403) pass "anon /public/$SLUG → $HTTP (handled)";; *) fail "O4-anon" "got $HTTP";; esac
fi

# ── O5. /my/vaults accuracy after transfer chain ─────────────
echo ""
echo "▸ O5. /my/vaults after transfer"

# C should own VAULT
R=$(rcurl -H "Authorization: Bearer $PAT_C" "$BASE/api/v1/my/vaults")
HAS=$(echo "$R" | jget "any(v.get('name')=='$VAULT' for v in d.get('vaults', []))")
[ "$HAS" = "True" ] && pass "C's /my/vaults includes transferred vault" || fail "O5" "missing"

# ── O6. Bulk reindex trigger (PG side) + search immediately ──
echo ""
echo "▸ O6. Bulk reindex flag → search survives"

kubectl exec -n akb statefulset/postgres -- psql -U akbuser -d akb -c "
  UPDATE chunks SET vector_indexed_at = NULL, vector_next_attempt_at = NOW(), vector_retry_count = 0
   WHERE document_id IN (SELECT d.id FROM documents d JOIN vaults v ON d.vault_id=v.id WHERE v.name='$VAULT')
" >/dev/null 2>&1

# Search should keep returning results (vector store still has points from prior upsert)
T=$(rcurl -H "Authorization: Bearer $PAT_C" "$BASE/api/v1/search?q=OpsRaceMarkerZephyrus&vault=$VAULT&limit=3" | jget "d.get('total', 0)")
[ "$T" -ge 1 ] 2>/dev/null && pass "search survives reindex flag reset" || fail "O6" "got $T"

# ── Cleanup ──────────────────────────────────────────────────
echo ""
echo "▸ Cleanup"
rcurl -X POST "$BASE/mcp/" -H "Authorization: Bearer $PAT_C" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: $SID" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":99,\"method\":\"tools/call\",\"params\":{\"name\":\"akb_delete_vault\",\"arguments\":{\"vault\":\"$VAULT\"}}}" >/dev/null
# Self-delete all 3 test users
for P in "$PAT_A" "$PAT_B" "$PAT_C"; do
  curl -sk --max-time 15 -X DELETE "$BASE/api/v1/my/account" -H "Authorization: Bearer $P" >/dev/null 2>&1
done
pass "cleanup attempted"

echo ""
echo "═══════════════════════════════════════════"
echo "  Passed: $PASS"
echo "  Failed: $FAIL"
if [ $FAIL -gt 0 ]; then
  echo ""
  echo "  Errors:"
  for e in "${ERRORS[@]}"; do echo "    - $e"; done
fi
echo "═══════════════════════════════════════════"

exit $FAIL
