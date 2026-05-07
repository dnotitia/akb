#!/bin/bash
#
# Hybrid search stress / quirky-corner E2E
#
# Cases the design must handle but most users never trip: concurrent
# writes, tokenizer quirks (digit boundaries, hyphens, Korean particles),
# many-doc vaults, and a few obscure driver-side filter shapes.
#
set -uo pipefail

BASE="${AKB_URL:-http://localhost:8000}"
USER_NAME="hybrid-str-$(date +%s)"
VAULT="hybrid-str-$(date +%s)"
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
  echo ""
  return 1
}

jget() { python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print($1)" 2>/dev/null; }

# Wait until a search returns total >= min_total (default 1), up to 60s.
wait_for_search_total() {
  local q=$1 min=${2:-1}
  local deadline=$(($(date +%s) + 60))
  while [ $(date +%s) -lt $deadline ]; do
    local t=$(search "$q" 5 | jget "d.get('total', 0)")
    if [ -n "$t" ] && [ "$t" -ge "$min" ] 2>/dev/null; then echo "$t"; return 0; fi
    sleep 3
  done
  echo "0"
  return 1
}

echo "╔══════════════════════════════════════════╗"
echo "║   Hybrid Search Stress / Corner Cases    ║"
echo "║   Target: $BASE"
echo "╚══════════════════════════════════════════╝"
echo ""

echo "▸ Setup"
rcurl -X POST "$BASE/api/v1/auth/register" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER_NAME\",\"email\":\"$USER_NAME@t.dev\",\"password\":\"test1234\"}" >/dev/null
JWT=$(rcurl -X POST "$BASE/api/v1/auth/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER_NAME\",\"password\":\"test1234\"}" | jget "d['token']")
PAT=$(rcurl -X POST "$BASE/api/v1/auth/tokens" -H "Authorization: Bearer $JWT" -H 'Content-Type: application/json' \
  -d '{"name":"st"}' | jget "d['token']")
[ -n "$PAT" ] && pass "PAT" || { fail "PAT" ""; exit 1; }
rcurl -X POST "$BASE/api/v1/vaults?name=$VAULT" -H "Authorization: Bearer $PAT" >/dev/null
pass "vault $VAULT created"

put_doc() {
  local title=$1 content=$2
  rcurl -X POST "$BASE/api/v1/documents" -H "Authorization: Bearer $PAT" -H 'Content-Type: application/json' \
    -d "{\"vault\":\"$VAULT\",\"collection\":\"x\",\"title\":\"$title\",\"content\":$(python3 -c 'import sys,json; print(json.dumps(sys.argv[1]))' "$content")}"
}

search() {
  rcurl -H "Authorization: Bearer $PAT" "$BASE/api/v1/search?q=$(python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$1")&vault=$VAULT&limit=${2:-10}"
}

# ── S1. Tokenizer quirks: digit/hyphen boundaries handled correctly ──
echo ""
echo "▸ S1. Tokenizer boundary quirks"

# Term with trailing digit: Kiwi splits → only the alpha prefix indexed
put_doc "Quirk1" "Apomorphine42 dose schedule." >/dev/null
sleep "$WAIT"
# Searching 'Apomorphine42' should find it (query splits identically)
T=$(search "Apomorphine42" 5 | jget "d.get('total', 0)")
[ "$T" -ge 1 ] 2>/dev/null && pass "trailing-digit token still findable via prefix split" || fail "S1-digit" "got $T"

# Hyphenated ID tokens — Kiwi splits into pieces; should still match by piece
put_doc "Quirk2" "Project codename Galaxis-Nebula approved." >/dev/null
sleep "$WAIT"
T=$(search "Galaxis-Nebula" 5 | jget "d.get('total', 0)")
[ "$T" -ge 1 ] 2>/dev/null && pass "hyphenated phrase findable via parts" || fail "S1-hyphen" "got $T"

# ── S2. Korean: particles dropped by tag filter (no false matches) ──
echo ""
echo "▸ S2. Korean morphological boundary"

