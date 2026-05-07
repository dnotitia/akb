#!/bin/bash
#
# Miscellaneous hybrid search edges
# (JWT/PAT lifecycle, unicode normalization, akb_edit × search,
# drill-down × search, rate-limit resilience)
#
set -uo pipefail

BASE="${AKB_URL:-http://localhost:8000}"
USER_NAME="hybrid-misc-$(date +%s)"
VAULT="hybrid-misc-$(date +%s)"
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

wait_for_search() {
  local q=$1 vault=$2
  local deadline=$(($(date +%s) + 60))
  while [ $(date +%s) -lt $deadline ]; do
    local t=$(rcurl -H "Authorization: Bearer $PAT" "$BASE/api/v1/search?q=$(python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$q")&vault=$vault&limit=5" | jget "d.get('total', 0)")
    [ -n "$t" ] && [ "$t" != "0" ] && { echo "$t"; return 0; }
    sleep 3
  done
  echo "0"
}

echo "╔══════════════════════════════════════════╗"
echo "║   Hybrid Miscellaneous                   ║"
echo "║   Target: $BASE"
echo "╚══════════════════════════════════════════╝"
echo ""

echo "▸ Setup"
rcurl -X POST "$BASE/api/v1/auth/register" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER_NAME\",\"email\":\"$USER_NAME@t.dev\",\"password\":\"test1234\"}" >/dev/null
JWT=$(rcurl -X POST "$BASE/api/v1/auth/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER_NAME\",\"password\":\"test1234\"}" | jget "d['token']")
PAT=$(rcurl -X POST "$BASE/api/v1/auth/tokens" -H "Authorization: Bearer $JWT" -H 'Content-Type: application/json' \
  -d '{"name":"m"}' | jget "d['token']")
[ -n "$PAT" ] && pass "PAT" || { fail "PAT" ""; exit 1; }
rcurl -X POST "$BASE/api/v1/vaults?name=$VAULT" -H "Authorization: Bearer $PAT" >/dev/null
pass "vault created"

put_doc() {
  local title=$1 content=$2
  for _ in 1 2 3; do
    local resp=$(rcurl -X POST "$BASE/api/v1/documents" -H "Authorization: Bearer $PAT" -H 'Content-Type: application/json' \
      -d "{\"vault\":\"$VAULT\",\"collection\":\"x\",\"title\":\"$title\",\"content\":$(python3 -c 'import sys,json; print(json.dumps(sys.argv[1]))' "$content")}")
    local id=$(echo "$resp" | jget "d.get('doc_id','')")
    [ -n "$id" ] && { echo "$resp"; return 0; }
    sleep 3
  done
  echo "$resp"
}

# ── M1. JWT vs PAT both work for search ──────────────────────
echo ""
echo "▸ M1. JWT and PAT both authenticate search"

put_doc "JWTProbe" "VelociraptorSongbird marker in jwt test." >/dev/null
wait_for_search "VelociraptorSongbird" "$VAULT" >/dev/null

T_JWT=$(rcurl -H "Authorization: Bearer $JWT" "$BASE/api/v1/search?q=VelociraptorSongbird&vault=$VAULT&limit=3" | jget "d.get('total', 0)")
T_PAT=$(rcurl -H "Authorization: Bearer $PAT" "$BASE/api/v1/search?q=VelociraptorSongbird&vault=$VAULT&limit=3" | jget "d.get('total', 0)")
[ "$T_JWT" -ge 1 ] 2>/dev/null && pass "JWT auth → total=$T_JWT" || fail "M1-jwt" "got $T_JWT"
[ "$T_PAT" -ge 1 ] 2>/dev/null && pass "PAT auth → total=$T_PAT" || fail "M1-pat" "got $T_PAT"
[ "$T_JWT" = "$T_PAT" ] && pass "JWT and PAT return identical totals" || fail "M1-parity" "JWT=$T_JWT PAT=$T_PAT"

# ── M2. Unicode NFC/NFD equivalence ──────────────────────────
echo ""
echo "▸ M2. Unicode normalization"

# NFC 'é' (single codepoint) vs NFD 'e\u0301' (base + combining).
NFC_DOC="cafévoyage"                   # single é
NFD_DOC=$(python3 -c "print('cafe\u0301voyage')")  # e + combining acute
put_doc "NFC" "$NFC_DOC literal marker." >/dev/null
wait_for_search "$NFC_DOC" "$VAULT" >/dev/null

# Query with NFD should ideally still hit (if backend normalizes). If not,
# at least it shouldn't crash.
R=$(rcurl -H "Authorization: Bearer $PAT" "$BASE/api/v1/search?q=$(python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$NFD_DOC")&vault=$VAULT&limit=3")
T=$(echo "$R" | jget "d.get('total', 0)")
[ -n "$T" ] && pass "NFD variant query handled (total=$T)" || fail "M2" "no response"

# ── M3. akb_edit preserves searchability ─────────────────────
echo ""
echo "▸ M3. akb_edit + search"

DOC_EDIT=$(put_doc "EditProbe" "InitialEditMarkerAlpha content here." | jget "d['doc_id']")
wait_for_search "InitialEditMarkerAlpha" "$VAULT" >/dev/null

