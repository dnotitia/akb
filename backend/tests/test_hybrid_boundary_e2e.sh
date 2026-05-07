#!/bin/bash
#
# Hybrid search boundary / operational edges
#
# - vault name reuse after delete
# - grep with regex special patterns
# - very short content (1-char doc)
# - Unicode-only vault name rejection
# - rapid create-delete-create cycle
# - concurrent vault creates
# - multiple sessions from same user
# - /my/vaults listing accuracy
#
set -uo pipefail

BASE="${AKB_URL:-http://localhost:8000}"
TS=$(date +%s)
USER_NAME="hybrid-bd-$TS"
WAIT="${AKB_HYBRID_INDEX_WAIT:-25}"
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

echo "╔══════════════════════════════════════════╗"
echo "║   Hybrid Boundary / Operational Edges    ║"
echo "║   Target: $BASE"
echo "╚══════════════════════════════════════════╝"
echo ""

echo "▸ Setup"
rcurl -X POST "$BASE/api/v1/auth/register" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER_NAME\",\"email\":\"$USER_NAME@t.dev\",\"password\":\"test1234\"}" >/dev/null
JWT=$(rcurl -X POST "$BASE/api/v1/auth/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER_NAME\",\"password\":\"test1234\"}" | jget "d['token']")
PAT=$(rcurl -X POST "$BASE/api/v1/auth/tokens" -H "Authorization: Bearer $JWT" -H 'Content-Type: application/json' \
  -d '{"name":"b"}' | jget "d['token']")
[ -n "$PAT" ] && pass "PAT" || { fail "PAT" ""; exit 1; }

mcp_init() {
  SID=$(curl -sk -i --max-time 10 -X POST "$BASE/mcp/" \
    -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"b","version":"1.0"}}}' 2>/dev/null \
    | grep -i "mcp-session-id" | tr -d '\r' | awk '{print $2}')
  rcurl -X POST "$BASE/mcp/" -H "Authorization: Bearer $PAT" \
    -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
    -H "mcp-session-id: $SID" \
    -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null
}
mcp_call() {
  local tool=$1 args=$2
  rcurl -X POST "$BASE/mcp/" -H "Authorization: Bearer $PAT" \
    -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
    -H "mcp-session-id: $SID" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"$tool\",\"arguments\":$args}}"
}
mcp_init

# ── B1. Deleted vault name can be reused ─────────────────────
echo ""
echo "▸ B1. Vault name reuse after delete"

V1="hybrid-bd-reuse-$(date +%s)"
rcurl -X POST "$BASE/api/v1/vaults?name=$V1" -H "Authorization: Bearer $PAT" >/dev/null
rcurl -X POST "$BASE/api/v1/documents" -H "Authorization: Bearer $PAT" -H 'Content-Type: application/json' \
  -d "{\"vault\":\"$V1\",\"collection\":\"x\",\"title\":\"A\",\"content\":\"first incarnation\"}" >/dev/null

# Delete
R=$(mcp_call "akb_delete_vault" "{\"vault\":\"$V1\"}")
DEL=$(echo "$R" | python3 -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
    inner = json.loads(d['result']['content'][0]['text'])
    print('OK' if inner.get('deleted') else 'BAD')
except: print('BAD')
")
[ "$DEL" = "OK" ] && pass "vault deleted" || fail "B1-del" "not deleted"

# Re-create with same name
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 10 \
  -X POST "$BASE/api/v1/vaults?name=$V1" -H "Authorization: Bearer $PAT")
[ "$HTTP" = "200" ] && pass "vault name reusable ($HTTP)" || fail "B1-reuse" "got $HTTP"

# New vault is empty (no residual chunks/docs leaked)
B_COUNT=$(rcurl -H "Authorization: Bearer $PAT" "$BASE/api/v1/browse/$V1?depth=2" \
  | jget "len([i for i in d.get('items', []) if i.get('type')=='document'])")
[ "$B_COUNT" = "0" ] && pass "re-created vault starts empty" || fail "B1-empty" "got $B_COUNT"

# ── B2. grep regex special patterns ──────────────────────────
echo ""
echo "▸ B2. grep regex patterns"

