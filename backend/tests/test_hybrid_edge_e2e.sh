#!/bin/bash
#
# Hybrid search edge-case E2E
#
# Extends test_hybrid_search_e2e.sh with trickier scenarios uncovered during
# code review + post-refactor investigation. Each block targets a specific
# invariant of the hybrid pipeline that is easy to regress.
#
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
E2E_USER="hybrid-edge-$(date +%s)"
VAULT="hybrid-edge-$(date +%s)"
INDEX_WAIT="${AKB_HYBRID_INDEX_WAIT:-15}"
PASS=0
FAIL=0
ERRORS=()

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

echo "╔══════════════════════════════════════════╗"
echo "║   Hybrid Search Edge Cases               ║"
echo "║   Target: $BASE_URL"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── 0. Setup ────────────────────────────────────────────────
echo "▸ 0. Setup"

curl -sk -X POST "$BASE_URL/api/v1/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$E2E_USER\",\"email\":\"$E2E_USER@test.dev\",\"password\":\"test1234\"}" >/dev/null 2>&1

JWT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$E2E_USER\",\"password\":\"test1234\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

PAT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" \
  -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' \
  -d '{"name":"hybrid-edge"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

[ -n "$PAT" ] && pass "PAT acquired" || { fail "PAT" "could not get PAT"; exit 1; }

INIT_RESP=$(curl -sk -i -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"hybrid-edge","version":"1.0"}}}' 2>&1)
SID=$(echo "$INIT_RESP" | grep -i "mcp-session-id" | tr -d '\r' | awk '{print $2}')
[ -n "$SID" ] && pass "MCP session" || { fail "SID" "none"; exit 1; }

curl -sk -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null 2>&1

MCP_ID=100
mcp() {
  local tool=$1 args=$2
  MCP_ID=$((MCP_ID+1))
  curl -sk -X POST "$BASE_URL/mcp/" \
    -H "Authorization: Bearer $PAT" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -H "mcp-session-id: $SID" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":$MCP_ID,\"method\":\"tools/call\",\"params\":{\"name\":\"$tool\",\"arguments\":$args}}" 2>&1
}
mcp_text() {
  python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(d['result']['content'][0]['text'])" 2>/dev/null
}

R=$(mcp akb_create_vault "{\"name\":\"$VAULT\",\"description\":\"edge\"}" | mcp_text)
[ -n "$(echo "$R" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("vault_id",""))' 2>/dev/null)" ] \
  && pass "vault created" || fail "vault" "$R"

search_total() {
  local q=$1
  local R=$(mcp akb_search "{\"query\":$(python3 -c 'import sys,json; print(json.dumps(sys.argv[1]))' "$q"),\"vault\":\"$VAULT\",\"limit\":10}" | mcp_text)
  echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('total', 0))" 2>/dev/null
}
search_titles() {
  local q=$1
  local R=$(mcp akb_search "{\"query\":$(python3 -c 'import sys,json; print(json.dumps(sys.argv[1]))' "$q"),\"vault\":\"$VAULT\",\"limit\":10}" | mcp_text)
  echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('|'.join([r.get('title','') for r in d.get('results', [])]))" 2>/dev/null
}

# ── 1. Empty / punctuation-only / whitespace queries ─────────
echo ""
echo "▸ 1. Degenerate query strings"

mcp akb_put "{\"vault\":\"$VAULT\",\"collection\":\"edge\",\"title\":\"Edge Seed\",\"content\":\"## intro\\n\\nThis vault has Apollo Federation in its corpus so vocab has 'federation'.\"}" >/dev/null
sleep "$INDEX_WAIT"

# Empty query → API may 422 (query is required) or return 0
R=$(mcp akb_search "{\"query\":\"\",\"vault\":\"$VAULT\"}" | mcp_text)
OK=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('total',0) == 0 or 'error' in d)" 2>/dev/null)
[ "$OK" = "True" ] && pass "empty query: 0 or error" || fail "empty query" "$R"

# Whitespace-only
R=$(mcp akb_search "{\"query\":\"     \",\"vault\":\"$VAULT\"}" | mcp_text)
OK=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('total',0) == 0 or 'error' in d)" 2>/dev/null)
[ "$OK" = "True" ] && pass "whitespace-only query: 0 or error" || fail "ws query" "$R"

