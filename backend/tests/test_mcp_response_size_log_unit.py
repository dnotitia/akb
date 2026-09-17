"""Every `tools/call` leaves one line saying what the response cost.

`tool_calls` records that a call happened but not how many bytes it sent
back, and the response payload is the number an agent actually pays for.
Adding a column to that table is a schema change; a log line measures the
same thing with no migration, which is what the payload-hygiene work
needs to size a before/after per tool.
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile

import pytest

from app.config import settings

# Importing the MCP server builds a GitService at module load, which mkdirs
# `git_storage_path` — redirect it first.
settings.git_storage_path = tempfile.mkdtemp(prefix="akb-response-size-vaults-")

from mcp_server import server as srv  # noqa: E402


@pytest.fixture
def quiet_sinks(monkeypatch):
    monkeypatch.setattr(srv.tool_usage, "record", lambda *a, **k: None)
    monkeypatch.setattr(srv.audit_log, "record_tool", lambda *a, **k: None)


def _records(caplog):
    return [r for r in caplog.records if r.name == srv.RESPONSE_SIZE_LOGGER]


def test_one_line_per_call_carries_tool_bytes_and_duration(monkeypatch, caplog, quiet_sinks):
    payload = {"kind": "search", "results": [{"uri": "akb://v/doc/a.md"}]}

    async def _dispatch(_name, _args, _user):
        return payload

    monkeypatch.setattr(srv, "_dispatch", _dispatch)

    with caplog.at_level(logging.INFO, logger=srv.RESPONSE_SIZE_LOGGER):
        out = asyncio.run(srv.call_tool("akb_search", {"query": "seam"}))

    (record,) = _records(caplog)
    assert record.tool == "akb_search"
    assert record.result_bytes == len(out.content[0].text.encode("utf-8"))
    assert record.duration_ms >= 0
    assert "tools/call" in record.getMessage()


def test_the_logged_size_is_bytes_not_characters(monkeypatch, caplog, quiet_sinks):
    async def _dispatch(_name, _args, _user):
        return {"summary": "한국어 본문"}

    monkeypatch.setattr(srv, "_dispatch", _dispatch)

    with caplog.at_level(logging.INFO, logger=srv.RESPONSE_SIZE_LOGGER):
        out = asyncio.run(srv.call_tool("akb_search", {"query": "본문"}))

    (record,) = _records(caplog)
    text = out.content[0].text
    assert record.result_bytes == len(text.encode("utf-8"))
    assert record.result_bytes > len(text)


def test_a_failing_call_is_measured_too(monkeypatch, caplog, quiet_sinks):
    async def _dispatch(_name, _args, _user):
        raise RuntimeError("boom")

    monkeypatch.setattr(srv, "_dispatch", _dispatch)

    with caplog.at_level(logging.INFO, logger=srv.RESPONSE_SIZE_LOGGER):
        out = asyncio.run(srv.call_tool("akb_search", {"query": "seam"}))

    (record,) = _records(caplog)
    assert record.tool == "akb_search"
    assert record.result_bytes == len(out.content[0].text.encode("utf-8"))
    assert "error" in json.loads(out.content[0].text)


def test_a_broken_logger_cannot_fail_the_call(monkeypatch, caplog, quiet_sinks):
    async def _dispatch(_name, _args, _user):
        return {"ok": True}

    def explode(*_args, **_kwargs):
        raise RuntimeError("log sink down")

    monkeypatch.setattr(srv, "_dispatch", _dispatch)
    monkeypatch.setattr(srv.logger_response, "info", explode)

    out = asyncio.run(srv.call_tool("akb_search", {"query": "seam"}))

    assert json.loads(out.content[0].text) == {"ok": True}