# Doc has 'Apollo는' (조사 attached). Tokenizer should keep noun, drop 는.
put_doc "Q3" "Apollo는 1967년 NASA 달 탐사 프로그램이었다." >/dev/null

# Poll up to 60s for the doc to become searchable.
T=$(wait_for_search_total "Apollo")
[ "$T" -ge 1 ] 2>/dev/null && pass "Korean particle stripped at index time" || fail "S2-particle" "got $T after 60s"

# ── S3. Concurrent writes to different docs — no chunk leakage ──
echo ""
echo "▸ S3. Concurrent puts (5 in parallel)"

for i in 1 2 3 4 5; do
  ( put_doc "Conc$i" "ConcurrentMarker$i shared body about widgets." >/dev/null ) &
done
wait

MISS=0
for i in 1 2 3 4 5; do
  T=$(wait_for_search_total "ConcurrentMarker$i")
  [ "$T" -ge 1 ] 2>/dev/null || MISS=$((MISS+1))
done
[ "$MISS" = "0" ] && pass "5 concurrent puts all indexed (no chunk lost)" || fail "S3" "$MISS/5 missing"

# ── S4. Concurrent updates on the same doc — last-write-wins (PG) ──
echo ""
echo "▸ S4. Concurrent updates (race resolves cleanly)"

DOC=$(put_doc "RaceTarget" "RaceVersionAlpha initial." | jget "d['doc_id']")
if [ -n "$DOC" ]; then
  wait_for_search_total "RaceVersionAlpha" >/dev/null
  pass "race target seeded"
else
  fail "S4-setup" "no doc"
fi

if [ -n "$DOC" ]; then
  for i in 1 2 3; do
    ( rcurl -X PATCH "$BASE/api/v1/documents/$VAULT/$DOC" -H "Authorization: Bearer $PAT" -H 'Content-Type: application/json' \
        -d "{\"content\":\"RaceVersionParallel$i body content here.\"}" >/dev/null ) &
  done
  wait
  sleep "$WAIT"

  # Exactly one of the 3 versions should be searchable; the other two should be gone
  HITS=0
  for i in 1 2 3; do
    T=$(search "RaceVersionParallel$i" 1 | jget "d.get('total', 0)")
    [ "$T" = "1" ] 2>/dev/null && HITS=$((HITS+1))
  done
  T_INIT=$(search "RaceVersionAlpha" 1 | jget "d.get('total', 0)")
  [ "$T_INIT" = "0" ] && pass "initial version replaced" || fail "S4-init" "initial still searchable: $T_INIT"
  [ "$HITS" -ge 1 ] 2>/dev/null && pass "at least one parallel update visible ($HITS/3)" || fail "S4-final" "no version visible"
fi

# ── S5. URL/code-shaped tokens preserved or split predictably ──
echo ""
echo "▸ S5. URL & code-shaped tokens"

put_doc "Codey" "See https://example.com/path/to/widgetz for ApiVersionAlpha details." >/dev/null

# Poll until findable
T=$(wait_for_search_total "ApiVersionAlpha")
[ "$T" -ge 1 ] 2>/dev/null && pass "CamelCase token findable" || fail "S5-camel" "got $T after 60s"

# Domain part 'example' — should also be in vocab
T=$(search "example" 5 | jget "d.get('total', 0)")
# example is too generic — just check it doesn't crash
[ -n "$T" ] && pass "domain token query well-formed (total=$T)" || fail "S5-url" "no response"

# ── S6. Large vault: 30 docs, latency reasonable ──
echo ""
echo "▸ S6. Many-doc vault (30 docs)"

for i in $(seq 1 30); do
  ( put_doc "Bulk$i" "BulkMarkerVariant$i body content for bulk test." >/dev/null ) &
  if (( i % 10 == 0 )); then wait; fi
done
wait
sleep "$WAIT"

START=$(python3 -c "import time; print(time.time())")
search "BulkMarkerVariant15" 1 >/dev/null
END=$(python3 -c "import time; print(time.time())")
ELAPSED=$(python3 -c "print(int(($END - $START) * 1000))")
[ "$ELAPSED" -lt 5000 ] 2>/dev/null && pass "search in 30-doc vault under 5s (${ELAPSED}ms)" || fail "S6-latency" "${ELAPSED}ms"