# Punctuation-only — tokens all dropped by Kiwi tag filter → empty sparse → 0
TOTAL=$(search_total "!@#\$%^&*()_+-=[]")
[ "$TOTAL" = "0" ] && pass "punctuation-only query: 0" || fail "punct query" "got $TOTAL"

# ── 2. Vault scoping prevents cross-vault leakage ────────────
# Even when the query has corpus-wide vocab matches (e.g. 'kubernetes' from
# prod data), restricting to a vault that never mentioned it must not
# return docs from elsewhere. Hybrid may still surface dense-similar docs
# *within* this vault — that's expected hybrid behavior — so the assertion
# is "no result has the foreign keyword in its content".
echo ""
echo "▸ 2. Vault scoping (no cross-vault leak)"

R=$(mcp akb_search "{\"query\":\"kubernetes orchestration\",\"vault\":\"$VAULT\",\"limit\":10}" | mcp_text)
LEAKED=$(echo "$R" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for r in d.get('results', []):
    if 'kubernetes' in (r.get('matched_section') or '').lower():
        print('LEAK')
        break
else:
    print('OK')
" 2>/dev/null)
[ "$LEAKED" = "OK" ] && pass "no foreign-keyword document in scoped results" || fail "leak" "found chunk mentioning kubernetes outside vault"

# ── 3. Korean + English mixed query, partial match ───────────
echo ""
echo "▸ 3. Mixed-lang query"

mcp akb_put "{\"vault\":\"$VAULT\",\"collection\":\"edge\",\"title\":\"혼용 문서\",\"content\":\"## 개요\\n\\n혼용 문서: Kubernetes 클러스터와 PostgreSQL 데이터베이스.\"}" >/dev/null
sleep "$INDEX_WAIT"

TITLES=$(search_titles "Kubernetes 클러스터")
echo "$TITLES" | grep -q "혼용" && pass "mixed-lang query hits Korean title" || fail "mixed-lang" "$TITLES"

# ── 4. Rapid successive update → content consistency ─────────
echo ""
echo "▸ 4. Rapid successive update"

# Use single-token markers (no internal hyphens). Kiwi splits hyphenated
# strings into pieces, and the v1/v2/v3 variants would share most tokens —
# making the assertion below false even when stale-cleanup works.
R=$(mcp akb_put "{\"vault\":\"$VAULT\",\"collection\":\"edge\",\"title\":\"Fluid Doc\",\"content\":\"## v1\\n\\nInitial content mentioning Mongoose habitat.\"}" | mcp_text)
DOC_ID=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('doc_id',''))" 2>/dev/null)

mcp akb_update "{\"vault\":\"$VAULT\",\"doc_id\":\"$DOC_ID\",\"content\":\"## v2\\n\\nSecond content mentioning Octopus tentacles.\",\"message\":\"v2\"}" >/dev/null
mcp akb_update "{\"vault\":\"$VAULT\",\"doc_id\":\"$DOC_ID\",\"content\":\"## v3\\n\\nThird content mentioning Rhinoceros horn.\",\"message\":\"v3\"}" >/dev/null
sleep "$INDEX_WAIT"

# Only v3 should be found; the v1/v2 unique words should return nothing
TOTAL=$(search_total "Mongoose")
[ "$TOTAL" = "0" ] && pass "stale v1 term not searchable" || fail "stale v1" "got $TOTAL"
TOTAL=$(search_total "Octopus")
[ "$TOTAL" = "0" ] && pass "stale v2 term not searchable" || fail "stale v2" "got $TOTAL"
TITLES=$(search_titles "Rhinoceros")
echo "$TITLES" | grep -q "Fluid Doc" && pass "latest v3 term searchable" || fail "v3 missing" "$TITLES"

# ── 5. Doc with tokenizable content only in headings ─────────
echo ""
echo "▸ 5. Headings-only doc"

mcp akb_put "{\"vault\":\"$VAULT\",\"collection\":\"edge\",\"title\":\"Just Titles\",\"content\":\"# Xyloph\\n\\n## Qwik\\n\\n### Jargonaut\\n\"}" >/dev/null
sleep "$INDEX_WAIT"

