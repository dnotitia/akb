#!/bin/bash
#
# Hybrid chaos / deeper corners
#
# Probes that hit places where production sometimes surprises us:
# - vocab write race (many new terms added in parallel)
# - MCP vs REST output parity for search
# - Korean compound noun decomposition behavior
# - vault with 100 docs
# - query that tokenizes to >100 terms (very verbose search)
#
set -uo pipefail

BASE="${AKB_URL:-http://localhost:8000}"
USER_NAME="hybrid-cha-$(date +%s)"
VAULT="hybrid-cha-$(date +%s)"
WAIT="${AKB_HYBRID_INDEX_WAIT:-30}"
PASS=0
FAIL=0
ERRORS=()

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

rcurl() {
  local out=""
  for _ in 1 2 3; do
    out=$(curl -sk --max-time 20 "$@" 2>/dev/null)
    if [ -n "$out" ]; then echo "$out"; return 0; fi
    sleep 1
  done
  echo ""; return 1
}
jget() { python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print($1)" 2>/dev/null; }

echo "╔══════════════════════════════════════════╗"
echo "║   Hybrid Chaos / Deeper Corners          ║"
echo "║   Target: $BASE"
echo "╚══════════════════════════════════════════╝"
echo ""

echo "▸ Setup"
rcurl -X POST "$BASE/api/v1/auth/register" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER_NAME\",\"email\":\"$USER_NAME@t.dev\",\"password\":\"test1234\"}" >/dev/null
JWT=$(rcurl -X POST "$BASE/api/v1/auth/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER_NAME\",\"password\":\"test1234\"}" | jget "d['token']")
PAT=$(rcurl -X POST "$BASE/api/v1/auth/tokens" -H "Authorization: Bearer $JWT" -H 'Content-Type: application/json' \
  -d '{"name":"c"}' | jget "d['token']")
[ -n "$PAT" ] && pass "PAT" || { fail "PAT" ""; exit 1; }
rcurl -X POST "$BASE/api/v1/vaults?name=$VAULT" -H "Authorization: Bearer $PAT" >/dev/null
pass "vault $VAULT created"

put() {
  local title=$1 content=$2
  rcurl -X POST "$BASE/api/v1/documents" -H "Authorization: Bearer $PAT" -H 'Content-Type: application/json' \
    -d "{\"vault\":\"$VAULT\",\"collection\":\"x\",\"title\":\"$title\",\"content\":$(python3 -c 'import sys,json; print(json.dumps(sys.argv[1]))' "$content")}"
}
search() {
  rcurl -H "Authorization: Bearer $PAT" "$BASE/api/v1/search?q=$(python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$1")&vault=$VAULT&limit=${2:-5}"
}

# ── C1. Vocab write race — 20 parallel puts, all new unique terms ──
echo ""
echo "▸ C1. Vocab write race (20 parallel new terms)"

V_BEFORE=$(rcurl "$BASE/health" | jget "d['vector_store']['bm25']['vocab_size']")
for i in $(seq 1 20); do
  HEX=$(python3 -c "import secrets; print(''.join(secrets.choice('abcdef') for _ in range(10)))")
  ( put "Race$i" "RaceTerm$HEX content for race test." >/dev/null ) &
done
wait
sleep "$WAIT"

V_AFTER=$(rcurl "$BASE/health" | jget "d['vector_store']['bm25']['vocab_size']")
DIFF=$((V_AFTER - V_BEFORE))
# Each put adds at least 2 new terms (racetermXXX + content-ish). 20 puts → ~20-40 new terms.
[ "$DIFF" -ge 20 ] 2>/dev/null && pass "vocab grew by $DIFF under parallel writes" || fail "C1" "only grew by $DIFF"

# Check no duplicate term_ids in vocab
DUP=$(kubectl exec -n akb statefulset/postgres -- psql -U akbuser -d akb -t -c "
  SELECT COUNT(*) FROM (
    SELECT term_id, COUNT(*) AS n FROM bm25_vocab GROUP BY term_id HAVING COUNT(*) > 1
  ) x" 2>/dev/null | tr -d ' \n')
[ "$DUP" = "0" ] && pass "no duplicate term_ids after race" || fail "C1-dup" "$DUP duplicate ids"

# ── C2. MCP akb_search ≡ REST /api/v1/search for same query ──
echo ""
echo "▸ C2. MCP vs REST parity"

put "ParitySrc" "ParityCornerEchelon marker content." >/dev/null
sleep "$WAIT"

REST_R=$(search "ParityCornerEchelon" 3)
REST_TITLES=$(echo "$REST_R" | jget "sorted(r.get('title','') for r in d.get('results', []))")

SID=$(curl -sk -i --max-time 10 -X POST "$BASE/mcp/" \
  -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"p","version":"1.0"}}}' 2>/dev/null \
  | grep -i "mcp-session-id" | tr -d '\r' | awk '{print $2}')
