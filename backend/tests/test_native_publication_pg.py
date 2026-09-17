"""Native document publication authority against an isolated PostgreSQL database."""
from __future__ import annotations

import importlib.util
import os
import uuid
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from app.config import settings
from app.services import publication_service as publications
from app.services.native_revision_service import NativeRevisionService
from app.services.uri_service import doc_uri

pytestmark = pytest.mark.asyncio
_BACKEND = Path(__file__).resolve().parents[1]
_DSN = os.environ.get(
    "AKB_TEST_DSN", "postgresql://akb:akb@localhost:5433/akb",  # pragma: allowlist secret
)


@pytest_asyncio.fixture
async def native_publications(monkeypatch):
    try:
        admin = await asyncpg.connect(_DSN, timeout=2)
    except (OSError, asyncpg.PostgresError):
        if os.environ.get("AKB_REQUIRE_PG_TESTS") == "1" or os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail("Required publication PostgreSQL fixture is unreachable")
        pytest.skip("Publication PostgreSQL fixture is unreachable")
    name = f"akb_native_publication_{uuid.uuid4().hex[:12]}"
    pool = None
    conn = None
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        dsn = f"{_DSN.rsplit('/', 1)[0]}/{name}"
        conn = await asyncpg.connect(dsn)
        await conn.execute((_BACKEND / "app/db/init.sql").read_text())
        for filename in (
            "048_native_revision_core.py",
            "053_native_revision_m1_pg_body.py",
            "055_native_revision_m1_file_storage.py",
            "057_native_revision_m1_payload_placement.py",
            "106_native_document_publications.py",
        ):
            spec = importlib.util.spec_from_file_location(
                f"publication_fixture_{filename}", _BACKEND / "app/db/migrations" / filename,
            )
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            await module.migrate(conn=conn)
        vault_id = await conn.fetchval(
            "INSERT INTO vaults (name, git_path) VALUES ('publication-native', '/tmp/unused.git') RETURNING id",
        )
        other_vault_id = await conn.fetchval(
            "INSERT INTO vaults (name, git_path) VALUES ('publication-other', '/tmp/other-unused.git') RETURNING id",
        )
        await conn.close()
        conn = None
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)

        async def get_pool():
            return pool

        monkeypatch.setattr(publications, "get_pool", get_pool)
        monkeypatch.setattr(settings, "document_revision_backend", "postgres_native")
        monkeypatch.setattr(settings, "public_base_url", "https://publication.example.invalid")
        yield pool, vault_id, other_vault_id, NativeRevisionService(pool)
    finally:
        if pool is not None:
            await pool.close()
        if conn is not None and not conn.is_closed():
            await conn.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


async def _create(service, vault_id, body, *, path="published.md", surface="document"):
    return await service.create_text(
        namespace_id=vault_id, surface=surface, path=path, payload=body,
        actor="publication-test", mutation_id=uuid.uuid4(), resource_id=uuid.uuid4(),
    )


async def _publish(*, path="published.md", section_filter=None):
    created = await publications.create_publication_for_vault(
        vault_name="publication-native", resource_type="document", doc_id=path,
        section_filter=section_filter,
    )
    stored = await publications.get_publication_by_slug(created["slug"])
    assert stored is not None
    return stored


async def test_native_publication_reads_current_body_without_legacy_document(native_publications):
    pool, vault_id, _, service = native_publications
    first = await _create(service, vault_id, "---\ntitle: Published native\ntype: note\n---\n\nFirst body\n")
    publication = await _publish()
    assert str(publication["native_document_id"]) == str(first.resource_id)
    assert publication.get("document_id") is None
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM documents") == 0
    resolved = await publications.resolve_document_publication(publication)
    assert resolved["title"] == "Published native"
    assert "First body" in resolved["content"]
    assert resolved["content_unavailable"] is False
    await service.replace_text(
        namespace_id=vault_id, surface="document", path="published.md",
        payload="---\ntitle: Updated native\n---\n\nSecond body\n", actor="publication-test",
        mutation_id=uuid.uuid4(), expected_revision_id=first.revision_id,
    )
    resolved = await publications.resolve_document_publication(publication)
    assert resolved["title"] == "Updated native"
    assert "Second body" in resolved["content"]
    assert "First body" not in resolved["content"]


async def test_native_publication_section_assets_and_missing_section_fail_closed(native_publications):
    _, vault_id, _, service = native_publications
    visible, hidden = uuid.uuid4(), uuid.uuid4()
    await _create(service, vault_id,
        f"# Public\n![visible](/api/assets/{visible})\n\n# Private\n![hidden](/api/assets/{hidden})\nsecret\n")
    publication = await _publish(section_filter="Public")
    resolved = await publications.resolve_document_publication(publication)
    assert str(visible) in resolved["content"]
    assert "secret" not in resolved["content"]
    assert await publications.resolve_document_publication_asset_ids(publication) == frozenset({visible})
    missing = await _publish(section_filter="Absent")
    resolved = await publications.resolve_document_publication(missing)
    assert resolved["section_not_found"] is True
    assert resolved["content"] == ""
    assert await publications.resolve_document_publication_asset_ids(missing) == frozenset()


