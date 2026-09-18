"""Public grep dispatch and multi-vault write boundaries."""
import tempfile
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.exceptions import ValidationError

settings.git_storage_path = tempfile.mkdtemp(prefix="akb-grep-contract-")


@pytest.mark.asyncio
@pytest.mark.parametrize("canonical,alias", [(True, False), (False, True)])
async def test_alias_conflict_before_storage(monkeypatch, canonical, alias):
    import app.services.search_service as module
    pool = AsyncMock(side_effect=AssertionError("must not access storage"))
    monkeypatch.setattr(module, "get_pool", pool)
    with pytest.raises(ValidationError, match="conflicts"):
        await module.SearchService().grep("x", include_text_files=canonical,
                                         measurement_include_text_files=alias)
    pool.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments,files,resource_output", [
    ({}, False, False), ({"include_text_files": True}, True, True),
    ({"measurement_include_text_files": True}, True, False),
    ({"include_text_files": True, "measurement_include_text_files": True}, True, True),
    ({"include_text_files": False}, False, False),
])
async def test_native_alias_dispatch(monkeypatch, arguments, files, resource_output):
    import app.services.search_service as module
    from app.services.m1_native_grep_service import M1NativeGrepService
    monkeypatch.setattr(settings, "document_revision_backend", "postgres_native")
    monkeypatch.setattr(module, "get_pool", AsyncMock(return_value=object()))
    native = AsyncMock(return_value={"results": []})
    monkeypatch.setattr(M1NativeGrepService, "grep_public", native)
    await module.SearchService().grep("x", user_id=str(uuid.uuid4()), **arguments)
    assert native.call_args.kwargs["include_text_files"] is files
    assert native.call_args.kwargs["resource_output"] is resource_output


@pytest.mark.asyncio
async def test_file_replace_before_storage(monkeypatch):
    import app.services.search_service as module
    pool = AsyncMock(side_effect=AssertionError("must not access storage"))
    monkeypatch.setattr(module, "get_pool", pool)
    with pytest.raises(ValidationError, match="does not support File"):
        await module.SearchService().grep("x", include_text_files=True, replace="y")
    pool.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_filters_and_write_acl(monkeypatch):
    import mcp_server.server as module
    check = AsyncMock()
    grep = AsyncMock(return_value={"results": []})
    monkeypatch.setattr(module, "check_vault_access", check)
    monkeypatch.setattr(module.search_service, "grep", grep)
    user = module._MCPUser(user_id="u", username="alice")
    filters = dict(vault=["a", "b"], doc_types=["note"], tags=["todo"],
                   include_archived=False, archive_scope="unarchived", include_text_files=False)
    await module._handle_grep({"pattern": "x", "replace": "y", **filters}, "u", user)
    assert [(c.args[1], c.kwargs["required_role"]) for c in check.call_args_list] == [
        ("a", "writer"), ("b", "writer"),
    ]
    for key, value in filters.items():
        assert grep.call_args.kwargs[key] == value
    assert grep.call_args.kwargs["measurement_include_text_files"] is None
    grep.reset_mock()
    check.side_effect = [None, PermissionError("denied")]
    with pytest.raises(PermissionError):
        await module._handle_grep({"pattern": "x", "replace": "y", "vault": ["a", "b"]}, "u", user)
    grep.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("vault", [None, [], [""], ""])
async def test_mcp_empty_write_scope(monkeypatch, vault):
    import mcp_server.server as module
    grep = AsyncMock()
    monkeypatch.setattr(module.search_service, "grep", grep)
    user = module._MCPUser(user_id="u", username="alice")
    result = await module._handle_grep({"pattern": "x", "replace": "y", "vault": vault}, "u", user)
    assert "vault is required" in str(result)
    grep.assert_not_called()


def test_rest_dispatch_and_limit(monkeypatch):
    from app.api.routes import search as module
    from types import SimpleNamespace
    app = FastAPI()
    app.include_router(module.router)
    app.dependency_overrides[module.get_current_user] = lambda: SimpleNamespace(user_id="u")
    grep = AsyncMock(return_value={"pattern": "x", "results": []})
    monkeypatch.setattr(module.search_service, "grep", grep)
    with TestClient(app) as client:
        response = client.get("/grep", params=[
            ("q", "x"), ("vault", "a"), ("vault", "b"), ("doc_types", "note"),
            ("tags", "todo"), ("archive_scope", "unarchived"), ("include_text_files", "true"),
        ])
        assert response.status_code == 200, response.text
        for key, value in dict(vault=["a", "b"], doc_types=["note"], tags=["todo"],
                               archive_scope="unarchived", include_text_files=True,
                               measurement_include_text_files=None).items():
            assert grep.call_args.kwargs[key] == value
        grep.reset_mock()
        assert client.get("/grep", params={"q": "x", "limit": 51}).status_code == 422
        grep.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_multiple_read_vaults_and_limit(monkeypatch):
    import mcp_server.server as module
    check = AsyncMock()
    grep = AsyncMock(return_value={"results": []})
    monkeypatch.setattr(module, "check_vault_access", check)
    monkeypatch.setattr(module.search_service, "grep", grep)
    user = module._MCPUser(user_id="u", username="alice")
    await module._handle_grep({"pattern": "x", "vault": ["a", "b", "a"]}, "u", user)
    assert [(c.args[1], c.kwargs["required_role"]) for c in check.call_args_list] == [
        ("a", "reader"), ("b", "reader"),
    ]
    grep.reset_mock()
    result = await module._handle_grep({"pattern": "x", "limit": 51}, "u", user)
    assert "between 1 and 50" in str(result)
    grep.assert_not_called()


def test_mcp_schema_accepts_vault_lists_and_exposes_filters():
    from jsonschema import validate
    from mcp_server.tools import TOOLS
    tool = next(tool for tool in TOOLS if tool.name == "akb_grep")
    validate({"pattern": "x", "vault": ["a", "b"], "include_text_files": True,
              "doc_types": ["note"], "tags": ["todo"], "archive_scope": "archived"}, tool.input_schema)
    assert tool.input_schema["properties"]["include_archived"]["default"] is True
