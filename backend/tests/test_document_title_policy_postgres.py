"""Title conflict checks at the document storage and Vault identity boundaries."""
import uuid

import pytest

from app.exceptions import ConflictError, DocumentTitleConflictError
from app.models.document import DocumentPutRequest, DocumentUpdateRequest
from app.services.access_service import check_vault_access, reset_authorized_vault
from app.services.document_service import DocumentService
from app.services.git_service import GitService
from app.services.native_document_service import NativeDocumentService
from tests.test_document_idempotency_postgres import counts
from tests.test_event_tail_postgres import _fresh_database, _make_vault


@pytest.mark.asyncio
async def test_native_title_conflicts_do_not_create_revisions_or_intents():
    async with _fresh_database() as pool:
        vault = "title-policy"
        vault_id = await _make_vault(pool, vault)
        service = NativeDocumentService(pool=pool)
        req = DocumentPutRequest(vault=vault, collection="notes", title="API Guide", content="body",
                                 title_conflict_policy="reject")
        existing = await service.put(req)
        target = await service.put(req.model_copy(update={"title": "Other"}))
        source = await service.put(req.model_copy(update={"collection": "drafts"}))
        before = await counts(pool, vault_id)
        for operation in (
            lambda: service.put(req.model_copy(update={"slug": "free"})),
            lambda: service.update(vault, target.path, DocumentUpdateRequest(title=req.title, title_conflict_policy="reject")),
            lambda: service.move(vault, source.path, collection=req.collection, title_conflict_policy="reject"),
        ):
            with pytest.raises(DocumentTitleConflictError) as failure:
                await operation()
            assert failure.value.details["existing_path"] == existing.path
            assert await counts(pool, vault_id) == before
        for doc in (existing, target, source):
            assert (await service.get(vault, doc.path)).current_commit == doc.current_commit


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["put", "move"])
async def test_native_filename_title_uses_the_public_name_at_the_destination(operation):
    async with _fresh_database() as pool:
        vault = "imported"
        vault_id = await _make_vault(pool, vault)
        service = NativeDocumentService(pool=pool)
        native = await service._native()
        raw = await native.create_text(namespace_id=vault_id, surface="document", path="drafts/initial.md",
                                       payload="Plain Markdown", actor="import", mutation_id=uuid.uuid4())
        current = await service.get(vault, raw.path)
        assert current.title == "initial.md"
        req = DocumentPutRequest(vault=vault, collection="notes", title="release.md", slug="guide", content="body",
                                 title_conflict_policy="reject")
        existing = await service.put(req)
        before = await counts(pool, vault_id)
        with pytest.raises(DocumentTitleConflictError) as failure:
            if operation == "put":
                await service.put(req.model_copy(update={"collection": "drafts", "title": current.title}))
            else:
                await service.move(vault, raw.path, collection="notes", slug="release", title_conflict_policy="reject")
        assert failure.value.details["existing_path"] == (raw.path if operation == "put" else existing.path)
        assert await counts(pool, vault_id) == before


@pytest.mark.asyncio
async def test_recreated_vault_is_rejected_before_title_or_source_lookup(tmp_path):
    async with _fresh_database() as pool:
        name, private = "original", "replacement"
        original = await _make_vault(pool, name)
        replacement = await _make_vault(pool, private)
        git = GitService(storage_path=str(tmp_path))
        for vault in (name, private):
            git.init_vault(vault)
        service = DocumentService(git=git)
        def request(vault, collection, slug):
            return DocumentPutRequest(vault=vault, collection=collection, title="Guide", slug=slug,
                                      content="body", title_conflict_policy="reject")

        twin = await service.put(request(private, "notes", "private-path"))
        source = await service.put(request(private, "drafts", "source"))
        actor = await pool.fetchval(
            "INSERT INTO users(username,email,password_hash) VALUES('writer','writer@example.invalid','disabled') RETURNING id"
        )
        await pool.execute("INSERT INTO vault_access(vault_id,user_id,role) VALUES($1,$2,'writer')", original, actor)
        try:
            await check_vault_access(str(actor), name, required_role="writer")
            async with pool.acquire() as conn, conn.transaction():
                await conn.execute("DELETE FROM vaults WHERE id=$1", original)
                await conn.execute("UPDATE vaults SET name=$1 WHERE id=$2", name, replacement)

            for operation in (
                lambda: service.put(request(name, "notes", "new")),
                lambda: service.get(name, source.path),
                lambda: service.update(name, source.path, DocumentUpdateRequest(title="Guide", title_conflict_policy="reject")),
                lambda: service.move(name, source.path, collection="notes", title_conflict_policy="reject"),
            ):
                with pytest.raises(ConflictError, match="Vault identity changed") as failure:
                    await operation()
                assert not failure.value.details
            rows = await pool.fetch("SELECT path,current_commit FROM documents WHERE vault_id=$1", replacement)
            assert {r["path"]: r["current_commit"] for r in rows} == {
                doc.path: doc.current_commit for doc in (source, twin)
            }
        finally:
            reset_authorized_vault()