async def test_native_publication_move_path_reuse_and_soft_delete(native_publications):
    pool, vault_id, _, service = native_publications
    first = await _create(service, vault_id, "Original identity\n")
    publication = await _publish()
    moved = await service.move_text(
        namespace_id=vault_id, surface="document", path="published.md", path_to="moved.md",
        actor="publication-test", mutation_id=uuid.uuid4(), expected_revision_id=first.revision_id,
    )
    await _create(service, vault_id, "Replacement at old path\n")
    stored = await publications.get_publication_by_slug(publication["slug"])
    assert stored["resource_uri"] == doc_uri("publication-native", "moved.md")
    assert str(stored["native_document_id"]) == str(first.resource_id)
    assert "Original identity" in (await publications.resolve_document_publication(stored))["content"]
    # Even a request carrying the pre-move row must resolve by immutable identity.
    assert "Original identity" in (await publications.resolve_document_publication(publication))["content"]
    await service.delete_resource(
        namespace_id=vault_id, surface="document", path="moved.md", actor="publication-test",
        mutation_id=uuid.uuid4(), expected_revision_id=moved.revision_id,
    )
    assert await publications.get_publication_by_slug(publication["slug"]) is None
    with pytest.raises(publications.PublicationNotFound) as missing:
        await publications.resolve_document_publication(publication)
    assert str(first.resource_id) not in str(missing.value)
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM native_resources WHERE resource_id=$1", first.resource_id) == 1


async def test_native_publication_rejects_cross_vault_and_file_identity(native_publications):
    pool, vault_id, other_vault_id, service = native_publications
    foreign = await _create(service, other_vault_id, "Foreign\n")
    file = await _create(service, vault_id, "File\n", path="file.txt", surface="file")
    async with pool.acquire() as conn:
        for resource_id in (foreign.resource_id, file.resource_id):
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await conn.execute(
                    "INSERT INTO publications (slug,vault_id,resource_type,resource_uri,native_document_id) "
                    "VALUES ($1,$2,'document',$3,$4)",
                    uuid.uuid4().hex, vault_id, doc_uri("publication-native", "published.md"), resource_id,
                )


async def test_native_publication_resolved_identity_cannot_retarget_after_move(native_publications, monkeypatch):
    _, vault_id, _, service = native_publications
    first = await _create(service, vault_id, "Chosen identity\n")
    original_create = publications.create_publication

    async def move_before_insert(**kwargs):
        await service.move_text(
            namespace_id=vault_id, surface="document", path="published.md", path_to="moved.md",
            actor="publication-test", mutation_id=uuid.uuid4(), expected_revision_id=first.revision_id,
        )
        await _create(service, vault_id, "Replacement identity\n")
        return await original_create(**kwargs)

    monkeypatch.setattr(publications, "create_publication", move_before_insert)
    with pytest.raises(ValueError, match="moved concurrently"):
        await _publish()


async def test_native_publication_binding_is_exclusive_with_legacy_identity(native_publications):
    pool, vault_id, _, service = native_publications
    native = await _create(service, vault_id, "Native authority\n")
    async with pool.acquire() as conn:
        legacy_id = await conn.fetchval(
            "INSERT INTO documents (vault_id,path,title) VALUES ($1,'legacy.md','Legacy') RETURNING id",
            vault_id,
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO publications (slug,vault_id,resource_type,resource_uri,native_document_id,document_id) "
                "VALUES ($1,$2,'document',$3,$4,$5)",
                uuid.uuid4().hex, vault_id, doc_uri("publication-native", "published.md"),
                native.resource_id, legacy_id,
            )


async def test_native_publication_resolved_identity_deleted_before_insert(native_publications, monkeypatch):
    pool, vault_id, _, service = native_publications
    first = await _create(service, vault_id, "Chosen identity\n")
    original_create = publications.create_publication

    async def delete_before_insert(**kwargs):
        await service.delete_resource(
            namespace_id=vault_id, surface="document", path="published.md",
            actor="publication-test", mutation_id=uuid.uuid4(), expected_revision_id=first.revision_id,
        )
        # A new resource at the vacated path must not inherit the attempted link.
        await _create(service, vault_id, "Replacement identity\n")
        return await original_create(**kwargs)

    monkeypatch.setattr(publications, "create_publication", delete_before_insert)
    with pytest.raises(ValueError, match="deleted concurrently"):
        await _publish()
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM publications") == 0
