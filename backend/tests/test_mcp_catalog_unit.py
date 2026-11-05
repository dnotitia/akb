"""The packaged grammar is complete, deterministic and configuration-free."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from app.services.tool_resource_policy import MCP_OPERATION_SCOPES, REST_OPERATION_SCOPES, required_oauth_scope
from mcp_server.tool_catalog import VAULT_SKILL_ACK_ARGUMENT, complete_catalog, with_skill_acknowledgement
from mcp_server.tools import TOOLS


def test_complete_catalog_retains_optional_creation_arguments():
    catalog = complete_catalog(TOOLS)
    assert {tool["name"] for tool in catalog} == set(MCP_OPERATION_SCOPES)
    create = next(tool for tool in catalog if tool["name"] == "akb_create_vault")
    assert {"template", "external_git"} <= create["inputSchema"]["properties"].keys()
    grep = next(tool for tool in catalog if tool["name"] == "akb_grep")
    assert VAULT_SKILL_ACK_ARGUMENT in grep["inputSchema"]["properties"]
    assert all(VAULT_SKILL_ACK_ARGUMENT not in tool.input_schema["properties"] for tool in TOOLS)


def test_catalog_refuses_collisions_and_incomplete_policy():
    with pytest.raises(ValueError, match="Duplicate"):
        complete_catalog([*TOOLS, TOOLS[0]])
    with pytest.raises(ValueError, match="differ"):
        complete_catalog(TOOLS[:-1])
    decorated = with_skill_acknowledgement(next(tool for tool in TOOLS if tool.name == "akb_put"))
    with pytest.raises(ValueError, match="collides"):
        with_skill_acknowledgement(decorated)


def test_exact_rest_policy_is_independent():
    assert len(REST_OPERATION_SCOPES) == 8
    assert required_oauth_scope("file.download", {}) == "akb:vault:read"
    assert all(scope == "akb:vault:write" for name, scope in REST_OPERATION_SCOPES.items() if name != "file.download")


def test_generator_needs_no_installation_config_and_checks_drift(tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts/generate_mcp_catalog.py"
    output = tmp_path / "catalog.json"
    env = {**os.environ, "PYTHONPATH": str(script.parent.parent / "backend")}
    command = [sys.executable, str(script), "--output", str(output)]
    result = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text()) == complete_catalog(TOOLS)
    assert subprocess.run([*command, "--check"], cwd=tmp_path, env=env, capture_output=True).returncode == 0
    output.write_text("[]\n")
    assert subprocess.run([*command, "--check"], cwd=tmp_path, env=env, capture_output=True).returncode == 1
