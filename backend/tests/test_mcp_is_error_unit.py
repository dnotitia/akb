"""A refused tool call says so in the MCP result, not only in its body (akb#675).

MCP reports a tool execution error inside the result with ``isError: true``.
AKB's refusals travel as the canonical ``err()`` envelope, ``{"error", "code",
...}``, and until this change the result never set the flag: a client that
branches on it read every refusal as a success. The body is unchanged, so a
client that reads the envelope keeps working.
"""

from __future__ import annotations

import asyncio
import json
import tempfile

import pytest

from app.config import settings
from app.exceptions import NotFoundError
from app.util.errors import NOT_FOUND, err

# Importing the MCP server builds a GitService at module load, which mkdirs
# `git_storage_path` — redirect it first.
settings.git_storage_path = tempfile.mkdtemp(prefix="akb-is-error-vaults-")

from mcp_server import server as srv  # noqa: E402


@pytest.fixture
def quiet_sinks(monkeypatch):
    monkeypatch.setattr(srv.tool_usage, "record", lambda *a, **k: None)
    monkeypatch.setattr(srv.audit_log, "record_tool", lambda *a, **k: None)


def _call(monkeypatch, outcome):
    async def _dispatch(_name, _args, _user):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(srv, "_dispatch", _dispatch)
    out = asyncio.run(srv.call_tool("akb_search", {"query": "seam"}))
    # The SDK model names the field is_error; the wire name is isError.
    assert out.model_dump(by_alias=True)["isError"] is out.is_error
    return out, json.loads(out.content[0].text)


def test_a_handler_refusal_sets_is_error_and_keeps_its_body(monkeypatch, quiet_sinks):
    envelope = err("Document not found: a.md", code=NOT_FOUND)

    out, body = _call(monkeypatch, envelope)

    assert out.is_error is True
    assert body == envelope


def test_an_exception_mapped_to_the_envelope_sets_is_error(monkeypatch, quiet_sinks):
    out, body = _call(monkeypatch, NotFoundError("Vault", "v"))

    assert out.is_error is True
    assert body["code"] == NOT_FOUND


def test_a_success_does_not_set_is_error(monkeypatch, quiet_sinks):
    out, body = _call(monkeypatch, {"uri": "akb://v/doc/a.md", "content": "body"})

    assert out.is_error is False
    assert body["content"] == "body"


@pytest.mark.parametrize(
    "payload",
    [
        {"error": None, "items": []},
        {"error": "partial", "items": []},
        {"errors": [{"code": "x"}], "items": []},
    ],
)
def test_only_the_canonical_envelope_sets_is_error(monkeypatch, quiet_sinks, payload):
    # err() always carries both a str error and a str code. A success payload
    # that happens to have an "error"-like field is not a refusal.
    out, _ = _call(monkeypatch, payload)

    assert out.is_error is False
