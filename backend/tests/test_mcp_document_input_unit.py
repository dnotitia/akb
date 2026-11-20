"""The real backend catalog and proxy decoration must agree on body inputs."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

from jsonschema import Draft202012Validator
import pytest

from mcp_server.tools import TOOLS


@pytest.fixture(scope="module")
def schemas():
    backend = {tool.name: tool.input_schema for tool in TOOLS if tool.name in {"akb_put", "akb_update"}}
    script = """
      import { readFileSync } from 'node:fs';
      import { AKBProxy } from './packages/akb-mcp-client/lib/proxy.mjs';
      const tools = JSON.parse(readFileSync(0, 'utf8'));
      const original = JSON.stringify(tools);
      const result = AKBProxy.prototype._decorateTools.call({ _fileToolsForClient: () => [] }, { tools });
      if (JSON.stringify(tools) !== original) throw new Error('backend catalog mutated');
      process.stdout.write(JSON.stringify(Object.fromEntries(result.tools.map(t => [t.name, t.inputSchema]))));
    """
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=Path(__file__).resolve().parents[2],
        input=json.dumps([{"name": name, "inputSchema": schema} for name, schema in backend.items()]),
        capture_output=True, text=True, check=True,
    )
    return backend, json.loads(completed.stdout)


@pytest.mark.parametrize("name", ["akb_put", "akb_update"])
def test_actual_proxy_schema_preserves_body_and_metadata_contracts(schemas, name):
    backend, proxy = schemas
    direct = Draft202012Validator(backend[name])
    decorated = Draft202012Validator(proxy[name])
    Draft202012Validator.check_schema(proxy[name])
    common = {"vault": "v", "title": "title"} if name == "akb_put" else {"uri": "akb://v/doc/note.md"}
    pin = {"expected_commit": "a" * 40} if name == "akb_update" else {}
    for content in ("body", ""):
        assert direct.is_valid({**common, **pin, "content": content})
        assert decorated.is_valid({**common, **pin, "content": content})
    assert decorated.is_valid({**common, **pin, "file": "/draft.md"})
    for content in ("body", "", None, False, 0, [], {}):
        assert not decorated.is_valid({**common, **pin, "file": "/draft.md", "content": content})
    for file in ("", None, False, 0, [], {}):
        assert not decorated.is_valid({**common, **pin, "file": file})
    for content in (False, 0, [], {}):
        assert not direct.is_valid({**common, **pin, "content": content})
        assert not decorated.is_valid({**common, **pin, "content": content})
    for extra in ({}, {"content": None}):
        assert direct.is_valid({**common, **extra}) == (name == "akb_update")
        assert decorated.is_valid({**common, **extra}) == (name == "akb_update")


def test_proxy_file_schema_retains_both_original_read_predicates(schemas):
    _, proxy = schemas
    validate = Draft202012Validator(proxy["akb_update"]).is_valid
    common = {"uri": "akb://v/doc/note.md", "file": "/draft.md"}
    assert not validate(common)
    assert not validate({**common, "expected_commit": None, "expected_content_hash": None})
    for field, length in (("expected_commit", 40), ("expected_content_hash", 64)):
        assert validate({**common, field: "a" * length})
        for invalid in ("", " ", "a" * (length - 1), "a" * length + "\n", 1):
            assert not validate({**common, field: invalid})
    assert validate({**common, "expected_commit": "a" * 40, "expected_content_hash": "b" * 64})
    assert not validate({**common, "expected_commit": "bad", "expected_content_hash": "b" * 64})
    assert validate({"uri": common["uri"], "title": "metadata", "tags": [], "summary": "", "domain": ""})


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["akb_put", "akb_update"])
@pytest.mark.parametrize("content", [False, 0, [], {}, {"private": "must not be echoed"}])
async def test_backend_rejects_invalid_content_before_storage(name, content, monkeypatch, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path))
    from mcp_server import server
    access = AsyncMock(side_effect=AssertionError("must not access storage"))
    monkeypatch.setattr(server, "check_vault_access", access)
    handler = server._handle_put if name == "akb_put" else server._handle_update
    result = await handler({"content": content}, "user", SimpleNamespace(username="writer"))
    assert result["code"] == "invalid_argument"
    assert result["details"] == {"field": "/content", "reason": "invalid_type"}
    assert "must not be echoed" not in str(result)
    access.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("args, reason", [({}, "required"), ({"content": None}, "invalid_type")])
async def test_backend_put_missing_and_null_body_are_caller_errors(args, reason, monkeypatch, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path))
    from mcp_server import server
    result = await server._handle_put(args, "user", SimpleNamespace(username="writer"))
    assert result["code"] == "invalid_argument"
    assert result["details"] == {"field": "/content", "reason": reason}