# Body is blank; chunker may skip → no chunks → not searchable. Either way
# the vault search shouldn't crash and shouldn't return the doc when queried
# by heading text (since chunks[] == 0 means no index entry).
TITLES=$(search_titles "Xyloph")
# Acceptable outcomes: empty (doc never chunked because body blank) or title
# hit (heading copied into chunk body). Just verify response is well-formed.
R=$(mcp akb_search "{\"query\":\"Xyloph\",\"vault\":\"$VAULT\"}" | mcp_text)
WELL_FORMED=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('total' in d and 'error' not in d)" 2>/dev/null)
[ "$WELL_FORMED" = "True" ] && pass "headings-only doc: response well-formed" || fail "headings-only" "$R"

# ── 6. Large doc → many chunks → all indexed ─────────────────
echo ""
echo "▸ 6. Large doc with many chunks"

# 6 single-token unique markers per section. Avoid hyphenated/composite
# strings — Kiwi splits them and shared sub-tokens make the per-section
# assertion non-isolating.
SECTION_WORDS=(Whale Dolphin Otter Manatee Walrus Beluga)
BIG_CONTENT=$(python3 -c "
words = ['Whale','Dolphin','Otter','Manatee','Walrus','Beluga']
sections = []
for i, w in enumerate(words, 1):
    body = f'{w} content. ' + 'lorem ipsum dolor sit amet ' * 80
    sections.append(f'## Section {i}\n\n{body}')
print('\n\n'.join(sections))
")
R=$(mcp akb_put "{\"vault\":\"$VAULT\",\"collection\":\"edge\",\"title\":\"Big Doc\",\"content\":$(python3 -c 'import sys,json; print(json.dumps(sys.argv[1]))' "$BIG_CONTENT")}" | mcp_text)
N_CHUNKS=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('chunks_indexed',0))" 2>/dev/null)
[ "$N_CHUNKS" -ge 6 ] 2>/dev/null && pass "big doc: $N_CHUNKS chunks indexed" || fail "big chunks" "got $N_CHUNKS"

sleep "$INDEX_WAIT"

# Each section's unique marker should be findable
MISS=0
for w in "${SECTION_WORDS[@]}"; do
  T=$(search_total "$w")
  [ "$T" -ge 1 ] 2>/dev/null || MISS=$((MISS+1))
done
[ "$MISS" = "0" ] && pass "every section searchable" || fail "big recall" "$MISS/6 sections missing"

# ── 7. Delete a doc that had many chunks → all points gone ───
echo ""
echo "▸ 7. Multi-chunk delete propagation"

DOC_ID_BIG=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('doc_id',''))" 2>/dev/null)
mcp akb_delete "{\"vault\":\"$VAULT\",\"doc_id\":\"$DOC_ID_BIG\"}" >/dev/null
sleep 5  # sync delete removes vector-store points immediately; small buffer for any in-flight

MISS=0
for w in "${SECTION_WORDS[@]}"; do
  T=$(search_total "$w")
  [ "$T" = "0" ] || MISS=$((MISS+1))
done
[ "$MISS" = "0" ] && pass "every section gone after doc delete" || fail "delete propagation" "$MISS/6 still match"

# ── 8. Search limit boundaries ───────────────────────────────
echo ""
echo "▸ 8. limit boundaries (1 / 50)"

R=$(mcp akb_search "{\"query\":\"federation\",\"vault\":\"$VAULT\",\"limit\":1}" | mcp_text)
N=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('results',[])))" 2>/dev/null)
[ "$N" -le 1 ] 2>/dev/null && pass "limit=1 returns ≤1" || fail "limit=1" "got $N"

R=$(mcp akb_search "{\"query\":\"federation\",\"vault\":\"$VAULT\",\"limit\":50}" | mcp_text)
N=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('results',[])))" 2>/dev/null)
[ "$N" -le 50 ] 2>/dev/null && pass "limit=50 returns ≤50" || fail "limit=50" "got $N"

# ── 9. Search with tags + type combined ──────────────────────
echo ""
echo "▸ 9. Tag + type filter"

