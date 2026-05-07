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
echo "$body" | grep -q '"embed_backfill"' && pass "embed_backfill present" || fail "embed_backfill" "$body"
echo "$body" | grep -q '"vector_store"' && pass "vector_store section present" || fail "vector_store section" "$body"
echo "$body" | grep -q '"bm25_vocab_size"' && pass "bm25_vocab_size present" || fail "bm25_vocab_size" "$body"

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
