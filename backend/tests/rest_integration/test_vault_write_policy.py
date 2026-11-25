from uuid import UUID

import asyncpg
import httpx
import pytest

from tests.rest_integration.helpers import register_user
from tests.rest_integration.security_helpers import bearer, make_admin

pytestmark = pytest.mark.live_services


async def write_document(
    api: httpx.AsyncClient, headers: dict[str, str], vault: str, title: str
) -> httpx.Response:
    return await api.post(
        "/api/v1/documents",
        headers=headers,
        json={"vault": vault, "collection": "e2e", "title": title, "content": "Policy test document"},
    )


async def test_managed_vault_write_policy_round_trip(api: httpx.AsyncClient, pg_pool: asyncpg.Pool) -> None:
    admin = await register_user(api, label="write-policy-admin")
    owner = await register_user(api, label="write-policy-owner")
    await make_admin(pg_pool, admin.id)
    owner_jwt = bearer(owner.jwt)

    vault = "managed-write-policy"
    unmanaged = "unmarked-write-policy"
    managed_by = "collector:write-policy-test"
    policy = f"/api/v1/admin/vaults/{vault}/write-policy"
    grant = f"{policy}/grants/{owner.pat_id}"

    created = await api.post("/api/v1/vaults", headers=owner_jwt, params={"name": vault})
    assert created.status_code == 200, created.text
    vault_id = UUID(created.json()["vault_id"])
    baseline = await write_document(api, owner_jwt, vault, "Before marking")
    assert baseline.status_code == 200, baseline.text

    marked = await api.put(policy, headers=admin.headers, json={"managed_by": managed_by, "note": "Round trip"})
    assert marked.status_code == 200, marked.text
    assert marked.json()["managed_by"] == managed_by
    denied_mark = await api.put(policy, headers=owner_jwt, json={"managed_by": "collector:denied"})
    assert denied_mark.status_code == 403, denied_mark.text

    for headers in (owner_jwt, owner.headers):
        denied = await write_document(api, headers, vault, "Denied owner write")
        assert denied.status_code == 403, denied.text
        assert managed_by in denied.text

    granted = await api.put(grant, headers=admin.headers)
    assert granted.status_code == 200, granted.text
    allowed = await write_document(api, owner.headers, vault, "Granted PAT write")
    assert allowed.status_code == 200, allowed.text

    bypassed = await write_document(api, admin.headers, vault, "Admin bypass write")
    assert bypassed.status_code == 200, bypassed.text
    assert await pg_pool.fetchval(
        "SELECT count(*) FROM events WHERE vault_id = $1 AND kind = 'vault.write_policy_admin_bypass'", vault_id
    ) >= 1
    policy_events = await pg_pool.fetch(
        "SELECT payload->>'action' AS action FROM events WHERE vault_id = $1 AND kind = 'vault.write_policy_changed'",
        vault_id,
    )
    assert sorted(row["action"] for row in policy_events) == ["grant_added", "marked"]

    unmanaged_create = await api.post("/api/v1/vaults", headers=owner_jwt, params={"name": unmanaged})
    assert unmanaged_create.status_code == 200, unmanaged_create.text
    conflict = await api.put(
        f"/api/v1/admin/vaults/{unmanaged}/write-policy/grants/{owner.pat_id}", headers=admin.headers
    )
    assert conflict.status_code == 409, conflict.text
    missing_token = await api.put(f"{policy}/grants/{UUID(int=0)}", headers=admin.headers)
    assert missing_token.status_code == 404, missing_token.text
    missing_vault = await api.put(
        "/api/v1/admin/vaults/missing-write-policy/write-policy",
        headers=admin.headers,
        json={"managed_by": managed_by},
    )
    assert missing_vault.status_code == 404, missing_vault.text

    unmarked = await api.delete(policy, headers=admin.headers)
    assert unmarked.status_code == 200, unmarked.text
    assert await pg_pool.fetchval(
        """SELECT count(*) FROM events
           WHERE vault_id = $1 AND kind = 'vault.write_policy_changed' AND payload->>'action' = 'unmarked'""",
        vault_id,
    ) == 1
    restored = await write_document(api, owner_jwt, vault, "After unmarking")
    assert restored.status_code == 200, restored.text
