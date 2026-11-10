"""Authenticated hybrid search must intersect resource filters with Vault read access."""
import asyncio

from mcp import Client

from .conftest import SecondaryMcpSession
from .runtime import RuntimeContext
from .test_product_e2e import _call_json, _create_vault
from .test_publication_okf_e2e import _upload_file


async def _seed_search_resources(client: Client, runtime: RuntimeContext, vault: str) -> dict[str, str]:
    resources = {}
    for label, kind, status in [
        ("note", "note", "active"), ("report", "report", "active"),
        ("plan", "plan", "active"), ("archive", "report", "archived"),
    ]:
        result = await _call_json(client, runtime, "akb_put", {
            "vault": vault, "collection": "guide", "title": label, "type": kind,
            "status": status, "tags": ["ops"], "content": f"searchmarker {label}",
        })
        resources[label] = result["uri"]
    table = await _call_json(client, runtime, "akb_create_table", {
        "vault": vault, "collection": "guide", "name": "searchmarker",
        "columns": [{"name": "value", "type": "text"}],
    })
    resources["table"] = table["uri"]
    resources["file"] = await _upload_file(
        runtime, vault, filename="searchmarker.txt", collection="guide",
        content=b"searchmarker file", mime_type="text/plain",
    )
    return resources


async def test_search_filters_intersect_readable_vaults(
    mcp_client: Client, runtime_session: RuntimeContext, secondary_mcp_client: SecondaryMcpSession,
) -> None:
    visible = await _create_vault(mcp_client, runtime_session, "filter-visible")
    private = await _create_vault(mcp_client, runtime_session, "filter-private")
    allowed = await _seed_search_resources(mcp_client, runtime_session, visible)
    hidden = await _seed_search_resources(mcp_client, runtime_session, private)
    actor = {"vault": visible, "user": secondary_mcp_client.username}
    await _call_json(mcp_client, runtime_session, "akb_grant", {**actor, "role": "reader"})
    args = {"query": "searchmarker", "collection": "guide", "limit": 30}
    known = [*allowed.values(), *hidden.values()]

    def uris(response):
        return {item["uri"] for item in response["results"]}

    # Prove every private control can actually be retrieved before checking for leaks.
    for _ in range(90):
        indexed = await _call_json(mcp_client, runtime_session, "akb_search", {
            **args, "source_uris": known, "include_archived": True,
        })
        if uris(indexed) == set(known):
            break
        await asyncio.sleep(2)
    assert uris(indexed) == set(known)

    async def search(**filters):
        return await _call_json(secondary_mcp_client.client, runtime_session, "akb_search", {**args, **filters})

    plain = {"query": args["query"]}
    reader = secondary_mcp_client.client
    default = await _call_json(reader, runtime_session, "akb_search", plain)
    explicit = await _call_json(reader, runtime_session, "akb_search", {**plain, "archive_scope": "unarchived"})
    assert default == explicit
    assert not uris(default).intersection(hidden.values())

    baseline = await search()
    assert uris(baseline) == set(allowed.values()) - {allowed["archive"]}
    assert await search(archive_scope="unarchived", doc_types=[]) == baseline
    assert await search(archive_scope="all") == await search(include_archived=True)
    assert await search(archive_scope="unarchived", include_archived=True) == baseline

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
    denied = await _call_json(secondary_mcp_client.client, runtime_session, "akb_search", {
        **args, **combined, "vault": private,
    }, expect_error=True)
    assert denied["code"] == "permission_denied"

    await _call_json(mcp_client, runtime_session, "akb_revoke", actor)
    revoked = await search(**combined, archive_scope="all")
    assert revoked["results"] == [] and revoked["total_matches"] == 0
