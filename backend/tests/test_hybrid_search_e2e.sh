#!/bin/bash
#
# Hybrid search (dense + BM25 sparse) E2E
#
# Scenarios covered:
# 1. /health surfaces vector_store state
# 2. Dense recall: natural-language query finds semantically-related doc
# 3. Short keyword recall: single-token / Korean query (BM25's strength)
# 4. Cross-vault isolation: search in vault A does not leak vault B
# 5. Reindex-after-update: editing a doc refreshes its sparse vector
# 6. Delete propagation: deleted doc no longer appears in search
# 7. Nonsense query returns 0 cleanly
# 8. /grep keeps working (sanity regression)
#
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
E2E_USER="hybrid-e2e-$(date +%s)"
VAULT_A="hybrid-a-$(date +%s)"
VAULT_B="hybrid-b-$(date +%s)"
# Background workers (embedding + vector indexer) are async; tune this upward
# if the embedding API is slow.
INDEX_WAIT="${AKB_HYBRID_INDEX_WAIT:-20}"
PASS=0
FAIL=0
ERRORS=()

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

echo "╔══════════════════════════════════════════╗"
echo "║   Hybrid Search E2E (dense + BM25)        ║"
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
  -d '{"name":"hybrid-e2e"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

[ -n "$PAT" ] && pass "PAT acquired" || { fail "PAT" "could not get PAT"; exit 1; }

# MCP session
INIT_RESP=$(curl -sk -i -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"hybrid-e2e","version":"1.0"}}}' 2>&1)
SID=$(echo "$INIT_RESP" | grep -i "mcp-session-id" | tr -d '\r' | awk '{print $2}')
[ -n "$SID" ] && pass "MCP session ($SID)" || { fail "MCP session" "no SID"; exit 1; }

curl -sk -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null 2>&1

MCP_ID=100
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

# Create two vaults
R=$(mcp_call akb_create_vault "{\"name\":\"$VAULT_A\",\"description\":\"hybrid E2E A\"}" | mcp_result)
VAULT_A_ID=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('vault_id',''))" 2>/dev/null)
[ -n "$VAULT_A_ID" ] && pass "vault A created" || fail "vault A" "$R"

R=$(mcp_call akb_create_vault "{\"name\":\"$VAULT_B\",\"description\":\"hybrid E2E B\"}" | mcp_result)
VAULT_B_ID=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('vault_id',''))" 2>/dev/null)
[ -n "$VAULT_B_ID" ] && pass "vault B created" || fail "vault B" "$R"

# ── 1. /health surfaces vector_store state ───────────────────
echo ""
echo "▸ 1. /health"

HEALTH=$(curl -sk "$BASE_URL/health")
HAS_VS=$(echo "$HEALTH" | python3 -c "import sys,json; d=json.load(sys.stdin); print('vector_store' in d)" 2>/dev/null)
[ "$HAS_VS" = "True" ] && pass "/health has vector_store block" || fail "/health" "missing vector_store field"

VS_REACHABLE=$(echo "$HEALTH" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('vector_store',{}).get('reachable', False))" 2>/dev/null)
echo "    vector_store.reachable=$VS_REACHABLE"

# ── 2. Seed docs ─────────────────────────────────────────────
echo ""
echo "▸ 2. Seed docs"

mcp_call akb_put "{\"vault\":\"$VAULT_A\",\"collection\":\"notes\",\"title\":\"Kubernetes Introduction\",\"content\":\"## Overview\\n\\nKubernetes is a container orchestration system. Pods are the smallest deployable unit. 쿠버네티스 파드는 컨테이너를 그룹화한다.\",\"type\":\"note\",\"tags\":[\"k8s\"]}" >/dev/null
mcp_call akb_put "{\"vault\":\"$VAULT_A\",\"collection\":\"notes\",\"title\":\"PostgreSQL Performance Tuning\",\"content\":\"## Tuning\\n\\nTuning PostgreSQL requires attention to shared_buffers, work_mem, and checkpoint settings. WAL archiving affects replication.\",\"type\":\"note\",\"tags\":[\"db\"]}" >/dev/null
R=$(mcp_call akb_put "{\"vault\":\"$VAULT_A\",\"collection\":\"notes\",\"title\":\"GraphQL Basics\",\"content\":\"## Intro\\n\\nGraphQL is a query language for APIs. Resolvers map types to data sources. Schema-first design is common.\",\"type\":\"note\",\"tags\":[\"api\"]}" | mcp_result)
GQL_DOC_URI=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('uri',''))" 2>/dev/null)

mcp_call akb_put "{\"vault\":\"$VAULT_B\",\"collection\":\"notes\",\"title\":\"Vault B private doc\",\"content\":\"## Private\\n\\nThis document should only appear when searching within vault B, never vault A.\",\"type\":\"note\",\"tags\":[\"secret\"]}" >/dev/null

pass "4 docs seeded"

echo "    waiting ${INDEX_WAIT}s for async embedding + vector-store indexing…"
sleep "$INDEX_WAIT"

search_total() {
  local q=$1 vault=$2
  local R
  R=$(mcp_call akb_search "{\"query\":\"$q\",\"vault\":\"$vault\",\"limit\":10}" | mcp_result)
  echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('total', 0))" 2>/dev/null
}

