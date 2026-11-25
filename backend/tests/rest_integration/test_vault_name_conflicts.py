import asyncio
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.config import settings
from tests.rest_integration.helpers import register_user
from tests.rest_integration.security_helpers import initialize_mcp, mcp_call

pytestmark = pytest.mark.live_services

MESSAGE = "Vault name is unavailable. Choose a different name."
CODE = "vault_name_unavailable"


def assert_safe_conflict(response: httpx.Response, attempted_name: str) -> None:
    assert response.status_code == 409, response.text
    assert response.json() == {
        "message": MESSAGE,
        "error": MESSAGE,
        "code": CODE,
        "detail": {"message": MESSAGE, "code": CODE},
    }
    assert attempted_name not in response.text


async def test_hidden_vault_name_conflict_does_not_disclose_the_vault(api: httpx.AsyncClient) -> None:
    owner = await register_user(api, label="vault-name-owner")
    other = await register_user(api, label="vault-name-other")
    vault = "hidden-vault"

    created = await api.post("/api/v1/vaults", params={"name": vault}, headers=owner.headers)
    assert created.status_code == 200, created.text
    listing = await api.get("/api/v1/my/vaults", headers=other.headers)
    assert listing.status_code == 200, listing.text
    assert vault not in {item["name"] for item in listing.json()["vaults"]}

    conflict = await api.post("/api/v1/vaults", params={"name": vault}, headers=other.headers)
    assert_safe_conflict(conflict, vault)

    await initialize_mcp(api, other.pat)
    reply = await mcp_call(api, other.pat, "akb_create_vault", name=vault)
    assert set(reply) == {"jsonrpc", "id", "result"}
    assert reply["jsonrpc"] == "2.0"
    assert reply["id"] == 2
    assert set(reply["result"]) <= {"content", "isError"}
    content, = reply["result"]["content"]
    assert set(content) == {"type", "text"}
    assert content["type"] == "text"
    assert json.loads(content["text"]) == {"error": MESSAGE, "code": CODE}


async def test_vault_create_race_preserves_the_winning_bare_repository(api: httpx.AsyncClient) -> None:
    owner = await register_user(api, label="vault-race-owner")
    vault = f"race-vault-{uuid4().hex[:8]}"
    repo = Path(settings.git_storage_path) / f"{vault}.git"

    replies = await asyncio.gather(*(
        api.post("/api/v1/vaults", headers=owner.headers, params={"name": vault})
        for _ in range(10)
    ))
    winners = [reply for reply in replies if reply.status_code == 200]
    assert len(winners) == 1, [(reply.status_code, reply.text) for reply in replies]
    for reply in replies:
        if reply is not winners[0]:
            assert_safe_conflict(reply, vault)

    assert (repo / "HEAD").is_file()
    document = await api.post(
        "/api/v1/documents",
        headers=owner.headers,
        json={
            "vault": vault,
            "collection": "checks",
            "title": "Winner remains usable",
            "content": "The winning repository survived every losing rollback.",
            "type": "note",
        },
    )
    assert document.status_code == 200, document.text
    assert document.json()["commit_hash"]

    deleted = await api.delete(f"/api/v1/vaults/{vault}", headers=owner.headers)
    assert deleted.status_code == 200, deleted.text
    assert not repo.exists()
    recreated = await api.post("/api/v1/vaults", headers=owner.headers, params={"name": vault})
    assert recreated.status_code == 200, recreated.text
    assert (repo / "HEAD").is_file()
