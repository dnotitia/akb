"""Unit test: Server('akb') initialization includes the bootstrap instructions.

INSTRUCTIONS lives in mcp_server/instructions.py (a lightweight module with
no heavy deps).  server.py imports and re-exports it; the Server("akb") call
in server.py receives instructions=INSTRUCTIONS.

We verify:
  1. The INSTRUCTIONS constant is well-formed.
  2. server.py's source wires INSTRUCTIONS into the Server(...) constructor,
     confirmed by a static grep of the source file (avoids importing the full
     server chain which requires kiwipiepy / psycopg / /data/ filesystem).
"""
import ast
import textwrap
from pathlib import Path

from mcp_server.instructions import INSTRUCTIONS


def test_instructions_constant_exists():
    assert isinstance(INSTRUCTIONS, str)
    assert len(INSTRUCTIONS) > 100  # not empty
    assert len(INSTRUCTIONS) < 2000  # not absurd
    assert "akb_help" in INSTRUCTIONS
    assert "vault-skill" in INSTRUCTIONS
    assert "secrets" in INSTRUCTIONS
    assert "akb_delete" in INSTRUCTIONS


def test_instructions_secrets_placeholder_literal():
    """The placeholder must render as ${{secrets.X}}, not ${secrets.X}."""
    assert "${{secrets.X}}" in INSTRUCTIONS


def test_server_carries_instructions():
    """server.py passes INSTRUCTIONS to Server('akb') via the instructions= kwarg.

    We parse server.py's AST to find the Server(..., instructions=INSTRUCTIONS)
    call, confirming the wiring without executing any heavy imports.
    """
    server_py = Path(__file__).resolve().parents[1] / "mcp_server" / "server.py"
    source = server_py.read_text()
    tree = ast.parse(source)

    found = False
    for node in ast.walk(tree):
        # Look for: server = Server("akb", instructions=INSTRUCTIONS)
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "Server"):
            continue
        for kw in node.keywords:
            if kw.arg == "instructions" and isinstance(kw.value, ast.Name) and kw.value.id == "INSTRUCTIONS":
                found = True
                break
        if found:
            break

    assert found, (
        "server.py does not call Server(..., instructions=INSTRUCTIONS). "
        "Check mcp_server/server.py for the Server() constructor call."
    )
