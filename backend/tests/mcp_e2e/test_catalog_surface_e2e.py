"""Live catalog, typed-input, and no-mutation checks through the MCP SDK."""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from mcp import Client
from mcp import types as mcp_types
from PIL import Image
from urllib.parse import urljoin

from .catalog_contract import (
    STDIO_LOCAL_OPERATIONS,
    assert_catalog_contract,
    call_target,
)
from .runtime import RuntimeContext, redact_error


SCENARIO = "mcp_catalog_surface_e2e"


def _public_result(result: mcp_types.CallToolResult, operation: str) -> dict[str, Any]:
    text = next(
        (item.text for item in result.content if isinstance(item, mcp_types.TextContent)),
        None,
    )
    assert isinstance(text, str), f"scenario={SCENARIO} operation={operation}: no JSON text"
    value = json.loads(text)
    assert isinstance(value, dict), f"scenario={SCENARIO} operation={operation}: response was not an object"
    return value


async def _call(
    client: Client,
    runtime_session: RuntimeContext,
    tools: list[mcp_types.Tool],
    operation: str,
    arguments: dict[str, Any],
) -> tuple[mcp_types.CallToolResult, dict[str, Any]]:
    name, public_arguments = call_target(tools, operation, arguments)
    try:
        response = await client.call_tool(name, public_arguments)
    except Exception as exc:
        pytest.fail(
            f"scenario={SCENARIO} tools/call {operation}: "
            + redact_error(exc, runtime_session.secrets)
        )
    return response, _public_result(response, operation)


async def _local_call(
    client: Client,
    runtime_session: RuntimeContext,
    name: str,
    arguments: dict[str, Any],
) -> tuple[mcp_types.CallToolResult, dict[str, Any]]:
    assert name in STDIO_LOCAL_OPERATIONS
    try:
        response = await client.call_tool(name, arguments)
    except Exception as exc:
        pytest.fail(
            f"scenario={SCENARIO} tools/call {name}: "
            + redact_error(exc, runtime_session.secrets)
        )
    return response, _public_result(response, name)


async def _local_write(
    client: Client,
    runtime_session: RuntimeContext,
    catalog: list[mcp_types.Tool],
    name: str,
    arguments: dict[str, Any],
) -> tuple[mcp_types.CallToolResult, dict[str, Any]]:
    response, payload = await _local_call(client, runtime_session, name, arguments)
    if payload.get("code") == "vault_skill_required":
        skill = payload.get("vault_skill")
        assert isinstance(skill, dict) and isinstance(skill.get("ack_token"), str)
        # The intervening read must not consume or retarget the pending token.
        identity_response, _identity = await _call(
            client, runtime_session, catalog, "akb_whoami", {}
        )
        assert identity_response.is_error is False
        response, payload = await _local_call(client, runtime_session, name, arguments)
    return response, payload


@pytest.mark.asyncio
async def test_actual_tools_list_has_complete_flat_or_mixed_operation_mapping(
    mcp_client: Client,
    mcp_transport: str,
    runtime_session: RuntimeContext,
) -> None:
    try:
        catalog = await mcp_client.list_tools(cache_mode="bypass")
    except Exception as exc:
        pytest.fail(f"scenario={SCENARIO} tools/list: {redact_error(exc, runtime_session.secrets)}")

    source = assert_catalog_contract(catalog.tools, transport=mcp_transport)
    assert source in {"flat", "mixed"}


