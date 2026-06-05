#!/bin/bash
#
# SeahorseDB native driver — wire-shape smoke E2E against a live Coral.
#
# Confirms the wire formats `seahorse_db.py` emits are accepted (and
# meaningfully understood) by a live Coral coordinator. Every assertion
# checks BOTH the HTTP status code AND `content-type: application/json`
# — Coral's same-port tonic gRPC fallback returns 200 OK +
# `content-type: application/grpc` + `grpc-status: 12` on unmatched
# REST paths, which is how 0.7.1 shipped a "6/6 PASS" smoke that was
# actually all gRPC fallbacks. Don't repeat that.
#
# Opt-in via env var. CI skips cleanly (SeahorseDB is dn-inc commercial
# and most environments don't have a Coral reachable):
#
#   SEAHORSEDB_CORAL_URL=http://localhost:NNNN \
#     bash backend/tests/test_seahorse_db_e2e.sh
#
# Scope (everything pinned to the 0.7.3 wire formats):
#   1. /health is reachable AND content-type json AND no gRPC fallback
#   2. POST /v2/tables with the exact CreateTableRequest shape the
#      driver builds (flat table_name + columns SCREAMING_SNAKE +
#      segmentation hash/single + indexes hnsw+inverted with
#      sparse_model=bm25). Verifies create succeeds + status 200 +
#      json.
#   3. GET /v2/tables/{name} (mirrors ensure_collection's existence
#      probe) returns the same schema we sent.
#   4. POST /v2/tables/{name}/data with Content-Type
#      application/x-ndjson (JSONL); rejects application/json with
#      400 (we assert the rejection so a future Coral that starts
#      accepting JSON doesn't silently break the driver's chosen
#      content-type).
#   5. POST /v2/tables/{name}/data/delete with delete_condition
#      SQL WHERE clause.
#   6. POST /v2/tables/{name}/data/hybrid-search with the dense+sparse
#      config objects, BM25 parameters + metadata, fusion block;
#      verifies the response envelope shape body.data.data is a
#      list-of-resultsets.
#   7. DELETE /v2/tables/{name} cleanup.
#
# Does NOT cover (intentionally):
# - End-to-end search relevance against a real corpus — that's
#   covered by AKB's test_hybrid_search_e2e.sh pointed at a backend
#   running vector_store_driver: seahorse-db. Tracked separately.
# - Eventual-consistency lag between POST /data and visibility in
#   hybrid-search. Driver shape correctness is what this smoke
#   guarantees; relevance + visibility lives upstream.
#
set -uo pipefail

CORAL_URL="${SEAHORSEDB_CORAL_URL:-}"
TABLE="${SEAHORSEDB_TABLE_NAME:-akb_smoke_$(date +%s)}"

if [ -z "$CORAL_URL" ]; then
    echo "==> SEAHORSEDB_CORAL_URL not set; skipping (this is OK in CI)."
    echo "    To run: SEAHORSEDB_CORAL_URL=http://localhost:NNNN \\"
    echo "            bash backend/tests/test_seahorse_db_e2e.sh"
    exit 0
fi

CORAL_URL="${CORAL_URL%/}"

# Reachability gate.
if ! curl -fsS "$CORAL_URL/health" >/dev/null 2>&1; then
    echo "ERROR: SEAHORSEDB_CORAL_URL=$CORAL_URL is not reachable." >&2
    echo "       (Bring the SeahorseDB stack up first; the host port" >&2
    echo "        is the one mapped to Coral's container :3003.)"   >&2
    exit 1
fi

PASSED=0
FAILED=0
ERRORS=()

step() { printf '\n\033[1;36m▸ %s\033[0m\n' "$*"; }
ok()   { printf '  \033[1;32m✓\033[0m %s\n' "$*"; PASSED=$((PASSED + 1)); }
bad()  { printf '  \033[1;31m✗\033[0m %s\n' "$*"; FAILED=$((FAILED + 1)); ERRORS+=("$*"); }

# ── helpers ─────────────────────────────────────────────────────────

# coral_call <method> <path> <expected_status> <label> [body_file]
# Asserts: HTTP status matches AND content-type starts with application/json.
# The application/json check is the defence against the gRPC fallback
# that silently returned HTTP 200 for unmatched REST routes in 0.7.1.
coral_call() {
    local method="$1" path="$2" expected="$3" label="$4" body="${5:-}"
    local args=(-sS -X "$method" -o /tmp/sdb_e2e_body
                -w "%{http_code}|%{content_type}"
                "$CORAL_URL$path")
    if [ -n "$body" ]; then
        args+=(-H "Content-Type: application/json"
               --data-binary "@$body")
    fi
    local out
    out=$(curl "${args[@]}")
    local status="${out%%|*}"
    local ct="${out#*|}"

    if [[ "$ct" != application/json* ]]; then
        bad "$label: content-type=$ct (expected application/json) — gRPC fallback?"
        return 1
    fi
    if [ "$status" != "$expected" ]; then
        bad "$label: HTTP $status (expected $expected) — body: $(head -c 200 /tmp/sdb_e2e_body)"
        return 1
    fi
    ok "$label: HTTP $status, content-type application/json"
    return 0
}

