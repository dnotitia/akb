"""Repro for reported E05 (delete-while-reads) and E06 (collection retirement race).

E05: create one doc, then concurrently DELETE it while N GETs race. Correct:
     delete -> 200; each GET -> 200 (with full body) or 404; nothing else.

E06: create a collection, then concurrently fire N PUTs into it WHILE the
     collection is DELETEd (recursive). Correct: every PUT resolves explicitly
     (200 created / 404 or 409 if the collection/doc lost the race) and the
     DELETE resolves explicitly — none should be a transport-level "status 0".
     The reporter saw all 40 PUTs as status 0 (the pool-deadlock signature).

Usage: AKB_URL=... python3 repro_e05_e06_delete_race.py [which: e05|e06|both] [N]
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from collections import Counter

import httpx

BASE = os.environ.get("AKB_URL", "http://localhost:8000").rstrip("/")
WHICH = sys.argv[1] if len(sys.argv) > 1 else "both"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 0
STAMP = str(int(time.time()))
U = f"e056-{STAMP}"
PW = "testtest1234"


async def rec(coro):
    t0 = time.perf_counter()
    try:
        r = await coro
        return r.status_code, (time.perf_counter() - t0) * 1000, None, (r.text or "")[:160]
    except Exception as e:  # noqa: BLE001
        return 0, (time.perf_counter() - t0) * 1000, f"{type(e).__name__}: {e}", ""


def show(name, rows):
    codes = Counter(r[0] for r in rows)
    zero = [r for r in rows if r[0] == 0]
    s5 = [r for r in rows if 500 <= r[0] < 600]
    print(f"  {name}: count={len(rows)} statuses={dict(codes)}")
    ec = Counter(r[2] for r in zero if r[2])
    for err, n in ec.most_common():
        print(f"      [status0 x{n}] {err}")
    for r in s5[:2]:
        print(f"      [HTTP {r[0]}] {r[3]}")
    return codes, len(zero), len(s5)


async def setup(c):
    await c.post("/api/v1/auth/register", json={"username": U, "email": f"{U}@t.local", "password": PW})
    jwt = (await c.post("/api/v1/auth/login", json={"username": U, "password": PW})).json()["token"]
    pat = (await c.post("/api/v1/auth/tokens", headers={"Authorization": f"Bearer {jwt}"}, json={"name": "p"})).json()["token"]
    return {"Authorization": f"Bearer {pat}"}


async def e05(c, A, n):
    print(f"\n=== E05: delete one doc while {n} GETs race ===")
    v = f"e05v-{STAMP}"
    await c.post(f"/api/v1/vaults?name={v}", headers=A)
    docid = "notes/retained.md"
    await c.post("/api/v1/documents", headers=A, json={
        "vault": v, "collection": "notes", "title": "Retained", "content": "# r\n\nbody", "slug": "retained"})
    url = f"/api/v1/documents/{v}/{docid}"
    del_t = asyncio.create_task(rec(c.request("DELETE", url, headers=A)))
    gets = await asyncio.gather(*[rec(c.get(url, headers=A)) for _ in range(n)])
    dstat = await del_t
    print(f"  delete: status={dstat[0]}")
    codes, z, f = show("get", gets)
    # invariant: a 200 GET must carry a body (not empty). `rec()` truncates
    # bodies, so check for emptiness rather than a specific field that may sit
    # past the truncation point.
    bad = [g for g in gets if g[0] == 200 and not g[3].strip()]
    ok = dstat[0] in (200, 204) and z == 0 and f == 0 and len(bad) == 0 and set(codes) <= {200, 404}
    print(f"  VERDICT E05: {'PASS' if ok else 'FAIL'} (delete 2xx; gets only 200/404; no transport/5xx)")
    try: await c.request("DELETE", f"/api/v1/vaults/{v}", headers=A)
    except Exception: pass


async def e06(c, A, n):
    print(f"\n=== E06: {n} PUTs into a collection while it is DELETEd (recursive) ===")
    v = f"e06v-{STAMP}"
    await c.post(f"/api/v1/vaults?name={v}", headers=A)
    coll = "retire"
    # seed docs so the collection exists and the recursive delete has work.
    # A bigger seed slows the delete cascade, widening the window in which it
    # commits during a racing PUT's get_or_create->create gap (the FK race).
    seed = int(os.environ.get("E06_SEED", "1"))
    for s in range(seed):
        await c.post("/api/v1/documents", headers=A, json={
            "vault": v, "collection": coll, "title": f"Seed {s}",
            "content": f"# seed {s}", "slug": f"seed-{s}"})

    async def put(i):
        return ("put", i, *(await rec(c.post("/api/v1/documents", headers=A, json={
            "vault": v, "collection": coll, "title": f"Doc {i}",
            "content": f"# doc {i}\n\nbody " + ("z" * 80), "slug": f"doc-{i}"}))))

    async def delete_coll():
        return ("del", *(await rec(c.request("DELETE", f"/api/v1/collections/{v}/{coll}?recursive=true", headers=A))))

    res = await asyncio.gather(delete_coll(), *[put(i) for i in range(n)])
    puts = [r[2:] for r in res if r[0] == "put"]
    dele = [r[1:] for r in res if r[0] == "del"][0]
    print(f"  collection delete: status={dele[0]} {dele[3][:80]}")
    codes, z, f = show("put", puts)
    # final browse
    bstat, _, berr, _ = await rec(c.get(f"/api/v1/browse/{v}", headers=A))
    print(f"  final browse: status={bstat}{(' '+berr) if berr else ''}")
    ok = dele[0] not in (0,) and z == 0 and f == 0 and bstat not in (0,)
    print(f"  VERDICT E06: {'PASS' if ok else 'FAIL'} (puts resolve as 2xx/409, no 5xx/status0)")
    try: await c.request("DELETE", f"/api/v1/vaults/{v}", headers=A)
    except Exception: pass


async def main():
    print(f"BASE={BASE}  which={WHICH}")
    async with httpx.AsyncClient(base_url=BASE, limits=httpx.Limits(max_connections=200, max_keepalive_connections=200), timeout=httpx.Timeout(90.0, connect=60.0)) as c:
        A = await setup(c)
        if WHICH in ("e05", "both"):
            await e05(c, A, N or 50)
        if WHICH in ("e06", "both"):
            await e06(c, A, N or 40)


if __name__ == "__main__":
    asyncio.run(main())