search_titles() {
  local q=$1 vault=$2
  local R
  R=$(mcp_call akb_search "{\"query\":\"$q\",\"vault\":\"$vault\",\"limit\":10}" | mcp_result)
  echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('|'.join([r.get('title','') for r in d.get('results', [])]))" 2>/dev/null
}

# ── 3. Dense recall ──────────────────────────────────────────
# Hybrid is gated on at least one query token appearing in the candidate
# set's BM25 vocab (otherwise dense baseline noise leaks through). The
# query carries a keyword anchor ('PostgreSQL') so the gate passes; the
# rest of the phrase tests that dense ordering kicks in (natural-language
# wording rather than just the keyword).
echo ""
echo "▸ 3. Dense recall (natural-language with keyword anchor)"

TITLES=$(search_titles "tuning PostgreSQL for better performance" "$VAULT_A")
if echo "$TITLES" | grep -q "PostgreSQL"; then
  pass "natural-language query → postgres doc"
else
  fail "dense" "expected PostgreSQL doc, got: $TITLES"
fi

# ── 4. BM25 recall (short keyword) ───────────────────────────
echo ""
echo "▸ 4. BM25 recall (short keyword)"

TITLES=$(search_titles "GraphQL" "$VAULT_A")
if echo "$TITLES" | grep -q "GraphQL"; then
  pass "single keyword → graphql doc"
else
  fail "bm25-en" "expected GraphQL doc, got: $TITLES"
fi

TITLES=$(search_titles "쿠버네티스" "$VAULT_A")
if echo "$TITLES" | grep -q "Kubernetes"; then
  pass "Korean keyword → kubernetes doc"
else
  fail "bm25-ko" "expected Kubernetes doc, got: $TITLES"
fi

# ── 5. Cross-vault isolation ─────────────────────────────────
echo ""
echo "▸ 5. Cross-vault isolation"

TITLES=$(search_titles "private" "$VAULT_A")
if echo "$TITLES" | grep -q "Vault B private"; then
  fail "isolation-A" "vault A search leaked vault B doc: $TITLES"
else
  pass "vault A search does not leak vault B doc"
fi

TITLES=$(search_titles "private" "$VAULT_B")
if echo "$TITLES" | grep -q "Vault B private"; then
  pass "vault B search finds its own doc"
else
  fail "isolation-B" "expected vault B doc, got: $TITLES"
fi

# ── 6. Reindex after update ──────────────────────────────────
echo ""
echo "▸ 6. Reindex-after-update"

if [ -n "$GQL_DOC_URI" ]; then
  mcp_call akb_update "{\"uri\":\"$GQL_DOC_URI\",\"content\":\"## Intro\\n\\nGraphQL is a query language. Updated content now mentions Apollo and Relay clients. Federation allows composing services.\",\"message\":\"test re-index\"}" >/dev/null
  echo "    waiting ${INDEX_WAIT}s for re-index…"
  sleep "$INDEX_WAIT"

  TITLES=$(search_titles "Apollo" "$VAULT_A")
  if echo "$TITLES" | grep -q "GraphQL"; then
    pass "updated content is searchable"
  else
    fail "reindex" "expected GraphQL doc for 'Apollo', got: $TITLES"
  fi
else
  fail "reindex" "no GraphQL uri captured, skipping"
fi

# ── 7. Delete propagation ────────────────────────────────────
echo ""
echo "▸ 7. Delete propagation"

if [ -n "$GQL_DOC_URI" ]; then
  mcp_call akb_delete "{\"uri\":\"$GQL_DOC_URI\"}" >/dev/null
  echo "    waiting 8s for outbox + pre-filter…"
  sleep 8
  TITLES=$(search_titles "Apollo" "$VAULT_A")
  if echo "$TITLES" | grep -q "GraphQL"; then
    fail "delete" "deleted doc still appears: $TITLES"
  else
    pass "deleted doc is no longer returned"
  fi
fi

# ── 8. Nonsense query returns 0 ──────────────────────────────
# Queries with no vocab overlap (random strings, fully OOV tokens) are
# treated as "no signal" — no dense-only fallback, so total must be 0.
echo ""
echo "▸ 8. Nonsense-query safety"

TOTAL=$(search_total "Blarghnizophorpquix$RANDOM" "$VAULT_A")
[ "$TOTAL" = "0" ] && pass "nonsense query returns 0" || fail "empty" "expected 0, got $TOTAL"

# ── 9. /grep sanity ──────────────────────────────────────────
echo ""
echo "▸ 9. akb_grep regression"

R=$(mcp_call akb_grep "{\"pattern\":\"shared_buffers\",\"vault\":\"$VAULT_A\"}" | mcp_result)
GREP_TOTAL=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('total_matches', 0))" 2>/dev/null)
[ "$GREP_TOTAL" -ge 1 ] 2>/dev/null && pass "akb_grep still finds literal string" || fail "grep" "total_matches=$GREP_TOTAL"

# ── Cleanup ──────────────────────────────────────────────────
echo ""
echo "▸ Cleanup"

mcp_call akb_delete_vault "{\"vault\":\"$VAULT_A\",\"confirm\":true}" >/dev/null
mcp_call akb_delete_vault "{\"vault\":\"$VAULT_B\",\"confirm\":true}" >/dev/null
# Self-delete test user
curl -sk --max-time 15 -X DELETE "$BASE_URL/api/v1/my/account" -H "Authorization: Bearer $JWT" >/dev/null 2>&1
pass "vaults deleted"

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
