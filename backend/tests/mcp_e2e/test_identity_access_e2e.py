"""Identity and self-versus-other access explanations through MCP SDK calls."""

from __future__ import annotations

import os

import pytest
from mcp import Client

from .conftest import SecondaryMcpSession
from .runtime import RuntimeContext
from .test_product_e2e import _call_json, _create_vault


@pytest.mark.asyncio
async def test_identity_and_self_or_other_access_explanations(
    mcp_client: Client,
    secondary_mcp_client: SecondaryMcpSession,
    runtime_session: RuntimeContext,
) -> None:
    vault = await _create_vault(mcp_client, runtime_session, "access-explain")
    owner_username = os.environ[runtime_session.descriptor.username_env]

    identity = await _call_json(mcp_client, runtime_session, "akb_whoami", {})
    assert identity.get("username") == owner_username

    owner_explanation = await _call_json(
        mcp_client,
        runtime_session,
        "akb_explain_access",
        {"vault": vault, "user": owner_username},
    )
    assert owner_explanation.get("effective_role") == "owner"
    assert owner_explanation.get("non_member_paths", {}).get("owner") is True

    granted = await _call_json(
        mcp_client,
        runtime_session,
        "akb_grant",
        {"vault": vault, "user": secondary_mcp_client.username, "role": "reader"},
    )
    assert granted.get("granted") is True

    self_explanation = await _call_json(
        secondary_mcp_client.client,
        runtime_session,
        "akb_explain_access",
        {"vault": vault, "user": secondary_mcp_client.username},
    )
    assert self_explanation.get("effective_role") == "reader"

    other_explanation = await _call_json(
        secondary_mcp_client.client,
        runtime_session,
        "akb_explain_access",
        {"vault": vault, "user": owner_username},
        expect_error=True,
    )
    assert other_explanation.get("code") == "permission_denied"

    owner_reads_other = await _call_json(
        mcp_client,
        runtime_session,
        "akb_explain_access",
        {"vault": vault, "user": secondary_mcp_client.username},
    )
    assert owner_reads_other.get("effective_role") == "reader"
