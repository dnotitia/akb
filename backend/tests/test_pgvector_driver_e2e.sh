#!/bin/bash
#
# PgvectorStore driver-specific E2E.
#
# Runs *inside* a backend container so the driver hits the main PG
# that the deployed AKB already has wired up. Uses an ephemeral
# schema name (`e2e_test_pgvec_<pid>`) so it can run on a live
# cluster without touching production tables. Always drops the
# schema in EXIT trap.
#
# Container-exec is k8s-by-default; for docker-compose set
# `BACKEND_EXEC="docker compose exec -T backend"` and (if relevant)
# clear AKB_NAMESPACE / BACKEND_POD lookup.
#
# Validates:
#   1. ensure_collection — schema + tables + HNSW index created;
#      second call is cached (no extra DDL).
#   2. upsert_one (posting shape) — chunk row + posting rows land.
#   3. search_dense — nearest neighbour returned.
#   4. search_sparse — BM25-weighted ranking.
#   5. hybrid_search — RRF fusion over both legs.
#   6. source_id filter — out-of-scope chunks excluded.
#   7. delete_point — vector_index row gone; posting rows cascade.
#   8. binary codec round-trip — register_vector takes; dense returns
#      with the right dimension.
#
# Usage:  bash backend/tests/test_pgvector_driver_e2e.sh
# Env:
#   BACKEND_EXEC  override the container-exec prefix.
#                 default: `kubectl exec -n $AKB_NAMESPACE <pod> --`
#                 docker-compose: `docker compose exec -T backend`
#   AKB_NAMESPACE k8s namespace (default: akb)

set -uo pipefail
NS="${AKB_NAMESPACE:-akb}"
SCHEMA="e2e_test_pgvec_$$"

if [ -z "${BACKEND_EXEC:-}" ]; then
  BACKEND_POD=$(kubectl get pod -n "$NS" -l app=akb-backend -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
  if [ -z "$BACKEND_POD" ]; then
    echo "✗ No backend pod found in namespace $NS, and BACKEND_EXEC not set."
    echo "  For docker-compose:  BACKEND_EXEC='docker compose exec -T backend' bash $0"
    exit 2
  fi
  BACKEND_EXEC="kubectl exec -n $NS $BACKEND_POD --"
  echo "Running via: $BACKEND_EXEC"
else
  echo "Running via: $BACKEND_EXEC (override)"
fi
echo "Scratch schema: $SCHEMA"
echo ""

PASS=0
FAIL=0
ERRORS=()

pass() { echo "  ✓ $1"; PASS=$((PASS + 1)); }
fail() { echo "  ✗ $1: $2"; FAIL=$((FAIL + 1)); ERRORS+=("$1: $2"); }

# Run a Python snippet in the backend pod. Stdout = whatever the
# snippet prints; non-zero exit propagates.
pyrun() {
  $BACKEND_EXEC python -c "$1"
}

cleanup() {
  pyrun "
import asyncio
from app.db.postgres import get_pool
async def drop():
    pool = await get_pool()
    async with pool.acquire() as c:
        await c.execute('DROP SCHEMA IF EXISTS \"$SCHEMA\" CASCADE')
asyncio.run(drop())
" >/dev/null 2>&1
}
trap cleanup EXIT

# Build a one-shot Python harness: instantiates a PgvectorStore on
# the scratch schema, exposes it as `s`, runs a small async fn `run`
# the test passes in, prints the return value as JSON.
run_test() {
  local body="$1"
  pyrun "
import asyncio, json, uuid
from app.db.postgres import get_pool
from app.services.vector_store.pgvector import PgvectorStore

async def main():
    s = PgvectorStore(
        dsn=None, schema='$SCHEMA', dense_dim=8,
        sparse_shape='posting', get_main_pool=get_pool,
    )
    out = await run(s)
    print(json.dumps(out, default=str))

async def run(s):
$body
asyncio.run(main())
"
}

# ── 1. ensure_collection ─────────────────────────────────────
echo "▸ 1. ensure_collection"
R=$(run_test "    await s.ensure_collection()
    await s.ensure_collection()  # cached, second call is no-op
    pool = await s._pool()
    async with pool.acquire() as c:
        ns_oid = await c.fetchval(
            \"SELECT oid FROM pg_namespace WHERE nspname=\$1\", '$SCHEMA',
        )
        tables = await c.fetchval(
            \"SELECT count(*) FROM pg_class WHERE relkind='r' AND relnamespace=\$1\",
            ns_oid,
        )
        idx = await c.fetchval(
            \"SELECT count(*) FROM pg_class WHERE relkind='i' AND relnamespace=\$1\",
            ns_oid,
        )
    return {'tables': int(tables), 'indexes': int(idx)}")
N_TABLES=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['tables'])")
N_IDX=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['indexes'])")
[ "$N_TABLES" = "2" ] && pass "schema + 2 tables (chunks, posting)" || fail "tables" "got $N_TABLES"
[ "$N_IDX" -ge "3" ] && pass "indexes built (HNSW + posting + source_id, $N_IDX total)" || fail "indexes" "got $N_IDX"

