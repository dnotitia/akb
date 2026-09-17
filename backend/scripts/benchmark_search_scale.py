"""Local scale/cache experiment; only the dedicated akb-search-scale-pg container.

build creates search_scale in a NEW disposable loopback database server.
measure restarts that container per case (PG buffers cold, OS cache UNKNOWN).
cover adds indexes explicitly. No production data, model calls or OS cache purge.
See docs/designs/search-performance.md for resource requirements and caveats.
"""

import argparse
import asyncio
import json
import subprocess
import time
import uuid

import asyncpg
import numpy as np
from pgvector.asyncpg import register_vector

CONTAINER = "akb-search-scale-pg"
DATABASE = "search_scale"
SPARSE = """WITH q AS (SELECT unnest($1::bigint[]) tid,unnest($2::real[]) w)
SELECT p.chunk_id,SUM(q.w*p.weight) score FROM posting p
JOIN q ON q.tid=p.term_id JOIN chunks c ON c.chunk_id=p.chunk_id
WHERE c.vault_id=ANY($3::uuid[]) GROUP BY p.chunk_id ORDER BY score DESC LIMIT 270"""
DENSE = """SELECT chunk_id FROM chunks WHERE vault_id=ANY($2::uuid[]) AND dense IS NOT NULL
ORDER BY dense <=> $1 LIMIT 270"""


def report(**data):
    print(json.dumps(data), flush=True)


def check_container():
    info = json.loads(subprocess.check_output(["docker", "inspect", CONTAINER]))[0]
    ports = info["HostConfig"]["PortBindings"]
    if ports.get("5432/tcp") != [{"HostIp": "127.0.0.1", "HostPort": "15434"}]:
        raise RuntimeError("Dedicated loopback container/port required")


async def connect(database=DATABASE):
    return await asyncpg.connect(host="127.0.0.1", port=15434, user="postgres", database=database)


def centers():
    return np.random.default_rng(223).standard_normal((32, 1024), dtype=np.float32)


async def build(rows, postings):
    admin = await connect("postgres")
    try:
        await admin.execute(f'CREATE DATABASE "{DATABASE}"')
    finally:
        await admin.close()
    conn = await connect()
    try:
        await conn.execute("CREATE EXTENSION vector")
        await register_vector(conn)
        await conn.execute(
            "CREATE TABLE chunks(chunk_id uuid PRIMARY KEY,vault_id uuid,source_id uuid,content text,dense vector(1024))"
        )
        await conn.execute("CREATE TABLE posting(term_id bigint,chunk_id uuid NOT NULL,weight real NOT NULL)")
        rng, cluster = np.random.default_rng(238), centers()
        started = time.perf_counter()
        for start in range(1, rows + 1, 2000):
            ids = range(start, min(start + 2000, rows + 1))
            dense = rng.standard_normal((len(ids), 1024), dtype=np.float32) * 0.1
            dense += cluster[np.arange(start, start + len(ids)) % 32]
            await conn.copy_records_to_table(
                "chunks",
                records=[
                    (
                        uuid.UUID(int=i),
                        uuid.UUID(int=1 + i % 100),
                        uuid.UUID(int=i),
                        "synthetic search corpus " * 40,
                        dense[j],
                    )
                    for j, i in enumerate(ids)
                ],
            )
            if start == 1 or (start - 1) % 100000 == 0:
                report(stage="vectors", loaded=start + len(ids) - 1, elapsed_s=round(time.perf_counter() - started, 1))
        per_row, extra = divmod(postings, rows)
        for start in range(1, rows + 1, 10000):
            await conn.execute(
                """INSERT INTO posting
                SELECT CASE WHEN t<=4 THEN t ELSE 5+((i*73+t)%683356) END,
                lpad(to_hex(i),32,'0')::uuid,1+(i%991)::real/1024+t::real/128
                FROM generate_series($1::bigint,$2::bigint)i
                CROSS JOIN LATERAL generate_series(1,$3::int+CASE WHEN i<=$4 THEN 1 ELSE 0 END)t""",
                start,
                min(start + 9999, rows),
                per_row,
                extra,
            )
            if start == 1 or (start - 1) % 100000 == 0:
                report(
                    stage="postings",
                    chunks_loaded=min(start + 9999, rows),
                    elapsed_s=round(time.perf_counter() - started, 1),
                )
        for sql in [
            "ALTER TABLE posting ADD PRIMARY KEY(term_id,chunk_id)",
            "ALTER TABLE posting ADD FOREIGN KEY(chunk_id) REFERENCES chunks(chunk_id) ON DELETE CASCADE",
            "CREATE INDEX idx_posting_term ON posting(term_id)",
            "CREATE INDEX idx_posting_chunk ON posting(chunk_id)",
            "CREATE INDEX idx_chunks_vault ON chunks(vault_id)",
            "CREATE INDEX idx_chunks_source ON chunks(source_id)",
            "CREATE INDEX idx_dense ON chunks USING hnsw(dense vector_cosine_ops) WITH(m=16,ef_construction=64) WHERE dense IS NOT NULL",
            "VACUUM ANALYZE chunks",
            "VACUUM ANALYZE posting",
        ]:
            report(stage="ddl_start", sql=sql)
            if "USING hnsw" in sql:
                # Build-only budget: avoid spilling the ~4.5 GiB synthetic graph.
                # Single-worker construction avoids allocating it in /dev/shm.
                await conn.execute("SET maintenance_work_mem='5GB'")
                await conn.execute("SET max_parallel_maintenance_workers=0")
            await conn.execute(sql)
            report(stage="ddl_done", elapsed_s=round(time.perf_counter() - started, 1))
        report(
            stage="complete",
            rows=rows,
            postings=postings,
            database_bytes=await conn.fetchval("SELECT pg_database_size(current_database())"),
        )
    finally:
        await conn.close()


