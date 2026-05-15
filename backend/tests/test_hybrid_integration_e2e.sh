#!/bin/bash
#
# Hybrid search × other features integration
#
# Verifies search behaves sanely alongside the rest of the surface area:
# graph (link/unlink), publications, collection filter, multi-vault,
# memory store separation, large docs, browse alignment.
#
set -uo pipefail

BASE="${AKB_URL:-http://localhost:8000}"
USER_NAME="hybrid-int-$(date +%s)"
VAULT_A="hybrid-int-a-$(date +%s)"
VAULT_B="hybrid-int-b-$(date +%s)"
WAIT="${AKB_HYBRID_INDEX_WAIT:-25}"
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
echo "║   Hybrid × Features Integration          ║"
echo "║   Target: $BASE"
echo "╚══════════════════════════════════════════╝"
echo ""

echo "▸ Setup"
rcurl -X POST "$BASE/api/v1/auth/register" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER_NAME\",\"email\":\"$USER_NAME@t.dev\",\"password\":\"test1234\"}" >/dev/null
JWT=$(rcurl -X POST "$BASE/api/v1/auth/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER_NAME\",\"password\":\"test1234\"}" | jget "d['token']")
PAT=$(rcurl -X POST "$BASE/api/v1/auth/tokens" -H "Authorization: Bearer $JWT" -H 'Content-Type: application/json' \
  -d '{"name":"i"}' | jget "d['token']")
[ -n "$PAT" ] && pass "PAT" || { fail "PAT" ""; exit 1; }
rcurl -X POST "$BASE/api/v1/vaults?name=$VAULT_A" -H "Authorization: Bearer $PAT" >/dev/null
rcurl -X POST "$BASE/api/v1/vaults?name=$VAULT_B" -H "Authorization: Bearer $PAT" >/dev/null
pass "two vaults created"

# Helpers — vault + collection are parameters
put() {
  local vault=$1 coll=$2 title=$3 content=$4 doc_type=${5:-note} tags=${6:-[]}
  rcurl -X POST "$BASE/api/v1/documents" -H "Authorization: Bearer $PAT" -H 'Content-Type: application/json' \
    -d "{\"vault\":\"$vault\",\"collection\":\"$coll\",\"title\":\"$title\",\"content\":$(python3 -c 'import sys,json; print(json.dumps(sys.argv[1]))' "$content"),\"type\":\"$doc_type\",\"tags\":$tags}"
}
search_v() {
  local vault=$1 q=$2 extra=$3
  rcurl -H "Authorization: Bearer $PAT" "$BASE/api/v1/search?q=$(python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$q")&vault=$vault$extra"
}

# ── INT1. Collection prefix filter ───────────────────────────
echo ""
echo "▸ INT1. Collection prefix filter"

put "$VAULT_A" "alpha" "DocA1" "Wormhole topology in collection alpha." >/dev/null
put "$VAULT_A" "beta" "DocB1" "Wormhole topology in collection beta." >/dev/null
sleep "$WAIT"

# Search restricted to collection alpha — only DocA1
TITLES_A=$(search_v "$VAULT_A" "Wormhole" "&collection=alpha&limit=5" \
  | jget "'|'.join(r.get('title','') for r in d.get('results', []))")
echo "$TITLES_A" | grep -q "DocA1" && ! echo "$TITLES_A" | grep -q "DocB1" \
  && pass "collection=alpha returns A only" || fail "INT1-alpha" "got: $TITLES_A"

TITLES_B=$(search_v "$VAULT_A" "Wormhole" "&collection=beta&limit=5" \
  | jget "'|'.join(r.get('title','') for r in d.get('results', []))")
echo "$TITLES_B" | grep -q "DocB1" && ! echo "$TITLES_B" | grep -q "DocA1" \
  && pass "collection=beta returns B only" || fail "INT1-beta" "got: $TITLES_B"

# ── INT2. doc_type filter alone ──────────────────────────────
echo ""
echo "▸ INT2. doc_type filter"

put "$VAULT_A" "x" "Spec1" "Particula CalibraSentinel design notes." spec >/dev/null
put "$VAULT_A" "x" "Note1" "Particula CalibraSentinel implementation comments." note >/dev/null
sleep "$WAIT"

TITLES=$(search_v "$VAULT_A" "CalibraSentinel" "&type=spec&limit=5" \
  | jget "'|'.join(r.get('title','') for r in d.get('results', []))")
echo "$TITLES" | grep -q "Spec1" && ! echo "$TITLES" | grep -q "Note1" \
  && pass "type=spec filter works" || fail "INT2" "got: $TITLES"

