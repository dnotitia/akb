"""A partial grep receipt survives stdio and agrees with independent HTTP reads."""
import json
import os
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

from .conftest import _prepare_secondary_user
from .runtime import RuntimeContext
from .test_product_e2e import _call_json, _create_vault, _text_content


async def test_grep_writer_receipt_over_stdio(mcp_client: Client, runtime_session: RuntimeContext):
    async def http(tool, args):
        return await _call_json(mcp_client, runtime_session, tool, args)

    vault = await _create_vault(mcp_client, runtime_session, "grep-receipt")
    writer, pat, _ = _prepare_secondary_user(runtime_session)
    await http("akb_grant", {"vault": vault, "user": writer, "role": "writer"})
    guide_uri = f"akb://{vault}/coll/overview/doc/vault-skill.md"
    guide = await http("akb_get", {"uri": guide_uri})
    pattern = "receipt marker"
    guide = await http("akb_update", {"uri": guide_uri, "expected_commit": guide["current_commit"], "content": pattern})
    note = await http("akb_put", {"vault": vault, "collection": "notes", "title": "Example", "content": pattern})

    proxy = Path(__file__).resolve().parents[3] / "packages/akb-mcp-client/bin/akb-mcp.mjs"
    params = StdioServerParameters(command="node", args=[str(proxy)], env={
        "PATH": os.environ["PATH"], "AKB_MCP_URL": runtime_session.descriptor.mcp_url, "AKB_PAT": pat,
    })
    async with Client(stdio_client(params), mode="auto", cache=None, read_timeout_seconds=30) as client:
        args = {"vault": vault, "pattern": pattern}
        matched = await _call_json(client, runtime_session, "akb_grep", args)
        assert [row["uri"] for row in matched["results"]] == [note["uri"], guide_uri]
        write = {**args, "replace": ""}
        result = await client.call_tool("akb_grep", write)
        payload = json.loads(_text_content(result, "akb_grep"))
        if payload.get("code") == "vault_skill_required":
            guide_info = payload["vault_skill"]
            assert guide_info["body"] and guide_info["ack_token"]
            result = await client.call_tool("akb_grep", {**write, "_vault_skill_ack": guide_info["ack_token"]})
            payload = json.loads(_text_content(result, "akb_grep"))
        assert result.is_error and payload["code"] == "permission_denied"
        assert payload["details"]["failed_uri"] == guide_uri
        assert payload["details"]["committed_replacements"] == payload["replaced_docs"] == 1
        assert payload["replacement_complete"] is False
        committed, = payload["replacements"]
        assert committed["uri"] == note["uri"] and committed["previous_commit"] == note["current_commit"]

    current = await http("akb_get", {"uri": note["uri"]})
    assert current["content"] == "" and current["current_commit"] == committed["commit"]
    protected = await http("akb_get", {"uri": guide_uri})
    assert protected["content"] == pattern and protected["current_commit"] == guide["current_commit"]
