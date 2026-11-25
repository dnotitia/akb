import httpx
import pytest

from tests.rest_integration.helpers import register_user
from tests.rest_integration.security_helpers import initialize_mcp, mcp_json

pytestmark = pytest.mark.live_services


async def test_private_vault_drill_down_and_health_require_vault_access(api: httpx.AsyncClient) -> None:
    owner = await register_user(api, label="sec-rest-owner")
    other = await register_user(api, label="sec-rest-other")
    vault = "sec-rest-private"

    initialized = await initialize_mcp(api, owner.pat)
    assert "mcp-session-id" not in initialized.headers
    created = await mcp_json(api, owner.pat, "akb_create_vault", name=vault)
    assert created["vault_id"]
    document = await mcp_json(
        api,
        owner.pat,
        "akb_put",
        vault=vault,
        collection="secrets",
        title="REST Security Document",
        content="# Private\nREST_SECURITY_MARKER",
    )
    params = {"uri": document["uri"]}
    assert (await api.get("/api/v1/drill-down", headers=owner.headers, params=params)).status_code == 200
    assert (await api.get("/api/v1/drill-down", headers=other.headers, params=params)).status_code == 403

    health_path = f"/health/vault/{vault}"
    allowed = await api.get(health_path, headers=owner.headers)
    assert allowed.status_code == 200, allowed.text
    assert "vector_store" in allowed.json()
    assert (await api.get(health_path, headers=other.headers)).status_code == 403
    assert (await api.get(health_path)).status_code == 401
    missing = await api.get("/health/vault/sec-rest-missing", headers=owner.headers)
    assert missing.status_code == 404, missing.text

    deleted = await mcp_json(api, owner.pat, "akb_delete_vault", vault=vault)
    assert deleted["deleted"] is True


async def test_health_disclosure_depends_on_authentication(api: httpx.AsyncClient) -> None:
    user = await register_user(api, label="sec-rest-health")

    anonymous = await api.get("/health")
    assert anonymous.status_code == 200, anonymous.text
    assert "vector_store" in anonymous.json()
    assert {"rbac", "audit"}.isdisjoint(anonymous.json())

    authenticated = await api.get("/health", headers=user.headers)
    assert authenticated.status_code == 200, authenticated.text
    assert {"vector_store", "rbac", "audit"} <= authenticated.json().keys()


async def test_vault_info_distinguishes_membership_from_public_access(api: httpx.AsyncClient) -> None:
    owner = await register_user(api, label="sec-rest-member")
    other = await register_user(api, label="sec-rest-public-user")
    vault = "sec-rest-public"
    info = f"/api/v1/vaults/{vault}/info"
    created = await api.post(
        "/api/v1/vaults", headers=owner.headers, params={"name": vault, "public_access": "writer"}
    )
    assert created.status_code == 200, created.text

    member_info = await api.get(info, headers=owner.headers)
    assert member_info.status_code == 200, member_info.text
    assert member_info.json()["role_source"] == "member"
    public_info = await api.get(info, headers=other.headers)
    assert public_info.status_code == 200, public_info.text
    assert public_info.json()["role_source"] == "public"
