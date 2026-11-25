"""Bare Git must reject retry keys on the actual authenticated MCP/REST boundary."""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from mcp import Client

from .helpers import Actor
from .mcp_client import call_json, create_vault, mcp_actor as mcp_actor, mcp_client as mcp_client

pytestmark = pytest.mark.live_services


async def test_unsupported_document_keys_fail_without_mutation_over_mcp_and_rest(
    mcp_client: Client, api: httpx.AsyncClient, mcp_actor: Actor,
) -> None:
    # This API fixture uses the Bare Git revision backend. Native concurrency and
    # cross-surface receipt behavior are covered by the Native tests.
    catalog = await mcp_client.list_tools(cache_mode="bypass")
    tools = {tool.name: tool for tool in catalog.tools}
    for name in ("akb_put", "akb_update"):
        assert tools[name].meta["akb/document_idempotency"] == {"supported": False}
        assert tools[name].input_schema["properties"]["idempotency_key"]["format"] == "uuid"

    async def call(name, arguments, *, error=False):
        return await call_json(mcp_client, name, arguments, expect_error=error)

    vault = await create_vault(mcp_client, "retry-unsupported")
    create = {
        "vault": vault, "collection": "notes", "slug": "fixed", "title": "Retry receipt",
        "content": "original 한글", "tags": ["initial"],
    }
    keyed_create = {**create, "idempotency_key": str(uuid4())}
    rejected = await call("akb_put", keyed_create, error=True)
    assert rejected["code"] == "document_idempotency_unsupported"
    rejected_rest = await api.post("/api/v1/documents", headers=mcp_actor.headers, json=keyed_create)
    assert rejected_rest.status_code == 501, rejected_rest.text
    # The rejected calls must not reserve the explicit path or write a doc.
    original = await call("akb_put", create)
    uri = original["uri"]
    path = f"/api/v1/documents/{vault}/{original['path']}"
    for patch in (
        {"title": "Changed metadata", "tags": []},
        {"content": "replacement", "expected_commit": original["current_commit"]},
    ):
        keyed_patch = {**patch, "idempotency_key": str(uuid4())}
        rejected = await call("akb_update", {"uri": uri, **keyed_patch}, error=True)
        assert rejected["code"] == "document_idempotency_unsupported"
        rejected_rest = await api.patch(path, headers=mcp_actor.headers, json=keyed_patch)
        assert rejected_rest.status_code == 501, rejected_rest.text
    current = await call("akb_get", {"uri": uri})
    assert current["current_commit"] == original["current_commit"]
    assert current["title"] == create["title"] and current["content"] == create["content"]
    assert current["tags"] == ["initial"]
    # A null key retains the existing metadata-only update contract.
    changed = await api.patch(path, headers=mcp_actor.headers, json={"title": "Keyless metadata", "idempotency_key": None})
    assert changed.status_code == 200, changed.text
    current = await call("akb_get", {"uri": uri})
    assert current["current_commit"] == changed.json()["current_commit"]
    assert current["title"] == "Keyless metadata" and current["content"] == create["content"]
    invalid = await call("akb_update", {"uri": uri, "idempotency_key": "invalid"}, error=True)
    assert invalid["code"] == "invalid_argument"
    assert invalid["details"]["field"] == "/idempotency_key"
