"""Real stdio file writes against the repository-owned HTTP/DB runtime."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil

from jsonschema import validate
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

from .runtime import RuntimeContext
from .test_product_e2e import _call_json, _create_vault, _text_content


async def test_file_inputs_preserve_metadata_and_original_read_protection(
    mcp_client: Client, runtime_session: RuntimeContext, tmp_path: Path,
) -> None:
    vault = await _create_vault(mcp_client, runtime_session, "file-input")
    node = shutil.which("node")
    assert node, "the stdio contract requires the repository's supported Node runtime"
    entrypoint = Path(__file__).resolve().parents[3] / "packages/akb-mcp-client/bin/akb-mcp.mjs"
    params = StdioServerParameters(
        command=node, args=[str(entrypoint)],
        env={"PATH": os.environ["PATH"], "AKB_MCP_URL": runtime_session.descriptor.mcp_url,
             "AKB_PAT": runtime_session.pat},
    )
    async with Client(stdio_client(params), mode="auto", cache=None, read_timeout_seconds=30) as client:
        async def call(name, args, *, error=False):
            result = await client.call_tool(name, args)
            payload = json.loads(_text_content(result, name))
            if payload.get("code") == "vault_skill_required":
                guide = payload["vault_skill"]
                assert guide["body"] and guide["ack_token"]
                result = await client.call_tool(name, {**args, "_vault_skill_ack": guide["ack_token"]})
                payload = json.loads(_text_content(result, name))
            assert result.is_error is error
            assert ("error" in payload) is error
            return payload

        catalog = await client.list_tools(cache_mode="bypass")
        schemas = {tool.name: tool.input_schema for tool in catalog.tools}
        file = tmp_path / "draft.md"
        file.write_text("body A 한글")
        create = {"vault": vault, "title": "File source", "file": str(file), "tags": ["original"]}
        validate(create, schemas["akb_put"])
        original = await call("akb_put", create)
        uri = original["uri"]
        for content in ("", None):
            rejected = await call("akb_update", {
                "uri": uri, "file": str(file), "content": content,
                "expected_commit": original["current_commit"],
            }, error=True)
            assert rejected["code"] == "invalid_argument"
            assert rejected["details"]["reason"] == "mutually_exclusive"
        for conditions in ({}, {"expected_commit": None}, {"expected_commit": ""}):
            rejected = await call("akb_update", {"uri": uri, "file": str(file), **conditions}, error=True)
            assert rejected["code"] == "invalid_argument"
        current = await call("akb_get", {"uri": uri})
        assert current["current_commit"] == original["current_commit"]

        for patch in ({"title": "Metadata only", "tags": [], "summary": "", "domain": ""},
                      {"content": None, "summary": "keep this metadata"}):
            validate({"uri": uri, **patch}, schemas["akb_update"])
            await call("akb_update", {"uri": uri, **patch})
        current = await call("akb_get", {"uri": uri})
        assert current["content"] == "body A 한글"
        assert current["title"] == "Metadata only" and current["tags"] == []
        assert current["summary"] == "keep this metadata"

        file.write_text("body B")
        update = {"uri": uri, "file": str(file), "expected_content_hash": original["content_hash"]}
        validate(update, schemas["akb_update"])
        changed = await call("akb_update", update)
        for conditions in (
            {"expected_commit": original["current_commit"], "expected_content_hash": changed["content_hash"]},
            {"expected_commit": changed["current_commit"], "expected_content_hash": original["content_hash"]},
        ):
            rejected = await call("akb_update", {"uri": uri, "file": str(file), **conditions}, error=True)
            assert rejected["code"] == "conflict"
        current = await call("akb_get", {"uri": uri})
        assert current["current_commit"] == changed["current_commit"]
        assert current["content"] == "body B"

        file.write_text("")
        await call("akb_update", {"uri": uri, "file": str(file),
                                 "expected_commit": changed["current_commit"],
                                 "expected_content_hash": changed["content_hash"]})
        current = await _call_json(mcp_client, runtime_session, "akb_get", {"uri": uri})
        assert current["content"] == ""
        assert current["summary"] == "keep this metadata" and current["tags"] == []
