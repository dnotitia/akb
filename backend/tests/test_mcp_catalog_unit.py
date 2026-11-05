
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from app.services.tool_resource_policy import MCP_OPERATION_SCOPES, REST_OPERATION_SCOPES, required_oauth_scope
from mcp_server.tool_catalog import VAULT_SKILL_ACK_ARGUMENT, complete_catalog, with_skill_acknowledgement
from mcp_server.tools import TOOLS, available_tools


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


def test_generator_needs_no_installation_config_and_checks_both_artifacts(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[2] / "scripts/generate_mcp_catalog.py"
    output = tmp_path / "catalog.json"
    refinements_output = tmp_path / "refinements.json"
    env = {**os.environ, "PYTHONPATH": str(script.parent.parent / "backend")}
    command = [sys.executable, str(script), "--output", str(output), "--refinements-output", str(refinements_output)]
    result = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text()) == complete_catalog(TOOLS)
    refinements = json.loads(refinements_output.read_text())
    assert set(refinements) == {"akb_create_vault"}
    assert len(refinements["akb_create_vault"]) == 3
    for templates, external_git in ((False, False), (False, True), (True, False), (True, True)):
        monkeypatch.setattr(
            "app.services.vault_creation_capabilities.get_vault_creation_capabilities",
            lambda: SimpleNamespace(templates=templates, external_git=external_git),
        )
        create = next(tool for tool in complete_catalog(available_tools()) if tool["name"] == "akb_create_vault")
        schema = {key: create[key] for key in ("inputSchema", "outputSchema") if key in create}
        assert (schema in refinements["akb_create_vault"]) is not (templates and external_git)
    assert all(set(schema) == {"inputSchema"} for schema in refinements["akb_create_vault"])
    assert subprocess.run([*command, "--check"], cwd=tmp_path, env=env, capture_output=True).returncode == 0
    for artifact in (output, refinements_output):
        original = artifact.read_text()
        artifact.write_text("[]\n")
        assert subprocess.run([*command, "--check"], cwd=tmp_path, env=env, capture_output=True).returncode == 1
        artifact.unlink()
        assert subprocess.run([*command, "--check"], cwd=tmp_path, env=env, capture_output=True).returncode == 1
        artifact.write_text(original)
    missing_argument = subprocess.run(command[:-2], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert missing_argument.returncode == 2
    assert "--refinements-output" in missing_argument.stderr
