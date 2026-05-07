#!/bin/bash
#
# Hybrid search deep invariants — REST-only for stability.
# (MCP session has a short TTL; long-running invariant probes don't fit it.)
#
set -uo pipefail

BASE="${AKB_URL:-http://localhost:8000}"
USER_NAME="hybrid-inv-$(date +%s)"
VAULT="hybrid-inv-$(date +%s)"
WAIT="${AKB_HYBRID_INDEX_WAIT:-25}"
PASS=0
FAIL=0
ERRORS=()

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

# Resilient curl: 5 retries on empty body, 15s max each. Returns body on stdout.
rcurl() {
  local out=""
  for _ in 1 2 3 4 5; do
    out=$(curl -sk --max-time 15 "$@" 2>/dev/null)
    if [ -n "$out" ]; then echo "$out"; return 0; fi
    sleep 2
  done
  echo ""
  return 1
}

jget() { python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print($1)" 2>/dev/null; }

# psql with retry on empty output
psql_q() {
  local sql=$1
  local out=""
  for _ in 1 2 3; do
    out=$(kubectl exec -n akb statefulset/postgres -- psql -U akbuser -d akb -t -c "$sql" 2>/dev/null | tr -d ' \n')
    if [ -n "$out" ]; then echo "$out"; return 0; fi
    sleep 2
  done
  echo ""
  return 1
}

# Wait until a PG query returns non-empty, up to 60s.
wait_for_psql() {
  local sql=$1
  local deadline=$(($(date +%s) + 60))
  while [ $(date +%s) -lt $deadline ]; do
    local out=$(psql_q "$sql")
    if [ -n "$out" ]; then echo "$out"; return 0; fi
    sleep 3
  done
  echo ""
  return 1
}

# Wait until search returns total>=1 (max 60s).
wait_for_search() {
  local q=$1 vault=$2
  local deadline=$(($(date +%s) + 60))
  while [ $(date +%s) -lt $deadline ]; do
    local t=$(rcurl -H "Authorization: Bearer $PAT" "$BASE/api/v1/search?q=$(python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$q")&vault=$vault&limit=5" | jget "d.get('total', 0)")
    if [ -n "$t" ] && [ "$t" != "0" ]; then echo "$t"; return 0; fi
    sleep 3
  done
  echo "0"
  return 1
}

echo "╔══════════════════════════════════════════╗"
echo "║   Hybrid Search Invariants               ║"
echo "║   Target: $BASE"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── Setup ────────────────────────────────────────────────────
echo "▸ Setup"
rcurl -X POST "$BASE/api/v1/auth/register" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER_NAME\",\"email\":\"$USER_NAME@t.dev\",\"password\":\"test1234\"}" >/dev/null
JWT=$(rcurl -X POST "$BASE/api/v1/auth/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER_NAME\",\"password\":\"test1234\"}" | jget "d['token']")
PAT=$(rcurl -X POST "$BASE/api/v1/auth/tokens" -H "Authorization: Bearer $JWT" -H 'Content-Type: application/json' \
  -d '{"name":"hi"}' | jget "d['token']")
[ -n "$PAT" ] && pass "PAT" || { fail "PAT" "no token"; exit 1; }

put_doc() {
  local title=$1 content=$2
  # rcurl already retries on empty body; add extra retry when doc_id missing
  # (e.g. transient 5xx returning a stringified error instead of put response).
  for _ in 1 2 3; do
    local resp=$(rcurl -X POST "$BASE/api/v1/documents" -H "Authorization: Bearer $PAT" -H 'Content-Type: application/json' \
      -d "{\"vault\":\"$VAULT\",\"collection\":\"x\",\"title\":\"$title\",\"content\":$(python3 -c 'import sys,json; print(json.dumps(sys.argv[1]))' "$content")}")
    local id=$(echo "$resp" | jget "d.get('doc_id','')")
    if [ -n "$id" ]; then echo "$resp"; return 0; fi
    sleep 3
  done
  echo "$resp"
  return 1
}
search() { rcurl -H "Authorization: Bearer $PAT" "$BASE/api/v1/search?q=$(python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$1")&vault=$VAULT&limit=${2:-10}"; }

rcurl -X POST "$BASE/api/v1/vaults?name=$VAULT" -H "Authorization: Bearer $PAT" >/dev/null
pass "vault $VAULT created"

# ── I1. term_id stability across recompute_stats ─────────────
echo ""
echo "▸ I1. term_id stable across recompute_stats"

# Single all-alpha lowercased token (no digits — Kiwi splits at digit boundary)
HEX=$(python3 -c "import secrets; print(''.join(secrets.choice('abcdef') for _ in range(12)))")
PROBE="stabprobe$HEX"
put_doc "Stab" "$PROBE marker." >/dev/null

# Poll until the term appears in vocab (up to 60s).
ID_BEFORE=$(wait_for_psql "SELECT term_id FROM bm25_vocab WHERE term='$PROBE'")
[ -n "$ID_BEFORE" ] && pass "term registered (id=$ID_BEFORE)" || fail "I1-register" "term '$PROBE' missing after 60s"

if [ -n "$ID_BEFORE" ]; then
  kubectl exec -n akb deploy/backend -- python -m scripts.init_bm25_vocab >/tmp/recompute.log 2>&1
  ID_AFTER=$(psql_q "SELECT term_id FROM bm25_vocab WHERE term='$PROBE'")
  [ "$ID_BEFORE" = "$ID_AFTER" ] && pass "term_id stable across recompute ($ID_BEFORE)" || fail "I1-mutated" "id changed: $ID_BEFORE → $ID_AFTER"
fi

# ── I2. PG indexed count ≈ vector_store indexed count ──────────────
echo ""
echo "▸ I2. PG vs vector_store indexed count"
PG_N=$(psql_q "SELECT COUNT(*) FROM chunks WHERE vector_indexed_at IS NOT NULL")
QD_N=$(rcurl "$BASE/health" | jget "d['vector_store']['backfill']['upsert']['indexed']")
if [ -n "$PG_N" ] && [ -n "$QD_N" ]; then
  DIFF=$(python3 -c "print(abs(int($PG_N) - int($QD_N)))")
  [ "$DIFF" -lt 100 ] && pass "pg=$PG_N vector_store=$QD_N (Δ$DIFF)" || fail "I2" "pg=$PG_N vector_store=$QD_N diff=$DIFF"
else
  fail "I2" "got pg='$PG_N' qd='$QD_N'"
fi

# ── I3. Empty-body doc → handled cleanly (no crash) ──────────
# The chunker may produce 0 or 1 chunk depending on heading presence.
# Either is fine; what matters is the put doesn't 500.
echo ""
echo "▸ I3. Empty-body doc handled cleanly"
R=$(put_doc "BlankBody" "")
N=$(echo "$R" | jget "d.get('chunks_indexed', 'err')")
[ "$N" != "err" ] && [ -n "$N" ] && pass "empty body returns chunks_indexed=$N" || fail "I3" "no chunks_indexed in response=$R"

# ── I4. ACL — non-grantee can't see private content ──────────
echo ""
echo "▸ I4. ACL cross-user isolation"
OTHER="other-$(date +%s)"
rcurl -X POST "$BASE/api/v1/auth/register" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$OTHER\",\"email\":\"$OTHER@t.dev\",\"password\":\"test1234\"}" >/dev/null
OJWT=$(rcurl -X POST "$BASE/api/v1/auth/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$OTHER\",\"password\":\"test1234\"}" | jget "d['token']")
OPAT=$(rcurl -X POST "$BASE/api/v1/auth/tokens" -H "Authorization: Bearer $OJWT" -H 'Content-Type: application/json' \
  -d '{"name":"o"}' | jget "d['token']")

put_doc "ACLProbe" "PrivataMargentum specific marker for ACL test." >/dev/null

# Poll for search hit (up to 60s) instead of fixed sleep.
OWNER_T=$(wait_for_search "PrivataMargentum" "$VAULT")
[ "$OWNER_T" -ge 1 ] 2>/dev/null && pass "owner sees own doc (total=$OWNER_T)" || fail "I4-owner" "got $OWNER_T after 60s"

OTHER_HITS=$(rcurl -H "Authorization: Bearer $OPAT" "$BASE/api/v1/search?q=PrivataMargentum&limit=10" \
  | jget "len([r for r in d.get('results',[]) if 'PrivataMargentum' in (r.get('matched_section') or '')])")
[ "$OTHER_HITS" = "0" ] && pass "other user does NOT see private content" || fail "I4-leak" "got $OTHER_HITS hits"

# ── I5. Sync delete propagation ──────────────────────────────
echo ""
echo "▸ I5. Sync-delete: post-delete search returns 0"
DI=$(put_doc "DelInv" "FluffaXylophone marker text for delete test." | jget "d['doc_id']")
[ -n "$DI" ] && pass "del-target put returned id" || fail "I5-setup" "no doc_id"

if [ -n "$DI" ]; then
  # Wait until it's searchable before we delete (poll up to 60s).
  wait_for_search "FluffaXylophone" "$VAULT" >/dev/null
  rcurl -X DELETE "$BASE/api/v1/documents/$VAULT/$DI" -H "Authorization: Bearer $PAT" >/dev/null
  sleep 3
  T=$(search "FluffaXylophone" 5 | jget "d.get('total', 0)")
  [ "$T" = "0" ] && pass "post-delete returns 0" || fail "I5" "still $T"
fi

# ── I6. Determinism: identical query → identical result SET ──
# Order may swap between equal-score results (RRF ties); we only assert
# the SET of returned doc_ids is identical across calls.
echo ""
echo "▸ I6. Search determinism (set equality)"
put_doc "DetermA" "GrobnitzPlasma alpha node." >/dev/null
put_doc "DetermB" "GrobnitzPlasma beta node related." >/dev/null
wait_for_search "GrobnitzPlasma" "$VAULT" >/dev/null

R1=$(search "GrobnitzPlasma" 5 | jget "','.join(sorted(r['doc_id'] for r in d.get('results', [])))")
R2=$(search "GrobnitzPlasma" 5 | jget "','.join(sorted(r['doc_id'] for r in d.get('results', [])))")
[ "$R1" = "$R2" ] && [ -n "$R1" ] && pass "identical doc_id set" || fail "I6" "R1='$R1' R2='$R2'"

# ── I7. Score determinism + monotonic ranking ────────────────
echo ""
echo "▸ I7. Score determinism + monotonic"
S1=$(search "GrobnitzPlasma" 1 | jget "d['results'][0]['score'] if d.get('results') else ''")
S2=$(search "GrobnitzPlasma" 1 | jget "d['results'][0]['score'] if d.get('results') else ''")
[ "$S1" = "$S2" ] && [ -n "$S1" ] && pass "top score stable ($S1)" || fail "I7-stable" "S1=$S1 S2=$S2"

SCORES=$(search "GrobnitzPlasma" 5 | jget "','.join(str(r['score']) for r in d.get('results', []))")
MONO=$(python3 -c "
xs = [float(x) for x in '$SCORES'.split(',') if x]
print('ok' if (len(xs) <= 1 or all(xs[i] >= xs[i+1] for i in range(len(xs)-1))) else 'bad')
")
[ "$MONO" = "ok" ] && pass "scores non-increasing ($SCORES)" || fail "I7-monotonic" "$SCORES"

# ── I8. Vocab is append-only across delete ───────────────────
echo ""
echo "▸ I8. Vocab append-only"
V_BEFORE=$(rcurl "$BASE/health" | jget "d['vector_store']['bm25_vocab_size']")
HEX2=$(python3 -c "import secrets; print(''.join(secrets.choice('abcdef') for _ in range(10)))")
TMP_TOKEN="vocabbump$HEX2"
TMP_ID=$(put_doc "Tmp" "$TMP_TOKEN content body." | jget "d['doc_id']")
sleep "$WAIT"
V_MID=$(rcurl "$BASE/health" | jget "d['vector_store']['bm25_vocab_size']")
[ -n "$TMP_ID" ] && rcurl -X DELETE "$BASE/api/v1/documents/$VAULT/$TMP_ID" -H "Authorization: Bearer $PAT" >/dev/null
sleep 3
V_AFTER=$(rcurl "$BASE/health" | jget "d['vector_store']['bm25_vocab_size']")
[ "$V_AFTER" -ge "$V_MID" ] 2>/dev/null && pass "vocab did not shrink ($V_BEFORE → $V_MID → $V_AFTER)" || fail "I8" "shrank: $V_MID → $V_AFTER"

# ── I9. Index recovery: marking vector_indexed_at=NULL ───────
echo ""
echo "▸ I9. Force-reindex doesn't break search"
put_doc "RecovA" "ReindexProbeAlpha content section." >/dev/null
sleep "$WAIT"
kubectl exec -n akb statefulset/postgres -- psql -U akbuser -d akb -c "
  UPDATE chunks SET vector_indexed_at = NULL, vector_next_attempt_at = NOW(), vector_retry_count = 0
   WHERE document_id IN (SELECT d.id FROM documents d JOIN vaults v ON d.vault_id=v.id WHERE v.name='$VAULT')
" >/dev/null 2>&1
# Search must keep working — the chunks are still in the vector store from prior upsert.
T=$(search "ReindexProbeAlpha" 1 | jget "d.get('total', 0)")
[ "$T" -ge 1 ] 2>/dev/null && pass "search functional immediately after reindex flag" || fail "I9" "got $T"

# ── I10. /health stability under load ────────────────────────
echo ""
echo "▸ I10. /health 5 consecutive calls all succeed"
OK=0
for _ in 1 2 3 4 5; do
  rcurl "$BASE/health" | jget "d['status']" | grep -q ok && OK=$((OK+1))
done
[ "$OK" = "5" ] && pass "/health 5/5" || fail "I10" "only $OK/5 OK"

# ── Cleanup ──────────────────────────────────────────────────
echo ""
echo "▸ Cleanup"
# Use MCP for vault delete (REST has no vault-delete endpoint)
rcurl -X POST "$BASE/mcp/" \
  -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2025-03-26\",\"capabilities\":{},\"clientInfo\":{\"name\":\"cleanup\",\"version\":\"1.0\"}}}" >/dev/null
SID2=$(curl -sk -i --max-time 10 -X POST "$BASE/mcp/" \
  -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2025-03-26\",\"capabilities\":{},\"clientInfo\":{\"name\":\"x\",\"version\":\"1.0\"}}}" 2>/dev/null \
  | grep -i "mcp-session-id" | tr -d '\r' | awk '{print $2}')
if [ -n "$SID2" ]; then
  rcurl -X POST "$BASE/mcp/" \
    -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" -H "mcp-session-id: $SID2" \
    -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null
  rcurl -X POST "$BASE/mcp/" \
    -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" -H "mcp-session-id: $SID2" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"akb_delete_vault\",\"arguments\":{\"vault\":\"$VAULT\"}}}" >/dev/null
fi

# Self-delete test user to avoid accumulating in DB
curl -sk --max-time 15 -X DELETE "$BASE/api/v1/my/account" -H "Authorization: Bearer $JWT" >/dev/null 2>&1
pass "cleanup attempted"

# ── Summary ──────────────────────────────────────────────────
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
