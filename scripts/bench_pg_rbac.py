#!/usr/bin/env python3
"""Microbenchmarks for the PG-native RBAC overhead.

Numbers we care about, since they feed the design doc's acceptance
criteria + every future "is per-user role SET LOCAL really cheap?"
question:

  1. SET LOCAL ROLE per transaction — overhead vs a plain transaction.
     The akb_sql hot path adds exactly one `SET LOCAL` per call; the
     claim in `docs/designs/pg-native-rbac/` is ~10 µs.
  2. CREATE/DROP ROLE round-trip — measured on the user lifecycle path
     (signup / delete). Dominates if you have a massive churn.
  3. on_grant + on_revoke pair — what an `akb_grant` / `akb_revoke`
     call costs in raw PG round-trips.
  4. reconcile_from_catalog at scale — startup cost with N users and
     M vaults. Useful to spot O(N²) regressions before prod.

Run inside the backend container so it shares the working DSN:

  docker compose exec -T \\
    -e AKB_TEST_DSN=postgresql://akb:akb@postgres:5432/akb \\
    backend python3 scripts/bench_pg_rbac.py

Pass `--users N --vaults M` to size the reconcile workload; the
synthetic users / vaults are dropped at the end so the dev DB stays
clean.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import time
import uuid

import asyncpg

# Make `app.*` importable when run from the backend container.
import sys
sys.path.insert(0, "/app")  # noqa: E402

from app.services.role_sync import (  # noqa: E402
    AUTHENTICATED_ROLE,
    RoleSync,
    user_role_name,
    vault_group_role_name,
)


_DSN = os.environ.get("AKB_TEST_DSN", "postgresql://akb:akb@postgres:5432/akb")


def _stats(samples_us: list[float], label: str) -> dict:
    samples_us.sort()
    n = len(samples_us)
    p50 = samples_us[n // 2]
    p95 = samples_us[int(n * 0.95)]
    p99 = samples_us[int(n * 0.99)] if n >= 100 else samples_us[-1]
    return {
        "label": label,
        "n": n,
        "mean_us": round(statistics.mean(samples_us), 2),
        "p50_us": round(p50, 2),
        "p95_us": round(p95, 2),
        "p99_us": round(p99, 2),
        "min_us": round(min(samples_us), 2),
        "max_us": round(max(samples_us), 2),
    }


async def bench_set_local_role(pool: asyncpg.Pool, n: int = 10_000) -> tuple[dict, dict]:
    """Overhead of `SET LOCAL ROLE` per transaction.

    Compares: a plain `BEGIN; SELECT 1; COMMIT` versus the same loop
    with `SET LOCAL ROLE akbuser` injected. Reusing akbuser (the
    connection's existing role) means PG validates membership but
    doesn't actually switch — that's a strict-lower-bound for the
    real akb_user_<uid> path."""
    plain = []
    with_set = []

    async with pool.acquire() as conn:
        # Pick the connection's current role for the SET LOCAL no-op.
        current_role = await conn.fetchval("SELECT current_user")
        set_local_sql = f'SET LOCAL ROLE "{current_role}"'

        # Warm-up.
        for _ in range(50):
            async with conn.transaction():
                await conn.execute("SELECT 1")

        for _ in range(n):
            t0 = time.perf_counter_ns()
            async with conn.transaction():
                await conn.execute("SELECT 1")
            plain.append((time.perf_counter_ns() - t0) / 1_000.0)

        for _ in range(n):
            t0 = time.perf_counter_ns()
            async with conn.transaction():
                await conn.execute(set_local_sql)
                await conn.execute("SELECT 1")
            with_set.append((time.perf_counter_ns() - t0) / 1_000.0)

    return _stats(plain, "tx plain"), _stats(with_set, "tx + SET LOCAL ROLE")


async def bench_role_lifecycle(pool: asyncpg.Pool, n: int = 500) -> dict:
    """CREATE ROLE + DROP ROLE round-trip via RoleSync hooks."""
    rs = RoleSync(pool)
    durations = []
    uids = [uuid.uuid4() for _ in range(n)]
    for uid in uids:
        t0 = time.perf_counter_ns()
        await rs.on_user_create(uid)
        durations.append((time.perf_counter_ns() - t0) / 1_000.0)
    # Drop pass.
    drop_durations = []
    for uid in uids:
        t0 = time.perf_counter_ns()
        await rs.on_user_delete(uid)
        drop_durations.append((time.perf_counter_ns() - t0) / 1_000.0)
    return {
        "create": _stats(durations, "on_user_create"),
        "drop": _stats(drop_durations, "on_user_delete"),
    }


async def bench_grant_revoke(pool: asyncpg.Pool, n: int = 200) -> dict:
    rs = RoleSync(pool)
    vid = uuid.uuid4()
    uids = [uuid.uuid4() for _ in range(n)]
    await rs.on_vault_create(vid, owner_user_id=None)
    for uid in uids:
        await rs.on_user_create(uid)

    grant_us = []
    revoke_us = []
    for uid in uids:
        t0 = time.perf_counter_ns()
        await rs.on_grant(vid, uid, "reader")
        grant_us.append((time.perf_counter_ns() - t0) / 1_000.0)
        t0 = time.perf_counter_ns()
        await rs.on_revoke(vid, uid)
        revoke_us.append((time.perf_counter_ns() - t0) / 1_000.0)

    # Cleanup.
    for uid in uids:
        await rs.on_user_delete(uid)
    await rs.on_vault_delete(vid)

    return {
        "grant": _stats(grant_us, "on_grant(reader)"),
        "revoke": _stats(revoke_us, "on_revoke"),
    }


async def bench_reconcile_at_scale(
    pool: asyncpg.Pool, users: int = 100, vaults: int = 50,
) -> dict:
    """Synthetic-load reconcile. Insert N users + M vaults +
    user-vault grants directly into the catalog, drop the
    corresponding PG roles, then measure how long
    `reconcile_from_catalog` takes to converge."""
    rs = RoleSync(pool)
    user_ids = [uuid.uuid4() for _ in range(users)]
    vault_ids = [uuid.uuid4() for _ in range(vaults)]

    async with pool.acquire() as conn:
        async with conn.transaction():
            for uid in user_ids:
                await conn.execute(
                    """
                    INSERT INTO users (id, username, email, password_hash)
                    VALUES ($1, $2, $3, 'x')
                    """,
                    uid, f"_bench_u_{uid.hex[:10]}", f"_bench_u_{uid.hex[:10]}@bench",
                )
            for vid in vault_ids:
                await conn.execute(
                    """
                    INSERT INTO vaults (id, name, description, git_path, owner_id)
                    VALUES ($1, $2, '', $3, $4)
                    """,
                    vid, f"_bench_v_{vid.hex[:10]}", f"/tmp/_bench/{vid.hex[:10]}.git",
                    user_ids[0],
                )
            # Sparse grants: each user gets reader on ~5 random vaults.
            import random
            for uid in user_ids:
                for vid in random.sample(vault_ids, min(5, len(vault_ids))):
                    await conn.execute(
                        """
                        INSERT INTO vault_access (id, vault_id, user_id, role, granted_by)
                        VALUES ($1, $2, $3, 'reader', $4)
                        ON CONFLICT DO NOTHING
                        """,
                        uuid.uuid4(), vid, uid, user_ids[0],
                    )
    try:
        t0 = time.perf_counter_ns()
        report = await rs.reconcile_from_catalog()
        elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000.0
        return {
            "users": users,
            "vaults": vaults,
            "elapsed_ms": round(elapsed_ms, 2),
            "report": str(report),
        }
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM vault_access WHERE user_id = ANY($1::uuid[])",
                user_ids,
            )
            await conn.execute(
                "DELETE FROM vaults WHERE id = ANY($1::uuid[])", vault_ids,
            )
            await conn.execute(
                "DELETE FROM users WHERE id = ANY($1::uuid[])", user_ids,
            )
        # Drop synthetic PG roles created by the reconcile.
        for uid in user_ids:
            await rs.on_user_delete(uid)
        for vid in vault_ids:
            await rs.on_vault_delete(vid)


async def main(args):
    pool = await asyncpg.create_pool(dsn=_DSN, min_size=1, max_size=4)
    try:
        print(f"DSN: {_DSN}")
        print()
        print("== 1. SET LOCAL ROLE overhead ==")
        plain, with_set = await bench_set_local_role(pool, n=args.tx_iters)
        for s in (plain, with_set):
            print(f"  {s['label']:<28} mean={s['mean_us']:>8.1f} µs  "
                  f"p50={s['p50_us']:>8.1f}  p95={s['p95_us']:>8.1f}  "
                  f"p99={s['p99_us']:>8.1f}")
        delta = with_set["p50_us"] - plain["p50_us"]
        print(f"  → p50 SET LOCAL ROLE overhead ≈ {delta:.1f} µs")
        print()

        print("== 2. on_user_create / on_user_delete ==")
        lc = await bench_role_lifecycle(pool, n=args.lifecycle_iters)
        for s in (lc["create"], lc["drop"]):
            print(f"  {s['label']:<28} mean={s['mean_us']:>8.1f} µs  "
                  f"p50={s['p50_us']:>8.1f}  p95={s['p95_us']:>8.1f}")
        print()

        print("== 3. on_grant / on_revoke ==")
        gr = await bench_grant_revoke(pool, n=args.grant_iters)
        for s in (gr["grant"], gr["revoke"]):
            print(f"  {s['label']:<28} mean={s['mean_us']:>8.1f} µs  "
                  f"p50={s['p50_us']:>8.1f}  p95={s['p95_us']:>8.1f}")
        print()

        print("== 4. reconcile_from_catalog at scale ==")
        rec = await bench_reconcile_at_scale(
            pool, users=args.users, vaults=args.vaults,
        )
        print(f"  users={rec['users']} vaults={rec['vaults']} → "
              f"{rec['elapsed_ms']:.1f} ms")
        print(f"  report: {rec['report']}")
    finally:
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tx-iters", type=int, default=2000)
    parser.add_argument("--lifecycle-iters", type=int, default=200)
    parser.add_argument("--grant-iters", type=int, default=100)
    parser.add_argument("--users", type=int, default=50)
    parser.add_argument("--vaults", type=int, default=25)
    args = parser.parse_args()
    asyncio.run(main(args))