# ndjson_post <path> <expected_status> <label> <body_file>
# Same as coral_call but with application/x-ndjson — the only content
# type Coral's /data insert handler accepts.
ndjson_post() {
    local path="$1" expected="$2" label="$3" body="$4"
    local out status ct
    out=$(curl -sS -X POST -o /tmp/sdb_e2e_body \
            -w "%{http_code}|%{content_type}" \
            -H "Content-Type: application/x-ndjson" \
            --data-binary "@$body" \
            "$CORAL_URL$path")
    status="${out%%|*}"
    ct="${out#*|}"

    if [[ "$ct" != application/json* ]]; then
        bad "$label: content-type=$ct (gRPC fallback?)"
        return 1
    fi
    if [ "$status" != "$expected" ]; then
        bad "$label: HTTP $status (body: $(head -c 200 /tmp/sdb_e2e_body))"
        return 1
    fi
    ok "$label: HTTP $status, content-type application/json"
    return 0
}

# Trap to make sure we don't leave the smoke's ephemeral table behind
# even if assertions abort mid-script.
cleanup_table() {
    curl -sS -X DELETE "$CORAL_URL/v2/tables/$TABLE" -o /dev/null
}
trap cleanup_table EXIT

# ── 1. /health ──────────────────────────────────────────────────────
step "1. /health"
coral_call GET /health 200 "Coral health"

# ── 2. POST /v2/tables (CreateTableRequest) ─────────────────────────
step "2. POST /v2/tables (create_table shape that SeahorseDbStore emits)"
cat > /tmp/sdb_e2e_create.json <<EOF
{
    "table_name": "$TABLE",
    "columns": [
        {"name": "id", "type": "INT64", "nullable": false},
        {"name": "chunk_id", "type": "STRING", "nullable": false},
        {"name": "embedding", "type": {"name": "DENSE_VECTOR", "element": "FLOAT32", "dim": 8}, "nullable": false},
        {"name": "sparse", "type": {"name": "SPARSE_VECTOR"}, "nullable": false},
        {"name": "content", "type": "STRING", "nullable": true},
        {"name": "section_path", "type": "STRING", "nullable": true},
        {"name": "chunk_index", "type": "INT64", "nullable": true},
        {"name": "source_type", "type": "STRING", "nullable": false},
        {"name": "source_id", "type": "STRING", "nullable": false}
    ],
    "segmentation": {"strategy": "hash", "columns": ["id"], "buckets": 1, "composition": "single"},
    "indexes": [
        {"type": "hnsw", "column": "embedding", "params": {"space": "ip", "ef_construction": 64, "M": 16}},
        {"type": "inverted", "column": "sparse", "params": {"sparse_model": "bm25"}}
    ]
}
EOF
coral_call POST /v2/tables 200 "create table $TABLE" /tmp/sdb_e2e_create.json

# ── 3. GET /v2/tables/{name} (ensure_collection probe) ──────────────
step "3. GET /v2/tables/{name} (round-trip + ensure_collection probe)"
if coral_call GET "/v2/tables/$TABLE" 200 "get table"; then
    # Verify a few fields actually round-tripped.
    if grep -q "\"sparse_model\":\"bm25\"" /tmp/sdb_e2e_body; then
        ok "sparse INVERTED index carries sparse_model=bm25"
    else
        bad "sparse INVERTED index missing sparse_model=bm25 in GET response"
    fi
    if grep -q "\"primary_key\":\\[\"id\"\\]" /tmp/sdb_e2e_body 2>/dev/null \
       || grep -q "\"strategy\":\"hash\"" /tmp/sdb_e2e_body; then
        ok "segmentation/PK shape round-trips"
    else
        bad "segmentation shape did NOT round-trip"
    fi
fi

# Negative: a name that doesn't exist must return 404 (not 200 with
# empty body, which is the gRPC-fallback failure mode).
coral_call GET "/v2/tables/${TABLE}_nope" 404 "missing table is real 404"