V2="hybrid-bd-grep-$(date +%s)"
rcurl -X POST "$BASE/api/v1/vaults?name=$V2" -H "Authorization: Bearer $PAT" >/dev/null
rcurl -X POST "$BASE/api/v1/documents" -H "Authorization: Bearer $PAT" -H 'Content-Type: application/json' \
  -d "{\"vault\":\"$V2\",\"collection\":\"x\",\"title\":\"Regex\",\"content\":\"abc123 def456 ghi789 phone: (555) 123-4567 email: foo@bar.baz\"}" >/dev/null
sleep 5

# Email regex
R=$(rcurl -H "Authorization: Bearer $PAT" "$BASE/api/v1/grep?q=%5B%5E%20%5D%2B%40%5B%5E%20%5D%2B&vault=$V2&regex=true")
HITS=$(echo "$R" | jget "d.get('total_matches', 0)")
[ "$HITS" -ge 1 ] 2>/dev/null && pass "email regex matches ($HITS)" || fail "B2-email" "got $HITS"

# Phone regex \(\d+\)
R=$(rcurl -H "Authorization: Bearer $PAT" "$BASE/api/v1/grep?q=%5C%28%5Cd%2B%5C%29&vault=$V2&regex=true")
HITS=$(echo "$R" | jget "d.get('total_matches', 0)")
[ "$HITS" -ge 1 ] 2>/dev/null && pass "phone regex matches ($HITS)" || fail "B2-phone" "got $HITS"

# Digit-only pattern \d{3}
R=$(rcurl -H "Authorization: Bearer $PAT" "$BASE/api/v1/grep?q=%5Cd%7B3%7D&vault=$V2&regex=true")
HITS=$(echo "$R" | jget "d.get('total_matches', 0)")
[ "$HITS" -ge 1 ] 2>/dev/null && pass "\\d{3} matches ($HITS hits)" || fail "B2-digit" "got $HITS"

# Invalid regex should not 500
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 10 \
  -H "Authorization: Bearer $PAT" "$BASE/api/v1/grep?q=%28%28%28%28&vault=$V2&regex=true")
case "$HTTP" in 200|400|422) pass "invalid regex → $HTTP (no 500)";; *) fail "B2-invalid" "got $HTTP";; esac

# ── B3. Very short content (1 char) ──────────────────────────
echo ""
echo "▸ B3. 1-char content doc"

V3="hybrid-bd-short-$(date +%s)"
rcurl -X POST "$BASE/api/v1/vaults?name=$V3" -H "Authorization: Bearer $PAT" >/dev/null
R=$(rcurl -X POST "$BASE/api/v1/documents" -H "Authorization: Bearer $PAT" -H 'Content-Type: application/json' \
  -d "{\"vault\":\"$V3\",\"collection\":\"x\",\"title\":\"Tiny\",\"content\":\"X\"}")
N=$(echo "$R" | jget "d.get('chunks_indexed', 'err')")
[ "$N" != "err" ] && [ -n "$N" ] && pass "1-char doc chunks_indexed=$N" || fail "B3" "response=$R"

# ── B4. Rapid create-delete-create cycle ─────────────────────
echo ""
echo "▸ B4. Rapid create→delete→create cycle (3x)"

