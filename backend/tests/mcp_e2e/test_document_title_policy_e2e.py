"""Display-title checks over authenticated MCP, including collision and access envelopes."""
from mcp import Client

from .conftest import SecondaryMcpSession
from .runtime import RuntimeContext
from .test_product_e2e import _call_json, _create_vault


async def test_title_policy_on_create_rename_and_move(
    mcp_client: Client, runtime_session: RuntimeContext, secondary_mcp_client: SecondaryMcpSession,
) -> None:
    async def call(tool, args, *, error=False):
        return await _call_json(mcp_client, runtime_session, tool, args, expect_error=error)

    vault = await _create_vault(mcp_client, runtime_session, "title-policy")
    catalog = await mcp_client.list_tools(cache_mode="bypass")
    schemas = {tool.name: tool.input_schema for tool in catalog.tools}
    for name in ("akb_put", "akb_update", "akb_move"):
        policy = schemas[name]["properties"]["title_conflict_policy"]
        assert policy["enum"] == ["allow", "reject"] and policy["default"] == "allow"
        assert "title_conflict_policy" not in schemas[name]["required"]

    create = {"vault": vault, "collection": "notes", "title": "API Guide", "content": "Original"}
    twin = await call("akb_put", {**create, "slug": "private-guide"})
    target = await call("akb_put", {**create, "title": "Other", "slug": "target"})
    source = await call("akb_put", {**create, "collection": "drafts", "title_conflict_policy": "reject"})
    attempts = [
        ("akb_put", {**create, "slug": "never-created"}),
        ("akb_update", {"uri": target["uri"], "title": "API Guide", "tags": ["never-written"]}),
        ("akb_move", {"uri": source["uri"], "collection": "notes"}),
    ]
    for tool, args in attempts:
        rejected = await call(tool, {**args, "title_conflict_policy": "reject"}, error=True)
        assert rejected["code"] == "document_title_conflict"
        assert rejected["details"]["existing_path"] == twin["path"]
        assert rejected["details"]["existing_title"] == "API Guide"
        denied = await _call_json(secondary_mcp_client.client, runtime_session, tool,
                                  {**args, "title_conflict_policy": "reject"}, expect_error=True)
        assert denied["code"] == "permission_denied" and "private-guide" not in str(denied)
        assert "details" not in denied
        for invalid_policy in (None, "overwrite"):
            invalid = await call(tool, {**args, "title_conflict_policy": invalid_policy}, error=True)
            assert invalid["code"] == "invalid_argument" and invalid["details"]["field"] == "/title_conflict_policy"

    for doc in (twin, target, source):
        current = await call("akb_get", {"uri": doc["uri"]})
        assert current["current_commit"] == doc["current_commit"]
    listing = await call("akb_browse", {"vault": vault, "collection": "notes", "content_type": "documents"})
    assert {item["uri"] for item in listing["items"]} == {twin["uri"], target["uri"]}

    duplicate = await call("akb_put", create)
    assert duplicate["uri"] != twin["uri"]
    renamed = await call("akb_update", {"uri": target["uri"], "title": "API Guide"})
    moved = await call("akb_move", {"uri": source["uri"], "collection": "notes", "title_conflict_policy": "allow"})
    assert moved["path"].startswith("notes/") and moved["uri"] != twin["uri"]
    # A tag-only edit on an existing duplicate does not recheck its title.
    edited = await call("akb_update", {"uri": target["uri"], "tags": ["reviewed"], "title_conflict_policy": "reject"})
    assert edited["previous_commit"] == renamed["current_commit"]
    stale = await call("akb_update", {"uri": target["uri"], "content": "Stale",
                       "expected_commit": renamed["current_commit"], "title_conflict_policy": "allow"}, error=True)
    assert stale["code"] == "conflict"
    current = await call("akb_get", {"uri": target["uri"]})
    assert current["content"] == "Original" and current["tags"] == ["reviewed"]
    assert current["current_commit"] == edited["current_commit"]
