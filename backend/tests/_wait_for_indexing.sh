# Shared helper sourced by E2E scripts.
#
# wait_for_indexing — polls /health until both the embedding backfill and
# the vector-store upsert backfill report pending=0, or until max_wait
# seconds pass. Lets tests run on slow remote embedding endpoints
# (OpenRouter, OpenAI) where the async embed_worker + vector_indexer
# can take several seconds to drain after a write burst.
#
# Usage: wait_for_indexing [max_wait_seconds]    (default: 90)

wait_for_indexing() {
  local max_wait="${1:-180}"
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
      [ "$stable" -ge 2 ] && { sleep 1; return 0; }
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