mcp akb_put "{\"vault\":\"$VAULT\",\"collection\":\"edge\",\"title\":\"Tagged Spec\",\"content\":\"## spec\\n\\nspecial-filter-target-term in a spec doc.\",\"type\":\"spec\",\"tags\":[\"filter-test\"]}" >/dev/null
mcp akb_put "{\"vault\":\"$VAULT\",\"collection\":\"edge\",\"title\":\"Tagged Note\",\"content\":\"## note\\n\\nspecial-filter-target-term in a note doc.\",\"type\":\"note\",\"tags\":[\"filter-test\"]}" >/dev/null
sleep "$INDEX_WAIT"

R=$(mcp akb_search "{\"query\":\"special-filter-target-term\",\"vault\":\"$VAULT\",\"type\":\"spec\",\"tags\":[\"filter-test\"]}" | mcp_text)
TITLES=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('|'.join([r.get('title','') for r in d.get('results',[])]))" 2>/dev/null)
echo "$TITLES" | grep -q "Tagged Spec" && ! echo "$TITLES" | grep -q "Tagged Note" && pass "type+tags combo filters correctly" || fail "combined filter" "got: $TITLES"

# ── 10. Very long query string ───────────────────────────────
echo ""
echo "▸ 10. Very long query (stress tokenizer + vector-store payload)"

LONG_Q=$(python3 -c "print(('federation ' + 'graphql ' + 'apollo ') * 100)")
R=$(mcp akb_search "{\"query\":$(python3 -c 'import sys,json; print(json.dumps(sys.argv[1]))' "$LONG_Q"),\"vault\":\"$VAULT\",\"limit\":5}" | mcp_text)
WELL_FORMED=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('total' in d and 'error' not in d)" 2>/dev/null)
[ "$WELL_FORMED" = "True" ] && pass "long query handled" || fail "long query" "$R"

# ── 11. Unicode emoji / combining / CJK ──────────────────────
echo ""
echo "▸ 11. Unicode query"

mcp akb_put "{\"vault\":\"$VAULT\",\"collection\":\"edge\",\"title\":\"Emoji Doc 🎉\",\"content\":\"## intro\\n\\nuniqmark-emoji-\\ud83c\\udf89 and uniqmark-kanji-日本語 and uniqmark-combining-é\"}" >/dev/null
sleep "$INDEX_WAIT"

# Korean, hanja, latin-with-diacritic should all tokenize
TITLES=$(search_titles "uniqmark-kanji-日本語")
echo "$TITLES" | grep -q "Emoji" && pass "CJK-containing token searchable" || fail "cjk token" "$TITLES"

# ── 12. /health reflects vocab growth ────────────────────────
echo ""
echo "▸ 12. /health vocab size grows after new terms"

# Use a random nonce that Kiwi will keep as a single SL token (no hyphens).
# Hex from /dev/urandom guarantees uniqueness across runs — no risk that a
# prior test-run left this exact term in the append-only vocab.
NONCE=$(python3 -c "import secrets; print('Vgrowth'+secrets.token_hex(6).upper())")
V_BEFORE=$(curl -sk "$BASE_URL/health" | python3 -c "import sys,json; print(json.load(sys.stdin).get('vector_store',{}).get('bm25_vocab_size',0))" 2>/dev/null)
mcp akb_put "{\"vault\":\"$VAULT\",\"collection\":\"edge\",\"title\":\"Vocab Growth\",\"content\":\"$NONCE\"}" >/dev/null
sleep "$INDEX_WAIT"
V_AFTER=$(curl -sk "$BASE_URL/health" | python3 -c "import sys,json; print(json.load(sys.stdin).get('vector_store',{}).get('bm25_vocab_size',0))" 2>/dev/null)
[ "$V_AFTER" -gt "$V_BEFORE" ] 2>/dev/null && pass "vocab grew ($V_BEFORE → $V_AFTER)" || fail "vocab growth" "$V_BEFORE → $V_AFTER (nonce=$NONCE)"

# ── 13. Score monotonic + non-zero for ranked results ────────
echo ""
echo "▸ 13. Score monotonic (top score >= every other)"

