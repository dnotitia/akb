"""Contract tests for the role-aware ``akb_list_vaults`` projection."""

from __future__ import annotations

import ast
from pathlib import Path

from mcp_server.vault_contract import project_accessible_vault


def test_accessible_vault_projection_preserves_the_callers_effective_role():
    result = project_accessible_vault(
        {
            "id": "00000000-0000-0000-0000-000000000001",
            "name": "gdn-state",
            "description": "Managed gardener state",
            "status": "active",
            "role": "writer",
            "created_at": "2026-07-12T00:00:00+00:00",
        }
    )

    assert result == {
        "name": "gdn-state",
        "description": "Managed gardener state",
        "role": "writer",
    }


def test_list_vaults_handler_uses_the_role_aware_projection():
    server_py = Path(__file__).resolve().parents[1] / "mcp_server" / "server.py"
    tree = ast.parse(server_py.read_text())
    handler = next(
        node
        for node in tree.body
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == "_handle_list_vaults"
    )

    calls = {
        node.func.id for node in ast.walk(handler) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "project_accessible_vault" in calls
