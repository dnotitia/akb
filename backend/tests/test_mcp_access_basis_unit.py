"""MCP must be able to name the basis a grant is held on.

REST learned this when bases were introduced; MCP did not. That asymmetry is
not cosmetic: a rule-driven grantor reaching AKB over MCP could only ever write
`direct`, so its later revoke would delete reasons that were never its to
remove — the exact defect source keys exist to prevent.

Two halves are asserted separately because they fail separately. A schema that
advertises `source_key` while the handler drops it accepts the argument and
ignores it; a handler that forwards `source_key` while the schema omits it is
rejected by `_dispatch`'s unknown-argument check before it runs. Either alone
reads as "MCP supports bases" and neither is.

The grant and revoke defaults are deliberately different, and that difference is
asserted rather than assumed: an omitted key on grant means `direct` (the
service's own default, so the argument must not be forwarded as None), while an
omitted key on revoke means EVERY basis (so it must be forwarded).
"""
from __future__ import annotations

import asyncio

import pytest

from mcp_server import server as mcp_server
from mcp_server.tools import TOOLS


def _schema(name: str) -> dict:
    for tool in TOOLS:
        if tool.name == name:
            return tool.inputSchema
    raise AssertionError(f"{name} is not advertised at all")


# ── the advertised surface ────────────────────────────────────


@pytest.mark.parametrize("tool", ["akb_grant", "akb_revoke"])
def test_basis_arguments_are_advertised(tool):
    props = _schema(tool)["properties"]
    assert "source_key" in props, f"{tool} cannot be told which basis it writes"
    assert "revision" in props, f"{tool} cannot carry a monotonic revision"
    # Neither is required: an agent that knows nothing about bases keeps working.
    assert "source_key" not in _schema(tool).get("required", [])


def test_explanation_is_reachable_over_mcp():
    props = _schema("akb_explain_access")["properties"]
    assert {"vault", "user"} <= set(props)


# ── what the handler actually forwards ────────────────────────


class _Spy:
    def __init__(self):
        self.kwargs = None
        self.args = None

    async def __call__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs
        return {"ok": True}


def _run(handler, args):
    return asyncio.run(handler(args, "uid-1", None))


def test_grant_forwards_a_named_basis(monkeypatch):
    spy = _Spy()
    monkeypatch.setattr(mcp_server, "grant_access", spy)
    _run(mcp_server._HANDLERS["akb_grant"],
         {"vault": "v", "user": "u", "role": "reader",
          "source_key": "team:abc", "revision": 7})
    assert spy.kwargs["source_key"] == "team:abc"
    assert spy.kwargs["revision"] == 7


def test_grant_without_a_basis_leaves_the_service_default_alone(monkeypatch):
    spy = _Spy()
    monkeypatch.setattr(mcp_server, "grant_access", spy)
    _run(mcp_server._HANDLERS["akb_grant"], {"vault": "v", "user": "u", "role": "reader"})
    # Not `source_key=None` — that would override `direct` with a value the
    # service never meant to receive.
    assert "source_key" not in spy.kwargs


def test_revoke_forwards_a_named_basis(monkeypatch):
    spy = _Spy()
    monkeypatch.setattr(mcp_server, "revoke_access", spy)
    _run(mcp_server._HANDLERS["akb_revoke"],
         {"vault": "v", "user": "u", "source_key": "team:abc"})
    assert spy.kwargs["source_key"] == "team:abc"


def test_revoke_without_a_basis_still_means_every_basis(monkeypatch):
    spy = _Spy()
    monkeypatch.setattr(mcp_server, "revoke_access", spy)
    _run(mcp_server._HANDLERS["akb_revoke"], {"vault": "v", "user": "u"})
    # Absent here is a meaning, not a gap: the administrator's revoke.
    assert spy.kwargs["source_key"] is None


def test_explain_handler_reaches_the_service(monkeypatch):
    spy = _Spy()
    monkeypatch.setattr(mcp_server, "explain_vault_access", spy)
    _run(mcp_server._HANDLERS["akb_explain_access"], {"vault": "v", "user": "u"})
    assert spy.args == ("uid-1", "v", "u")
