# Shared helper sourced by E2E scripts.
#
# wait_for_indexing — polls /health until both the embedding backfill and
# the vector-store upsert backfill report pending=0, then waits an extra
# `AKB_VISIBILITY_BUFFER` seconds (default 60) for the upstream vector
# store to actually surface the upserted points in search results.
#
# Why the buffer
# --------------
# Seahorse (the managed vector store the AKB validation tier runs
# against) indexes asynchronously — `vector_indexed_at` is set the
# moment /v2/data upsert returns 200 OK, but the new point isn't
# visible to /v2/data/search until propagation completes. Measured on
# the validation tier during #42 debugging:
#   - simple PUT visible at ~5–10s
#   - update (drop+insert) visible at ~30–55s
# 60s is the conservative buffer that gets test_hybrid_search_e2e to
# 21/21 stably. Drivers without this lag (pgvector for local dev) can
# set AKB_VISIBILITY_BUFFER=0 to skip the wait entirely.
#
# Override with AKB_VISIBILITY_BUFFER=0 when running against a
# zero-lag driver (e.g. local pgvector); the polling stays correct
# either way.
#
# Usage: wait_for_indexing [max_wait_seconds]    (default: 180)

wait_for_indexing() {
  local max_wait="${1:-180}"
  local buffer="${AKB_VISIBILITY_BUFFER:-60}"
  local start
  start=$(date +%s)
  # Require pending=0 to hold for two consecutive polls — guards against
  # racing through a brief gap between embed_worker batches and the next
  # write that the test issued just before this call.
  local stable=0
  while true; do
    local h
    h=$(curl -sk --max-time 5 "$BASE_URL/health" 2>/dev/null)
    local pending
    pending=$(echo "$h" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    e = d.get('embed_backfill', {}).get('pending', 0)
    q = d.get('vector_store', {}).get('backfill', {}).get('upsert', {}).get('pending', 0)
    r = d.get('embed_backfill', {}).get('retrying', 0)
    print(int(e) + int(q) + int(r))
except Exception:
    print(999)
" 2>/dev/null)
    if [ "$pending" = "0" ]; then
      stable=$((stable + 1))
      if [ "$stable" -ge 2 ]; then
        [ "$buffer" -gt 0 ] && sleep "$buffer"
        return 0
      fi
    else
      stable=0
    fi
    if [ $(($(date +%s) - start)) -ge "$max_wait" ]; then
      echo "  ! wait_for_indexing timeout (${max_wait}s) — pending=$pending" >&2
      return 1
    fi
    sleep 2
  done
}