rcurl -X POST "$BASE/mcp/" -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null
MCP_RAW=$(rcurl -X POST "$BASE/mcp/" -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: $SID" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"akb_search\",\"arguments\":{\"query\":\"ParityCornerEchelon\",\"vault\":\"$VAULT\",\"limit\":3}}}")
MCP_TITLES=$(echo "$MCP_RAW" | python3 -c "
import sys, json
d = json.loads(sys.stdin.read())
try:
    inner = json.loads(d['result']['content'][0]['text'])
    print(sorted(r.get('title','') for r in inner.get('results', [])))
except Exception as e:
    print(f'err: {e}')
")
[ "$REST_TITLES" = "$MCP_TITLES" ] && pass "MCP ≡ REST: $REST_TITLES" || fail "C2" "REST=$REST_TITLES MCP=$MCP_TITLES"

# ── C3. Korean compound noun decomposition — common parts still match ──
echo ""
echo "▸ C3. Korean compound decomposition"

# '인공지능' is typically split into '인공' + '지능' by Kiwi. Put a doc
# with the compound, then query with either part.
put "AI-KR" "인공지능 플랫폼 개발 가이드 (최신)" >/dev/null
sleep "$WAIT"

# Full compound
T=$(search "인공지능" 3 | jget "d.get('total', 0)")
[ "$T" -ge 1 ] 2>/dev/null && pass "full compound 인공지능 matches" || fail "C3-full" "got $T"

# Part: '인공' alone
T=$(search "인공" 3 | jget "d.get('total', 0)")
[ "$T" -ge 1 ] 2>/dev/null && pass "sub-token 인공 matches compound doc" || fail "C3-part" "got $T"

# ── C4. Verbose query (100+ tokens) ──────────────────────────
echo ""
echo "▸ C4. Verbose 100-token query"

put "Verbose" "VerboseMatcherTokenX unique anchor here." >/dev/null
sleep "$WAIT"

VERBOSE=$(python3 -c "print('random noise filler ' * 50 + 'VerboseMatcherTokenX')")
T=$(search "$VERBOSE" 3 | jget "d.get('total', 0)")
[ "$T" -ge 1 ] 2>/dev/null && pass "100-token query with embedded anchor finds doc" || fail "C4" "got $T"

# ── C5. Very-large vault (60 docs) under bulk ────────────────
# (Test already has plenty of docs from C1; add more to exceed 60.)
echo ""
echo "▸ C5. 60-doc vault latency"

for i in $(seq 1 40); do
  ( put "Bulk$i" "BulkPatternTom$i widget content." >/dev/null ) &
  if (( i % 10 == 0 )); then wait; fi
done
wait
sleep "$WAIT"

START=$(python3 -c "import time; print(time.time())")
T=$(search "BulkPatternTom25" 1 | jget "d.get('total', 0)")
END=$(python3 -c "import time; print(time.time())")
ELAPSED=$(python3 -c "print(int(($END - $START) * 1000))")
[ "$ELAPSED" -lt 5000 ] 2>/dev/null && pass "specific-doc lookup ${ELAPSED}ms" || fail "C5-latency" "${ELAPSED}ms"
[ "$T" -ge 1 ] 2>/dev/null && pass "recall preserved in 60-doc vault" || fail "C5-recall" "got $T"

# ── C6. Collection 2-part path filter works ──────────────────
echo ""
echo "▸ C6. Nested collection path"

put "NestedA" "CastellanDeepDive content for nested test." "spec" '[]' >/dev/null  # default 'x' collection
sleep "$WAIT"

# Search with collection prefix — since our put uses 'x' as root
T=$(rcurl -H "Authorization: Bearer $PAT" \
  "$BASE/api/v1/search?q=CastellanDeepDive&vault=$VAULT&collection=x&limit=3" \
  | jget "d.get('total', 0)")
[ "$T" -ge 1 ] 2>/dev/null && pass "collection prefix match" || fail "C6" "got $T"

T_NONE=$(rcurl -H "Authorization: Bearer $PAT" \
  "$BASE/api/v1/search?q=CastellanDeepDive&vault=$VAULT&collection=nonexistent&limit=3" \
  | jget "d.get('total', 0)")
[ "$T_NONE" = "0" ] && pass "nonexistent collection → 0" || fail "C6-miss" "got $T_NONE"

# ── C7. Document with only whitespace content ────────────────
echo ""
echo "▸ C7. Whitespace-only content"

R=$(put "WSOnly" "        ")
N=$(echo "$R" | jget "d.get('chunks_indexed', 'err')")
[ "$N" != "err" ] && [ -n "$N" ] && pass "whitespace-only handled (chunks=$N)" || fail "C7" "response=$R"

# ── C8. Search with only 2-char Korean query ─────────────────
echo ""
echo "▸ C8. Very short Korean query"

put "ShortKR" "달 탐사 임무 프로그램 설계" >/dev/null
sleep "$WAIT"

T=$(search "탐사" 3 | jget "d.get('total', 0)")
[ "$T" -ge 1 ] 2>/dev/null && pass "2-char Korean noun query finds doc" || fail "C8" "got $T"

# ── Cleanup ──────────────────────────────────────────────────
echo ""
echo "▸ Cleanup"
if [ -n "$SID" ]; then
  rcurl -X POST "$BASE/mcp/" -H "Authorization: Bearer $PAT" \
    -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
    -H "mcp-session-id: $SID" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":99,\"method\":\"tools/call\",\"params\":{\"name\":\"akb_delete_vault\",\"arguments\":{\"vault\":\"$VAULT\"}}}" >/dev/null
fi

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
