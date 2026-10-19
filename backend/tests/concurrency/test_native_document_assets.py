"""Committed Native publication/attachment invariants, using the real ledger."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.exceptions import ValidationError
from app.models.document import DocumentPutRequest, DocumentUpdateRequest
from app.repositories.native_revision_repo import NativeRevisionRepository
from app.services import asset_gc_worker, asset_service, native_asset_ref_backfill
from app.services.m1_pg_body_store import M1PgBodyStore
from app.services.native_document_service import NativeDocumentService
from app.services.native_revision_service import NativeRevisionService
from tests.concurrency.test_native_ledger_b_core import _create_args, _fresh_database, _load_migration, _MIGRATION

pytestmark = pytest.mark.asyncio


async def _asset(pool, vault_id):
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """INSERT INTO vault_files
               (vault_id, kind, upload_state, name, s3_key, mime_type,
                size_bytes, content_hash, hash_verified_at)
               VALUES ($1, 'attachment', 'confirmed', 'image.png', $2,
                       'image/png', 1, $3, NOW()) RETURNING id""",
            vault_id, str(uuid.uuid4()), "a" * 64,
        )


def _body(asset_id):
    return f"![image](/api/assets/{asset_id})\n"


async def _refs(pool, resource_id):
    async with pool.acquire() as conn:
        return set(await conn.fetchval(
            "SELECT array_agg(asset_id) FROM document_asset_refs WHERE native_document_id=$1",
            resource_id,
        ) or [])


async def test_direct_document_publication_and_remove_last_image():
    async with _fresh_database() as (pool, vault_id):
        asset = await _asset(pool, vault_id)
        service = NativeRevisionService(pool, payload_store=M1PgBodyStore(pool))
        args = _create_args(vault_id)
        args["payload"] = _body(asset)
        created = await service.create_text(**args)
        assert await _refs(pool, created.resource_id) == {asset}
        replaced = await service.replace_text(
            **{**args, "payload": "no images", "mutation_id": uuid.uuid4()},
            expected_revision_id=created.revision_id,
        )
        assert await _refs(pool, created.resource_id) == set()
        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT count(*) FROM document_asset_revision_refs WHERE commit_hash=$1 AND asset_id=$2",
                created.revision_id, asset,
            ) == 1
        assert (await service.get_current(namespace_id=vault_id, surface="document", path=args["path"])).revision_id == replaced.revision_id


async def test_new_missing_image_is_rejected_without_publishing_a_document():
    async with _fresh_database() as (pool, vault_id):
        service = NativeRevisionService(pool, payload_store=M1PgBodyStore(pool))
        args = _create_args(vault_id)
        args["payload"] = _body(uuid.uuid4())
        with pytest.raises(ValidationError, match="unavailable image"):
            await service.create_text(**args)
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM native_resources") == 0


@pytest.mark.parametrize("competitor", ["replace", "delete"])
async def test_delayed_response_cannot_republish_old_image_references(competitor):
    async with _fresh_database() as (pool, vault_id):
        image_a, image_b = await _asset(pool, vault_id), await _asset(pool, vault_id)
        committed, resume = asyncio.Event(), asyncio.Event()

        async def pause(boundary):
            if boundary == "authority.after_commit_before_response":
                committed.set()
                await resume.wait()

        delayed = NativeRevisionService(pool, payload_store=M1PgBodyStore(pool), failpoint=pause)
        other = NativeRevisionService(pool, payload_store=M1PgBodyStore(pool))
        args = {**_create_args(vault_id), "payload": _body(image_a)}
        task = asyncio.create_task(delayed.create_text(**args))
        try:
            await asyncio.wait_for(committed.wait(), 5)
            head = await other.get_current(namespace_id=vault_id, surface="document", path=args["path"])
            common = {"namespace_id": vault_id, "surface": "document", "path": args["path"],
                      "actor": "competitor", "mutation_id": uuid.uuid4(), "expected_revision_id": head.revision_id}
            if competitor == "replace":
                await other.replace_text(**common, payload=_body(image_b))
            else:
                await other.delete_resource(**common)
        finally:
            resume.set()
            receipt = await asyncio.wait_for(task, 5)
        assert await _refs(pool, receipt.resource_id) == ({image_b} if competitor == "replace" else set())


async def test_failed_publication_rolls_back_claim_refs_head_and_events():
    async with _fresh_database() as (pool, vault_id):
        image = await _asset(pool, vault_id)

        def fail(boundary):
            if boundary == "authority.before_commit":
                raise RuntimeError("injected publication failure")

        service = NativeRevisionService(pool, payload_store=M1PgBodyStore(pool), failpoint=fail)
        with pytest.raises(RuntimeError, match="injected"):
            await service.create_text(**{**_create_args(vault_id), "payload": _body(image)})
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT attachment_claimed_at FROM vault_files WHERE id=$1", image) is None
            for table in ("native_resources", "native_revisions", "document_asset_refs", "document_asset_revision_refs", "events"):
                assert await conn.fetchval(f"SELECT count(*) FROM {table}") == 0


@pytest.mark.parametrize("operation", ["replace", "delete"])
async def test_facade_delayed_save_cannot_overwrite_newer_refs(operation):
    async with _fresh_database() as (pool, vault_id):
        async with pool.acquire() as conn:
            vault = await conn.fetchval("SELECT name FROM vaults WHERE id=$1", vault_id)
        first, second = await _asset(pool, vault_id), await _asset(pool, vault_id)
        documents = NativeDocumentService(pool=pool)
        created = await documents.put(DocumentPutRequest(vault=vault, collection="notes", title="Images", content="empty"))
        committed, resume = asyncio.Event(), asyncio.Event()

        async def pause(boundary):
            if boundary == "authority.after_commit_before_response":
                committed.set()
                await resume.wait()

        delayed = NativeDocumentService(pool=pool, failpoint=pause)
        task = asyncio.create_task(delayed.update(vault, created.path, DocumentUpdateRequest(content=_body(first), expected_commit=created.current_commit)))
        try:
            await asyncio.wait_for(committed.wait(), 5)
            if operation == "replace":
                _, current = await documents._current(vault, created.path)
                await documents.update(vault, created.path, DocumentUpdateRequest(content=_body(second), expected_commit=current.revision_id))
            else:
                await documents.delete(vault, created.path)
        finally:
            resume.set()
            await asyncio.wait_for(task, 5)
        async with pool.acquire() as conn:
            resource = await conn.fetchval("SELECT resource_id FROM native_resources WHERE current_path=$1", created.path)
        assert await _refs(pool, resource) == ({second} if operation == "replace" else set())


async def test_broken_import_stays_editable_but_new_broken_images_are_rejected():
    async with _fresh_database() as (pool, vault_id):
        missing, another = uuid.uuid4(), uuid.uuid4()
        service = NativeRevisionService(pool, payload_store=M1PgBodyStore(pool))
        args = {**_create_args(vault_id), "payload": _body(missing)}
        created = await service.create_text(**args, allow_unavailable_asset_refs=True)
        changed = await service.replace_text(**{**args, "payload": _body(missing)+"edit", "mutation_id": uuid.uuid4()})
        assert changed.parent_revision_id == created.revision_id
        with pytest.raises(ValidationError, match="unavailable image"):
            await service.replace_text(**{**args, "payload": _body(missing)+_body(another), "mutation_id": uuid.uuid4()})
        assert await _refs(pool, created.resource_id) == set()


async def test_move_uses_locked_body_not_corrupted_live_cache_and_replay_is_inert():
    async with _fresh_database() as (pool, vault_id):
        image, wrong = await _asset(pool, vault_id), await _asset(pool, vault_id)
        service = NativeRevisionService(pool, payload_store=M1PgBodyStore(pool))
        args = {**_create_args(vault_id), "payload": _body(image)}
        created = await service.create_text(**args)
        async with pool.acquire() as conn:
            await conn.execute("UPDATE document_asset_refs SET asset_id=$1 WHERE native_document_id=$2", wrong, created.resource_id)
        moved = await service.move_text(
            namespace_id=vault_id, surface="document", path=args["path"], path_to="moved/note.md",
            actor="test", mutation_id=uuid.uuid4(), expected_revision_id=created.revision_id,
        )
        assert await _refs(pool, created.resource_id) == {image}
        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT count(*) FROM document_asset_revision_refs WHERE document_path=$1 AND commit_hash=$2 AND asset_id=$3",
                args["path"], created.revision_id, image,
            ) == 1
        await service.delete_resource(namespace_id=vault_id, surface="document", path=moved.path, actor="test", mutation_id=uuid.uuid4())
        async with pool.acquire() as conn:
            before = await conn.fetch("SELECT * FROM document_asset_revision_refs ORDER BY commit_hash, asset_id")
            events = await conn.fetchval("SELECT count(*) FROM events")
        assert (await service.create_text(**args)).idempotent_replay
        assert await _refs(pool, created.resource_id) == set()
        async with pool.acquire() as conn:
            assert await conn.fetch("SELECT * FROM document_asset_revision_refs ORDER BY commit_hash, asset_id") == before
            assert await conn.fetchval("SELECT count(*) FROM events") == events


async def test_delete_retains_actual_head_and_collection_policy_does_not_extend_it():
    async with _fresh_database() as (pool, vault_id):
        image = await _asset(pool, vault_id)
        service = NativeRevisionService(pool, payload_store=M1PgBodyStore(pool))
        args = {**_create_args(vault_id), "payload": _body(image)}
        created = await service.create_text(**args)
        async with pool.acquire() as conn:
            await conn.execute("UPDATE document_asset_revision_refs SET retain_until=NOW()-INTERVAL '1 day'")
        await service.delete_resource(namespace_id=vault_id, surface="document", path=created.path, actor="test", mutation_id=uuid.uuid4())
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT bool_and(retain_until>NOW()) FROM document_asset_revision_refs")
        other = await service.create_text(**{**args, "path": "other.md", "mutation_id": uuid.uuid4()})
        async with pool.acquire() as conn:
            await conn.execute("UPDATE document_asset_revision_refs SET retain_until=NOW()-INTERVAL '1 day' WHERE commit_hash=$1", other.revision_id)
        await service.delete_resource(namespace_id=vault_id, surface="document", path=other.path, actor="test", mutation_id=uuid.uuid4(), retain_document_assets=False)
        assert await _refs(pool, other.resource_id) == set()
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT bool_and(retain_until<NOW()) FROM document_asset_revision_refs WHERE commit_hash=$1", other.revision_id)


@pytest.mark.parametrize("claim_first", [True, False])
async def test_real_gc_and_publication_both_winner_orders(monkeypatch, claim_first):
    async with _fresh_database() as (pool, vault_id):
        async with pool.acquire() as conn:
            await _load_migration(_MIGRATION.with_name("019_s3_delete_outbox.py")).migrate(conn=conn)
        image = await _asset(pool, vault_id)
        async with pool.acquire() as conn:
            await conn.execute("UPDATE vault_files SET created_at=NOW()-INTERVAL '90 days', updated_at=NOW()-INTERVAL '90 days' WHERE id=$1", image)

        async def fixture_pool():
            return pool

        monkeypatch.setattr(asset_gc_worker, "get_pool", fixture_pool)
        service = NativeRevisionService(pool, payload_store=M1PgBodyStore(pool))
        args = {**_create_args(vault_id), "payload": _body(image)}
        if claim_first:
            paused, resume = asyncio.Event(), asyncio.Event()

            async def pause(boundary):
                if boundary == "authority.before_commit":
                    paused.set()
                    await resume.wait()

            service.failpoint = pause
            task = asyncio.create_task(service.create_text(**args))
            try:
                await asyncio.wait_for(paused.wait(), 5)
                assert await asyncio.wait_for(asset_gc_worker.collect_once(), 5) == 0
            finally:
                resume.set()
                receipt = await asyncio.wait_for(task, 5)
            assert await _refs(pool, receipt.resource_id) == {image}
        else:
            collected, resume, claiming = asyncio.Event(), asyncio.Event(), asyncio.Event()
            original_enqueue = asset_gc_worker.enqueue_delete
            original_claim = asset_service.claim_document_asset_ids

            async def pause_enqueue(*args, **kwargs):
                result = await original_enqueue(*args, **kwargs)
                collected.set()
                await resume.wait()
                return result

            async def observe_claim(*args, **kwargs):
                claiming.set()
                return await original_claim(*args, **kwargs)

            monkeypatch.setattr(asset_gc_worker, "enqueue_delete", pause_enqueue)
            monkeypatch.setattr(asset_service, "claim_document_asset_ids", observe_claim)
            gc = asyncio.create_task(asset_gc_worker.collect_once())
            write = None
            try:
                await asyncio.wait_for(collected.wait(), 5)
                write = asyncio.create_task(service.create_text(**args))
                await asyncio.wait_for(claiming.wait(), 5)
                assert not write.done()
            finally:
                resume.set()
                assert await asyncio.wait_for(gc, 5) == 1
                if write is not None:
                    with pytest.raises(ValidationError, match="unavailable image"):
                        await asyncio.wait_for(write, 5)
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM s3_delete_outbox") == (0 if claim_first else 1)


async def test_repair_clears_empty_body_refs_and_does_not_extend_retention(monkeypatch):
    async with _fresh_database() as (pool, vault_id):
        image = await _asset(pool, vault_id)
        service = NativeRevisionService(pool, payload_store=M1PgBodyStore(pool))
        created = await service.create_text(**_create_args(vault_id))
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO document_asset_refs (vault_id,native_document_id,asset_id) VALUES ($1,$2,$3)", vault_id, created.resource_id, image)

        async def fixture_pool():
            return pool

        monkeypatch.setattr(native_asset_ref_backfill, "get_pool", fixture_pool)
        report = await native_asset_ref_backfill.backfill_native_asset_refs()
        assert report.failed == 0 and report.complete
        assert await _refs(pool, created.resource_id) == set()

        replacement = await service.replace_text(**{**_create_args(vault_id), "payload": _body(image)})
        async with pool.acquire() as conn:
            before = await conn.fetchval("SELECT retain_until FROM document_asset_revision_refs WHERE commit_hash=$1", replacement.revision_id)
        report = await native_asset_ref_backfill.backfill_native_asset_refs()
        assert report.failed == 0
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT retain_until FROM document_asset_revision_refs WHERE commit_hash=$1", replacement.revision_id) == before
        await service.delete_resource(namespace_id=vault_id, surface="document", path=created.path, actor="test", mutation_id=uuid.uuid4())
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO document_asset_refs (vault_id,native_document_id,asset_id) VALUES ($1,$2,$3)", vault_id, created.resource_id, image)
        audit = await native_asset_ref_backfill.backfill_native_asset_refs(dry_run=True)
        assert audit.stale_refs == 1 and audit.refs_removed == 0
        assert await _refs(pool, created.resource_id) == {image}
        repair = await native_asset_ref_backfill.backfill_native_asset_refs()
        assert repair.stale_refs == repair.refs_removed == 1
        assert await _refs(pool, created.resource_id) == set()

@pytest.mark.parametrize("operation", ["replace", "delete"])
async def test_existing_document_failure_rolls_back_refs_and_retention(operation):
    async with _fresh_database() as (pool, vault_id):
        first, second = await _asset(pool, vault_id), await _asset(pool, vault_id)
        service = NativeRevisionService(pool, payload_store=M1PgBodyStore(pool))
        args = {**_create_args(vault_id), "payload": _body(first)}
        created = await service.create_text(**args)
        async with pool.acquire() as conn:
            before = await conn.fetch("SELECT * FROM document_asset_revision_refs")
            claim = await conn.fetchrow("SELECT attachment_claimed_at,updated_at FROM vault_files WHERE id=$1", first)

        def fail(boundary):
            if boundary == "authority.before_commit":
                raise RuntimeError("rollback existing document")

        service.failpoint = fail
        with pytest.raises(RuntimeError, match="rollback existing"):
            if operation == "replace":
                await service.replace_text(**{**args, "payload": _body(second), "mutation_id": uuid.uuid4()})
            else:
                await service.delete_resource(namespace_id=vault_id, surface="document", path=created.path, actor="test", mutation_id=uuid.uuid4())
        head = await service.get_current(namespace_id=vault_id, surface="document", path=created.path)
        assert head.revision_id == created.revision_id and await _refs(pool, created.resource_id) == {first}
        async with pool.acquire() as conn:
            assert await conn.fetch("SELECT * FROM document_asset_revision_refs") == before
            assert await conn.fetchrow("SELECT attachment_claimed_at,updated_at FROM vault_files WHERE id=$1", first) == claim
            assert await conn.fetchval("SELECT attachment_claimed_at FROM vault_files WHERE id=$1", second) is None


async def test_delete_retry_retains_new_head_not_original_snapshot(monkeypatch):
    async with _fresh_database() as (pool, vault_id):
        first, second = await _asset(pool, vault_id), await _asset(pool, vault_id)
        async with pool.acquire() as conn:
            vault = await conn.fetchval("SELECT name FROM vaults WHERE id=$1", vault_id)
        documents = NativeDocumentService(pool=pool)
        created = await documents.put(DocumentPutRequest(vault=vault, collection="notes", title="Retry", content=_body(first)))
        original_delete = NativeRevisionService.delete_resource
        new_head = None

        async def race(service, **kwargs):
            nonlocal new_head
            if new_head is None:
                changed = await documents.update(vault, created.path, DocumentUpdateRequest(content=_body(second), expected_commit=created.current_commit))
                new_head = changed.current_commit
                async with pool.acquire() as conn:
                    await conn.execute("UPDATE document_asset_revision_refs SET retain_until=NOW()-INTERVAL '1 day' WHERE commit_hash=$1", new_head)
            return await original_delete(service, **kwargs)

        monkeypatch.setattr(NativeRevisionService, "delete_resource", race)
        assert await documents.delete(vault, created.path)
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT bool_and(retain_until>NOW()) FROM document_asset_revision_refs WHERE commit_hash=$1 AND asset_id=$2", new_head, second)


async def test_repair_serializes_with_delete_without_resurrecting_refs(monkeypatch):
    async with _fresh_database() as (pool, vault_id):
        image = await _asset(pool, vault_id)
        service = NativeRevisionService(pool, payload_store=M1PgBodyStore(pool))
        created = await service.create_text(**{**_create_args(vault_id), "payload": _body(image)})
        paused, resume, deleting = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original_lock = NativeRevisionRepository.lock_resource
        first = True

        async def lock(conn, resource_id):
            nonlocal first
            if first:
                first = False
                resource = await original_lock(conn, resource_id)
                paused.set()
                await resume.wait()
                return resource
            deleting.set()
            return await original_lock(conn, resource_id)

        async def fixture_pool():
            return pool

        monkeypatch.setattr(native_asset_ref_backfill, "get_pool", fixture_pool)
        monkeypatch.setattr(NativeRevisionRepository, "lock_resource", staticmethod(lock))
        repair = asyncio.create_task(native_asset_ref_backfill.backfill_native_asset_refs())
        delete = None
        try:
            await asyncio.wait_for(paused.wait(), 5)
            delete = asyncio.create_task(service.delete_resource(namespace_id=vault_id, surface="document", path=created.path, actor="test", mutation_id=uuid.uuid4()))
            await asyncio.wait_for(deleting.wait(), 5)
            assert not delete.done()
        finally:
            resume.set()
            assert (await asyncio.wait_for(repair, 5)).failed == 0
            if delete is not None:
                await asyncio.wait_for(delete, 5)
        assert await _refs(pool, created.resource_id) == set()
