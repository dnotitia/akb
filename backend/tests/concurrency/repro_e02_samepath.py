"""Repro for the reported 'E02: Duplicate ingestion of the same source page'.

Fires N concurrent PUTs to the EXACT SAME (vault, collection, slug) — i.e. the
same logical document path — and reports the outcome distribution. Correct
behaviour under the (vault_id, path) advisory lock is: exactly ONE PUT wins
(200/201) and the rest get 409 Conflict; none should be a transport error.

The reporter saw all 12 as transport-level "status 0". This decodes the
actual exception behind status 0 and shows which (if any) PUT won.

Usage:  AKB_URL=http://localhost:8000 python3 repro_e02_samepath.py [N] [client_max_conns]
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from collections import Counter

import httpx

BASE = os.environ.get("AKB_URL", "http://localhost:8000").rstrip("/")
N = int(sys.argv[1]) if len(sys.argv) > 1 else 12
CLIENT_MAX_CONNS = int(sys.argv[2]) if len(sys.argv) > 2 else 50

STAMP = str(int(time.time()))
U = f"e02-{STAMP}"
PW = "testtest1234"


async def record(coro):
    t0 = time.perf_counter()
    try:
        r = await coro
        dt = (time.perf_counter() - t0) * 1000
        body = ""
        try:
            body = r.text[:140]
        except Exception:
            pass
        return r.status_code, dt, None, body
    except Exception as e:  # noqa: BLE001
        dt = (time.perf_counter() - t0) * 1000
        return 0, dt, f"{type(e).__name__}: {e}", ""


async def main():
    print(f"BASE={BASE}  N={N}  client_max_conns={CLIENT_MAX_CONNS}")
    limits = httpx.Limits(max_connections=CLIENT_MAX_CONNS, max_keepalive_connections=CLIENT_MAX_CONNS)
    timeout = httpx.Timeout(60.0, connect=60.0)

    async with httpx.AsyncClient(base_url=BASE, limits=limits, timeout=timeout) as c:
        await c.post("/api/v1/auth/register", json={"username": U, "email": f"{U}@t.local", "password": PW})
        jwt = (await c.post("/api/v1/auth/login", json={"username": U, "password": PW})).json()["token"]
        H = {"Authorization": f"Bearer {jwt}"}
        pat = (await c.post("/api/v1/auth/tokens", headers=H, json={"name": "p"})).json()["token"]
        A = {"Authorization": f"Bearer {pat}"}

        v = f"e02v-{STAMP}"
        await c.post(f"/api/v1/vaults?name={v}", headers=A)

        # identical body -> identical (vault, collection, slug) -> identical path
        body = {
            "vault": v, "collection": "ingest",
            "title": "Same Source Page", "content": "# same\n\nidentical body " + ("x" * 200),
            "type": "note", "slug": "same-source-page",
        }

        # livez poller to detect server-wide stalls during the burst
        stop = asyncio.Event()
        livez_lat: list[float] = []
        livez_codes: Counter = Counter()

        async def poll_livez():
            while not stop.is_set():
                st, dt, _, _ = await record(c.get("/livez"))
                livez_codes[st] += 1
                if st == 200:
                    livez_lat.append(dt)
                await asyncio.sleep(0.005)

        livez_task = asyncio.create_task(poll_livez())

        async def do_put(i):
            st, dt, err, b = await record(c.post("/api/v1/documents", headers=A, json=body))
            return i, st, dt, err, b

        res = await asyncio.gather(*[do_put(i) for i in range(N)])

        stop.set()
        await livez_task

        codes = Counter(r[1] for r in res)
        succ = [r for r in res if r[1] in (200, 201)]
        conflicts = [r for r in res if r[1] == 409]
        zeros = [r for r in res if r[1] == 0]
        others = [r for r in res if r[1] not in (200, 201, 409, 0)]

        print(f"\n  duplicate_put: count={N} statuses={dict(codes)}")
        print(f"  successes={len(succ)}  conflicts(409)={len(conflicts)}  status0={len(zeros)}  other={len(others)}")
        if succ:
            print(f"  winner index={succ[0][0]} latency={succ[0][2]:.1f}ms")
        # decode status-0 exceptions
        errc = Counter(r[3] for r in zeros if r[3])
        for err, n in errc.most_common():
            print(f"      [status0 cause x{n}] {err}")
        # show a couple of 409 bodies + any 'other' bodies
        if conflicts:
            print(f"      [409 sample] {conflicts[0][4]}")
        for r in others:
            print(f"      [HTTP {r[1]}] idx={r[0]} {r[4]}")
        lat_lo = min((r[2] for r in res if r[1] != 0), default=0)
        lat_hi = max((r[2] for r in res if r[1] != 0), default=0)
        print(f"  non-zero latency range: {lat_lo:.0f}..{lat_hi:.0f}ms")
        print(f"  livez: {dict(livez_codes)} p50={(sorted(livez_lat)[len(livez_lat)//2] if livez_lat else 0):.1f}ms "
              f"max={(max(livez_lat) if livez_lat else 0):.1f}ms")

        # verdict
        ok = len(succ) == 1 and len(conflicts) == N - 1 and len(zeros) == 0
        print(f"\n  VERDICT: {'PASS' if ok else 'FAIL'} "
              f"(expect successes=1, conflicts={N-1}, status0=0)")

        try:
            await c.request("DELETE", f"/api/v1/vaults/{v}", headers=A)
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