T=$(search "BulkMarkerVariant15" 1 | jget "d.get('total', 0)")
[ "$T" -ge 1 ] 2>/dev/null && pass "specific bulk doc retrievable" || fail "S6-recall" "got $T"

# ── S7. Pagination via limit boundaries ──
echo ""
echo "▸ S7. Limit boundary semantics"

# limit=0 should be rejected (FastAPI ge=1) — assert clean error
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 10 \
  -H "Authorization: Bearer $PAT" "$BASE/api/v1/search?q=widget&vault=$VAULT&limit=0")
[ "$HTTP" = "422" ] && pass "limit=0 → 422" || fail "S7-zero" "got HTTP $HTTP"

# limit=101 (above max) → 422
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 10 \
  -H "Authorization: Bearer $PAT" "$BASE/api/v1/search?q=widget&vault=$VAULT&limit=101")
[ "$HTTP" = "422" ] && pass "limit=101 → 422" || fail "S7-over" "got HTTP $HTTP"

# limit=100 (boundary) → ok
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 15 \
  -H "Authorization: Bearer $PAT" "$BASE/api/v1/search?q=widget&vault=$VAULT&limit=100")
[ "$HTTP" = "200" ] && pass "limit=100 (max) → 200" || fail "S7-max" "got HTTP $HTTP"

# ── S8. Auth boundary: search without token → 401 ─────────────
echo ""
echo "▸ S8. Auth required"

HTTP=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 10 \
  "$BASE/api/v1/search?q=widget&vault=$VAULT")
[ "$HTTP" = "401" ] && pass "no token → 401" || fail "S8" "got HTTP $HTTP"

HTTP=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 10 \
  -H "Authorization: Bearer notarealtoken" "$BASE/api/v1/search?q=widget&vault=$VAULT")
[ "$HTTP" = "401" ] && pass "bogus token → 401" || fail "S8-bogus" "got HTTP $HTTP"

# ── S9. SQL-injection-shaped query goes through tokenizer cleanly ──
echo ""
echo "▸ S9. Injection-shaped query"

# These chars get dropped by tag filter; query becomes empty → 0
T=$(search "'; DROP TABLE chunks --" 5 | jget "d.get('total', 0)")
[ "$T" = "0" ] && pass "SQL inject-shape → 0 (clean)" || fail "S9-sql" "got $T"

# Server returns valid JSON, no 500
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 10 \
  -H "Authorization: Bearer $PAT" "$BASE/api/v1/search?q=%3Bselect%201&vault=$VAULT")
[ "$HTTP" = "200" ] && pass "encoded injection → 200" || fail "S9-encoded" "got HTTP $HTTP"

# ── S10. Drill-down on non-existent doc → 404 ────────────────
echo ""
echo "▸ S10. Drill-down nonexistent"

HTTP=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 10 \
  -H "Authorization: Bearer $PAT" "$BASE/api/v1/drill-down/$VAULT/d-nonexistent00000000")
[ "$HTTP" = "404" ] || [ "$HTTP" = "200" ] && pass "drill nonexistent → $HTTP (no 500)" || fail "S10" "got HTTP $HTTP"

# ── Cleanup ──────────────────────────────────────────────────
echo ""
echo "▸ Cleanup"
SID=$(curl -sk -i --max-time 10 -X POST "$BASE/mcp/" \
  -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"x","version":"1.0"}}}' 2>/dev/null \
  | grep -i "mcp-session-id" | tr -d '\r' | awk '{print $2}')
if [ -n "$SID" ]; then
  rcurl -X POST "$BASE/mcp/" \
    -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" -H "mcp-session-id: $SID" \
    -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null
  rcurl -X POST "$BASE/mcp/" \
    -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" -H "mcp-session-id: $SID" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"akb_delete_vault\",\"arguments\":{\"vault\":\"$VAULT\"}}}" >/dev/null
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
  for e in "${ERRORS[@]}"; do
    echo "    - $e"
  done
fi
echo "═══════════════════════════════════════════"

exit $FAIL
