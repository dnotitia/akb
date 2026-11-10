"""Public retry identity against real Native revisions and transaction locks."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.models.document import DocumentPutRequest, DocumentUpdateRequest
from app.services.native_document_service import NativeDocumentService
from tests.test_event_tail_postgres import _fresh_database, _make_vault

pytestmark = pytest.mark.asyncio


def put_request(vault="retry-documents", **values):
    return DocumentPutRequest(vault=vault, collection="notes", title="Retry document",
                              content="original", **values)


async def counts(pool, vault_id):
    async with pool.acquire() as conn:
        return (
            await conn.fetchval("SELECT count(*) FROM native_resources WHERE namespace_id=$1", vault_id),
            await conn.fetchval("SELECT count(*) FROM native_revisions WHERE namespace_id=$1", vault_id),
            await conn.fetchval("SELECT count(*) FROM native_revision_activity WHERE namespace_id=$1", vault_id),
            await conn.fetchval("SELECT count(*) FROM native_invalidation_intents WHERE namespace_id=$1", vault_id),
        )


async def test_put_replay_precedes_slug_title_checks_and_uses_original_receipt():
    async with _fresh_database() as pool:
        vault_id = await _make_vault(pool, "retry-documents")
        service = NativeDocumentService(pool=pool)
        actor = uuid.uuid4()
        req = put_request(slug="fixed", title_conflict_policy="reject", idempotency_key=uuid.uuid4())
        first = await service.put(req, agent_id="writer", idempotency_actor=actor)
        before = await counts(pool, vault_id)
        # A fresh service has no in-memory replay cache or original timestamp.
        second = await NativeDocumentService(pool=pool).put(req, agent_id="renamed-writer", idempotency_actor=actor)
        assert second == first
        assert await counts(pool, vault_id) == before == (1, 1, 1, 1)
        with pytest.raises(ConflictError):
            await service.put(req.model_copy(update={"content": "different"}), idempotency_actor=actor)
        with pytest.raises(ConflictError):
            await service.update(req.vault, first.path, DocumentUpdateRequest(
                title="cross-operation", idempotency_key=req.idempotency_key,
            ), idempotency_actor=actor)
        assert await counts(pool, vault_id) == before


@pytest.mark.parametrize("explicit_slug", [False, True])
async def test_simultaneous_puts_share_one_revision_despite_dynamic_fields(explicit_slug):
    async with _fresh_database() as pool:
        vault_id = await _make_vault(pool, "retry-documents")
        barrier = asyncio.Barrier(4)
        async def pause(name):
            if name == "payload.after_prepare_before_tx":
                await barrier.wait()
        service = NativeDocumentService(pool=pool, failpoint=pause)
        req = put_request(idempotency_key=uuid.uuid4(), slug="fixed" if explicit_slug else None)
        actor = uuid.uuid4()
        results = await asyncio.wait_for(asyncio.gather(*(
            service.put(req, agent_id="writer", idempotency_actor=actor) for _ in range(4)
        )), timeout=20)
        assert all(value == results[0] for value in results)
        assert await counts(pool, vault_id) == (1, 1, 1, 1)


@pytest.mark.parametrize("body_update", [False, True])
async def test_concurrent_updates_and_later_replay_preserve_receipt_and_occ(body_update):
    async with _fresh_database() as pool:
        vault_id = await _make_vault(pool, "retry-documents")
        service = NativeDocumentService(pool=pool)
        created = await service.put(put_request())
        barrier = asyncio.Barrier(4)
        async def pause(name):
            if name == "payload.after_prepare_before_tx":
                await barrier.wait()
        concurrent = NativeDocumentService(pool=pool, failpoint=pause)
        actor = uuid.uuid4()
        req = DocumentUpdateRequest(
            tags=[], summary="", domain="", title="Updated",
            content="changed" if body_update else None,
            expected_commit=created.current_commit if body_update else None,
            expected_content_hash=created.content_hash if body_update else None,
            idempotency_key=uuid.uuid4(),
        )
        results = await asyncio.wait_for(asyncio.gather(*(
            concurrent.update("retry-documents", created.path, req, idempotency_actor=actor) for _ in range(4)
        )), timeout=20)
        assert all(value == results[0] for value in results)
        assert await counts(pool, vault_id) == (1, 2, 2, 2)
        later = await service.update("retry-documents", created.path, DocumentUpdateRequest(title="Later"))
        assert await service.update("retry-documents", created.path, req, idempotency_actor=actor) == results[0]
        assert (await service.get("retry-documents", created.path)).title == "Later"
        assert await counts(pool, vault_id) == (1, 3, 3, 3)
        # Rebinding the key to a new condition or metadata is a different request.
        with pytest.raises(ConflictError):
            await service.update("retry-documents", created.path, req.model_copy(update={"expected_commit": later.current_commit}), idempotency_actor=actor)
        with pytest.raises(ConflictError):
            await service.update("retry-documents", created.path, req.model_copy(update={"summary": "changed"}), idempotency_actor=actor)
        with pytest.raises(ConflictError):
            await service.update("retry-documents", created.path, DocumentUpdateRequest(content="stale", expected_commit=created.current_commit))


async def test_committed_response_loss_can_be_recovered_after_restart():
    async with _fresh_database() as pool:
        vault_id = await _make_vault(pool, "retry-documents")
        async def lose_response(name):
            if name == "authority.after_commit_before_response":
                raise ConnectionResetError("response lost after commit")
        crashing = NativeDocumentService(pool=pool, failpoint=lose_response)
        actor = uuid.uuid4()
        req = put_request(idempotency_key=uuid.uuid4(), slug="fixed")
        with pytest.raises(ConnectionResetError):
            await crashing.put(req, idempotency_actor=actor)
        restarted = NativeDocumentService(pool=pool)
        created = await restarted.put(req, idempotency_actor=actor)
        update = DocumentUpdateRequest(content="new", expected_commit=created.current_commit, idempotency_key=uuid.uuid4())
        with pytest.raises(ConnectionResetError):
            await crashing.update(req.vault, created.path, update, idempotency_actor=actor)
        recovered = await restarted.update(req.vault, created.path, update, idempotency_actor=actor)
        assert recovered.previous_commit == created.current_commit
        assert recovered == await restarted.update(req.vault, created.path, update, idempotency_actor=actor)
        assert await counts(pool, vault_id) == (1, 2, 2, 2)


async def test_same_key_is_scoped_to_actor_and_vault_and_missing_vault_denies_replay():
    async with _fresh_database() as pool:
        vault_id = await _make_vault(pool, "retry-documents")
        other_id = await _make_vault(pool, "retry-other")
        service = NativeDocumentService(pool=pool)
        actor = uuid.uuid4()
        req = put_request(idempotency_key=uuid.uuid4())
        first = await service.put(req, idempotency_actor=actor)
        other_actor = await service.put(req, idempotency_actor=uuid.uuid4())
        other_vault = await service.put(req.model_copy(update={"vault": "retry-other"}), idempotency_actor=actor)
        assert len({first.commit_hash, other_actor.commit_hash, other_vault.commit_hash}) == 3
        assert await counts(pool, vault_id) == (2, 2, 2, 2)
        assert await counts(pool, other_id) == (1, 1, 1, 1)
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM vaults WHERE id=$1", vault_id)
        with pytest.raises(NotFoundError):
            await service.put(req, idempotency_actor=actor)


async def test_commit_between_early_lookup_and_precondition_check_rechecks_receipt(monkeypatch):
    async with _fresh_database() as pool:
        await _make_vault(pool, "retry-documents")
        service = NativeDocumentService(pool=pool)
        actor = uuid.uuid4()
        req = put_request(idempotency_key=uuid.uuid4(), slug="fixed")
        first = await service.put(req, idempotency_actor=actor)
        real_replay = service._replay_request
        calls = 0
        async def initially_missing(*args):
            nonlocal calls
            calls += 1
            return None if calls == 1 else await real_replay(*args)
        monkeypatch.setattr(service, "_replay_request", initially_missing)
        assert await service.put(req, idempotency_actor=actor) == first
        assert calls == 2


async def test_concurrent_different_requests_with_one_key_have_one_winner():
    async with _fresh_database() as pool:
        vault_id = await _make_vault(pool, "retry-documents")
        barrier = asyncio.Barrier(2)
        async def pause(name):
            if name == "payload.after_prepare_before_tx":
                await barrier.wait()
        service = NativeDocumentService(pool=pool, failpoint=pause)
        req = put_request(idempotency_key=uuid.uuid4())
        actor = uuid.uuid4()
        results = await asyncio.wait_for(asyncio.gather(
            service.put(req, idempotency_actor=actor),
            service.put(req.model_copy(update={"content": "different"}), idempotency_actor=actor),
            return_exceptions=True,
        ), timeout=20)
        assert sum(isinstance(value, ConflictError) for value in results) == 1
        assert sum(not isinstance(value, Exception) for value in results) == 1
        assert await counts(pool, vault_id) == (1, 1, 1, 1)


@pytest.mark.parametrize("options", [{}, {"title_conflict_policy": "reject"}], ids=["legacy", "reject"])
async def test_mcp_and_rest_share_receipts_but_reauthorize_scope_and_revoked_membership(monkeypatch, tmp_path, options):
    from app.config import settings
    from app.models.vault_scope import VaultScope, current_vault_scope
    from app.services.access_service import reset_authorized_vault
    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path))
    from mcp_server import server
    from app.api.routes import documents

    async with _fresh_database() as pool:
        vault_id = await _make_vault(pool, "retry-documents")
        async with pool.acquire() as conn:
            actor = await conn.fetchval("INSERT INTO users(username,email,password_hash) VALUES('retry-writer','retry@example.invalid','disabled') RETURNING id")
            await conn.execute("INSERT INTO vault_access(vault_id,user_id,role) VALUES($1,$2,'writer')", vault_id, actor)
        service = NativeDocumentService(pool=pool)
        monkeypatch.setattr(server, "doc_service", service)
        monkeypatch.setattr(documents, "doc_service", service)
        user = SimpleNamespace(username="retry-writer", user_id=str(actor))
        req = put_request(idempotency_key=uuid.uuid4(), slug="fixed", **options)
        args = req.model_dump(mode="json", exclude_unset=True)
        first = await server._handle_put(args, str(actor), user)
        assert (await documents.put_document(req, user)).model_dump() == first
        other_policy = "allow" if options else "reject"
        with pytest.raises(ConflictError):
            await server._handle_put({**args, "title_conflict_policy": other_policy}, str(actor), user)
        update = DocumentUpdateRequest(title="Changed", idempotency_key=uuid.uuid4(), **options)
        updated = await documents.update_document(req.vault, first["path"], update, user)
        patch = {"uri": first["uri"], **update.model_dump(mode="json", exclude_unset=True)}
        assert await server._handle_update(patch, str(actor), user) == updated.model_dump()
        with pytest.raises(ConflictError):
            await server._handle_update({**patch, "title_conflict_policy": other_policy}, str(actor), user)
        assert await server._handle_put(args, str(actor), user) == first
        before = await counts(pool, vault_id)
        token = current_vault_scope.set(VaultScope(prefixes=(), extra_vaults=frozenset()))
        try:
            with pytest.raises(ForbiddenError):
                await server._handle_put(args, str(actor), user)
            with pytest.raises(ForbiddenError):
                await documents.update_document(req.vault, first["path"], update, user)
        finally:
            current_vault_scope.reset(token)
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM vault_access WHERE vault_id=$1 AND user_id=$2", vault_id, actor)
        with pytest.raises(ForbiddenError):
            await documents.put_document(req, user)
        with pytest.raises(ForbiddenError):
            await server._handle_update(patch, str(actor), user)
        assert await counts(pool, vault_id) == before
        reset_authorized_vault()


async def test_precommit_failure_rolls_back_identity_and_keyless_calls_remain_independent():
    async with _fresh_database() as pool:
        vault_id = await _make_vault(pool, "retry-documents")
        async def fail_before_commit(name):
            if name == "authority.before_commit":
                raise RuntimeError("transaction failed")
        failing = NativeDocumentService(pool=pool, failpoint=fail_before_commit)
        req = put_request(idempotency_key=uuid.uuid4())
        actor = uuid.uuid4()
        with pytest.raises(RuntimeError, match="transaction failed"):
            await failing.put(req, idempotency_actor=actor)
        assert await counts(pool, vault_id) == (0, 0, 0, 0)
        service = NativeDocumentService(pool=pool)
        recovered = await service.put(req, idempotency_actor=actor)
        assert recovered == await service.put(req, idempotency_actor=actor)
        unkeyed = req.model_copy(update={"idempotency_key": None})
        one = await service.put(unkeyed)
        two = await service.put(unkeyed)
        assert len({recovered.current_commit, one.current_commit, two.current_commit}) == 3
        assert await counts(pool, vault_id) == (3, 3, 3, 3)


async def test_vault_identity_change_between_lookup_and_write_is_rejected(monkeypatch):
    async with _fresh_database() as pool:
        original = await _make_vault(pool, "retry-documents")
        replacement = await _make_vault(pool, "retry-other")
        service = NativeDocumentService(pool=pool)
        # Simulate name resolution switching to a newly-created Vault after
        # the initial receipt lookup, without a timing-dependent deletion.
        monkeypatch.setattr(service, "_vault_id", AsyncMock(side_effect=[original, replacement]))
        with pytest.raises(ConflictError, match="Vault identity changed"):
            await service.put(put_request(idempotency_key=uuid.uuid4()), idempotency_actor=uuid.uuid4())
        assert await counts(pool, original) == (0, 0, 0, 0)
        assert await counts(pool, replacement) == (0, 0, 0, 0)


@pytest.mark.parametrize("surface", ["rest", "mcp"])
@pytest.mark.parametrize("operation,keyed", [("put", True), ("update", True), ("put", False), ("update", False)])
async def test_recreation_after_writer_check_cannot_target_replacement(monkeypatch, tmp_path, surface, operation, keyed):
    from app.config import settings
    from app.services.access_service import check_vault_access, reset_authorized_vault
    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path))
    from app.api.routes import documents
    from mcp_server import server

    async with _fresh_database() as pool:
        name = "recreated-vault"
        original = await _make_vault(pool, name)
        async with pool.acquire() as conn:
            actor = await conn.fetchval(
                "INSERT INTO users(username,email,password_hash) VALUES('writer','writer@example.invalid','disabled') RETURNING id"
            )
            await conn.execute("INSERT INTO vault_access(vault_id,user_id,role) VALUES($1,$2,'writer')", original, actor)
        service = NativeDocumentService(pool=pool)
        handler = documents if surface == "rest" else server
        monkeypatch.setattr(handler, "doc_service", service)
        # Create the future private Vault separately so it already has a
        # matching document path. Only the old Vault grants this user access.
        replacement = await _make_vault(pool, "future-vault")
        existing = await service.put(put_request(vault="future-vault"))
        before = await counts(pool, replacement)
        receipt_lookup = AsyncMock(wraps=service._replay_request)
        monkeypatch.setattr(service, "_replay_request", receipt_lookup)

        async def authorize_then_replace(*args, **kwargs):
            approved = await check_vault_access(*args, **kwargs)
            assert approved["vault_id"] == original
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("DELETE FROM vaults WHERE id=$1", original)
                    await conn.execute("UPDATE vaults SET name=$1 WHERE id=$2", name, replacement)
            with pytest.raises(ForbiddenError):
                await check_vault_access(*args, **kwargs)
            return approved

        monkeypatch.setattr(handler, "check_vault_access", authorize_then_replace)
        user = SimpleNamespace(username="writer", user_id=str(actor))
        key = uuid.uuid4() if keyed else None
        try:
            with pytest.raises(ConflictError, match="Vault identity changed"):
                if operation == "put":
                    create = put_request(vault=name, idempotency_key=key)
                    if surface == "rest":
                        await documents.put_document(create, user)
                    else:
                        await server._handle_put(create.model_dump(mode="json"), str(actor), user)
                else:
                    update = DocumentUpdateRequest(title="Unauthorized edit", idempotency_key=key)
                    if surface == "rest":
                        await documents.update_document(name, existing.path, update, user)
                    else:
                        await server._handle_update(
                            {"uri": f"akb://{name}/doc/{existing.path}", **update.model_dump(mode="json")},
                            str(actor), user,
                        )
        finally:
            reset_authorized_vault()
        receipt_lookup.assert_not_awaited()
        assert await counts(pool, replacement) == before
        assert (await service.get(name, existing.path)).current_commit == existing.current_commit
