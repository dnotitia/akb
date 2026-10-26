"""Required HTTP checks against runtime-owned API replicas, never external hosts."""
from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import asynccontextmanager
import time
import uuid

import httpx


def _ok(response: httpx.Response, status: int = 200):
    # Responses can contain credentials. Report status, never echo their body.
    assert response.status_code == status, f"publication probe: HTTP {response.status_code}, expected {status}"
    return response.json()


@asynccontextmanager
async def _publication(owner):
    vault = "publication-budget-test-" + uuid.uuid4().hex[:12]
    _ok(await owner.post("/api/v1/vaults", params={"name": vault}))
    try:
        doc = _ok(await owner.post("/api/v1/documents", json={
            "vault": vault, "collection": "qa", "slug": "canary",
            "title": "Password budget canary", "content": "Temporary public budget test.",
        }))
        password = uuid.uuid4().hex
        link = _ok(await owner.post(f"/api/v1/publications/{vault}/create", json={
            "uri": doc["uri"], "password": password,
        }))
        yield vault, doc["uri"], link["slug"], password
    finally:
        _ok(await owner.delete("/api/v1/vaults/" + vault))


async def _wrong(client, slug, ordinal):
    return await client.post(f"/api/v1/public/{slug}/auth",
        json={"password": "wrong-" + uuid.uuid4().hex},  # pragma: allowlist secret -- generated invalid input
        headers={"X-Forwarded-For": f"198.51.100.{ordinal}"})


async def _token_get(client, slug, token):
    # Query tokens would leak into the API access log; use the supported cookie.
    client.cookies.set("akb_publication_token", token)
    try:
        return await client.get(f"/api/v1/public/{slug}")
    finally:
        client.cookies.clear()


async def run_checks(origins, pat, *, replace_replica, hold_budget_lock):
    """Caller owns both processes, replacement and the private test database."""
    assert len(origins) == 2 and len(set(origins)) == 2, "two distinct owned replicas required"
    evidence = {}
    async with (
        httpx.AsyncClient(base_url=origins[0], timeout=30, trust_env=False,
                          headers={"Authorization": "Bearer " + pat}) as owner,
        httpx.AsyncClient(base_url=origins[0], timeout=30, trust_env=False) as first,
        httpx.AsyncClient(base_url=origins[1], timeout=30, trust_env=False) as second,
    ):
        replicas = [first, second]
        async with _publication(owner) as (vault, uri, slug, password):
            token = _ok(await first.post(f"/api/v1/public/{slug}/auth", json={"password": password}))["token"]
            distribution = Counter()
            for ordinal in range(31):
                index = ordinal % 2
                _ok(await _wrong(replicas[index], slug, ordinal), 401)
                distribution[index] += 1
            for index, replica in enumerate(replicas):
                locked = await _wrong(replica, slug, 100 + index)
                _ok(locked, 429)
                assert int(locked.headers["Retry-After"]) > 0
                _ok(await replica.post(f"/api/v1/public/{slug}/auth", json={"password": password}), 429)
            _ok(await owner.post(f"/api/v1/public/{slug}/auth", json={"password": password}))
            _ok(await _wrong(first, slug, 200), 429)  # manager cannot clear anonymous state
            _ok(await _token_get(first, slug, token))
            other = _ok(await owner.post(f"/api/v1/publications/{vault}/create", json={"uri": uri, "password": password}))
            assert other["slug"] != slug
            _ok(await second.post(f"/api/v1/public/{other['slug']}/auth", json={"password": password}))
            evidence["shared_budget"] = {"routing_counts": dict(distribution), "wrong_passwords": 31}

        async with _publication(owner) as (_vault, _uri, slug, _password):
            admission = asyncio.Semaphore(8)

            async def attempt(ordinal):
                async with admission:
                    return (await _wrong(replicas[ordinal % 2], slug, ordinal)).status_code

            statuses = Counter(await asyncio.gather(*(attempt(i) for i in range(40))))
            assert statuses == {401: 31, 429: 9}, statuses
            evidence["concurrent_budget"] = dict(statuses)

        async with _publication(owner) as (_vault, _uri, slug, password):
            for i in range(5):
                _ok(await _wrong(replicas[i % 2], slug, 210), 401)
            _ok(await second.post(f"/api/v1/public/{slug}/auth", json={"password": password},
                                 headers={"X-Forwarded-For": "198.51.100.210"}))
            _ok(await _wrong(first, slug, 210), 401)
        async with _publication(owner) as (_vault, _uri, slug, password):
            for i in range(30):
                _ok(await _wrong(replicas[i % 2], slug, i), 401)
            _ok(await second.post(f"/api/v1/public/{slug}/auth", json={"password": password},
                                 headers={"X-Forwarded-For": "198.51.100.210"}))
            _ok(await _wrong(first, slug, 211), 401)
            _ok(await _wrong(second, slug, 212), 429)
        evidence["boundary_refunds"] = {"per_ip": True, "publication": True}

        async with _publication(owner) as (_vault, _uri, slug, _password):
            for i in range(6):
                _ok(await _wrong(replicas[i % 2], slug, 220), 401)
            locked_at = time.monotonic()
            initial = await _wrong(first, slug, 220)
            _ok(initial, 429)
            retry_after = int(initial.headers["Retry-After"])
            evidence["replacement"] = await replace_replica()
            # Wait only for the original ten-second backoff, not a fresh budget.
            await asyncio.sleep(max(0, locked_at + retry_after + 0.1 - time.monotonic()))
            _ok(await _wrong(second, slug, 220), 401)
            retained = await _wrong(first, slug, 220)
            _ok(retained, 429)
            assert int(retained.headers["Retry-After"]) > 10
            evidence["replacement"]["retained_escalation"] = True

        async with _publication(owner) as (_vault, _uri, slug, password):
            token = _ok(await first.post(f"/api/v1/public/{slug}/auth", json={"password": password}))["token"]
            _ok(await _wrong(first, slug, 230), 401)
            async with hold_budget_lock(slug):
                for client in (second, owner):
                    unavailable = await client.post(f"/api/v1/public/{slug}/auth", json={"password": password})
                    _ok(unavailable, 503)
                    assert int(unavailable.headers["Retry-After"]) > 0
                _ok(await _token_get(second, slug, token))
                async with _publication(owner) as (_v, _u, other_slug, other_password):
                    _ok(await first.post(f"/api/v1/public/{other_slug}/auth", json={"password": other_password}))
            _ok(await second.post(f"/api/v1/public/{slug}/auth", json={"password": password}))
        evidence["contention"] = {"anonymous": 503, "manager": 503, "token": 200, "other_publication": 200, "recovered": 200}
    return evidence
