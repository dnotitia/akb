"""PostgreSQL checks of the per-process Vault write admission.

A transaction that writes a Vault's events waits for the Vault's event lock
(migration 132), and used to wait holding its pool connection. A burst of
writes to one Vault then took every connection, and a request for another
Vault waited for one until the writers' 5-second lock deadline freed them.
Every event-writing path now waits for admission before it takes a
connection; at most `vault_write_concurrency` transactions of a Vault hold one.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import time
import uuid

import asyncpg
import pytest

from app.config import settings
from app.db import postgres as postgres_module
from app.exceptions import ConflictError, WriteBusyError
from app.models.document import DocumentPutRequest
from app.repositories import events_repo
from app.repositories.events_repo import emit_event
from app.services import access_service, file_service
from app.services import app_rollout_worker as rollout_worker
from app.services import document_service as document_service_module
from app.services.collection_service import CollectionService
from app.services.document_service import DocumentService
from app.services.git_service import GitService
from app.services.vault_write_admission import is_admitted, vault_write_admission
from tests.test_app_rollout_event_lock_postgres import _installation
from tests.test_events_commit_order_postgres import (
    _OpenTransaction,
    _database_dsn,
    _fresh_database,
    _make_vault,
)
from tests.test_vault_write_admission_paths_postgres import _AnyRoleSync, _Context, _vault_full

# The application pool of the storm test, and the writers sent at one Vault.
_POOL_SIZE = 8
_WRITERS = 24
# How long another transaction holds the busy Vault's event lock.
_HOLD_SECONDS = 3.0
# The longest another Vault's request may wait for a connection meanwhile.
_OTHER_VAULT_WAIT_BOUND = 1.0
# How long the File storm measures, once its confirmations wait.
_FILE_HOLD_SECONDS = 2.5


async def test_a_write_storm_on_one_vault_leaves_connections_for_another_vault(monkeypatch):
    """While a transaction holds one Vault's event lock, 24 collection creates
    go to that Vault through a pool of 8 connections, and a reader of another
    Vault takes a connection every 0.1 s. Before admission the creates took
    all 8 connections to wait for the lock, and the reader waited until they
    gave up at the lock's deadline. Now two of them wait with a connection and
    the reader is served at once; once the lock is free, every create commits.
    """
    monkeypatch.setattr(settings, "vault_write_concurrency", 2, raising=False)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 30.0, raising=False)
    async with _fresh_database() as admin_pool:
        storm = await _make_vault(admin_pool, "storm")
        quiet = await _make_vault(admin_pool, "quiet")
        database = await admin_pool.fetchval("SELECT current_database()")
        app_pool = await asyncpg.create_pool(_database_dsn(database), min_size=1, max_size=_POOL_SIZE)
        previous_pool = postgres_module._pool
        postgres_module._pool = app_pool
        collections = CollectionService(git=object())
        writers: list[asyncio.Task] = []
        waits: list[float] = []
        try:
            async with admin_pool.acquire() as holder:
                async with _OpenTransaction(holder):
                    await emit_event(holder, "document.put", vault_id=storm, payload={"marker": "holder"})
                    writers = [
                        asyncio.create_task(collections.create(
                            vault="storm", path=f"burst-{i}", summary=None, agent_id="storm",
                        ))
                        for i in range(_WRITERS)
                    ]
                    await asyncio.sleep(0.5)
                    until = time.monotonic() + _HOLD_SECONDS
                    while time.monotonic() < until:
                        started = time.monotonic()
                        async with app_pool.acquire() as reader:
                            await reader.fetchval(
                                "SELECT count(*) FROM collections WHERE vault_id = $1", quiet,
                            )
                        waits.append(time.monotonic() - started)
                        await asyncio.sleep(0.1)
                # The holder rolled back; the storm can commit.
            results = await asyncio.wait_for(
                asyncio.gather(*writers, return_exceptions=True), timeout=60,
            )
        finally:
            for task in writers:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*writers, return_exceptions=True)
            postgres_module._pool = previous_pool
            await app_pool.close()

        failures = [type(r).__name__ for r in results if isinstance(r, BaseException)]
        print(
            f"\nother-Vault pool wait: max {max(waits):.3f}s over {len(waits)} reads; "
            f"storm writes failed: {len(failures)} of {_WRITERS} {sorted(set(failures))}"
        )
        assert max(waits) < _OTHER_VAULT_WAIT_BOUND, waits
        assert failures == []
        assert await admin_pool.fetchval(
            "SELECT count(*) FROM collections WHERE vault_id = $1", storm,
        ) == _WRITERS


async def test_duplicate_confirmations_of_one_file_leave_connections_for_another_vault(
    tmp_path, monkeypatch,
):
    """While a transaction holds one Vault's event lock, a confirmation of a
    pending File publishes it and waits for that lock, holding the File's
    row. Then 23 more confirmations of the File go to the Vault through a pool
    of 8 connections, and a reader of another Vault takes a connection every
    0.1 s. Each of them first refreshes the File's lease, which waits for the
    File's row. The refresh used to take its connection before the Vault's
    admission, so the refreshes took every connection and the reader waited
    until the publication committed. Now it is admitted too, and the reader is
    served at once; once the lock is free, every confirmation returns the
    File, which is published once.
    """
    monkeypatch.setattr(settings, "vault_write_concurrency", 2, raising=False)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 30.0, raising=False)
    async with _fresh_database() as admin_pool:
        storm = await _make_vault(admin_pool, "storm-file")
        quiet = await _make_vault(admin_pool, "quiet")
        ctx = _Context(pool=admin_pool, tmp_path=tmp_path, monkeypatch=monkeypatch)
        blobs = ctx.fake_objects()
        fid = await ctx.file(storm, blobs, state="pending")
        files = file_service.FileService()
        database = await admin_pool.fetchval("SELECT current_database()")
        app_pool = await asyncpg.create_pool(_database_dsn(database), min_size=1, max_size=_POOL_SIZE)
        previous_pool = postgres_module._pool
        postgres_module._pool = app_pool
        confirms: list[asyncio.Task] = []
        waits: list[float] = []
        try:
            async with admin_pool.acquire() as holder:
                async with _OpenTransaction(holder):
                    await emit_event(holder, "document.put", vault_id=storm, payload={"marker": "holder"})

                    def confirm() -> asyncio.Task:
                        return asyncio.create_task(files.confirm_upload(storm, str(fid), actor_id="storm"))

                    confirms = [confirm()]
                    # The first one locks the File's row and waits for the
                    # event lock, which the holder keeps for less than the
                    # lock's 5-second deadline in all.
                    await asyncio.sleep(0.5)
                    confirms += [confirm() for _ in range(_WRITERS - 1)]
                    await asyncio.sleep(0.5)
                    until = time.monotonic() + _FILE_HOLD_SECONDS
                    while time.monotonic() < until:
                        started = time.monotonic()
                        async with app_pool.acquire() as reader:
                            await reader.fetchval(
                                "SELECT count(*) FROM vault_files WHERE vault_id = $1", quiet,
                            )
                        waits.append(time.monotonic() - started)
                        await asyncio.sleep(0.1)
            results = await asyncio.wait_for(
                asyncio.gather(*confirms, return_exceptions=True), timeout=60,
            )
        finally:
            for task in confirms:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*confirms, return_exceptions=True)
            postgres_module._pool = previous_pool
            await app_pool.close()

        failures = [type(r).__name__ for r in results if isinstance(r, BaseException)]
        print(
            f"\nother-Vault pool wait: max {max(waits):.3f}s over {len(waits)} reads; "
            f"confirmations failed: {len(failures)} of {_WRITERS} {sorted(set(failures))}"
        )
        assert max(waits) < _OTHER_VAULT_WAIT_BOUND, waits
        assert failures == []
        assert await admin_pool.fetchval(
            "SELECT count(*) FROM events WHERE vault_id = $1 AND kind = 'file.put'", storm,
        ) == 1


async def test_a_refused_replacement_is_confirmed_again_without_a_new_upload(tmp_path, monkeypatch):
    """The Vault's admission refuses a File replacement's confirmation after
    the server copied the uploaded bytes. The refusal keeps the upload, so the
    client confirms the same replacement again once the Vault has room."""
    monkeypatch.setattr(settings, "vault_write_concurrency", 1, raising=False)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 0.3, raising=False)
    async with _fresh_database() as pool:
        ctx = _Context(pool=pool, tmp_path=tmp_path, monkeypatch=monkeypatch)
        vault = "replace-again"
        vault_id = await _make_vault(pool, vault)
        blobs = ctx.fake_objects()
        fid = await ctx.file(vault_id, blobs, state="confirmed")
        files = file_service.FileService()
        replacement = b"replacement bytes"
        digest = hashlib.sha256(replacement).hexdigest()
        prepared = await files.initiate_replace(vault, vault_id, str(fid), actor_id="tester", content_hash=digest)
        rid = uuid.UUID(prepared["replacement_id"])
        blobs[file_service._replacement_staging_key(vault, fid, rid)] = replacement

        def confirm():
            return files.confirm_replace(
                vault, vault_id, str(fid), str(rid), actor_id="tester", content_hash=digest,
            )

        async with _vault_full(vault_id):
            with pytest.raises(WriteBusyError):
                await confirm()
        result = await confirm()

        assert result["content_hash"] == digest
        assert await pool.fetchval("SELECT content_hash FROM vault_files WHERE id = $1", fid) == digest
        assert await pool.fetchval(
            "SELECT count(*) FROM events WHERE vault_id = $1 AND kind = 'file.update'", vault_id,
        ) == 1


@pytest.mark.parametrize("competitor", ["admission", "document"])
async def test_a_new_vault_is_seeded_before_other_writers_reach_it(competitor, tmp_path, monkeypatch):
    """Another request reaches a new Bare Git Vault as soon as its row commits,
    before the creation writes its seed document. One takes the Vault's only
    admission slot and keeps it past the admission deadline; the other writes
    a document. The creation took the Vault's write lane and admission before
    the row became visible and holds both through the seed, so the seed does
    not wait for either: the creation completes, and the document writer goes
    after it. A creation that took only the admission would hold it while it
    waited for the lane the document writer took, until one of them gave up.
    """
    monkeypatch.setattr(settings, "vault_write_concurrency", 1, raising=False)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 0.5, raising=False)
    async with _fresh_database() as pool:
        documents = DocumentService(git=GitService(storage_path=str(tmp_path)))
        name = f"seeded-{competitor}"
        racers: list[asyncio.Task] = []

        async def keep_admission(vault_id) -> None:
            async with vault_write_admission(vault_id):
                await asyncio.sleep(2.0)

        class _RacingRoleSync(_AnyRoleSync):
            async def on_vault_create(self, vault_id, _owner) -> None:
                # The Vault's row has committed: another request can reach it.
                # It runs in a context of its own, as a request does, not in
                # the creation's, whose admission and lane it would inherit.
                if competitor == "admission":
                    racer = keep_admission(vault_id)
                else:
                    racer = documents.put(DocumentPutRequest(
                        vault=name, collection="notes", title="Racer", content="racer body",
                    ))
                racers.append(asyncio.create_task(racer, context=contextvars.Context()))
                await asyncio.sleep(0.1)

        monkeypatch.setattr(document_service_module, "get_role_sync", lambda: _RacingRoleSync())
        try:
            vault_id = uuid.UUID(await asyncio.wait_for(documents.create_vault(name), timeout=30))
            outcomes = await asyncio.wait_for(asyncio.gather(*racers, return_exceptions=True), timeout=30)
        finally:
            for task in racers:
                if not task.done():
                    task.cancel()

        paths = {r["path"] for r in await pool.fetch("SELECT path FROM documents WHERE vault_id = $1", vault_id)}
        assert any(path.startswith("overview/") for path in paths), paths
        if competitor == "document":
            assert not [o for o in outcomes if isinstance(o, BaseException)], outcomes
            assert any(path.startswith("notes/") for path in paths), paths


async def _admitted_event_writes(monkeypatch) -> list[tuple[uuid.UUID, bool]]:
    """Record, for each event written for a Vault, whether its writer was
    admitted for that Vault."""
    seen: list[tuple[uuid.UUID, bool]] = []
    noted = events_repo.note_event_write

    def note(vault_id, kind):
        if vault_id is not None:
            seen.append((uuid.UUID(str(vault_id)), is_admitted(vault_id)))
        noted(vault_id, kind)

    monkeypatch.setattr(events_repo, "note_event_write", note)
    return seen


def _vault_replaced_while_waiting(pool, monkeypatch, owner, attribute: str, times: int) -> None:
    """Delete the Vault and create another with its name right after the
    write read the name's id for its admission, ``times`` times."""
    resolve = getattr(owner, attribute)
    left = {"times": times}

    async def resolve_then_replace(*args, **kwargs):
        resolved = await resolve(*args, **kwargs)
        if left["times"]:
            left["times"] -= 1
            name = await pool.fetchval("SELECT name FROM vaults WHERE id = ANY($1::uuid[])", list(
                resolved if isinstance(resolved, list) else [resolved]
            ))
            await pool.execute("DELETE FROM vaults WHERE name = $1", name)
            await _make_vault(pool, name)
        return resolved

    monkeypatch.setattr(owner, attribute, resolve_then_replace)


