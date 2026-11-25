"""Authenticated hybrid search must intersect resource filters with Vault read access."""

from collections.abc import Awaitable, Callable

import httpx
import pytest
from mcp import Client

from .file_fixtures import object_store as object_store
from .file_fixtures import upload_file
from .helpers import Actor
from .mcp_client import (
    SecondaryMcpSession,
    call_json,
    create_vault,
    mcp_actor as mcp_actor,
    mcp_client as mcp_client,
    secondary_mcp_client as secondary_mcp_client,
)
from .search_fixtures import drain_search_index as drain_search_index

pytestmark = pytest.mark.live_services


def _filter_snapshot(response: dict) -> dict:
    # Filter equivalence concerns identities, metadata and the response envelope.
    return {
        **response,
        "results": sorted(
            ({key: value for key, value in item.items() if key != "score"} for item in response["results"]),
            key=lambda item: (item["uri"], item["chunk_index"]),
        ),
    }


async def _seed_search_resources(
    api: httpx.AsyncClient, client: Client, owner: Actor, vault: str,
) -> dict[str, str]:
    resources = {}
    for label, kind, status in [
        ("note", "note", "active"), ("report", "report", "active"),
        ("plan", "plan", "active"), ("archive", "report", "archived"),
    ]:
        result = await call_json(client, "akb_put", {
            "vault": vault, "collection": "guide", "title": label, "type": kind,
            "status": status, "tags": ["ops"], "content": f"searchmarker {label}",
        })
        resources[label] = result["uri"]
    table = await call_json(client, "akb_create_table", {
        "vault": vault, "collection": "guide", "name": "searchmarker",
        "columns": [{"name": "value", "type": "text"}],
    })
    resources["table"] = table["uri"]
    resources["file"] = await upload_file(
        api, owner, vault, filename="searchmarker.txt", collection="guide",
        content=b"searchmarker file", mime_type="text/plain",
    )
    return resources


@pytest.mark.usefixtures("object_store")
async def test_search_filters_intersect_readable_vaults(
    api: httpx.AsyncClient,
    mcp_client: Client,
    secondary_mcp_client: SecondaryMcpSession,
    mcp_actor: Actor,
    drain_search_index: Callable[[], Awaitable[None]],
) -> None:
    visible = await create_vault(mcp_client, "filter-visible")
    private = await create_vault(mcp_client, "filter-private")
    allowed = await _seed_search_resources(api, mcp_client, mcp_actor, visible)
    hidden = await _seed_search_resources(api, mcp_client, mcp_actor, private)
    actor = {"vault": visible, "user": secondary_mcp_client.username}
    await call_json(mcp_client, "akb_grant", {**actor, "role": "reader"})
    args = {"query": "searchmarker", "collection": "guide", "limit": 30}
    known = [*allowed.values(), *hidden.values()]

    def uris(response):
        return {item["uri"] for item in response["results"]}

    # Prove every private control can actually be retrieved before checking for leaks.
    await drain_search_index()
    indexed = await call_json(mcp_client, "akb_search", {
        **args, "source_uris": known, "include_archived": True,
    })
    assert uris(indexed) == set(known)

    async def search(**filters):
        return await call_json(secondary_mcp_client.client, "akb_search", {**args, **filters})

    # Keep this comparison scoped to the explicitly seeded resources.
    plain = {"query": args["query"], "source_uris": known}
    reader = secondary_mcp_client.client
    default = await call_json(reader, "akb_search", plain)
    explicit = await call_json(reader, "akb_search", {**plain, "archive_scope": "unarchived"})
    assert _filter_snapshot(default) == _filter_snapshot(explicit)
    assert uris(default) == set(allowed.values()) - {allowed["archive"]}
    assert not uris(default).intersection(hidden.values())

    baseline = await search()
    assert uris(baseline) == set(allowed.values()) - {allowed["archive"]}
    assert _filter_snapshot(await search(archive_scope="unarchived", doc_types=[])) == _filter_snapshot(baseline)
    assert _filter_snapshot(await search(archive_scope="all")) == _filter_snapshot(await search(include_archived=True))
    assert _filter_snapshot(await search(archive_scope="unarchived", include_archived=True)) == _filter_snapshot(baseline)

    for kind in ("file", "table"):
        assert uris(await search(source_type=kind, source_uris=known)) == {allowed[kind]}
        assert uris(await search(source_type=kind, archive_scope="archived")) == set()

    combined = {"doc_types": ["note", "report"], "source_type": "document", "source_uris": known, "tags": ["ops"]}
    assert uris(await search(**combined)) == {allowed["note"], allowed["report"]}
    assert uris(await search(**combined, type="report")) == {allowed["report"]}
    assert uris(await search(**combined, type="plan")) == set()
    archived = await search(**combined, include_archived=False, archive_scope="archived")
    assert uris(archived) == {allowed["archive"]} and archived["archive_scope"] == "archived"
    only_hidden = await search(**{**combined, "source_uris": list(hidden.values())}, archive_scope="all")
    assert only_hidden["results"] == [] and only_hidden["total_matches"] == 0
    dated = await search(**combined, time_range={"field": "created_at", "gte": "2000-01-01T00:00:00Z"})
    assert uris(dated) == {allowed["note"], allowed["report"]}
    denied = await call_json(secondary_mcp_client.client, "akb_search", {
        **args, **combined, "vault": private,
    }, expect_error=True)
    assert denied["code"] == "permission_denied"

    await call_json(mcp_client, "akb_revoke", actor)
    revoked = await search(**combined, archive_scope="all")
    assert revoked["results"] == [] and revoked["total_matches"] == 0