async def restart():
    await asyncio.to_thread(subprocess.run, ["docker", "restart", CONTAINER], check=True, capture_output=True)
    for _ in range(60):
        try:
            conn = await connect()
            return conn
        except OSError, asyncpg.PostgresError:
            await asyncio.sleep(1)
    raise RuntimeError("Dedicated test database did not restart")


async def explain(conn, sql, args):
    async with conn.transaction():
        await conn.execute("SET LOCAL hnsw.iterative_scan=relaxed_order")
        await conn.execute("SET LOCAL hnsw.ef_search=200")
        await conn.execute("SET LOCAL statement_timeout='180s'")
        data = json.loads(await conn.fetchval("EXPLAIN (ANALYZE,BUFFERS,TIMING OFF,FORMAT JSON) " + sql, *args))[0]
    plan = data["Plan"]

    def scans(node):
        current = (
            [
                {
                    key: node[key]
                    for key in (
                        "Node Type",
                        "Index Name",
                        "Actual Rows",
                        "Shared Read Blocks",
                        "Heap Fetches",
                        "Rows Removed by Filter",
                    )
                    if key in node
                }
            ]
            if "Scan" in node["Node Type"]
            else []
        )
        return current + [entry for child in node.get("Plans", []) for entry in scans(child)]

    return {
        "execution_ms": data["Execution Time"],
        "rows": plan["Actual Rows"],
        **{
            key: plan.get(key, 0)
            for key in [
                "Shared Hit Blocks",
                "Shared Read Blocks",
                "I/O Read Time",
                "Temp Read Blocks",
                "Temp Written Blocks",
            ]
        },
        "plan": scans(plan),
    }


async def measure(label, repeats):
    for kind in ["dense", "sparse"]:
        for scope in [1, 10, 100]:
            conn = await restart()
            try:
                await register_vector(conn)
                # Keep planning policy fixed while comparing cache states.
                await conn.execute("SET plan_cache_mode=force_custom_plan")
                vaults = [uuid.UUID(int=i) for i in range(1, scope + 1)]
                sql, args = (
                    (DENSE, (centers()[0], vaults))
                    if kind == "dense"
                    else (SPARSE, ([1, 515, 9915], [1.0, 2.0, 3.0], vaults))
                )
                for run in range(repeats + 1):
                    result = await explain(conn, sql, args)
                    # Keep the plan for the first/last run to identify scan choices.
                    if run not in {0, repeats}:
                        result.pop("plan")
                    report(
                        stage="query",
                        label=label,
                        kind=kind,
                        scope_percent=scope,
                        cache="pg_cold_os_unknown" if run == 0 else "repeat",
                        run=run,
                        **result,
                    )
                # Different queries and opposite leg compete for the same cache.
                for group in range(4):
                    await explain(conn, DENSE, (centers()[group], vaults))
                    await explain(conn, SPARSE, ([1, 5 + group * 997], [1.0, 2.0], vaults))
                report(
                    stage="query",
                    label=label,
                    kind=kind,
                    scope_percent=scope,
                    cache="after_mixed_workload",
                    **await explain(conn, sql, args),
                )
            finally:
                await conn.close()
    dense_conn = await restart()
    sparse_conn = await connect()
    try:
        await register_vector(dense_conn)
        for conn in [dense_conn, sparse_conn]:
            await conn.execute("SET plan_cache_mode=force_custom_plan")
        vaults = [uuid.UUID(int=i) for i in range(1, 11)]
        for run in range(repeats + 1):
            started = time.perf_counter()
            dense_result, sparse_result = await asyncio.gather(
                explain(dense_conn, DENSE, (centers()[0], vaults)),
                explain(sparse_conn, SPARSE, ([1, 515, 9915], [1.0, 2.0, 3.0], vaults)),
            )
            report(
                stage="parallel_legs",
                label=label,
                scope_percent=10,
                run=run,
                cache="pg_cold_os_unknown" if run == 0 else "repeat",
                wall_ms=round((time.perf_counter() - started) * 1000, 2),
                dense=dense_result,
                sparse=sparse_result,
            )
    finally:
        await dense_conn.close()
        await sparse_conn.close()


async def cover():
    conn = await connect()
    try:
        for sql in [
            "CREATE INDEX idx_posting_covering ON posting(term_id,chunk_id) INCLUDE(weight)",
            "CREATE INDEX idx_chunks_covering ON chunks(vault_id) INCLUDE(chunk_id)",
        ]:
            report(stage="ddl_start", sql=sql)
            await conn.execute(sql)
        report(
            stage="cover_complete", database_bytes=await conn.fetchval("SELECT pg_database_size(current_database())")
        )
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["build", "measure", "cover"])
    parser.add_argument("--rows", type=int, default=997935)
    parser.add_argument("--postings", type=int, default=71536566)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--label", default="baseline")
    args = parser.parse_args()
    if args.rows < 100 or args.postings < args.rows * 4 or args.postings > args.rows * 683360 or args.repeats < 1:
        parser.error("Require rows>=100, 4..683360 postings per row, repeats>=1")
    check_container()
    asyncio.run(
        build(args.rows, args.postings)
        if args.mode == "build"
        else measure(args.label, args.repeats)
        if args.mode == "measure"
        else cover()
    )
