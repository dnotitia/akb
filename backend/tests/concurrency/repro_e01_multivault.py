"""Repro for the reported 'E01 Multi-vault knowledge burst' failure.

Fires 100 concurrent PUTs + 300 concurrent GETs across N vaults against a
running AKB backend, while polling /livez. The point is to DECODE what the
reporter's harness recorded as "status 0": status 0 means no HTTP response
was obtained, i.e. the client raised before/while talking to the server.
We capture the exact exception class+message for every failure so we can
tell a server fault from client-side connection-pool saturation.

Usage:  AKB_URL=http://localhost:8000 python3 repro_e01_multivault.py [client_max_conns]
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from collections import Counter

import httpx

BASE = os.environ.get("AKB_URL", "http://localhost:8000").rstrip("/")
N_VAULTS = int(os.environ.get("E01_VAULTS", "10"))
N_PUT = int(os.environ.get("E01_PUT", "100"))
N_GET = int(os.environ.get("E01_GET", "300"))
# Client-side connection ceiling. The reporter's harness almost certainly
# used *some* limit; we sweep it to see if status-0 tracks the CLIENT pool.
CLIENT_MAX_CONNS = int(sys.argv[1]) if len(sys.argv) > 1 else 100

STAMP = str(int(time.time()))
U = f"e01-{STAMP}"
PW = "testtest1234"


def pct(xs, p):
    if not xs:
        return 0.0
    s = sorted(xs)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


async def record(coro):
    """Run a request coro, return (status:int, latency_ms:float, err:str|None).

    status 0 == no HTTP response (exception). err carries the decoded cause.
    """
    t0 = time.perf_counter()
    try:
        r = await coro
        dt = (time.perf_counter() - t0) * 1000
        return r.status_code, dt, None
    except Exception as e:  # noqa: BLE001 -- we WANT to see every failure mode
        dt = (time.perf_counter() - t0) * 1000
        return 0, dt, f"{type(e).__name__}: {e}"


async def main():
    print(f"BASE={BASE}  vaults={N_VAULTS}  put={N_PUT}  get={N_GET}  client_max_conns={CLIENT_MAX_CONNS}")
    limits = httpx.Limits(max_connections=CLIENT_MAX_CONNS, max_keepalive_connections=CLIENT_MAX_CONNS)
    timeout = httpx.Timeout(60.0, connect=60.0)

    async with httpx.AsyncClient(base_url=BASE, limits=limits, timeout=timeout) as c:
        # ---- setup: user + PAT ----
        await c.post("/api/v1/auth/register", json={"username": U, "email": f"{U}@t.local", "password": PW})
        jwt = (await c.post("/api/v1/auth/login", json={"username": U, "password": PW})).json()["token"]
        H = {"Authorization": f"Bearer {jwt}"}
        pat = (await c.post("/api/v1/auth/tokens", headers=H, json={"name": "p"})).json()["token"]
        A = {"Authorization": f"Bearer {pat}"}

        # ---- setup: N vaults ----
        vaults = [f"e01v-{STAMP}-{i}" for i in range(N_VAULTS)]
        for v in vaults:
            await c.post(f"/api/v1/vaults?name={v}", headers=A)

        # ---- livez poller during the burst ----
        stop = asyncio.Event()
        livez_codes: Counter = Counter()
        livez_lat: list[float] = []

        async def poll_livez():
            while not stop.is_set():
                st, dt, _ = await record(c.get("/livez"))
                livez_codes[st] += 1
                if st == 200:
                    livez_lat.append(dt)
                await asyncio.sleep(0.001)

        livez_task = asyncio.create_task(poll_livez())

        # ---- BURST 1: 100 concurrent PUTs across vaults ----
        def put_body(i):
            v = vaults[i % N_VAULTS]
            return v, {
                "vault": v, "collection": "burst",
                "title": f"doc-{i}", "content": f"# doc {i}\n\nbody {i} " + ("x" * 200),
                "type": "note", "slug": f"doc-{i}",
            }

        async def do_put(i):
            v, body = put_body(i)
            return await record(c.post("/api/v1/documents", headers=A, json=body))

        put_res = await asyncio.gather(*[do_put(i) for i in range(N_PUT)])

        # collect the paths that succeeded so GETs hit real docs
        ok_targets = []
        for i in range(N_PUT):
            st, _, _ = put_res[i]
            if st in (200, 201):
                v, _ = put_body(i)
                ok_targets.append((v, f"burst/doc-{i}.md"))

        # ---- BURST 2: 300 concurrent GETs ----
        async def do_get(j):
            if ok_targets:
                v, docid = ok_targets[j % len(ok_targets)]
            else:
                v, docid = vaults[j % N_VAULTS], "burst/doc-0.md"
            return await record(c.get(f"/api/v1/documents/{v}/{docid}", headers=A))

        get_res = await asyncio.gather(*[do_get(j) for j in range(N_GET)])

        stop.set()
        await livez_task

        # ---- report ----
        def summarize(name, res):
            codes = Counter(st for st, _, _ in res)
            lat = [dt for st, dt, _ in res if st in (200, 201)]
            errs = Counter(err for st, _, err in res if err)
            print(f"\n  {name}: count={len(res)} statuses={dict(codes)} "
                  f"p50={pct(lat,50):.1f}ms p95={pct(lat,95):.1f}ms max={(max(lat) if lat else 0):.1f}ms")
            for err, n in errs.most_common():
                print(f"      [status0 cause x{n}] {err}")
            # show a couple of non-2xx HTTP bodies if any
            return codes

        summarize("PUT", put_res)
        summarize("GET", get_res)
        print(f"\n  livez: count={sum(livez_codes.values())} statuses={dict(livez_codes)} "
              f"p50={pct(livez_lat,50):.1f}ms p95={pct(livez_lat,95):.1f}ms max={(max(livez_lat) if livez_lat else 0):.1f}ms")

        # cleanup vaults
        for v in vaults:
            try:
                await c.request("DELETE", f"/api/v1/vaults/{v}", headers=A)
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(main())