# ── 4. POST /v2/tables/{name}/data — JSONL ──────────────────────────
step "4. POST /data — JSONL (application/x-ndjson)"
cat > /tmp/sdb_e2e_rec.jsonl <<EOF
{"id": 42, "chunk_id": "00000000-0000-0000-0000-00000000002a", "embedding": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8], "sparse": "1:0.5 3:0.7", "content": "hello", "section_path": "", "chunk_index": 0, "source_type": "document", "source_id": "00000000-0000-0000-0000-000000000001"}
EOF
ndjson_post "/v2/tables/$TABLE/data" 200 "insert one record JSONL" /tmp/sdb_e2e_rec.jsonl

# Negative: application/json must be rejected. If a future Coral
# starts accepting it, the driver's content-type choice silently
# becomes underspecified — better to know loudly.
step "4b. POST /data with application/json must be rejected"
out=$(curl -sS -X POST -o /tmp/sdb_e2e_body \
      -w "%{http_code}|%{content_type}" \
      -H "Content-Type: application/json" \
      --data-binary @/tmp/sdb_e2e_rec.jsonl \
      "$CORAL_URL/v2/tables/$TABLE/data")
status="${out%%|*}"
ct="${out#*|}"
if [[ "$ct" != application/json* ]]; then
    bad "wrong content-type rejection: returned ct=$ct"
elif [ "$status" = "400" ] && grep -q "Unsupported Content-Type" /tmp/sdb_e2e_body; then
    ok "rejected with HTTP 400 + 'Unsupported Content-Type' (expected)"
else
    bad "expected HTTP 400 + 'Unsupported Content-Type', got HTTP $status — Coral changed insert content-type policy?"
fi

# ── 5. POST /data/delete — SQL WHERE ────────────────────────────────
step "5. POST /data/delete — SQL WHERE clause"
cat > /tmp/sdb_e2e_del.json <<EOF
{"delete_condition": "chunk_id = '00000000-0000-0000-0000-00000000002a'"}
EOF
coral_call POST "/v2/tables/$TABLE/data/delete" 200 "delete by chunk_id SQL clause" /tmp/sdb_e2e_del.json

# Idempotency: deleting again must still return 200 (no row matches).
coral_call POST "/v2/tables/$TABLE/data/delete" 200 "delete idempotent re-fire" /tmp/sdb_e2e_del.json

# ── 6. POST /data/hybrid-search ─────────────────────────────────────
step "6. POST /data/hybrid-search — dense+sparse+BM25 metadata+fusion"
cat > /tmp/sdb_e2e_hs.json <<EOF
{
    "top_k": 5,
    "dense": {
        "column": "embedding",
        "vectors": [[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]],
        "parameters": {"ef_search": 64}
    },
    "sparse": {
        "column": "sparse",
        "vectors": ["1:0.5 3:0.7"],
        "parameters": {"k": 1.2, "b": 0.75},
        "metadata": {"N": 1, "avgdl": 1.0, "df": ["1:1 3:1"]}
    },
    "fusion": {"type": "rrf", "parameters": {"k": 60}},
    "projection": "chunk_id, source_type, source_id, section_path, content"
}
EOF
if coral_call POST "/v2/tables/$TABLE/data/hybrid-search" 200 "hybrid-search with BM25 metadata" /tmp/sdb_e2e_hs.json; then
    # Verify the response envelope shape the driver parses.
    if python3 -c "
import json, sys
body = json.load(open('/tmp/sdb_e2e_body'))
data = body.get('data') or {}
results = data.get('data') or []
# The driver reads body['data']['data'][0] as the hit list; either
# the resultset is empty (0 hits, fine — Kafka lag) or it's a list.
if not isinstance(results, list):
    sys.exit('outer data.data is not a list: type=' + type(results).__name__)
if results and not isinstance(results[0], list):
    sys.exit('inner data.data[0] is not a list: type=' + type(results[0]).__name__)
" 2>/tmp/sdb_e2e_shapeerr; then
        ok "response envelope body.data.data is list-of-resultsets (driver parses this)"
    else
        bad "envelope shape unexpected: $(cat /tmp/sdb_e2e_shapeerr)"
    fi
fi

# ── 7. DELETE /v2/tables/{name} ─────────────────────────────────────
step "7. DELETE /v2/tables/{name} (cleanup)"
coral_call DELETE "/v2/tables/$TABLE" 200 "drop table"

# ── Results ─────────────────────────────────────────────────────────
echo
echo "═══════════════════════════════════════════"
echo "  Passed: $PASSED"
echo "  Failed: $FAILED"
if [ $FAILED -gt 0 ]; then
    echo
    echo "  Errors:"
    for e in "${ERRORS[@]}"; do
        echo "    - $e"
    done
fi
echo "═══════════════════════════════════════════"

[ $FAILED -eq 0 ]
