#!/bin/bash
#
# AKB probe endpoints smoke test.
# Covers /livez, /readyz, /health and the one critical property:
# /livez must stay fast under a burst of concurrent requests (this is
# what kubelet uses to decide whether to kill the pod).
#
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
PASS=0
FAIL=0
ERRORS=()

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

echo "╔══════════════════════════════════════════╗"
echo "║   AKB Probes E2E                         ║"
echo "║   Target: $BASE_URL"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── 1. /livez ────────────────────────────────────────────────
echo "▸ 1. /livez"

body=$(curl -sk "$BASE_URL/livez")
[[ "$body" == '{"status":"alive"}' ]] && pass "returns alive" || fail "alive body" "$body"

code=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/livez")
[[ "$code" == "200" ]] && pass "200 OK" || fail "livez status" "$code"

# ── 2. /readyz shape ─────────────────────────────────────────
echo ""
echo "▸ 2. /readyz"

body=$(curl -sk "$BASE_URL/readyz")
echo "$body" | grep -q '"status"' && pass "has status" || fail "readyz status" "$body"
echo "$body" | grep -q '"detail"' && pass "has detail" || fail "readyz detail" "$body"
echo "$body" | grep -q '"pool"' && pass "detail.pool reported" || fail "detail.pool" "$body"
echo "$body" | grep -q '"db"' && pass "detail.db reported" || fail "detail.db" "$body"

# Vector store can be ok or degraded:* — either is acceptable for readyz
echo "$body" | grep -qE '"vector_store":"(ok|degraded:.*)"' \
  && pass "vector_store is ok or degraded" \
  || fail "vector_store status" "$body"

# ── 3. /health returns detailed stats ────────────────────────
echo ""
echo "▸ 3. /health"

body=$(curl -sk "$BASE_URL/health")
echo "$body" | grep -q '"vector_store"' && pass "vector_store section present" || fail "vector_store section" "$body"
# Indexing queue stats live under `vector_store.backfill` after the
# embed+sparse+upsert single-stage refactor (no more standalone
# `embed_backfill` top-level key).
echo "$body" | python3 -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if 'upsert' in d['vector_store']['backfill'] else 1)" \
  && pass "vector_store.backfill.upsert present" \
  || fail "backfill stats" "$body"
# BM25 stats block — added by the auto-refresh refactor; replaces
# the flat `bm25_vocab_size` key.
echo "$body" | python3 -c "
import sys, json
bm25 = json.load(sys.stdin)['vector_store'].get('bm25', {})
required = {'total_docs', 'avgdl', 'tokenizer', 'vocab_size', 'last_recomputed_at'}
missing = required - set(bm25.keys())
sys.exit(0 if not missing else 1)
" && pass "vector_store.bm25 has required keys" || fail "bm25 keys" "$body"
echo "$body" | grep -q '"metadata_backfill"' && pass "metadata_backfill present" || fail "metadata_backfill" "$body"
echo "$body" | grep -q '"external_git"' && pass "external_git present" || fail "external_git" "$body"

# ── 4. /livez under concurrent load ──────────────────────────
# The whole point of splitting /livez out of /health is that it must
# stay fast even when the pod is busy. If any of 10 concurrent calls
# takes > 3s, kubelet will start killing the pod under load.
echo ""
echo "▸ 4. /livez under burst (10 concurrent)"

tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
for _ in $(seq 1 10); do
  (curl -sk -o /dev/null -w "%{time_total}\n" --max-time 10 "$BASE_URL/livez" >>"$tmp") &
done
wait

max=$(sort -n "$tmp" | tail -1)
cnt=$(wc -l <"$tmp" | tr -d ' ')
awk -v m="$max" 'BEGIN { exit !(m+0 < 3.0) }' \
  && pass "slowest of $cnt calls: ${max}s (< 3s)" \
  || fail "burst slowest" "${max}s — event loop may be blocked"

# ── Summary ──────────────────────────────────────────────────
echo ""
echo "───────────────────────────────────────────"
echo "  passed: $PASS"
echo "  failed: $FAIL"
if [[ $FAIL -gt 0 ]]; then
  echo ""
  echo "Failures:"
  for e in "${ERRORS[@]}"; do echo "  - $e"; done
  exit 1
fi