async def test_a_vault_write_is_admitted_for_the_vault_it_finds_by_name(monkeypatch):
    """A Vault's write resolves the name to an id for its admission before its
    transaction. When the Vault is deleted and another created with the name
    meanwhile, the transaction finds the other Vault: it releases its
    connection and admission and starts again for the Vault it found, and a
    second replacement is a conflict."""
    async with _fresh_database() as pool:
        seen = await _admitted_event_writes(monkeypatch)
        await _make_vault(pool, "renamed")
        _vault_replaced_while_waiting(pool, monkeypatch, access_service, "_vault_id_for_admission", 1)
        await access_service.set_vault_write_policy("tester", "renamed", "collector:test")
        current = await pool.fetchval("SELECT id FROM vaults WHERE name = 'renamed'")
        assert await pool.fetchval("SELECT count(*) FROM vault_write_policy WHERE vault_id = $1", current) == 1
        assert seen and all(vault == current and admitted for vault, admitted in seen), seen

        await _make_vault(pool, "renamed-twice")
        _vault_replaced_while_waiting(pool, monkeypatch, access_service, "_vault_id_for_admission", 2)
        with pytest.raises(ConflictError):
            await access_service.set_vault_write_policy("tester", "renamed-twice", "collector:test")


async def test_a_rollout_target_refused_by_its_vaults_admission_is_released(monkeypatch):
    """The worker leases a rollout target and then waits for the target's
    Vault. When the admission refuses it, the worker gives the lease back, so
    the next run takes the target again at once, rather than after the lease
    expires with the rest of its batch held behind it."""
    monkeypatch.setattr(settings, "vault_write_concurrency", 1, raising=False)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 0.3, raising=False)
    async with _fresh_database() as pool:
        vault_id, job_id, _items = await _installation(pool, own_items=True)
        async with _vault_full(vault_id):
            assert await rollout_worker.run_once() == 0
        target = await pool.fetchrow(
            "SELECT state, attempts, lease_owner, lease_expires_at FROM app_rollout_targets WHERE job_id = $1",
            job_id,
        )
        assert dict(target) == {"state": "pending", "attempts": 0, "lease_owner": None, "lease_expires_at": None}
        assert await rollout_worker.run_once() == 1
        assert await pool.fetchval("SELECT status FROM app_rollout_jobs WHERE id = $1", job_id) == "applied"