# ── 2. upsert_one ────────────────────────────────────────────
echo "▸ 2. upsert_one (posting shape)"
R=$(run_test "    cid = uuid.uuid4()
    sid = uuid.uuid4()
    await s.upsert_one(
        chunk_id=str(cid), content='hello world', section_path='',
        chunk_index=0, dense=[0.1]*8,
        sparse_indices=[10, 20, 30], sparse_values=[1.0, 0.5, 0.25],
        source_type='document', source_id=str(sid),
    )
    pool = await s._pool()
    async with pool.acquire() as c:
        n_chunks = await c.fetchval(\"SELECT count(*) FROM \\\"$SCHEMA\\\".chunks WHERE chunk_id=\$1\", cid)
        n_post = await c.fetchval(\"SELECT count(*) FROM \\\"$SCHEMA\\\".posting WHERE chunk_id=\$1\", cid)
    return {'cid': str(cid), 'sid': str(sid), 'chunk_rows': int(n_chunks), 'posting_rows': int(n_post)}")
CHUNKS=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['chunk_rows'])")
POSTING=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['posting_rows'])")
[ "$CHUNKS" = "1" ] && pass "1 chunk row inserted" || fail "chunks" "got $CHUNKS"
[ "$POSTING" = "3" ] && pass "3 posting rows (one per term)" || fail "posting" "got $POSTING"

# ── 3-5. search legs + hybrid ────────────────────────────────
echo "▸ 3. search legs + hybrid RRF"
R=$(run_test "    sid = uuid.uuid4()
    sid_other = uuid.uuid4()
    cids = [uuid.uuid4() for _ in range(3)]
    # 3 chunks, distinct sparse terms, dense vectors close to a probe.
    for i, c in enumerate(cids):
        await s.upsert_one(
            chunk_id=str(c), content=f'doc {i}', section_path='', chunk_index=i,
            dense=[1.0 - i*0.1] * 8,
            sparse_indices=[100 + i], sparse_values=[float(i+1)],
            source_type='document', source_id=str(sid),
        )
    # Distractor in another source.
    distractor = uuid.uuid4()
    await s.upsert_one(
        chunk_id=str(distractor), content='wrong vault', section_path='', chunk_index=0,
        dense=[0.9]*8, sparse_indices=[100], sparse_values=[5.0],
        source_type='document', source_id=str(sid_other),
    )
    # Dense-only
    dense_hits = await s.hybrid_search(
        query_text='', query_dense=[1.0]*8,
        query_sparse_indices=[], query_sparse_values=[],
        source_ids=[str(sid)], limit=3, prefetch_per_leg=10,
    )
    # Sparse-only
    sparse_hits = await s.hybrid_search(
        query_text='', query_dense=None,
        query_sparse_indices=[102], query_sparse_values=[1.0],
        source_ids=[str(sid)], limit=3, prefetch_per_leg=10,
    )
    # Hybrid
    hybrid_hits = await s.hybrid_search(
        query_text='', query_dense=[1.0]*8,
        query_sparse_indices=[100, 101, 102], query_sparse_values=[1.0, 1.0, 1.0],
        source_ids=[str(sid)], limit=3, prefetch_per_leg=10,
    )
    return {
        'dense_first': dense_hits[0].chunk_id if dense_hits else None,
        'expected_dense': str(cids[0]),  # closest to [1.0]*8
        'sparse_first': sparse_hits[0].chunk_id if sparse_hits else None,
        'expected_sparse': str(cids[2]),  # term 102, weight 3.0
        'hybrid_count': len(hybrid_hits),
        'distractor_id': str(distractor),
        'distractor_in_results': any(h.chunk_id == str(distractor) for h in dense_hits + sparse_hits + hybrid_hits),
    }")
DENSE_OK=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['dense_first']==d['expected_dense'])")
SPARSE_OK=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['sparse_first']==d['expected_sparse'])")
HYBRID_N=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['hybrid_count'])")
LEAK=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['distractor_in_results'])")
[ "$DENSE_OK" = "True" ] && pass "dense leg returns nearest neighbour" || fail "dense" "$R"
[ "$SPARSE_OK" = "True" ] && pass "sparse leg returns highest BM25 weight" || fail "sparse" "$R"
[ "$HYBRID_N" -ge "1" ] && pass "hybrid leg fuses both ($HYBRID_N hits)" || fail "hybrid" "$R"
[ "$LEAK" = "False" ] && pass "source_id filter excludes distractor" || fail "source_filter" "distractor leaked"