# ── INT3. Tags array intersect filter ────────────────────────
echo ""
echo "▸ INT3. Tags filter (array intersect)"

put "$VAULT_A" "x" "Tagged1" "AthenaCalliope library notes." note '["library","priority-high"]' >/dev/null
put "$VAULT_A" "x" "Tagged2" "AthenaCalliope production notes." note '["production"]' >/dev/null
sleep "$WAIT"

TITLES=$(rcurl -H "Authorization: Bearer $PAT" \
  "$BASE/api/v1/search?q=AthenaCalliope&vault=$VAULT_A&tags=priority-high&limit=5" \
  | jget "'|'.join(r.get('title','') for r in d.get('results', []))")
echo "$TITLES" | grep -q "Tagged1" && ! echo "$TITLES" | grep -q "Tagged2" \
  && pass "tags=priority-high isolates" || fail "INT3" "got: $TITLES"

# ── INT4. Cross-vault search by same user (no vault filter) ──
echo ""
echo "▸ INT4. Cross-vault search (own vaults only)"

put "$VAULT_B" "x" "BVaultDoc" "RhetoricaProsodyMix in vault B." >/dev/null
sleep "$WAIT"

R=$(rcurl -H "Authorization: Bearer $PAT" "$BASE/api/v1/search?q=RhetoricaProsodyMix&limit=5")
T=$(echo "$R" | jget "d.get('total', 0)")
[ "$T" -ge 1 ] 2>/dev/null && pass "owner finds doc in vault B (no vault filter)" || fail "INT4" "got $T"

# Should find from vault B specifically
V=$(echo "$R" | jget "d.get('results', [{}])[0].get('vault','')")
[ "$V" = "$VAULT_B" ] && pass "result vault is $VAULT_B" || fail "INT4-vault" "got '$V'"

# ── INT5. akb_link/unlink does NOT affect search results ─────
echo ""
echo "▸ INT5. Knowledge graph link doesn't pollute search"

D1_RESP=$(put "$VAULT_A" "x" "LinkSrc" "OstrichBeakRatio measurement.")
D2_RESP=$(put "$VAULT_A" "x" "LinkDst" "OstrichBeakRatio reference doc.")
D1_PATH=$(echo "$D1_RESP" | jget "d['path']")
D2_PATH=$(echo "$D2_RESP" | jget "d['path']")
D1_URI="akb://$VAULT_A/doc/$D1_PATH"
D2_URI="akb://$VAULT_A/doc/$D2_PATH"
sleep "$WAIT"

# Link via MCP
SID=$(curl -sk -i --max-time 10 -X POST "$BASE/mcp/" \
  -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"x","version":"1.0"}}}' 2>/dev/null \
  | grep -i "mcp-session-id" | tr -d '\r' | awk '{print $2}')
if [ -n "$SID" ]; then
  rcurl -X POST "$BASE/mcp/" -H "Authorization: Bearer $PAT" \
    -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
    -H "mcp-session-id: $SID" \
    -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null
  rcurl -X POST "$BASE/mcp/" -H "Authorization: Bearer $PAT" \
    -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
    -H "mcp-session-id: $SID" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"akb_link\",\"arguments\":{\"source\":\"$D1_URI\",\"target\":\"$D2_URI\",\"relation\":\"depends_on\"}}}" >/dev/null
fi
sleep 3

# Both linked docs must appear in search; total may exceed 2 because
# hybrid RRF fuses dense neighbors. Check both linked docs present.
TITLES=$(search_v "$VAULT_A" "OstrichBeakRatio" "&limit=20" \
  | jget "'|'.join(r.get('title','') for r in d.get('results', []))")
echo "$TITLES" | grep -q "LinkSrc" && echo "$TITLES" | grep -q "LinkDst" \
  && pass "both linked docs appear in search results" || fail "INT5" "missing in: $TITLES"

# ── INT6. Memory store does NOT leak into document search ────
echo ""
echo "▸ INT6. memory store separation from chunks"

# Put a memory entry via MCP
if [ -n "$SID" ]; then
  rcurl -X POST "$BASE/mcp/" -H "Authorization: Bearer $PAT" \
    -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
    -H "mcp-session-id: $SID" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"tools/call\",\"params\":{\"name\":\"akb_remember\",\"arguments\":{\"category\":\"trivia\",\"text\":\"PinguinValkyrieMarker memory entry only.\"}}}" >/dev/null
fi
sleep 3

