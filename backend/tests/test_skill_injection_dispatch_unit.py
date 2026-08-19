"""Vault-skill auto-injection at the MCP dispatch chokepoint.

Covers the shared attribution helper and the four paths through `call_tool`
that decide whether a `vault_skill` payload is attached:

- success + attributable vault  → attached
- handler returned an error dict → not attached
- handler raised (error envelope) → not attached
- no attributable vault          → injector never awaited

The last two matter most: a failing call is the wrong moment to teach
conventions, and the common multi-vault/no-vault call must not pay for a
lookup it cannot use.
"""

from __future__ import annotations

import json
import tempfile

import pytest

from app.config import settings
from app.services.tool_usage import vault_of_call

# server.py selects the process-scoped DocumentService at module load, whose
# legacy implementation creates the git storage path via mkdir. These tests
# never write documents; this just keeps the lazy import off `/data/vaults`.
settings.git_storage_path = tempfile.mkdtemp(prefix="akb-skill-inject-test-vaults-")

from app.services import vault_skill_service  # noqa: E402
from mcp_server import server as server_mod  # noqa: E402

# `Server.call_tool()`'s decorator registers an inner `handler` on the
# request-handler map and returns the ORIGINAL function unchanged, so the
# module symbol is the plain coroutine and is callable directly.
call_tool = server_mod.call_tool

_SENTINEL = {
    "vault": "v1",
    "version": "abc123",
    "reason": "first_touch",
    "body": "# conventions",
    "truncated": False,
}


def test_vault_of_call_public_name():
    assert vault_of_call("akb_get", {"uri": "akb://v1/doc/notes/a.md"}) == "v1"
    assert vault_of_call("akb_search", {"vault": "v2", "query": "x"}) == "v2"
    assert vault_of_call("akb_sql", {"vaults": ["a", "b"]}) is None


@pytest.fixture
def wired(monkeypatch):
    """Stub the chokepoint's collaborators and record injector calls.

    Returns a dict with `calls` (the (session_id, vault) tuples the injector
    was invoked with) and a `dispatch` setter.
    """
    calls: list[tuple] = []

    async def fake_payload(session_id, vault):
        calls.append((session_id, vault))
        return dict(_SENTINEL)

    async def fake_get_user():
        return server_mod._MCPUser()

    monkeypatch.setattr(vault_skill_service, "injection_payload", fake_payload)
    monkeypatch.setattr(server_mod, "_get_user", fake_get_user)
    monkeypatch.setattr(server_mod, "_session_id", lambda: "sess-1")

    state = {"calls": calls}

    def set_dispatch(fn):
        monkeypatch.setattr(server_mod, "_dispatch", fn)

    state["dispatch"] = set_dispatch
    return state


async def _run(name: str, args: dict) -> dict:
    out = await call_tool(name, args)
    return json.loads(out[0].text)


async def test_success_path_attaches_payload(wired):
    async def ok(name, args, user):
        return {"ok": 1}

    wired["dispatch"](ok)

    body = await _run("akb_get", {"uri": "akb://v1/doc/notes/a.md"})

    assert body["ok"] == 1
    assert body["vault_skill"] == _SENTINEL
    assert wired["calls"] == [("sess-1", "v1")]


async def test_raised_exception_gets_envelope_without_injection(wired):
    async def boom(name, args, user):
        raise RuntimeError("handler exploded")

    wired["dispatch"](boom)

    body = await _run("akb_get", {"uri": "akb://v1/doc/notes/a.md"})

    assert body.get("error")
    assert "vault_skill" not in body
    # The error path must not even consult the injector.
    assert wired["calls"] == []


async def test_handler_error_dict_gets_no_injection(wired):
    async def denied(name, args, user):
        return {"error": "x", "code": "FORBIDDEN"}

    wired["dispatch"](denied)

    body = await _run("akb_get", {"uri": "akb://v1/doc/notes/a.md"})

    assert body["code"] == "FORBIDDEN"
    assert "vault_skill" not in body
    assert wired["calls"] == []


async def test_no_attributable_vault_never_awaits_injector(wired):
    async def ok(name, args, user):
        return {"vaults": [], "total": 0}

    wired["dispatch"](ok)

    # Multi-vault akb_sql has no single target, so attribution yields None and
    # the hot path must skip the lookup entirely.
    body = await _run("akb_sql", {"vaults": ["a", "b"], "query": "SELECT 1"})

    assert "vault_skill" not in body
    assert wired["calls"] == []


async def test_injector_failure_never_fails_the_call(monkeypatch, wired):
    """The attach is best-effort: a raising injector still returns the result."""
    async def ok(name, args, user):
        return {"ok": 1}

    async def exploding(session_id, vault):
        raise RuntimeError("cache backend down")

    wired["dispatch"](ok)
    monkeypatch.setattr(vault_skill_service, "injection_payload", exploding)

    body = await _run("akb_get", {"uri": "akb://v1/doc/notes/a.md"})

    assert body == {"ok": 1}