@pytest.mark.asyncio
async def test_live_schema_errors_preserve_fields_and_leave_state_unchanged(
    mcp_client: Client,
    mcp_transport: str,
    runtime_session: RuntimeContext,
) -> None:
    catalog = await mcp_client.list_tools(cache_mode="bypass")
    tools = catalog.tools
    source = assert_catalog_contract(tools, transport=mcp_transport)
    tool_by_name = {tool.name: tool for tool in tools}

    create_table_tool = tool_by_name["akb_create_table"]
    import_tool = tool_by_name["akb_import"]
    assert create_table_tool.input_schema["properties"]["columns"]["type"] == "array"
    assert import_tool.input_schema["properties"]["files"]["type"] == "object"

    vault = f"mcp-schema-{uuid4().hex[:12]}"
    create_vault, created_vault = await _call(
        mcp_client, runtime_session, tools, "akb_create_vault", {"name": vault}
    )
    assert create_vault.is_error is False
    assert created_vault.get("name") == vault

    bad_columns, columns_error = await _call(
        mcp_client,
        runtime_session,
        tools,
        "akb_create_table",
        {"vault": vault, "name": "bad_columns", "columns": "[]"},
    )
    assert bad_columns.is_error is True
    assert columns_error["code"] == "invalid_argument"
    assert columns_error["details"]["field"] == "columns"
    assert columns_error["details"]["expected_type"] == "array"
    assert columns_error.get("hint")

    bad_files, files_error = await _call(
        mcp_client,
        runtime_session,
        tools,
        "akb_import",
        {"vault": vault, "files": [{"mcp_import_shape_probe.md": "# Must not import"}]},
    )
    assert bad_files.is_error is True
    assert files_error["code"] == "invalid_argument"
    assert files_error["details"]["field"] == "files"
    assert files_error["details"]["expected_type"] == "object"
    assert files_error.get("hint")

    missing_uri, required_error = await _call(
        mcp_client, runtime_session, tools, "akb_get", {}
    )
    assert missing_uri.is_error is True
    assert required_error["code"] == "invalid_argument"
    assert required_error["details"]["field"] == "uri"
    assert required_error["details"]["expected_type"] == "string"
    assert required_error.get("hint")

    wrong_uri_type, uri_type_error = await _call(
        mcp_client, runtime_session, tools, "akb_get", {"uri": 42}
    )
    assert wrong_uri_type.is_error is True
    assert uri_type_error["code"] == "invalid_argument"
    assert uri_type_error["details"]["field"] == "uri"
    assert uri_type_error["details"]["expected_type"] == "string"

    bad_coordinate, coordinate_error = await _call(
        mcp_client,
        runtime_session,
        tools,
        "akb_get",
        {"uri": "not-a-canonical-uri"},
    )
    assert bad_coordinate.is_error is True
    assert coordinate_error["code"] == "invalid_uri"

    wrong_field, field_error = await _call(
        mcp_client,
        runtime_session,
        tools,
        "akb_drill_down",
        {"uri": f"akb://{vault}/doc/missing.md", "vault": vault},
    )
    assert wrong_field.is_error is True
    assert field_error["code"] == "unknown_argument"
    assert field_error["details"]["field"] == "vault"
    assert field_error.get("hint")

    if source == "mixed":
        bad_action = await mcp_client.call_tool(
            "akb_document_read",
            {"action": "not_an_action", "uri": f"akb://{vault}/doc/missing.md"},
        )
        action_error = _public_result(bad_action, "akb_document_read")
        assert bad_action.is_error is True
        assert action_error["code"] == "invalid_argument"
        assert action_error["details"]["field"] == "action"
        assert "section" in action_error["details"]["allowed_values"]
        assert action_error.get("hint")

    info_result, info = await _call(
        mcp_client, runtime_session, tools, "akb_vault_info", {"vault": vault}
    )
    assert info_result.is_error is False
    assert not any(table.get("name") == "bad_columns" for table in info.get("tables", []))
    browse_result, browse = await _call(
        mcp_client, runtime_session, tools, "akb_browse", {"vault": vault}
    )
    assert browse_result.is_error is False
    assert not any("mcp_import_shape_probe" in str(item) for item in browse.get("items", []))