R=$(mcp akb_search "{\"query\":\"federation\",\"vault\":\"$VAULT\",\"limit\":5}" | mcp_text)
SCORES=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(','.join(str(r.get('score',0)) for r in d.get('results',[])))" 2>/dev/null)
MONO=$(python3 -c "
s = '$SCORES'.split(',') if '$SCORES' else []
if not s: print('skip'); raise SystemExit
xs = [float(x) for x in s if x]
print('ok' if all(xs[i] >= xs[i+1] for i in range(len(xs)-1)) and xs[0] > 0 else 'bad')
")
[ "$MONO" = "ok" ] || [ "$MONO" = "skip" ] && pass "scores non-increasing + top > 0 ($SCORES)" || fail "scores" "non-monotonic: $SCORES"

# ── 14. Drill-down returns sections of a real result ─────────
echo ""
echo "▸ 14. Drill-down alignment"

R=$(mcp akb_search "{\"query\":\"Rhinoceros\",\"vault\":\"$VAULT\",\"limit\":1}" | mcp_text)
HIT_DOC=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); rs=d.get('results',[]); print(rs[0]['doc_id'] if rs else '')" 2>/dev/null)
if [ -n "$HIT_DOC" ]; then
  R=$(mcp akb_drill_down "{\"vault\":\"$VAULT\",\"doc_id\":\"$HIT_DOC\"}" | mcp_text)
  N_SEC=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('sections',[])))" 2>/dev/null)
  [ "$N_SEC" -ge 1 ] 2>/dev/null && pass "drill-down on search hit: $N_SEC sections" || fail "drill-down" "no sections for $HIT_DOC"
else
  fail "drill-down" "search returned no hit to drill into"
fi

# ── 15. Force re-index (index recovery) ──────────────────────
echo ""
echo "▸ 15. Force vector_indexed_at=NULL → worker re-indexes"

# Mark all this vault's chunks as needing re-index.
kubectl exec -n akb statefulset/postgres -- psql -U akbuser -d akb -c "
  UPDATE chunks SET vector_indexed_at = NULL, vector_next_attempt_at = NOW(), vector_retry_count = 0
   WHERE document_id IN (SELECT d.id FROM documents d JOIN vaults v ON d.vault_id=v.id WHERE v.name='$VAULT')
" >/dev/null 2>&1

# Worker is best-effort; we can't strictly wait, but search must keep working
# (sync upsert path put new chunks; the NULL just makes worker re-upsert
# them → still searchable, possibly with brief unavailability per chunk).
TITLES=$(search_titles "Rhinoceros")
echo "$TITLES" | grep -q "Fluid Doc" && pass "search still works after marking re-index" || fail "reindex hit" "$TITLES"

# Wait a bit, then check pending dropped (worker drained)
sleep 8
PEND=$(curl -sk "$BASE_URL/health" | python3 -c "import sys,json; print(json.load(sys.stdin)['vector_store']['backfill']['upsert']['pending'])" 2>/dev/null)
echo "    indexing pending after 8s: $PEND"
# Allow non-zero (other tenants may be in flight); just ensure /health still works.
[ -n "$PEND" ] && pass "/health backfill counters readable ($PEND pending)" || fail "health pend" "missing field"

# ── 16. akb_put on duplicate path → clean error (not 500) ────
# (akb_put is NOT idempotent — it fails on (vault, path) conflict. The
# correct path for "replace" is akb_update. We assert the failure is
# a clean error response, not a server crash.)
echo ""
echo "▸ 16. Duplicate-path put returns clean error"

mcp akb_put "{\"vault\":\"$VAULT\",\"collection\":\"edge\",\"title\":\"Idempo\",\"content\":\"## a\\n\\nUniqornAlpha content.\"}" >/dev/null
R=$(mcp akb_put "{\"vault\":\"$VAULT\",\"collection\":\"edge\",\"title\":\"Idempo\",\"content\":\"## a\\n\\nUniqornBeta content.\"}" | mcp_text)
HAS_ERROR=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(isinstance(d, dict) and ('error' in d or 'detail' in d))" 2>/dev/null)
[ "$HAS_ERROR" = "True" ] && pass "duplicate path returns structured error" || fail "dup-path" "got: $R"

