"""Repro for reported 'E03: Design review edit race' and 'E04: Incident note
live update' — N concurrent UPDATEs (PATCH) to ONE existing document, with
optional concurrent reads (E04).

These are unconditional updates (no expected_commit / OCC), so the correct
outcome under the (vault_id, path) advisory lock is: every update serializes
and succeeds (200); none should 5xx or time out. The reporter saw ~19/25 as
HTTP 500 after ~30s (== statement_timeout=30000) — the signature of a
connection-pool deadlock in the write path, not a clean serialization.

Usage:
  AKB_URL=http://localhost:8000 python3 repro_e03_update_race.py [N_UPDATES] [N_READS]
  (N_READS>0 reproduces E04; N_READS=0 reproduces E03)
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from collections import Counter

import httpx

BASE = os.environ.get("AKB_URL", "http://localhost:8000").rstrip("/")
N_UPD = int(sys.argv[1]) if len(sys.argv) > 1 else 25
N_GET = int(sys.argv[2]) if len(sys.argv) > 2 else 0

STAMP = str(int(time.time()))
U = f"e03-{STAMP}"
PW = "testtest1234"


async def record(coro):
    t0 = time.perf_counter()
    try:
        r = await coro
        dt = (time.perf_counter() - t0) * 1000
        body = ""
        try:
            body = r.text
        except Exception:
            pass
        return r.status_code, dt, None, body
    except Exception as e:  # noqa: BLE001
        dt = (time.perf_counter() - t0) * 1000
        return 0, dt, f"{type(e).__name__}: {e}", ""


def pct(xs, p):
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))]


async def main():
    mode = "E04 (updates+reads)" if N_GET > 0 else "E03 (updates only)"
    print(f"BASE={BASE}  mode={mode}  updates={N_UPD}  reads={N_GET}")
    limits = httpx.Limits(max_connections=400, max_keepalive_connections=400)
    timeout = httpx.Timeout(90.0, connect=60.0)

    async with httpx.AsyncClient(base_url=BASE, limits=limits, timeout=timeout) as c:
        await c.post("/api/v1/auth/register", json={"username": U, "email": f"{U}@t.local", "password": PW})
        jwt = (await c.post("/api/v1/auth/login", json={"username": U, "password": PW})).json()["token"]
        pat = (await c.post("/api/v1/auth/tokens", headers={"Authorization": f"Bearer {jwt}"}, json={"name": "p"})).json()["token"]
        A = {"Authorization": f"Bearer {pat}"}

        v = f"e03v-{STAMP}"
        await c.post(f"/api/v1/vaults?name={v}", headers=A)

        # create the single target doc
        coll, slug = "review", "design-doc"
        docid = f"{coll}/{slug}.md"
        await c.post("/api/v1/documents", headers=A, json={
            "vault": v, "collection": coll, "title": "Design Doc",
            "content": "# Design\n\noriginal body", "type": "note", "slug": slug,
        })

        upd_url = f"/api/v1/documents/{v}/{docid}"
        get_url = f"/api/v1/documents/{v}/{docid}"

        async def do_update(i):
            body = {"content": f"# Design\n\nrevision {i} " + ("y" * 120),
                    "message": f"rev {i}"}
            st, dt, err, b = await record(c.patch(upd_url, headers=A, json=body))
            return ("upd", i, st, dt, err, b[:160])

        async def do_get(j):
            st, dt, err, b = await record(c.get(get_url, headers=A))
            return ("get", j, st, dt, err, b)

        tasks = [do_update(i) for i in range(N_UPD)] + [do_get(j) for j in range(N_GET)]
        res = await asyncio.gather(*tasks)

        upd = [r for r in res if r[0] == "upd"]
        get = [r for r in res if r[0] == "get"]

        def summarize(name, rows):
            codes = Counter(r[2] for r in rows)
            lat = [r[3] for r in rows]
            succ = [r for r in rows if r[2] == 200]
            conf = [r for r in rows if r[2] == 409]
            s5xx = [r for r in rows if 500 <= r[2] < 600]
            zero = [r for r in rows if r[2] == 0]
            print(f"\n  {name}: count={len(rows)} statuses={dict(codes)} "
                  f"p50={pct(lat,50):.0f}ms p95={pct(lat,95):.0f}ms max={(max(lat) if lat else 0):.0f}ms")
            print(f"    success(200)={len(succ)} conflict(409)={len(conf)} 5xx={len(s5xx)} status0={len(zero)}")
            # decode 5xx + status0 causes
            for r in s5xx[:2]:
                print(f"      [HTTP {r[2]} @ {r[3]:.0f}ms] {r[5]}")
            ec = Counter(r[4] for r in zero if r[4])
            for err, n in ec.most_common():
                print(f"      [status0 x{n}] {err}")
            return len(succ), len(conf), len(s5xx), len(zero)

        su, cu, fu, zu = summarize("update", upd)
        if get:
            summarize("get", get)

        # final consistency: read the doc, compare body+commit
        st, _, _, b = await record(c.get(get_url, headers=A))
        import json as _j
        try:
            d = _j.loads(b)
            print(f"\n  final: current_commit={(d.get('current_commit') or '')[:12]} "
                  f"body_first_line={(d.get('content') or '').splitlines()[0] if d.get('content') else ''!r}")
        except Exception:
            pass

        ok = fu == 0 and zu == 0 and (su + cu) == N_UPD
        print(f"\n  VERDICT: {'PASS' if ok else 'FAIL'} "
              f"(expect all {N_UPD} updates resolve as 200/409, no 5xx/status0)")

        try:
            await c.request("DELETE", f"/api/v1/vaults/{v}", headers=A)
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