# ── 6. delete_point ──────────────────────────────────────────
echo "▸ 6. delete_point + cascade"
R=$(run_test "    cid = uuid.uuid4()
    sid = uuid.uuid4()
    await s.upsert_one(
        chunk_id=str(cid), content='to-delete', section_path='', chunk_index=0,
        dense=[0.1]*8, sparse_indices=[1, 2], sparse_values=[0.5, 0.5],
        source_type='document', source_id=str(sid),
    )
    await s.delete_point(str(cid))
    pool = await s._pool()
    async with pool.acquire() as c:
        chunks = await c.fetchval(\"SELECT count(*) FROM \\\"$SCHEMA\\\".chunks WHERE chunk_id=\$1\", cid)
        posting = await c.fetchval(\"SELECT count(*) FROM \\\"$SCHEMA\\\".posting WHERE chunk_id=\$1\", cid)
    return {'chunks': int(chunks), 'posting': int(posting)}")
CH=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['chunks'])")
PO=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['posting'])")
[ "$CH" = "0" ] && pass "chunk row removed" || fail "delete-chunk" "got $CH"
[ "$PO" = "0" ] && pass "posting rows cascaded" || fail "delete-cascade" "got $PO"

# ── 7. binary codec round-trip ───────────────────────────────
echo "▸ 7. pgvector binary codec round-trip"
R=$(run_test "    cid = uuid.uuid4()
    sid = uuid.uuid4()
    src_dense = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
    await s.upsert_one(
        chunk_id=str(cid), content='codec', section_path='', chunk_index=0,
        dense=src_dense, sparse_indices=[], sparse_values=[],
        source_type='document', source_id=str(sid),
    )
    pool = await s._pool()
    async with pool.acquire() as c:
        await s._ensure_codec(c)
        row = await c.fetchrow(\"SELECT dense FROM \\\"$SCHEMA\\\".chunks WHERE chunk_id=\$1\", cid)
        # pgvector binary codec returns numpy.ndarray-like (or list)
        dense_back = list(row['dense']) if row['dense'] is not None else []
    return {'len': len(dense_back), 'first': float(dense_back[0]) if dense_back else None}")
LEN=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['len'])")
FIRST=$(echo "$R" | python3 -c "import sys,json; v=json.load(sys.stdin)['first']; print(round(v,2) if v is not None else 0)")
[ "$LEN" = "8" ] && pass "round-trip length 8" || fail "codec-len" "got $LEN"
[ "$FIRST" = "0.1" ] && pass "round-trip first value 0.1" || fail "codec-value" "got $FIRST"

# ── Summary ──────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════"
echo "  Passed: $PASS"
echo "  Failed: $FAIL"
if [ "$FAIL" -gt 0 ]; then
  echo ""
  echo "  Errors:"
  for e in "${ERRORS[@]}"; do echo "    - $e"; done
fi
echo "═══════════════════════════════════════════"

[ "$FAIL" = "0" ] && exit 0 || exit 1