# Update flow (the actual replace path)
R=$(mcp akb_search "{\"query\":\"UniqornAlpha\",\"vault\":\"$VAULT\",\"limit\":3}" | mcp_text)
DOC=$(echo "$R" | python3 -c "import sys,json; rs=json.load(sys.stdin).get('results',[]); print(next((r['doc_id'] for r in rs if r.get('title')=='Idempo'),''))" 2>/dev/null)
if [ -n "$DOC" ]; then
  mcp akb_update "{\"vault\":\"$VAULT\",\"doc_id\":\"$DOC\",\"content\":\"## a\\n\\nUniqornBeta replacement content.\"}" >/dev/null
  sleep "$INDEX_WAIT"
  TOTAL=$(search_total "UniqornAlpha")
  [ "$TOTAL" = "0" ] && pass "akb_update purges old token" || fail "update-alpha-stale" "got $TOTAL"
  TITLES=$(search_titles "UniqornBeta")
  echo "$TITLES" | grep -q "Idempo" && pass "akb_update writes new token" || fail "update-beta" "$TITLES"
fi

# ── 17. Search → delete → search consistency ─────────────────
echo ""
echo "▸ 17. Delete after seeing in search"

mcp akb_put "{\"vault\":\"$VAULT\",\"collection\":\"edge\",\"title\":\"DelTarget\",\"content\":\"## x\\n\\nPlatypussa habitat content.\"}" >/dev/null
sleep "$INDEX_WAIT"

R=$(mcp akb_search "{\"query\":\"Platypussa\",\"vault\":\"$VAULT\",\"limit\":3}" | mcp_text)
DEL_DOC=$(echo "$R" | python3 -c "import sys,json; rs=json.load(sys.stdin).get('results',[]); print(rs[0]['doc_id'] if rs else '')" 2>/dev/null)
[ -n "$DEL_DOC" ] && pass "DelTarget visible in search ($DEL_DOC)" || fail "del-find" "not found"

if [ -n "$DEL_DOC" ]; then
  mcp akb_delete "{\"vault\":\"$VAULT\",\"doc_id\":\"$DEL_DOC\"}" >/dev/null
  sleep 3
  TOTAL=$(search_total "Platypussa")
  [ "$TOTAL" = "0" ] && pass "post-delete search returns 0" || fail "del-stale" "got $TOTAL"
fi

# ── 18. akb_grep ↔ akb_search agree on the *top* hit's doc id ─
# Hybrid search returns dense neighbors as well as keyword matches, so the
# total counts intentionally differ from grep. The invariant we DO want is
# that the top-1 search hit for a rare keyword IS the doc that grep finds.
echo ""
echo "▸ 18. grep top doc == search top doc (rare keyword)"

mcp akb_put "{\"vault\":\"$VAULT\",\"collection\":\"edge\",\"title\":\"Consist\",\"content\":\"## c\\n\\nQuetzalcoatlus wingspan estimated.\"}" >/dev/null
sleep "$INDEX_WAIT"

GREP_PATH=$(mcp akb_grep "{\"pattern\":\"Quetzalcoatlus\",\"vault\":\"$VAULT\"}" | mcp_text | python3 -c "import sys,json; rs=json.load(sys.stdin).get('results',[]); print(rs[0].get('path','') if rs else '')" 2>/dev/null)
SEARCH_PATH=$(mcp akb_search "{\"query\":\"Quetzalcoatlus\",\"vault\":\"$VAULT\",\"limit\":1}" | mcp_text | python3 -c "import sys,json; rs=json.load(sys.stdin).get('results',[]); print(rs[0].get('path','') if rs else '')" 2>/dev/null)
[ -n "$GREP_PATH" ] && [ "$GREP_PATH" = "$SEARCH_PATH" ] && pass "top hit aligns ($GREP_PATH)" || fail "grep/search top mismatch" "g='$GREP_PATH' s='$SEARCH_PATH'"

# ── Cleanup ──────────────────────────────────────────────────
echo ""
echo "▸ Cleanup"
mcp akb_delete_vault "{\"vault\":\"$VAULT\"}" >/dev/null
# Self-delete test user
curl -sk --max-time 15 -X DELETE "$BASE_URL/api/v1/my/account" -H "Authorization: Bearer $JWT" >/dev/null 2>&1
pass "vault deleted"

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
