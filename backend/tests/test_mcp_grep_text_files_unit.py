from __future__ import annotations

import hashlib
import tempfile
import uuid

import pytest

from app.config import settings

# Importing the MCP server selects the process-scoped legacy DocumentService,
# which creates its git storage directory at module load.
settings.git_storage_path = tempfile.mkdtemp(prefix="akb-mcp-grep-file-test-vaults-")


def test_akb_grep_schema_exposes_guarded_text_file_measurement_argument():
    from mcp_server.tools import TOOLS

    grep = next(tool for tool in TOOLS if tool.name == "akb_grep")
    argument = grep.inputSchema["properties"]["measurement_include_text_files"]

    assert grep.inputSchema["properties"]["pattern"]["minLength"] == 1
    replacement_budget = grep.inputSchema["properties"]["max_replacements"]
    assert replacement_budget["default"] == 50
    assert replacement_budget["maximum"] == 1000
    assert (
        "treated literally"
        in grep.inputSchema["properties"]["replace"]["description"].lower()
    )
    assert "does not limit replacement writes" in grep.inputSchema["properties"]["limit"]["description"]
    assert argument["type"] == "boolean"
    assert argument["default"] is False
    assert "guarded native" in argument["description"].lower()


@pytest.mark.asyncio
async def test_mcp_grep_preserves_legacy_scope_and_defaults_to_documents(monkeypatch):
    import mcp_server.server as mcp_server

    access_checks = []
    grep_calls = []

    async def check_access(uid, vault, *, required_role):
        access_checks.append((uid, vault, required_role))

    async def grep(**kwargs):
        grep_calls.append(kwargs)
        return {"pattern": kwargs["pattern"], "results": []}

    monkeypatch.setattr(mcp_server, "check_vault_access", check_access)
    monkeypatch.setattr(mcp_server.search_service, "grep", grep)
    user = mcp_server._MCPUser(user_id="user-1", username="alice")

    result = await mcp_server._HANDLERS["akb_grep"](
        {"pattern": "needle", "vault": "legacy-vault", "collection": "notes"},
        user.user_id,
        user,
    )

    assert result == {"pattern": "needle", "results": []}
    assert access_checks == [("user-1", "legacy-vault", "reader")]
    assert grep_calls[0]["vault"] == "legacy-vault"
    assert grep_calls[0]["collection"] == "notes"
    assert grep_calls[0]["max_replacements"] == 50
    assert grep_calls[0]["measurement_include_text_files"] is False


@pytest.mark.asyncio
async def test_mcp_grep_passes_explicit_text_file_measurement_argument(monkeypatch):
    import mcp_server.server as mcp_server

    grep_calls = []

    async def check_access(_uid, _vault, *, required_role):
        assert required_role == "reader"

    async def grep(**kwargs):
        grep_calls.append(kwargs)
        return {"pattern": kwargs["pattern"], "results": []}

    monkeypatch.setattr(mcp_server, "check_vault_access", check_access)
    monkeypatch.setattr(mcp_server.search_service, "grep", grep)
    user = mcp_server._MCPUser(user_id="user-1", username="alice")

    await mcp_server._HANDLERS["akb_grep"](
        {
            "pattern": "needle",
            "vault": "measurement",
            "max_replacements": 17,
            "measurement_include_text_files": True,
        },
        user.user_id,
        user,
    )

    assert grep_calls[0]["measurement_include_text_files"] is True
    assert grep_calls[0]["max_replacements"] == 17


@pytest.mark.asyncio
async def test_public_native_grep_file_identity_and_default_exclusion(monkeypatch):
    from app.services.m1_native_grep_service import HeadBody, M1NativeGrepService
    from app.services.m1_pg_body_store import M1PgBodyStore

    document = HeadBody(
        namespace_id=uuid.uuid4(),
        vault="measurement",
        resource_id=uuid.uuid4(),
        surface="document",
        path="notes/readme.md",
        revision_id="a" * 40,
        digest="b" * 64,
        byte_size=len(b"document needle\n"),
        canonical_bytes=b"document needle\n",
    )
    file_bytes = b"file needle\n"
    file = HeadBody(
        namespace_id=document.namespace_id,
        vault="measurement",
        resource_id=uuid.uuid4(),
        surface="file",
        path="src/example.txt",
        revision_id="c" * 40,
        digest=hashlib.sha256(file_bytes).hexdigest(),
        byte_size=len(file_bytes),
        canonical_bytes=file_bytes,
    )
    service = M1NativeGrepService(object())  # type: ignore[arg-type]

    async def head_bodies(*, surfaces, **_kwargs):
        return [body for body in (document, file) if body.surface in surfaces]

    monkeypatch.setattr(service, "_head_bodies", head_bodies)

    legacy = await service.grep_public("needle", user_id=uuid.uuid4())
    with_files = await service.grep_public(
        "needle", user_id=uuid.uuid4(), include_text_files=True,
    )

    assert [row["uri"] for row in legacy["results"]] == [document.uri]
    assert "resource_type" not in legacy["results"][0]
    # Placement is not part of the File-only additive head identity: it rides
    # on every native row, so a Document-only native grep still reports which
    # placement its bytes came from.
    assert legacy["results"][0]["payload_placement"] == M1PgBodyStore.selected_placement
    assert with_files["results"][1] == {
        "uri": file.uri,
        "vault": "measurement",
        "path": "src/example.txt",
        "title": "example.txt",
        "matches": [{"section": None, "text": "file needle"}],
        "resource_type": "file",
        "revision": file.revision_id,
        "content_hash": file.digest,
        "payload_placement": M1PgBodyStore.selected_placement,
    }


@pytest.mark.asyncio
async def test_text_file_measurement_argument_fails_closed_when_native_arm_is_off(monkeypatch):
    from app.exceptions import ValidationError
    from app.services.search_service import SearchService

    monkeypatch.setattr(settings, "document_revision_backend", "bare_git_current")
    monkeypatch.setattr(settings, "native_revision_m1_measurement_only", False)
    monkeypatch.setattr(settings, "db_name", "akb")

    with pytest.raises(ValidationError, match="guarded native measurement backend"):
        await SearchService().grep(
            "needle",
            vault="legacy-vault",
            user_id=str(uuid.uuid4()),
            measurement_include_text_files=True,
        )


def test_text_file_measurement_body_rejects_binary_bytes():
    from app.exceptions import ValidationError
    from app.services.m1_pg_body_store import M1PgBodyStore

    with pytest.raises(ValidationError):
        M1PgBodyStore._verified_bytes(b"needle\x00\xff")
