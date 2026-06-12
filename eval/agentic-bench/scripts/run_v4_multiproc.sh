#!/usr/bin/env bash
# v4 multi-process runner — no intra-process MCP session race.
#
# 8 background processes (2 chunks × 4 arms), each running sequentially
# inside its own OS process. parallel=1 means each process opens one
# MCP session at a time, so the anyio TaskGroup race we saw at
# parallel≥2 cannot occur. Process-to-process is OS-isolated.
set -euo pipefail
cd "$(dirname "$0")/.."

: "${AKB_MCP_URL:?set AKB_MCP_URL}"
: "${AKB_PAT:?set AKB_PAT}"
: "${LLM_API_KEY:?set LLM_API_KEY}"
: "${LLM_MODEL:=qwen/qwen-2.5-72b-instruct}"
: "${RUNS_DIR:=runs_v4}"

ARMS=(A1_search_only A2_grep_only A3_tree A4_all)
# Default to the repo-relative backend venv; override with PY=... if your
# venv lives elsewhere or you're running outside the repo tree.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PY="${PY:-${SCRIPT_DIR}/../../../backend/.venv/bin/python}"

mkdir -p "$RUNS_DIR"

# qid chunks (50 + 50 = 100 per arm). Build comma-separated lists.
chunk_a=$(seq -f q%03g 1 50 | paste -sd, -)
chunk_b=$(seq -f q%03g 51 100 | paste -sd, -)

PIDS=()
for arm in "${ARMS[@]}"; do
    for CHUNK_NAME in a b; do
        if [[ "$CHUNK_NAME" == "a" ]]; then QIDS="$chunk_a"; else QIDS="$chunk_b"; fi
        LOGFILE="$RUNS_DIR/log_${arm}_${CHUNK_NAME}.log"
        echo "launching $arm chunk $CHUNK_NAME -> $LOGFILE"
        RUNS_DIR="$RUNS_DIR" \
        AKB_MCP_URL="$AKB_MCP_URL" \
        AKB_PAT="$AKB_PAT" \
        LLM_API_KEY="$LLM_API_KEY" \
        LLM_MODEL="$LLM_MODEL" \
        "$PY" -m src.runner --arm "$arm" --qids "$QIDS" --parallel 1 \
            > "$LOGFILE" 2>&1 &
        PIDS+=($!)
    done
done

echo "spawned ${#PIDS[@]} processes: ${PIDS[*]}"
echo "waiting..."

FAILS=0
for pid in "${PIDS[@]}"; do
    if ! wait "$pid"; then
        FAILS=$((FAILS + 1))
    fi
done

echo "all done. process-level fails (non-zero exit due to per-q FAILs): $FAILS"
exit 0
