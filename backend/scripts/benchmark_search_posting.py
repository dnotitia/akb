"""Local-only synthetic posting-query experiment (no production data or model calls).

Starts no services. Requires disposable PostgreSQL on localhost:15434 with
the postgres role using local trust. Owns one randomly named database and drops
only that database on exit. Prints JSON timings and checks exact ranking parity.
"""

import argparse
import asyncio
import json
import statistics
import time
import uuid

import asyncpg


BASE = """
WITH q AS (SELECT unnest($1::bigint[]) tid, unnest($2::real[]) w)
SELECT p.chunk_id, SUM(q.w*p.weight) score FROM posting p
JOIN q ON q.tid=p.term_id JOIN chunks c ON c.chunk_id=p.chunk_id
WHERE c.vault_id=ANY($3::uuid[]) {restriction}
GROUP BY p.chunk_id ORDER BY score DESC,p.chunk_id LIMIT $4
"""


async def main(rows, repeats):
    database = f"search_bench_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(host="127.0.0.1", port=15434, user="postgres", database="postgres")
    await admin.execute(f'CREATE DATABASE "{database}"')
    conn = None
    try:
        conn = await asyncpg.connect(host="127.0.0.1", port=15434, user="postgres", database=database)
        await conn.execute("SET statement_timeout='60s'")
        await conn.execute("CREATE TABLE chunks(chunk_id uuid PRIMARY KEY,vault_id uuid,source_id uuid,content text)")
        await conn.execute(
            "CREATE TABLE posting(term_id bigint,chunk_id uuid REFERENCES chunks,weight real,PRIMARY KEY(term_id,chunk_id))"
        )
        await conn.execute("CREATE INDEX idx_posting_term ON posting(term_id)")
        await conn.execute("CREATE INDEX idx_posting_chunk ON posting(chunk_id)")
        await conn.execute("CREATE INDEX idx_chunks_vault ON chunks(vault_id)")
        started = time.perf_counter()
        await conn.execute(
            """INSERT INTO chunks SELECT md5(i::text)::uuid,md5(('v'||(i%100))::text)::uuid,
            md5(('s'||i)::text)::uuid,repeat('synthetic ',100) FROM generate_series(1,$1::int)i""",
            rows,
        )
        await conn.execute(
            """INSERT INTO posting SELECT CASE WHEN t<=4 THEN t ELSE 5+((i*17+t)%10000) END,
            md5(i::text)::uuid,(1+(i%991)::real/1024+t::real/128) FROM generate_series(1,$1::int)i CROSS JOIN generate_series(1,64)t""",
            rows,
        )
        await conn.execute("VACUUM ANALYZE chunks")
        await conn.execute("VACUUM ANALYZE posting")
        print(
            json.dumps({"rows": rows, "postings": rows * 64, "setup_s": round(time.perf_counter() - started, 2)}),
            flush=True,
        )
        vaults = await conn.fetch("SELECT DISTINCT vault_id FROM chunks ORDER BY vault_id")
        expected = {}
        for phase in ["baseline", "covering"]:
            if phase == "covering":
                await conn.execute("CREATE INDEX idx_posting_covering ON posting(term_id,chunk_id) INCLUDE(weight)")
                await conn.execute("CREATE INDEX idx_chunks_covering ON chunks(vault_id) INCLUDE(chunk_id)")
            size = await conn.fetchval("SELECT pg_total_relation_size('posting')")
            print(json.dumps({"phase": phase, "posting_bytes": size}), flush=True)
            for plan in ["force_custom_plan", "force_generic_plan"]:
                await conn.execute(f"SET plan_cache_mode={plan}")
                for label, terms in [("rare", [515, 9915]), ("mixed", [1, 515, 9915]), ("common", [1, 2, 3])]:
                    for scope in [1, 10, 100]:
                        args = (
                            terms,
                            [1.0 + index for index in range(len(terms))],
                            [r["vault_id"] for r in vaults[:scope]],
                            90,
                        )
                        sql = BASE.format(restriction="")
                        statement = await conn.prepare(sql)
                        timings = []
                        for _ in range(repeats + 1):
                            t = time.perf_counter()
                            result = await statement.fetch(*args)
                            timings.append((time.perf_counter() - t) * 1000)
                        values = [(str(r["chunk_id"]), r["score"]) for r in result]
                        key = (plan, label, scope)
                        if phase == "baseline":
                            expected[key] = values
                        assert values == expected[key], (phase, key, "rank mismatch")
                        print(
                            json.dumps(
                                {
                                    "phase": phase,
                                    "plan": plan,
                                    "terms": label,
                                    "scope_percent": scope,
                                    "p50_ms": round(statistics.median(timings[1:]), 2),
                                    "max_ms": round(max(timings[1:]), 2),
                                    "results": len(values),
                                }
                            ),
                            flush=True,
                        )
        print(json.dumps({"rank_parity": "passed", "cases": len(expected)}), flush=True)
    finally:
        if conn:
            await conn.close()
        await admin.execute(f'DROP DATABASE "{database}"')
        await admin.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=100000)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.rows < 100 or args.repeats < 1:
        parser.error("rows must be >= 100 and repeats >= 1")
    asyncio.run(main(args.rows, args.repeats))