# Memory shouldn't appear in search results (it's its own table)
T=$(search_v "$VAULT_A" "PinguinValkyrieMarker" "&limit=10" | jget "d.get('total', 0)")
[ "$T" = "0" ] && pass "memory entry not in document search" || fail "INT6" "got $T (leaked!)"

# ── INT7. Large doc (~10KB) chunks correctly + searchable ────
echo ""
echo "▸ INT7. Large doc (10KB) handles many chunks"

LARGE=$(python3 -c "
sections = []
for i in range(8):
    body = f'## Section {i}\n\n' + 'lorem ipsum '*200
    sections.append(body)
sections.insert(3, '## Marker section\n\nQuixotePalindromeMagnetar specific marker here.')
print('\n\n'.join(sections))
")
R=$(put "$VAULT_A" "x" "BigDoc" "$LARGE")
N=$(echo "$R" | jget "d.get('chunks_indexed', 0)")
[ "$N" -ge 5 ] 2>/dev/null && pass "10KB doc → $N chunks" || fail "INT7-chunks" "got $N"

sleep "$WAIT"
T=$(search_v "$VAULT_A" "QuixotePalindromeMagnetar" "&limit=3" | jget "d.get('total', 0)")
[ "$T" -ge 1 ] 2>/dev/null && pass "marker inside large doc retrievable" || fail "INT7-recall" "got $T"

# ── INT8. Numeric-only query → returns 0 if not in any chunk ──
echo ""
echo "▸ INT8. Numeric token query"

put "$VAULT_A" "x" "Numero" "WidgetSerial 47823 calibration log." >/dev/null
sleep "$WAIT"

# 47823 is a unique digit-only token → vocab has it → finds doc
T=$(search_v "$VAULT_A" "47823" "&limit=3" | jget "d.get('total', 0)")
[ "$T" -ge 1 ] 2>/dev/null && pass "specific digit token findable" || fail "INT8" "got $T"

# Nonexistent digit
T=$(search_v "$VAULT_A" "99999999999" "&limit=3" | jget "d.get('total', 0)")
[ "$T" = "0" ] && pass "nonexistent digit → 0" || fail "INT8-miss" "got $T"

# ── INT9. Browse + search list same documents ────────────────
echo ""
echo "▸ INT9. Browse/search agreement"

# Browse the vault root → count distinct doc_ids reachable. Compare with
# search total to ensure search never exceeds the corpus size.
B_COUNT=$(rcurl -H "Authorization: Bearer $PAT" "$BASE/api/v1/browse/$VAULT_A?depth=2" \
  | jget "len([i for i in d.get('items', []) if i.get('type')=='document'])")
echo "    browse distinct docs: $B_COUNT"

S_COUNT=$(rcurl -H "Authorization: Bearer $PAT" "$BASE/api/v1/search?q=marker&vault=$VAULT_A&limit=50" \
  | jget "d.get('total', 0)")
echo "    search 'marker' total: $S_COUNT"
[ "$S_COUNT" -le "$B_COUNT" ] 2>/dev/null && pass "search ($S_COUNT) ≤ browse ($B_COUNT)" || fail "INT9" "search=$S_COUNT > browse=$B_COUNT"

# ── INT10. akb_grep + akb_search both find the same unique term ──
echo ""
echo "▸ INT10. grep & search both find a unique term"

put "$VAULT_A" "x" "Twin" "OcarinaZephyrCascade marker for both finders." >/dev/null
sleep "$WAIT"

S=$(search_v "$VAULT_A" "OcarinaZephyrCascade" "&limit=3" | jget "d.get('total', 0)")
G=$(rcurl -H "Authorization: Bearer $PAT" \
  "$BASE/api/v1/grep?q=OcarinaZephyrCascade&vault=$VAULT_A" \
  | jget "d.get('total_docs', 0)")
[ "$S" -ge 1 ] 2>/dev/null && [ "$G" = "1" ] 2>/dev/null \
  && pass "search($S)+grep($G) both find unique marker" || fail "INT10" "S=$S G=$G"

# ── Cleanup ──────────────────────────────────────────────────
echo ""
echo "▸ Cleanup"
if [ -n "$SID" ]; then
  for V in "$VAULT_A" "$VAULT_B"; do
    rcurl -X POST "$BASE/mcp/" -H "Authorization: Bearer $PAT" \
      -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
      -H "mcp-session-id: $SID" \
      -d "{\"jsonrpc\":\"2.0\",\"id\":99,\"method\":\"tools/call\",\"params\":{\"name\":\"akb_delete_vault\",\"arguments\":{\"vault\":\"$V\"}}}" >/dev/null
  done
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