# Edit: replace Alpha → Beta via MCP
SID=$(curl -sk -i --max-time 10 -X POST "$BASE/mcp/" \
  -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"m","version":"1.0"}}}' 2>/dev/null \
  | grep -i "mcp-session-id" | tr -d '\r' | awk '{print $2}')
rcurl -X POST "$BASE/mcp/" -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null

rcurl -X POST "$BASE/mcp/" -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: $SID" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"akb_edit\",\"arguments\":{\"vault\":\"$VAULT\",\"doc_id\":\"$DOC_EDIT\",\"old_string\":\"InitialEditMarkerAlpha\",\"new_string\":\"RevisedEditMarkerBeta\"}}}" >/dev/null

# Wait for re-index
DEADLINE=$(($(date +%s) + 60))
while [ $(date +%s) -lt $DEADLINE ]; do
  T=$(rcurl -H "Authorization: Bearer $PAT" "$BASE/api/v1/search?q=RevisedEditMarkerBeta&vault=$VAULT&limit=3" | jget "d.get('total', 0)")
  [ "$T" -ge 1 ] 2>/dev/null && break
  sleep 3
done
[ "$T" -ge 1 ] 2>/dev/null && pass "post-edit new token searchable" || fail "M3-new" "got $T"

# Old token should be gone
T_OLD=$(rcurl -H "Authorization: Bearer $PAT" "$BASE/api/v1/search?q=InitialEditMarkerAlpha&vault=$VAULT&limit=3" | jget "d.get('total', 0)")
[ "$T_OLD" = "0" ] && pass "post-edit old token purged" || fail "M3-old" "got $T_OLD"

# ── M4. drill-down returns sections aligned with chunks ──────
echo ""
echo "▸ M4. drill-down chunk alignment"

LONG_CONTENT=$(python3 -c "
print('## Intro\n\nFirst section content about widgets.')
print()
print('## Middle\n\nSecond section about PrismaCascadeRetinue marker.')
print()
print('## End\n\nThird section wrapping up.')
")
DD=$(put_doc "DrillTarget" "$LONG_CONTENT" | jget "d['doc_id']")
wait_for_search "PrismaCascadeRetinue" "$VAULT" >/dev/null

# drill-down should return sections
DD_RAW=$(rcurl -H "Authorization: Bearer $PAT" "$BASE/api/v1/drill-down/$VAULT/$DD")
N_SEC=$(echo "$DD_RAW" | jget "len(d.get('sections', []))")
[ "$N_SEC" -ge 1 ] 2>/dev/null && pass "drill-down returned $N_SEC sections" || fail "M4" "got $N_SEC"

# One of the sections should mention our marker
HAS_MARKER=$(echo "$DD_RAW" | jget "any('PrismaCascadeRetinue' in (s.get('content') or '') for s in d.get('sections', []))")
[ "$HAS_MARKER" = "True" ] && pass "drill-down section contains marker" || fail "M4-marker" "marker not in sections"

# ── M5. Rate-limit / burst resilience ────────────────────────
echo ""
echo "▸ M5. Burst of 30 search calls (no 5xx)"

put_doc "BurstTarget" "TangerineCumulus marker for burst test." >/dev/null
wait_for_search "TangerineCumulus" "$VAULT" >/dev/null

FAILS=0
for i in $(seq 1 30); do
  HTTP=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 10 \
    -H "Authorization: Bearer $PAT" "$BASE/api/v1/search?q=TangerineCumulus&vault=$VAULT&limit=1")
  # 429 (rate-limit) would be OK here; only 5xx counts as failure
  case "$HTTP" in
    5*) FAILS=$((FAILS+1)) ;;
  esac
done
[ "$FAILS" = "0" ] && pass "30 burst calls: no 5xx" || fail "M5" "$FAILS/30 were 5xx"

# ── M6. Vault list returns the user's own vaults ─────────────
echo ""
echo "▸ M6. /vaults list contains own vault"

R=$(rcurl -H "Authorization: Bearer $PAT" "$BASE/api/v1/vaults")
HAS=$(echo "$R" | jget "any(v.get('name')=='$VAULT' for v in d.get('vaults', []))")
[ "$HAS" = "True" ] && pass "own vault listed" || fail "M6" "vault missing in list"

# ── M7. Search handles emoji-only query gracefully ───────────
echo ""
echo "▸ M7. Emoji-only query"

HTTP=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 10 \
  -H "Authorization: Bearer $PAT" "$BASE/api/v1/search?q=%F0%9F%8E%89&vault=$VAULT&limit=3")
[ "$HTTP" = "200" ] && pass "emoji query → 200" || fail "M7" "got HTTP $HTTP"

# ── M8. Search with q=single-space ───────────────────────────
echo ""
echo "▸ M8. Single-space query"

HTTP=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 10 \
  -H "Authorization: Bearer $PAT" "$BASE/api/v1/search?q=%20&vault=$VAULT&limit=3")
[ "$HTTP" = "200" ] || [ "$HTTP" = "422" ] && pass "space query → $HTTP" || fail "M8" "got HTTP $HTTP"

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
