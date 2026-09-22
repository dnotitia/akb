"""Backend-aware discovery and explicit unsupported vault-creation requests."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.services import template_registry
from app.services.native_document_service import (
    NativeDocumentService,
    NativeRevisionUnsupportedSurfaceError,
)
from app.services.vault_creation_capabilities import get_vault_creation_capabilities
from mcp_server.help import HELP, _resolve_help
from mcp_server.tools import TOOLS, available_tools


@pytest.mark.parametrize("backend", ["bare_git", "bare_git_current", "postgres_native", "native_ledger_m1"])
def test_backend_catalog_preserves_bare_git_and_filters_native(monkeypatch, backend):
    before = [tool.model_dump() for tool in TOOLS]
    monkeypatch.setattr(settings, "document_revision_backend", backend)
    legacy = backend.startswith("bare_git")
    capabilities = get_vault_creation_capabilities()
    assert capabilities.templates is legacy
    assert capabilities.external_git is legacy
    create = next(tool for tool in available_tools() if tool.name == "akb_create_vault")
    for argument in ("template", "external_git"):
        assert (argument in create.input_schema["properties"]) is legacy
    assert create.input_schema["required"] == ["name"]
    assert [tool.model_dump() for tool in TOOLS] == before
    if legacy:
        assert create is next(tool for tool in TOOLS if tool.name == "akb_create_vault")
    else:
        assert "empty vault creation" in create.description
        assert "Pass `external_git`" not in create.description


@pytest.mark.parametrize("topic", ["access", "onboarding", "akb_create_vault", "create_vault"])
def test_help_matches_backend_for_exact_and_alias_topics(monkeypatch, topic):
    canonical = "akb_create_vault" if topic == "create_vault" else topic
    monkeypatch.setattr(settings, "document_revision_backend", "bare_git")
    assert _resolve_help(topic) == HELP[canonical]
    assert 'template="engineering"' in _resolve_help(topic)
    monkeypatch.setattr(settings, "document_revision_backend", "postgres_native")
    text = _resolve_help(topic)
    assert 'template="engineering"' not in text
    assert "unavailable" in text
    assert "M1" not in text
    if topic == "onboarding":
        assert 'akb_create_collection(vault="my-project", path="decisions")' in text
        assert "### Step 3: Invite team members" in text


@pytest.fixture
def surfaces(monkeypatch, tmp_path):
    # Import the existing facade before selecting the mode being exercised;
    # requests below use an injected service and never connect to storage.
    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    from app.api.routes import documents
    from mcp_server import server

    monkeypatch.setattr(documents, "check_vault_scope", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "check_vault_scope", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_get_user", AsyncMock(return_value=server._MCPUser()))
    monkeypatch.setattr(server.audit_log, "record_tool", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server.tool_usage, "record", lambda *_args, **_kwargs: None)
    return documents, server


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["bare_git", "postgres_native"])
async def test_rest_template_discovery_matches_backend(monkeypatch, surfaces, backend):
    documents, _ = surfaces
    monkeypatch.setattr(settings, "document_revision_backend", backend)
    summaries = [SimpleNamespace(
        name="engineering", display_name="Engineering", description="Software development",
        collection_count=1, collections=[SimpleNamespace(path="specs", name="Specs")],
    )]
    monkeypatch.setattr(template_registry, "list_summaries", lambda: summaries)
    templates = await documents.list_vault_templates(user=SimpleNamespace(user_id="user"))
    assert [template.name for template in templates] == (["engineering"] if backend == "bare_git" else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["bare_git", "postgres_native"])
@pytest.mark.parametrize("ack_version", [None, 2])
async def test_mcp_discovery_keeps_backend_filter_when_decorating_ack(monkeypatch, surfaces, backend, ack_version):
    _, server = surfaces
    monkeypatch.setattr(settings, "document_revision_backend", backend)
    monkeypatch.setattr(server, "_vault_skill_preflight_version", lambda: ack_version)
    tools = await server.list_tools()
    create = next(tool for tool in tools if tool.name == "akb_create_vault")
    properties = create.input_schema["properties"]
    assert ("template" in properties) is (backend == "bare_git")
    assert ("external_git" in properties) is (backend == "bare_git")
    assert (server.VAULT_SKILL_ACK_ARGUMENT in properties) is (ack_version == 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("option", [{"template": "engineering"}, {"external_git": {"url": "https://example.com/repo.git"}}])
async def test_native_explicit_unsupported_mcp_request_retains_stable_error(monkeypatch, surfaces, option):
    _, server = surfaces
    monkeypatch.setattr(settings, "document_revision_backend", "postgres_native")
    service = NativeDocumentService(pool=MagicMock())
    monkeypatch.setattr(service, "_pool", AsyncMock(side_effect=AssertionError("must not reach storage")))
    monkeypatch.setattr(server, "doc_service", service)
    result = await server.call_tool("akb_create_vault", {"name": "new-vault", **option})
    body = json.loads(result.content[0].text)
    assert body["code"] == "native_revision_surface_unsupported"
    assert "PostgreSQL Native" in body["error"]
    assert "M1" not in body["error"]
    service._pool.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_explicit_rest_template_rejects_before_storage(monkeypatch, surfaces):
    documents, _ = surfaces
    monkeypatch.setattr(settings, "document_revision_backend", "postgres_native")
    monkeypatch.setattr(template_registry, "list_names", lambda: ["engineering"])
    service = NativeDocumentService(pool=MagicMock())
    monkeypatch.setattr(service, "_pool", AsyncMock(side_effect=AssertionError("must not reach storage")))
    monkeypatch.setattr(documents, "doc_service", service)
    with pytest.raises(NativeRevisionUnsupportedSurfaceError) as caught:
        await documents.create_vault(name="new-vault", template="engineering", user=SimpleNamespace(user_id="user"))
    assert caught.value.status_code == 501
    assert caught.value.code == "native_revision_surface_unsupported"
    assert "PostgreSQL Native" in str(caught.value)
    service._pool.assert_not_awaited()


@pytest.mark.asyncio
async def test_bare_git_rest_and_mcp_still_forward_creation_options(monkeypatch, surfaces):
    documents, server = surfaces
    monkeypatch.setattr(settings, "document_revision_backend", "bare_git")
    monkeypatch.setattr(template_registry, "list_names", lambda: ["engineering"])
    service = SimpleNamespace(create_vault=AsyncMock(return_value="vault-id"))
    monkeypatch.setattr(documents, "doc_service", service)
    monkeypatch.setattr(server, "doc_service", service)
    await documents.create_vault(name="new-vault", template="engineering", user=SimpleNamespace(user_id="user"))
    assert service.create_vault.await_args.kwargs["template"] == "engineering"
    external = {"url": "https://example.com/repo.git"}
    monkeypatch.setattr(server, "_external_git_view", AsyncMock(return_value=external))
    result = await server.call_tool("akb_create_vault", {"name": "mirror", "external_git": external})
    assert service.create_vault.await_args.kwargs["external_git"] == external
    assert json.loads(result.content[0].text)["external_git"] == external
