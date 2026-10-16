"""Opt-in shared-budget proof using one or more actual API process origins.

AKB_PUBLICATION_TEST_URLS is a comma-separated list of direct replica origins;
AKB_E2E_PAT is the fixture Vault owner. Never point these tests at production.
The owner-created temporary Vaults/publications are deleted through normal APIs.
"""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
import os
import ssl
import uuid

import httpx
import pytest


def _ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


@pytest.fixture
def publication_clients():
    raw = os.getenv("AKB_PUBLICATION_TEST_URLS")
    if not raw:
        pytest.skip("set AKB_PUBLICATION_TEST_URLS for actual process/HTTP accounting")
    token = os.getenv("AKB_E2E_PAT")
    if not token:
        pytest.fail("supply a fixture Vault-owner PAT")
    origins = [url.strip() for url in raw.split(",")]
    ca_file = os.getenv("AKB_E2E_CA_FILE")
    verify = ssl.create_default_context(cafile=ca_file) if ca_file else True
    with ExitStack() as stack:
        owner = stack.enter_context(httpx.Client(base_url=origins[0], verify=verify, timeout=30,
                                                headers={"Authorization": "Bearer " + token}))
        replicas = [stack.enter_context(httpx.Client(base_url=url, verify=verify, timeout=30)) for url in origins]
        yield owner, replicas


@contextmanager
def _publication(owner):
    vault = "publication-budget-test-" + uuid.uuid4().hex[:12]
    _ok(owner.post("/api/v1/vaults", params={"name": vault}))
    try:
        doc = _ok(owner.post("/api/v1/documents", json={"vault": vault, "collection": "qa",
                  "slug": "canary", "title": "Password budget canary", "content": "Temporary public budget test."}))
        password = uuid.uuid4().hex
        link = _ok(owner.post(f"/api/v1/publications/{vault}/create", json={"uri": doc["uri"], "password": password}))
        yield vault, doc["uri"], link["slug"], password
    finally:
        _ok(owner.delete("/api/v1/vaults/" + vault))


def _wrong(client, slug, ordinal):
    return client.post(f"/api/v1/public/{slug}/auth", json={"password": "wrong-" + uuid.uuid4().hex},  # pragma: allowlist secret -- generated invalid test input
                       headers={"X-Forwarded-For": f"198.51.100.{ordinal}"})


def test_budget_is_shared_without_locking_out_the_manager_or_token_holder(publication_clients):
    owner, replicas = publication_clients
    with _publication(owner) as (vault, uri, slug, password):
        token = _ok(replicas[0].post(f"/api/v1/public/{slug}/auth", json={"password": password}))["token"]
        distribution = Counter()
        for ordinal in range(31):
            index = ordinal % len(replicas)
            assert _wrong(replicas[index], slug, ordinal).status_code == 401
            distribution[index] += 1
        for index, replica in enumerate(replicas):
            locked = _wrong(replica, slug, 100 + index)
            assert locked.status_code == 429, locked.text
            assert int(locked.headers["Retry-After"]) > 0
        # Manager recovery stays outside anonymous counters; it must not let
        # an anonymous attacker piggyback on the manager's successful password.
        _ok(owner.post(f"/api/v1/public/{slug}/auth", json={"password": password}))
        assert _wrong(replicas[0], slug, 200).status_code == 429
        replicas[0].cookies.set("akb_publication_token", token)
        _ok(replicas[0].get(f"/api/v1/public/{slug}"))
        replicas[0].cookies.clear()
        other = _ok(owner.post(f"/api/v1/publications/{vault}/create", json={"uri": uri, "password": password}))
        assert other["slug"] != slug
        _ok(replicas[-1].post(f"/api/v1/public/{other['slug']}/auth", json={"password": password}))
        print({"routing_counts": dict(distribution), "aggregate_wrong_passwords": 31, "shared_lock": True})


def test_concurrent_reservations_do_not_all_pass_a_stale_counter(publication_clients):
    owner, replicas = publication_clients
    with _publication(owner) as (_vault, _uri, slug, _password):
        def attempt(ordinal):
            response = _wrong(replicas[ordinal % len(replicas)], slug, ordinal)
            return response.status_code

        with ThreadPoolExecutor(max_workers=8) as executor:
            statuses = Counter(executor.map(attempt, range(40)))
        assert statuses == {401: 31, 429: 9}, statuses
        print({"concurrent_statuses": dict(statuses), "replica_origins": len(replicas)})
