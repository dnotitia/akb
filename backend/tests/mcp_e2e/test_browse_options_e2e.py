"""MCP name and resource pages must preserve the cursor and current reader grant."""
from mcp import Client

from .conftest import SecondaryMcpSession
from .runtime import RuntimeContext
from .test_product_e2e import _call_json, _create_vault
from .test_publication_okf_e2e import _upload_file


async def test_browse_options_and_cursor_authorization(
    mcp_client: Client, runtime_session: RuntimeContext, secondary_mcp_client: SecondaryMcpSession,
) -> None:
    vault = await _create_vault(mcp_client, runtime_session, "browse-options")
    other = await _create_vault(mcp_client, runtime_session, "browse-private")
    reader = secondary_mcp_client.client
    actor = {"vault": vault, "user": secondary_mcp_client.username}
    await _call_json(mcp_client, runtime_session, "akb_grant", {**actor, "role": "reader"})
    docs = {}
    for slug, status in [("a", "archived"), ("b", "active")]:
        created = await _call_json(mcp_client, runtime_session, "akb_put", {
            "vault": vault, "collection": "guide", "title": f"공통 {slug}", "slug": slug,
            "status": status, "content": "body",
        })
        docs[slug] = created["uri"]
    await _call_json(mcp_client, runtime_session, "akb_create_collection", {"vault": vault, "path": "guide/sub"})
    table = await _call_json(mcp_client, runtime_session, "akb_create_table", {
        "vault": vault, "collection": "guide", "name": "data", "columns": [{"name": "value", "type": "text"}],
    })
    file_uri = await _upload_file(runtime_session, vault, filename="sample.txt", collection="guide",
                                  content=b"example", mime_type="text/plain")
    target = {"uri": f"akb://{vault}/coll/guide"}

    async def browse(options, *, error=False):
        return await _call_json(reader, runtime_session, "akb_browse", {**target, **options}, expect_error=error)

    legacy = await browse({})
    assert legacy["total"] == legacy["returned"] == 4
    assert any(item["type"] == "collection" for item in legacy["items"])
    assert await browse({"archive_scope": "unarchived"}) == legacy

    resources = await browse({"content_type": "resources"})
    assert "total" not in resources and resources["complete"] is True
    assert {item["uri"] for item in resources["items"]} == {docs["b"], table["uri"], file_uri}
    filtered = await browse({"content_type": "resources", "filter": "공통 b", "page_size": 1, "archive_scope": "all"})
    assert filtered["items"] == [] and filtered["scanned"] == 1 and filtered["next_cursor"]
    archived = await browse({"archive_scope": "archived", "name_query": "공통", "content_type": "resources"})
    assert [item["uri"] for item in archived["items"]] == [docs["a"]]

    opts = {"content_type": "resources", "name_query": "공통", "archive_scope": "unarchived",
            "include_archived": True, "page_size": 1}
    first = await browse(opts)
    assert first["items"] == [] and first["returned"] == 0 and first["scanned"] == 1
    assert first["complete"] is False and first["next_cursor"]
    resume = {**opts, "cursor": first["next_cursor"]}
    for change in [{"name_query": "different"}, {"archive_scope": "all"}, {"content_type": "documents"}]:
        rejected = await browse({**resume, **change}, error=True)
        assert rejected["code"] == "invalid_argument"
    second = await browse(resume)
    assert [item["uri"] for item in second["items"]] == [docs["b"]]
    assert second["complete"] is True and second["next_cursor"] is None

    hidden = await browse({**resume, "uri": f"akb://{other}", "vault": vault}, error=True)
    assert hidden["code"] == "permission_denied"
    await _call_json(mcp_client, runtime_session, "akb_revoke", actor)
    revoked = await browse(resume, error=True)
    assert revoked["code"] == "permission_denied"