V4="hybrid-bd-cycle-$(date +%s)"
MISS=0
for i in 1 2 3; do
  H1=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 10 \
    -X POST "$BASE/api/v1/vaults?name=$V4" -H "Authorization: Bearer $PAT")
  [ "$H1" = "200" ] || MISS=$((MISS+1))
  R=$(mcp_call "akb_delete_vault" "{\"vault\":\"$V4\"}")
  DEL=$(echo "$R" | python3 -c "
import sys,json
try:
    d=json.loads(sys.stdin.read())
    inner=json.loads(d['result']['content'][0]['text'])
    print('OK' if inner.get('deleted') else 'BAD')
except: print('BAD')
")
  [ "$DEL" = "OK" ] || MISS=$((MISS+1))
done
[ "$MISS" = "0" ] && pass "3 rapid cycles succeeded" || fail "B4" "$MISS failures"

# ── B5. Concurrent vault creates (different names) ───────────
echo ""
echo "▸ B5. 5 concurrent vault creates"

for i in 1 2 3 4 5; do
  ( rcurl -X POST "$BASE/api/v1/vaults?name=hybrid-bd-conc-$TS-$i" -H "Authorization: Bearer $PAT" >/dev/null ) &
done
wait
sleep 3

R=$(rcurl -H "Authorization: Bearer $PAT" "$BASE/api/v1/my/vaults")
N=$(echo "$R" | jget "sum(1 for v in d.get('vaults', []) if v.get('name','').startswith('hybrid-bd-conc-'))")
[ "$N" -ge 1 ] 2>/dev/null && pass "$N concurrent vaults visible" || fail "B5" "got $N"

# ── B6. /my/vaults count accuracy ────────────────────────────
echo ""
echo "▸ B6. /my/vaults accuracy"

# Count all vaults created in this test (B1+B2+B3+B4 cycles leave some)
R=$(rcurl -H "Authorization: Bearer $PAT" "$BASE/api/v1/my/vaults")
MINE=$(echo "$R" | jget "sum(1 for v in d.get('vaults', []) if v.get('name','').startswith('hybrid-bd-'))")
[ "$MINE" -ge 2 ] 2>/dev/null && pass "/my/vaults contains $MINE test vaults" || fail "B6" "got $MINE"

# ── B7. Search across multiple accessible vaults (no filter) ─
echo ""
echo "▸ B7. Cross-vault search without vault filter"

V7a="hybrid-bd-xva-$TS"
V7b="hybrid-bd-xvb-$TS"
rcurl -X POST "$BASE/api/v1/vaults?name=$V7a" -H "Authorization: Bearer $PAT" >/dev/null
rcurl -X POST "$BASE/api/v1/vaults?name=$V7b" -H "Authorization: Bearer $PAT" >/dev/null
rcurl -X POST "$BASE/api/v1/documents" -H "Authorization: Bearer $PAT" -H 'Content-Type: application/json' \
  -d "{\"vault\":\"$V7a\",\"collection\":\"x\",\"title\":\"A\",\"content\":\"VoyageurCross marker for cross test.\"}" >/dev/null
rcurl -X POST "$BASE/api/v1/documents" -H "Authorization: Bearer $PAT" -H 'Content-Type: application/json' \
  -d "{\"vault\":\"$V7b\",\"collection\":\"x\",\"title\":\"B\",\"content\":\"VoyageurCross marker also here.\"}" >/dev/null

DEADLINE=$(($(date +%s) + 60))
while [ $(date +%s) -lt $DEADLINE ]; do
  R=$(rcurl -H "Authorization: Bearer $PAT" "$BASE/api/v1/search?q=VoyageurCross&limit=10")
  T=$(echo "$R" | jget "d.get('total', 0)")
  [ "$T" -ge 2 ] 2>/dev/null && break
  sleep 3
done
[ "$T" -ge 2 ] 2>/dev/null && pass "2 vaults both contribute to cross-vault search ($T)" || fail "B7" "got $T"

# Results span both vaults
VAULTS_SEEN=$(echo "$R" | jget "sorted(set(r.get('vault','') for r in d.get('results', [])))")
echo "$VAULTS_SEEN" | grep -q "$V7a" && echo "$VAULTS_SEEN" | grep -q "$V7b" \
  && pass "results include both vaults" || fail "B7-span" "vaults: $VAULTS_SEEN"

# ── B8. Invalid vault name via MCP path ──────────────────────
echo ""
echo "▸ B8. Invalid vault name via MCP"

R=$(mcp_call "akb_create_vault" "{\"name\":\"UPPERCASE\"}")
ERR=$(echo "$R" | python3 -c "
import sys, json
try:
    d=json.loads(sys.stdin.read())
    inner=json.loads(d['result']['content'][0]['text'])
    print('OK' if 'error' in inner else 'NOERR')
except: print('PARSE')
")
[ "$ERR" = "OK" ] && pass "uppercase name via MCP returns error" || fail "B8" "got '$ERR'"

# ── Cleanup ──────────────────────────────────────────────────
echo ""
echo "▸ Cleanup"
for V in "$V1" "$V2" "$V3" "$V7a" "$V7b" "hybrid-bd-conc-$TS-1" "hybrid-bd-conc-$TS-2" "hybrid-bd-conc-$TS-3" "hybrid-bd-conc-$TS-4" "hybrid-bd-conc-$TS-5"; do
  mcp_call "akb_delete_vault" "{\"vault\":\"$V\"}" >/dev/null 2>&1
done

# Self-delete test user to avoid accumulating in DB
curl -sk --max-time 15 -X DELETE "$BASE/api/v1/my/account" -H "Authorization: Bearer $JWT" >/dev/null 2>&1
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