@pytest.mark.asyncio
async def test_stdio_local_file_and_image_operations_use_real_proxy_and_state(
    stdio_mcp_client: Client,
    runtime_session: RuntimeContext,
    tmp_path: Path,
) -> None:
    catalog = await stdio_mcp_client.list_tools(cache_mode="bypass")
    assert_catalog_contract(catalog.tools, transport="stdio")
    names = {tool.name for tool in catalog.tools}
    assert set(STDIO_LOCAL_OPERATIONS) <= names

    vault = f"mcp-stdio-local-{uuid4().hex[:12]}"
    create_response, created = await _call(
        stdio_mcp_client,
        runtime_session,
        catalog.tools,
        "akb_create_vault",
        {"name": vault},
    )
    assert create_response.is_error is False
    assert created.get("name") == vault

    source = tmp_path / "source.txt"
    source.write_text("stdio file before update\n", encoding="utf-8")
    put_file, uploaded = await _local_write(
        stdio_mcp_client,
        runtime_session,
        catalog.tools,
        "akb_put_file",
        {"vault": vault, "file_path": str(source), "collection": "artifacts"},
    )
    assert put_file.is_error is False
    file_uri = uploaded.get("uri")
    assert isinstance(file_uri, str) and file_uri.startswith(f"akb://{vault}/")

    downloaded_path = tmp_path / "downloaded.txt"
    get_file, downloaded = await _local_call(
        stdio_mcp_client,
        runtime_session,
        "akb_get_file",
        {"uri": file_uri, "save_to": str(downloaded_path)},
    )
    assert get_file.is_error is False
    assert downloaded_path.read_bytes() == source.read_bytes()
    assert isinstance(downloaded.get("content_hash"), str)
    assert isinstance(downloaded.get("version"), str)

    replacement = tmp_path / "replacement.txt"
    replacement.write_text("stdio file after update\n", encoding="utf-8")
    update_file, updated = await _local_write(
        stdio_mcp_client,
        runtime_session,
        catalog.tools,
        "akb_update_file",
        {
            "uri": file_uri,
            "file_path": str(replacement),
            "expected_content_hash": downloaded["content_hash"],
            "expected_version": downloaded["version"],
        },
    )
    assert update_file.is_error is False
    assert updated.get("uri") == file_uri
    stale_file, stale = await _local_write(
        stdio_mcp_client,
        runtime_session,
        catalog.tools,
        "akb_update_file",
        {
            "uri": file_uri,
            "file_path": str(source),
            "expected_content_hash": downloaded["content_hash"],
            "expected_version": downloaded["version"],
        },
    )
    assert stale_file.is_error is True
    assert stale.get("code") == "conflict"

    final_download = tmp_path / "final.txt"
    final_file_response, _final_file = await _local_call(
        stdio_mcp_client,
        runtime_session,
        "akb_get_file",
        {"uri": file_uri, "save_to": str(final_download)},
    )
    assert final_file_response.is_error is False
    assert final_download.read_bytes() == replacement.read_bytes()

    delete_file, deleted = await _local_write(
        stdio_mcp_client,
        runtime_session,
        catalog.tools,
        "akb_delete_file",
        {"uri": file_uri},
    )
    assert delete_file.is_error is False
    assert deleted.get("deleted") is True
    browse_response, browse = await _call(
        stdio_mcp_client,
        runtime_session,
        catalog.tools,
        "akb_browse",
        {"vault": vault, "content_type": "files"},
    )
    assert browse_response.is_error is False
    assert not any(item.get("uri") == file_uri for item in browse.get("items", []))

    image_path = tmp_path / "abandoned.png"
    image_buffer = BytesIO()
    Image.new("RGBA", (1, 1), (20, 30, 40, 255)).save(image_buffer, format="PNG")
    image_path.write_bytes(image_buffer.getvalue())
    put_image, image = await _local_write(
        stdio_mcp_client,
        runtime_session,
        catalog.tools,
        "akb_put_image",
        {"vault": vault, "file_path": str(image_path), "alt_text": "temporary pixel"},
    )
    assert put_image.is_error is False
    image_url = image.get("url")
    assert isinstance(image_url, str) and image_url.startswith("/api/assets/")
    assert isinstance(image.get("markdown"), str)

    discard_image, discarded = await _local_write(
        stdio_mcp_client,
        runtime_session,
        catalog.tools,
        "akb_discard_image",
        {"vault": vault, "url": image_url},
    )
    assert discard_image.is_error is False
    assert discarded.get("discarded") is True
    with httpx.Client(timeout=30.0, trust_env=False) as client:
        asset_response = client.get(
            urljoin(f"{runtime_session.descriptor.app_origin}/", image_url.lstrip("/")),
            headers={"Authorization": f"Bearer {runtime_session.pat}"},
        )
    assert asset_response.status_code == 404
