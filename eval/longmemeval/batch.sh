#!/usr/bin/env bash
# LongMemEval benchmark batch runner.
#
# Spawns N workers concurrently, each writing to its own NDJSON shard.
# Resume works across reruns because run.py keys dedup on
# (adapter, question_id) per shard, and shards are partitioned by
# hash(qid) mod N -- a worker always sees the same subset across runs.
#
# Wall budget caps each worker's runtime, not the batch as a whole.
# That's a feature: bumping LIMIT or rerunning after fixing indexing
# picks up where you left off without re-running completed questions.
#
# Usage:
#   ./batch.sh                                  # 3 workers, no wall cap
#   WORKERS=5 WALL=600 ./batch.sh               # 5 workers, 10-min cap each
#   DATASET=/path/to/longmemeval_s.json LIMIT=10 ./batch.sh
#   STRATIFY=5 ./batch.sh                       # 5 per question_type
#
# After a partial run, just re-invoke the same command -- shards resume.
# Merge for analysis:
#   cat reports/longmemeval-akb.shard-*.ndjson > reports/longmemeval-akb.ndjson

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

WORKERS=${WORKERS:-3}
WALL=${WALL:-}
DATASET=${DATASET:-$HOME/datasets/longmemeval/longmemeval_s.json}
ADAPTER=${ADAPTER:-akb-hybrid+rerank}
TOPK=${TOPK:-5}
REPORTS_DIR=${REPORTS_DIR:-"$REPO_ROOT/eval/reports"}
NDJSON_PREFIX=${NDJSON_PREFIX:-longmemeval-akb}
AKB_URL=${AKB_URL:-http://localhost:18000}
LIMIT=${LIMIT:-}
STRATIFY=${STRATIFY:-}
MAX_INDEX_WAIT=${MAX_INDEX_WAIT:-300}

mkdir -p "$REPORTS_DIR"

if [[ ! -f "$DATASET" ]]; then
  echo "error: dataset not found: $DATASET" >&2
  echo "       download with: curl -Lo $DATASET https://huggingface.co/datasets/xiaowu0162/longmemeval/resolve/main/longmemeval_s" >&2
  exit 2
fi

echo "batch: workers=$WORKERS wall=${WALL:-unbounded}s adapter=$ADAPTER topk=$TOPK"
echo "       dataset=$DATASET"
echo "       reports=$REPORTS_DIR  prefix=$NDJSON_PREFIX"
[[ -n "$LIMIT" ]] && echo "       limit=$LIMIT (per worker, applied after shard)"
[[ -n "$STRATIFY" ]] && echo "       stratify=$STRATIFY per question_type"
echo

declare -a PIDS=()
declare -a LOGS=()
declare -a SHARDS=()
for ((i=0; i<WORKERS; i++)); do
  SHARD="$REPORTS_DIR/$NDJSON_PREFIX.shard-${i}of${WORKERS}.ndjson"
  LOG="$REPORTS_DIR/$NDJSON_PREFIX.shard-${i}of${WORKERS}.log"
  SHARDS+=("$SHARD")
  LOGS+=("$LOG")
  ARGS=(
    --dataset "$DATASET"
    --ndjson "$SHARD"
    --adapter "$ADAPTER"
    --top-k "$TOPK"
    --worker-id "$i"
    --total-workers "$WORKERS"
    --akb-url "$AKB_URL"
    --max-index-wait-seconds "$MAX_INDEX_WAIT"
  )
  [[ -n "$WALL" ]] && ARGS+=(--max-wall-seconds "$WALL")
  [[ -n "$LIMIT" ]] && ARGS+=(--limit "$LIMIT")
  [[ -n "$STRATIFY" ]] && ARGS+=(--stratify "$STRATIFY")
  python3 "$SCRIPT_DIR/run.py" "${ARGS[@]}" >"$LOG" 2>&1 &
  PIDS+=($!)
  echo "  worker $i started (pid=${PIDS[$i]}, log=$LOG)"
done
echo

# Propagate signals so each run.py runs its DELETE /my/account cleanup.
cleanup() {
  echo
  echo "received signal — propagating SIGTERM to all workers"
  for pid in "${PIDS[@]}"; do
    kill -TERM "$pid" 2>/dev/null || true
  done
}
trap cleanup INT TERM

# Wait individually so we can capture each exit code.
declare -a EXIT_CODES=()
for i in "${!PIDS[@]}"; do
  if wait "${PIDS[$i]}"; then
    EXIT_CODES+=(0)
  else
    EXIT_CODES+=($?)
  fi
done
echo

# Aggregate.
echo "=== summary ==="
TOTAL=0; HITS=0; OK=0; INGEST_ERR=0; WAIT_ERR=0; SEARCH_ERR=0
for ((i=0; i<WORKERS; i++)); do
  SHARD="${SHARDS[$i]}"
  STATS=$(python3 - "$SHARD" <<'PY'
import json, sys
ok = hits = ie = we = se = total = 0
try:
    for line in open(sys.argv[1]):
        r = json.loads(line)
        if r.get("type") == "run_meta_start":
            continue
        total += 1
        s = r.get("status")
        if s == "ok":
            ok += 1
            if r.get("hit_at_k"):
                hits += 1
        elif s == "ingest_error":
            ie += 1
        elif s == "index_wait_timeout":
            we += 1
        elif s == "search_error":
            se += 1
except FileNotFoundError:
    pass
print(total, ok, hits, ie, we, se)
PY
  )
  read -r T O H IE WE SE <<< "$STATS"
  printf "  shard %d/%d (exit %s): %d processed, %d ok, %d hits, errors=(ingest=%d wait=%d search=%d)\n" \
    "$i" "$WORKERS" "${EXIT_CODES[$i]}" "$T" "$O" "$H" "$IE" "$WE" "$SE"
  TOTAL=$((TOTAL + T))
  HITS=$((HITS + H))
  OK=$((OK + O))
  INGEST_ERR=$((INGEST_ERR + IE))
  WAIT_ERR=$((WAIT_ERR + WE))
  SEARCH_ERR=$((SEARCH_ERR + SE))
done
echo "  ─────"
if [[ $OK -gt 0 ]]; then
  RECALL=$(python3 -c "print(f'{$HITS/$OK:.1%}')")
else
  RECALL="n/a"
fi
echo "  TOTAL: $TOTAL processed, $OK ok, $HITS hits, R@$TOPK=$RECALL, errors=(ingest=$INGEST_ERR wait=$WAIT_ERR search=$SEARCH_ERR)"
echo
echo "Per-type breakdown:"
python3 - "${SHARDS[@]}" <<'PY'
import json, sys
from collections import defaultdict
by_type = defaultdict(lambda: {"total": 0, "ok": 0, "hits": 0, "abs": 0})
for path in sys.argv[1:]:
    try:
        f = open(path)
    except FileNotFoundError:
        continue
    for line in f:
        r = json.loads(line)
        if r.get("type") == "run_meta_start":
            continue
        t = r.get("question_type", "?")
        by_type[t]["total"] += 1
        if r.get("is_abstention"):
            by_type[t]["abs"] += 1
        if r.get("status") == "ok":
            by_type[t]["ok"] += 1
            if r.get("hit_at_k"):
                by_type[t]["hits"] += 1
for t, c in sorted(by_type.items()):
    eff_ok = c["ok"]
    recall = (c["hits"] / eff_ok * 100) if eff_ok else 0.0
    print(f"  {t:30s} total={c['total']:4d} ok={c['ok']:4d} hits={c['hits']:4d} R@K={recall:5.1f}%  abstention={c['abs']}")
PY

echo
echo "Merge shards for analysis:"
echo "  cat $REPORTS_DIR/$NDJSON_PREFIX.shard-*.ndjson > $REPORTS_DIR/$NDJSON_PREFIX.ndjson"

# Propagate worker failure -- CI / automation must not treat a partial
# benchmark with non-zero shards as a successful run.
ANY_FAIL=0
for code in "${EXIT_CODES[@]}"; do
  if [[ "$code" -ne 0 ]]; then
    ANY_FAIL=1
  fi
done
if [[ $ANY_FAIL -ne 0 ]]; then
  echo
  echo "ERROR: at least one worker exited non-zero (see per-shard exit codes above)" >&2
  exit 1
fi
