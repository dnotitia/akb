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
from app.util.errors import INVALID_ARGUMENT, METHOD_NOT_ALLOWED, MULTI_STATEMENT, PERMISSION_DENIED
from app.models.document import DocumentPutRequest
from app.repositories import events_repo, vault_write_policy_repo
from app.repositories.events_repo import emit_event
from app.services import (
    access_service,
    file_service,
    table_service,
    user_sql_executor,
)
from app.services import app_rollout_worker as rollout_worker
from app.services import document_service as document_service_module
from app.services.collection_service import CollectionService
from app.services.document_service import DocumentService
from app.services.git_service import GitService
from app.services.native_document_service import NativeDocumentService
from app.services.native_revision_service import NativeRevisionService
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


async def test_sql_is_admitted_for_the_vault_it_finds_by_name(monkeypatch):
    """The same for raw SQL, whose declared Vault names are resolved to ids
    for its admission before its transaction checks their write policy."""
    async with _fresh_database() as pool:
        await _make_vault(pool, "sql-renamed")
        admin = await pool.fetchval(
            "INSERT INTO users (id, username, email, password_hash, is_admin) "
            "VALUES ($1, 'sql-renamed-admin', 'sql-renamed-admin@example.com', '!fixture!', TRUE) RETURNING id",
            uuid.uuid4(),
        )
        await pool.execute("CREATE TABLE sql_renamed_rows (value text)")
        executor = user_sql_executor.UserSqlExecutor(pool)
        admitted_runs: list[bool] = []
        run_statement = user_sql_executor._run_statement

        async def record(conn, *args, **kwargs):
            if await conn.fetchval("SHOW transaction_read_only") == "off":
                current = await conn.fetchval("SELECT id FROM vaults WHERE name = 'sql-renamed'")
                admitted_runs.append(is_admitted(current))
            return await run_statement(conn, *args, **kwargs)

        monkeypatch.setattr(user_sql_executor, "_run_statement", record)
        _vault_replaced_while_waiting(pool, monkeypatch, executor, "_vault_ids", 1)

        def insert():
            return executor.execute(
                user_id=admin, actor_id="tester", sql="INSERT INTO sql_renamed_rows VALUES ('row')",
                is_admin=True, vault_names=["sql-renamed"], enforce_vault_write_policy=True,
            )

        await insert()
        assert admitted_runs == [True]
        _vault_replaced_while_waiting(pool, monkeypatch, executor, "_vault_ids", 2)
        with pytest.raises(ConflictError):
            await insert()
        assert await pool.fetchval("SELECT count(*) FROM sql_renamed_rows") == 1


async def test_a_rollout_target_refused_by_its_vaults_admission_is_released(monkeypatch):
    """The worker leases a rollout target and then waits for the target's
    Vault. When the admission refuses it, the worker gives the lease back and
    defers the target briefly, rather than hold it, with the rest of its batch
    behind it, until the lease expires. The run reports work done, so the
    runner claims again at once; nothing else is eligible. Once the deferral
    ends, the next run takes the target and applies it."""
    monkeypatch.setattr(settings, "vault_write_concurrency", 1, raising=False)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 0.3, raising=False)
    async with _fresh_database() as pool:
        vault_id, job_id, _items = await _installation(pool, own_items=True)
        async with _vault_full(vault_id):
            assert await rollout_worker.run_once() == 1
        target = await pool.fetchrow(
            "SELECT state, attempts, lease_owner, lease_expires_at > NOW() AS deferred "
            "FROM app_rollout_targets WHERE job_id = $1",
            job_id,
        )
        assert dict(target) == {"state": "pending", "attempts": 0, "lease_owner": None, "deferred": True}
        assert await rollout_worker.run_once() == 0
        # The deferral ends.
        await pool.execute("UPDATE app_rollout_targets SET lease_expires_at = NOW() WHERE job_id = $1", job_id)
        assert await rollout_worker.run_once() == 1
        assert await pool.fetchval("SELECT status FROM app_rollout_jobs WHERE id = $1", job_id) == "applied"


async def test_sql_runs_read_only_without_admission_only_when_asked(tmp_path, monkeypatch):
    """A Vault's admission is full. A SELECT sent with `read_only` runs at once,
    in a read-only transaction without admission, and PostgreSQL refuses a
    data-modifying WITH sent that way. Without `read_only` a statement is a
    write, whatever it is, and waits for the Vault's admission; no statement is
    tried first to find out what it does."""
    monkeypatch.setattr(settings, "vault_write_concurrency", 1, raising=False)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 0.3, raising=False)
    async with _fresh_database() as pool:
        ctx = _Context(pool=pool, tmp_path=tmp_path, monkeypatch=monkeypatch)
        vault = "sql-reads"
        vault_id = await _make_vault(pool, vault)
        monkeypatch.setattr(table_service, "get_role_sync", lambda: _AnyRoleSync())
        pg_name = await ctx.table(vault, vault_id)
        admin = await ctx.user(f"{vault}-admin", admin=True)
        monkeypatch.setattr(user_sql_executor, "_executor", user_sql_executor.UserSqlExecutor(pool))

        def run(sql: str, **kwargs):
            return table_service.execute_sql(
                vault_names=[vault], user_id=str(admin), actor_id="tester", sql=sql, is_admin=True,
                **kwargs,
            )

        count = f"SELECT count(*) AS n FROM {pg_name}"
        modifying = (
            f"WITH added AS (INSERT INTO {pg_name} (value) VALUES ('row') RETURNING value) "
            "SELECT value FROM added"
        )
        async with _vault_full(vault_id):
            started = time.monotonic()
            read = await run(count, read_only=True)
            assert time.monotonic() - started < 0.3
            assert read["items"] == [{"n": 0}]
            refused = await run(modifying, read_only=True)
            assert (refused["code"], refused["details"]["pg_sqlstate"]) == (PERMISSION_DENIED, "25006")
            with pytest.raises(WriteBusyError):
                await run(count)
        assert await pool.fetchval(f"SELECT count(*) FROM {pg_name}") == 0


async def test_sql_is_admitted_for_the_tables_its_plan_writes_before_it_runs(tmp_path, monkeypatch):
    """A statement declared for one Vault whose plan writes another Vault's
    table is admitted for both before it runs, so it runs once, admitted for
    the Vault it writes; the check of its queued row changes before commit
    then finds nothing to add."""
    async with _fresh_database() as pool:
        ctx = _Context(pool=pool, tmp_path=tmp_path, monkeypatch=monkeypatch)
        await _make_vault(pool, "plan-declared")
        target = await _make_vault(pool, "plan-target")
        pg_name = await ctx.table("plan-target", target)
        admin = await ctx.user("plan-target-admin", admin=True)
        monkeypatch.setattr(user_sql_executor, "_executor", user_sql_executor.UserSqlExecutor(pool))
        writes: list[bool] = []
        run_statement = user_sql_executor._run_statement

        async def record(conn, *args, **kwargs):
            if await conn.fetchval("SHOW transaction_read_only") == "off":
                writes.append(is_admitted(target))
            return await run_statement(conn, *args, **kwargs)

        monkeypatch.setattr(user_sql_executor, "_run_statement", record)
        result = await table_service.execute_sql(
            vault_names=["plan-declared"], user_id=str(admin), actor_id="tester",
            sql=f"INSERT INTO {pg_name} (value) VALUES ('row')", is_admin=True,
        )
        assert result["affected_rows"] == 1
        assert writes == [True]



async def test_sql_writes_waiting_for_a_table_lock_leave_connections_for_another_vault(tmp_path, monkeypatch):
    """A transaction holds a dynamic table's ACCESS EXCLUSIVE lock, as a column
    being added does while that transaction waits for its Vault's event lock.
    24 raw SQL writes to the table go to its Vault through a pool of 8
    connections, and a reader of another Vault takes a connection every 0.1 s.
    Each write first reads its plan, which waits for the table's lock: at most
    two plans are read at once, each waits at most half a second and then
    fails with write_busy, and the writes that get a plan wait for the table
    admitted, at most two at a time. The reader is served at once. Each write
    used to try its statement first on a connection of its own, without
    admission, and those tries took every connection while they waited."""
    monkeypatch.setattr(settings, "vault_write_concurrency", 2, raising=False)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 30.0, raising=False)
    monkeypatch.setattr(settings, "akb_sql_plan_concurrency", 2, raising=False)
    monkeypatch.setattr(settings, "akb_sql_plan_lock_timeout_ms", 500, raising=False)
    async with _fresh_database() as admin_pool:
        ctx = _Context(pool=admin_pool, tmp_path=tmp_path, monkeypatch=monkeypatch)
        vault = "storm-sql"
        vault_id = await _make_vault(admin_pool, vault)
        quiet = await _make_vault(admin_pool, "quiet")
        monkeypatch.setattr(table_service, "get_role_sync", lambda: _AnyRoleSync())
        pg_name = await ctx.table(vault, vault_id)
        admin = await ctx.user(f"{vault}-admin", admin=True)
        database = await admin_pool.fetchval("SELECT current_database()")
        app_pool = await asyncpg.create_pool(_database_dsn(database), min_size=1, max_size=_POOL_SIZE)
        previous_pool = postgres_module._pool
        postgres_module._pool = app_pool
        monkeypatch.setattr(user_sql_executor, "_executor", user_sql_executor.UserSqlExecutor(app_pool))
        writes: list[asyncio.Task] = []
        waits: list[float] = []
        try:
            async with admin_pool.acquire() as holder:
                async with _OpenTransaction(holder):
                    await holder.execute(f"LOCK TABLE {pg_name} IN ACCESS EXCLUSIVE MODE")
                    writes = [
                        asyncio.create_task(table_service.execute_sql(
                            vault_names=[vault], user_id=str(admin), actor_id="storm",
                            sql=f"INSERT INTO {pg_name} (value) VALUES ('row')", is_admin=True,
                        ))
                        for _ in range(_WRITERS)
                    ]
                    await asyncio.sleep(0.5)
                    until = time.monotonic() + _FILE_HOLD_SECONDS
                    while time.monotonic() < until:
                        started = time.monotonic()
                        try:
                            # The lock is held until this loop ends, so a
                            # pool the writes took would never free up.
                            async with app_pool.acquire(timeout=_OTHER_VAULT_WAIT_BOUND * 3) as reader:
                                await reader.fetchval(
                                    "SELECT count(*) FROM collections WHERE vault_id = $1", quiet,
                                )
                        except TimeoutError:
                            waits.append(time.monotonic() - started)
                            break
                        waits.append(time.monotonic() - started)
                        await asyncio.sleep(0.1)
            results = await asyncio.wait_for(asyncio.gather(*writes, return_exceptions=True), timeout=60)
        finally:
            for task in writes:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*writes, return_exceptions=True)
            postgres_module._pool = previous_pool
            await app_pool.close()

        busy = sum(isinstance(r, WriteBusyError) for r in results)
        committed = sum(isinstance(r, dict) and r.get("affected_rows") == 1 for r in results)
        others = [r for r in results if not isinstance(r, WriteBusyError) and not (
            isinstance(r, dict) and r.get("affected_rows") == 1)]
        print(
            f"\nother-Vault pool wait: max {max(waits):.3f}s over {len(waits)} reads; "
            f"writes committed {committed}, write_busy {busy}, other {others}"
        )
        assert max(waits) < _OTHER_VAULT_WAIT_BOUND, waits
        assert others == []
        assert busy > 0
        assert await admin_pool.fetchval(f"SELECT count(*) FROM {pg_name}") == committed


async def test_sql_plans_waiting_for_one_vaults_lock_do_not_hold_back_another_vault(tmp_path, monkeypatch):
    """A transaction holds Vault A's table lock. 30 raw SQL writes to that
    table read their plans two at a time, each waiting half a second for the
    lock. A write to Vault B sent behind them takes its turn after one more of
    A's plans, since statements declaring different Vaults take turns for the
    plan slots, and commits within a second and a half. Plans used to be read
    in arrival order, and B's write waited behind A's until its 3-second
    deadline."""
    monkeypatch.setattr(settings, "vault_write_concurrency", 2, raising=False)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 3.0, raising=False)
    monkeypatch.setattr(settings, "akb_sql_plan_concurrency", 2, raising=False)
    monkeypatch.setattr(settings, "akb_sql_plan_lock_timeout_ms", 500, raising=False)
    async with _fresh_database() as pool:
        ctx = _Context(pool=pool, tmp_path=tmp_path, monkeypatch=monkeypatch)
        monkeypatch.setattr(table_service, "get_role_sync", lambda: _AnyRoleSync())
        busy_id = await _make_vault(pool, "plans-busy")
        quiet_id = await _make_vault(pool, "plans-quiet")
        busy_table = await ctx.table("plans-busy", busy_id)
        quiet_table = await ctx.table("plans-quiet", quiet_id)
        admin = await ctx.user("plans-admin", admin=True)
        monkeypatch.setattr(user_sql_executor, "_executor", user_sql_executor.UserSqlExecutor(pool))

        def write(vault: str, table: str):
            return table_service.execute_sql(
                vault_names=[vault], user_id=str(admin), actor_id=vault,
                sql=f"INSERT INTO {table} (value) VALUES ('row')", is_admin=True,
            )

        busy_writes: list[asyncio.Task] = []
        try:
            async with pool.acquire() as holder:
                async with _OpenTransaction(holder):
                    await holder.execute(f"LOCK TABLE {busy_table} IN ACCESS EXCLUSIVE MODE")
                    busy_writes = [asyncio.create_task(write("plans-busy", busy_table)) for _ in range(30)]
                    await asyncio.sleep(0.2)
                    started = time.monotonic()
                    quiet = await write("plans-quiet", quiet_table)
                    took = time.monotonic() - started
            outcomes = await asyncio.wait_for(asyncio.gather(*busy_writes, return_exceptions=True), timeout=60)
        finally:
            for task in busy_writes:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*busy_writes, return_exceptions=True)

        print(f"\nother-Vault write behind 30 waiting plans: {took:.3f}s")
        assert quiet.get("affected_rows") == 1, quiet
        assert took < 1.5, took
        assert all(
            isinstance(o, WriteBusyError) or (isinstance(o, dict) and o.get("affected_rows") == 1)
            for o in outcomes
        ), outcomes


async def test_a_statement_writing_an_undeclared_vault_is_rolled_back_not_run_again(tmp_path, monkeypatch):
    """No statement akb_sql accepts can create a function, but a function an
    administrator installed can write a dynamic table its caller's plan does
    not show. The write's queued row change is checked before commit: the
    statement is rolled back with an error naming the Vault it wrote, and is
    not run again. It used to run once more, admitted for that Vault, and so
    did whatever else the function does that a rollback does not undo, here
    advancing a sequence."""
    async with _fresh_database() as pool:
        ctx = _Context(pool=pool, tmp_path=tmp_path, monkeypatch=monkeypatch)
        await _make_vault(pool, "fn-declared")
        target = await _make_vault(pool, "fn-target")
        monkeypatch.setattr(table_service, "get_role_sync", lambda: _AnyRoleSync())
        pg_name = await ctx.table("fn-target", target)
        admin = await ctx.user("fn-admin", admin=True)
        monkeypatch.setattr(user_sql_executor, "_executor", user_sql_executor.UserSqlExecutor(pool))
        await pool.execute("CREATE SEQUENCE fn_calls")
        await pool.execute(
            "CREATE FUNCTION fn_writes_elsewhere() RETURNS void LANGUAGE sql AS "
            f"$$ SELECT nextval('fn_calls'); INSERT INTO {pg_name} (value) VALUES ('row') $$"
        )

        result = await table_service.execute_sql(
            vault_names=["fn-declared"], user_id=str(admin), actor_id="tester",
            sql="SELECT fn_writes_elsewhere()", is_admin=True,
        )
        assert (result["code"], result["details"]["undeclared_vaults"]) == (INVALID_ARGUMENT, ["fn-target"])
        assert await pool.fetchval(f"SELECT count(*) FROM {pg_name}") == 0
        assert await pool.fetchval("SELECT last_value FROM fn_calls") == 1
        assert await pool.fetchval("SELECT count(*) FROM events WHERE vault_id = $1", target) == 0


async def test_akb_sql_cannot_add_writes_its_plan_and_admission_do_not_see(tmp_path, monkeypatch):
    """A raw SQL write is admitted for the Vaults its plan writes, and the
    events of its dynamic-table rows are inserted at commit. akb_sql accepts one
    SELECT, WITH, INSERT, UPDATE or DELETE statement, so a caller cannot create
    a function that writes out of the plan's sight, run a DO block, or have the
    deferred event trigger fire early with SET CONSTRAINTS. No function a SQL
    role may call writes a table: apart from trigger functions, which cannot be
    called, there is a check of a payload's encoding and the Vault event lock,
    which needs a table no SQL role is granted."""
    async with _fresh_database() as pool:
        ctx = _Context(pool=pool, tmp_path=tmp_path, monkeypatch=monkeypatch)
        vault = "sql-guard"
        vault_id = await _make_vault(pool, vault)
        admin = await ctx.user(f"{vault}-admin", admin=True)
        monkeypatch.setattr(user_sql_executor, "_executor", user_sql_executor.UserSqlExecutor(pool))
        for sql, code in (
            ("CREATE FUNCTION pg_temp.f() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$", METHOD_NOT_ALLOWED),
            ("DO $$ BEGIN PERFORM 1; END $$", METHOD_NOT_ALLOWED),
            ("SET CONSTRAINTS ALL IMMEDIATE", METHOD_NOT_ALLOWED),
            ("SELECT 1; CREATE FUNCTION pg_temp.f() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$", MULTI_STATEMENT),
        ):
            result = await table_service.execute_sql(
                vault_names=[vault], user_id=str(admin), actor_id="tester", sql=sql, is_admin=True,
            )
            assert result.get("code") == code, (sql, result)

        callable_functions = {
            r["proname"]: (r["prosecdef"], r["result"])
            for r in await pool.fetch(
                """
                SELECT p.proname, p.prosecdef, pg_get_function_result(p.oid) AS result
                  FROM pg_proc p
                  JOIN pg_namespace n ON n.oid = p.pronamespace
                 WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
                   AND n.nspname NOT LIKE 'pg_toast%'
                   AND has_function_privilege('public', p.oid, 'EXECUTE')
                   AND pg_get_function_result(p.oid) <> 'trigger'
                   AND p.prolang IN (SELECT oid FROM pg_language WHERE lanname IN ('sql', 'plpgsql'))
                """
            )
        }
        assert callable_functions == {
            "akb_is_utf8_payload": (False, "boolean"),
            "akb_lock_vault_events": (False, "void"),
        }
        assert not await pool.fetchval(
            "SELECT count(*) FROM information_schema.routine_privileges "
            "WHERE grantee LIKE 'akb\\_%' AND privilege_type = 'EXECUTE'"
        )
        assert not await pool.fetchval("SELECT has_schema_privilege('public', 'public', 'CREATE')")
        role = f"akb_user_{uuid.uuid4().hex}"
        await pool.execute(f'CREATE ROLE "{role}" NOLOGIN')
        try:
            async with pool.acquire() as conn:
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    async with conn.transaction():
                        await conn.execute(f'SET LOCAL ROLE "{role}"')
                        await conn.execute("SELECT akb_lock_vault_events($1)", vault_id)
        finally:
            await pool.execute(f'DROP ROLE "{role}"')


async def test_a_busy_vaults_rollout_target_does_not_hold_back_another_rollout(monkeypatch):
    """Two rollouts wait. The target claimed first belongs to a Vault whose
    admission stays full. After each refusal the worker defers that target
    instead of making it the next claim again, so the other rollout is
    applied meanwhile."""
    monkeypatch.setattr(settings, "vault_write_concurrency", 1, raising=False)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 0.3, raising=False)
    async with _fresh_database() as pool:
        installed = {}
        for name in ("rollout-a", "rollout-b"):
            vault_id, job_id, _items = await _installation(pool, own_items=True, vault=name, app_key=name)
            installed[job_id] = vault_id
        claimed_first = await pool.fetchval(
            "SELECT job_id FROM app_rollout_targets ORDER BY job_id, batch_no, ordinal LIMIT 1",
        )
        busy_job = claimed_first
        other_job = next(job for job in installed if job != busy_job)
        async with _vault_full(installed[busy_job]):
            outcomes = [await rollout_worker.run_once() for _ in range(3)]
        # Deferred, applied, nothing eligible.
        assert outcomes == [1, 1, 0], outcomes
        assert await pool.fetchval("SELECT status FROM app_rollout_jobs WHERE id = $1", other_job) == "applied"
        deferred = await pool.fetchrow(
            "SELECT state, lease_owner, lease_expires_at > NOW() AS later FROM app_rollout_targets WHERE job_id = $1",
            busy_job,
        )
        assert dict(deferred) == {"state": "pending", "lease_owner": None, "later": True}


async def test_a_deferred_rollout_target_does_not_idle_the_worker(monkeypatch):
    """The same two rollouts, run by the worker's real runner, which sleeps
    its idle interval (10 seconds) after an iteration that did no work. The
    iteration that defers the busy Vault's target reports work done, so the
    runner claims again at once and applies the other rollout within
    seconds. It used to report no work and sleep, and the target's 5-second
    deferral had ended when it woke, so the busy target was claimed first
    again, every time."""
    from app.services import _backfill

    monkeypatch.setattr(settings, "vault_write_concurrency", 1, raising=False)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 0.3, raising=False)
    monkeypatch.setattr(_backfill, "_RUNNERS", [])
    async with _fresh_database() as pool:
        installed = {}
        for name in ("runner-a", "runner-b"):
            vault_id, job_id, _items = await _installation(pool, own_items=True, vault=name, app_key=name)
            installed[job_id] = vault_id
        busy_job = await pool.fetchval(
            "SELECT job_id FROM app_rollout_targets ORDER BY job_id, batch_no, ordinal LIMIT 1",
        )
        other_job = next(job for job in installed if job != busy_job)
        runner = _backfill.BackfillRunner("rollout-under-test", rollout_worker._process_once, idle_secs=10)

        async def other_applied() -> None:
            while await pool.fetchval(
                "SELECT status FROM app_rollout_jobs WHERE id = $1", other_job,
            ) != "applied":
                await asyncio.sleep(0.05)

        async with _vault_full(installed[busy_job]):
            runner.start()
            try:
                await asyncio.wait_for(other_applied(), timeout=5)
            finally:
                assert await runner.stop(timeout=10)
        assert await pool.fetchval("SELECT status FROM app_rollout_jobs WHERE id = $1", busy_job) != "applied"


async def test_vault_creations_waiting_for_the_role_lock_leave_the_git_lane_to_other_vaults(tmp_path, monkeypatch):
    """Another transaction holds the role authority lock, as long table DDL
    does. Eight Bare Git Vault creations reach that lock after their first Git
    commit, each holding its new Vault's gate. They hold none of the eight
    global Git lane slots while they wait, so a document write to another
    Vault takes a slot and commits. A creation used to hold a slot from start
    to end. The creations complete once the lock is released."""
    from app.services import write_lane

    monkeypatch.setattr(settings, "write_lane_concurrency", 8, raising=False)
    monkeypatch.setattr(settings, "write_lane_queue_timeout_secs", 2.0, raising=False)
    monkeypatch.setattr(write_lane, "_global_slots", None)
    async with _fresh_database() as pool:
        documents = DocumentService(git=GitService(storage_path=str(tmp_path)))
        monkeypatch.setattr(document_service_module, "get_role_sync", lambda: _AnyRoleSync())
        await documents.create_vault("lane-other")
        creations: list[asyncio.Task] = []
        try:
            async with pool.acquire() as holder:
                async with holder.transaction():
                    await holder.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", "akb:role-sync:reconcile",
                    )
                    creations = [
                        asyncio.create_task(documents.create_vault(f"lane-waiting-{i}")) for i in range(8)
                    ]

                    async def all_waiting() -> None:
                        while await pool.fetchval(
                            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted "
                            "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())",
                        ) < 8:
                            await asyncio.sleep(0.02)

                    await asyncio.wait_for(all_waiting(), timeout=20)
                    written = await asyncio.wait_for(documents.put(DocumentPutRequest(
                        vault="lane-other", collection="notes", title="During", content="written meanwhile",
                    )), timeout=10)
                    assert written.path.startswith("notes/")
            await asyncio.wait_for(asyncio.gather(*creations), timeout=60)
        finally:
            for task in creations:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*creations, return_exceptions=True)
        assert await pool.fetchval("SELECT count(*) FROM vaults WHERE name LIKE 'lane-waiting-%'") == 8


class _GitLaneTaken:
    """Other Vaults' Git writes take every global write-lane slot, each in a
    context of its own as a request's would be, and keep them for
    ``seconds``. Records how many Vault-lifecycle calls waited for a slot
    when they let go."""

    def __init__(self, slots: int, seconds: float) -> None:
        self.slots = slots
        self.seconds = seconds
        self.tasks: list[asyncio.Task] = []
        self.lifecycle_waiters = 0

    async def take(self) -> None:
        from app.services import write_lane

        taken = asyncio.Semaphore(0)
        release = asyncio.Event()

        async def hold(index: int) -> None:
            async with write_lane.write_lane(f"lane-busy-{index}"):
                taken.release()
                await release.wait()

        self.tasks = [
            asyncio.create_task(hold(index), context=contextvars.Context()) for index in range(self.slots)
        ]
        for _ in range(self.slots):
            await asyncio.wait_for(taken.acquire(), timeout=5)

        async def release_later() -> None:
            await asyncio.sleep(self.seconds)
            self.lifecycle_waiters = write_lane.snapshot()["lifecycle_waiters"]
            release.set()

        self.tasks.append(asyncio.create_task(release_later(), context=contextvars.Context()))

    async def done(self) -> None:
        await asyncio.wait_for(asyncio.gather(*self.tasks, return_exceptions=True), timeout=30)


def _lane_taken_at_vault_row(monkeypatch, lane: _GitLaneTaken) -> None:
    """Take the lane when the new Vault's first Git commit and row exist."""

    class _LaneTakingRoleSync(_AnyRoleSync):
        async def on_vault_create(self, vault_id, _owner) -> None:
            await lane.take()

    monkeypatch.setattr(document_service_module, "get_role_sync", lambda: _LaneTakingRoleSync())


async def test_a_new_vaults_seed_waits_for_a_busy_git_lane(tmp_path, monkeypatch):
    """Other Vaults' Git writes take every global write-lane slot right after
    a new Bare Git Vault's first commit and row exist, and keep them past the
    lane's deadline. The seed document's write waits for a slot without that
    deadline, as Vault lifecycle work does, and the creation completes once a
    slot frees. It used to give up at the deadline, after the Vault's commit
    and row existed."""
    from app.services import write_lane

    monkeypatch.setattr(settings, "write_lane_concurrency", 2, raising=False)
    monkeypatch.setattr(settings, "write_lane_queue_timeout_secs", 0.3, raising=False)
    monkeypatch.setattr(write_lane, "_global_slots", None)
    async with _fresh_database() as pool:
        documents = DocumentService(git=GitService(storage_path=str(tmp_path)))
        lane = _GitLaneTaken(slots=2, seconds=1.5)
        _lane_taken_at_vault_row(monkeypatch, lane)
        try:
            vault_id = uuid.UUID(await asyncio.wait_for(documents.create_vault("seeded-busy-lane"), timeout=30))
        finally:
            await lane.done()
        assert lane.lifecycle_waiters == 1
        assert await pool.fetchval(
            "SELECT count(*) FROM documents WHERE vault_id = $1 AND path LIKE 'overview/%'", vault_id,
        ) == 1


async def test_a_failed_vault_creation_is_undone_while_the_git_lane_is_busy(tmp_path, monkeypatch):
    """The lane is taken the same way, and the seed then fails for another
    reason. The compensation waits for a slot without the lane's deadline,
    then removes the Vault's row and repository, so the name can be used
    again. It used to give up at the deadline and leave both in place."""
    from app.services import write_lane

    monkeypatch.setattr(settings, "write_lane_concurrency", 2, raising=False)
    monkeypatch.setattr(settings, "write_lane_queue_timeout_secs", 0.3, raising=False)
    monkeypatch.setattr(write_lane, "_global_slots", None)
    async with _fresh_database() as pool:
        git = GitService(storage_path=str(tmp_path))
        documents = DocumentService(git=git)
        lane = _GitLaneTaken(slots=2, seconds=1.5)
        _lane_taken_at_vault_row(monkeypatch, lane)

        def seed_fails(_vault: str):
            raise RuntimeError("seed refused by the test")

        monkeypatch.setattr(document_service_module, "build_vault_skill_seed_request", seed_fails)
        try:
            with pytest.raises(RuntimeError, match="seed refused"):
                await asyncio.wait_for(documents.create_vault("undone-busy-lane"), timeout=30)
        finally:
            await lane.done()
        assert await pool.fetchval("SELECT count(*) FROM vaults WHERE name = 'undone-busy-lane'") == 0
        assert not git.vault_exists("undone-busy-lane")
        assert lane.lifecycle_waiters == 1


async def test_a_file_confirmation_refused_at_its_publication_keeps_its_lease_renewal(tmp_path, monkeypatch):
    """A File upload confirmation is admitted twice: to renew its lease, and,
    after it read the upload, to publish it. When the Vault fills up while
    the upload is read, the publication is refused with write_busy; the lease
    renewal stays committed and the File stays pending. The same confirmation
    sent again once the Vault has room publishes it."""
    monkeypatch.setattr(settings, "vault_write_concurrency", 1, raising=False)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 0.3, raising=False)
    async with _fresh_database() as pool:
        ctx = _Context(pool=pool, tmp_path=tmp_path, monkeypatch=monkeypatch)
        vault_id = await _make_vault(pool, "confirm-twice")
        blobs = ctx.fake_objects()
        fid = await ctx.file(vault_id, blobs, state="pending")
        before = await pool.fetchval("SELECT updated_at FROM vault_files WHERE id = $1", fid)
        held, release = asyncio.Event(), asyncio.Event()

        async def hold() -> None:
            async with vault_write_admission(vault_id):
                held.set()
                await release.wait()

        reading = file_service.s3_adapter.iter_chunks
        holders: list[asyncio.Task] = []

        def fill_while_reading(key):
            if not holders:
                holders.append(asyncio.get_running_loop().create_task(hold()))
            return reading(key)

        files = file_service.FileService()
        monkeypatch.setattr(file_service.s3_adapter, "iter_chunks", fill_while_reading)
        try:
            with pytest.raises(WriteBusyError):
                await files.confirm_upload(vault_id, str(fid), actor_id="tester")
        finally:
            release.set()
            await asyncio.gather(*holders, return_exceptions=True)
        assert held.is_set()
        row = await pool.fetchrow("SELECT upload_state, updated_at FROM vault_files WHERE id = $1", fid)
        assert row["upload_state"] == "pending" and row["updated_at"] > before
        assert await pool.fetchval(
            "SELECT count(*) FROM events WHERE vault_id = $1 AND kind = 'file.put'", vault_id,
        ) == 0

        monkeypatch.setattr(file_service.s3_adapter, "iter_chunks", reading)
        await files.confirm_upload(vault_id, str(fid), actor_id="tester")
        assert await pool.fetchval("SELECT upload_state FROM vault_files WHERE id = $1", fid) == "confirmed"


async def test_a_recursive_delete_refused_part_way_says_it_partly_completed(monkeypatch):
    """A Native recursive delete commits each document on its own. When the
    Vault's admission refuses the second document after the first was
    deleted, the delete fails with a write_busy that says the operation partly
    completed and that sending it again continues it, over MCP and REST; the
    same delete sent again completes it. A refusal before anything was
    deleted keeps the plain write_busy."""
    from app.services import collection_service as collection_service_module
    from app.util.errors import WRITE_BUSY, exception_envelope
    from tests.test_collection_delete_native_atomic_postgres import _native_vault

    monkeypatch.setattr(settings, "vault_write_concurrency", 1, raising=False)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 0.3, raising=False)
    async with _fresh_database() as pool:
        vault = "partial-delete"
        vault_id = await _native_vault(pool, monkeypatch, vault)
        delete_document = collection_service_module._delete_native_document
        holders: list[asyncio.Task] = []
        release = asyncio.Event()

        async def hold() -> None:
            async with vault_write_admission(vault_id):
                await release.wait()

        async def busy_after_the_first(*args, **kwargs):
            deleted = await delete_document(*args, **kwargs)
            if not holders:
                holders.append(asyncio.create_task(hold()))
                await asyncio.sleep(0)
            return deleted

        def delete():
            return CollectionService(git=object()).delete(
                vault=vault, path="notes", recursive=True, agent_id="tester",
            )

        monkeypatch.setattr(collection_service_module, "_delete_native_document", busy_after_the_first)
        try:
            with pytest.raises(WriteBusyError) as partial:
                await delete()
        finally:
            release.set()
            await asyncio.gather(*holders, return_exceptions=True)
        assert partial.value.partially_applied
        envelope = exception_envelope(partial.value)
        assert (envelope["code"], envelope["details"]["partially_applied"]) == (WRITE_BUSY, True)
        assert "Part of the operation may have completed" in envelope["hint"]
        assert partial.value.hint == envelope["hint"]
        assert await pool.fetchval(
            "SELECT count(*) FROM native_resources WHERE namespace_id = $1 AND lifecycle = 'live'", vault_id,
        ) == 1

        monkeypatch.setattr(collection_service_module, "_delete_native_document", delete_document)
        async with _vault_full(vault_id):
            with pytest.raises(WriteBusyError) as plain:
                await delete()
        assert not plain.value.partially_applied
        assert "no partial write occurred" in exception_envelope(plain.value)["hint"]
        result = await delete()
        assert result["deleted_docs"] == 1


@pytest.mark.parametrize("backend", ["postgres_native", "bare_git"])
async def test_a_grep_replacement_refused_part_way_names_what_it_replaced(backend, tmp_path, monkeypatch):
    """A grep replacement over three documents commits each on its own. The
    Vault's admission refuses the second after the first committed. The
    response is a write_busy that says the request partly completed, and its
    hint names the replaced document with its new revision and asks to apply
    the replacement only to the documents not replaced yet: the same request
    sent again would turn the replaced document's 'note note' into 'note note
    note note'. It used to keep write_busy's hint that nothing was written."""
    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path))
    monkeypatch.setattr(settings, "document_revision_backend", backend)
    monkeypatch.setattr(settings, "vault_write_concurrency", 1, raising=False)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 0.3, raising=False)
    from mcp_server import server

    vault = f"grep-busy-{backend.replace('_', '-')}"
    async with _fresh_database() as pool:
        vault_id = await _make_vault(pool, vault)
        writer = await pool.fetchval(
            "INSERT INTO users(username,email,password_hash) "
            "VALUES('grep-writer','grep-writer@example.invalid','disabled') RETURNING id"
        )
        await pool.execute("INSERT INTO vault_access(vault_id,user_id,role) VALUES($1,$2,'writer')", vault_id, writer)
        if backend == "bare_git":
            git = GitService(storage_path=str(tmp_path))
            git.init_vault(vault)
            docs = DocumentService(git=git)
        else:
            docs = NativeDocumentService(pool=pool)
        monkeypatch.setattr(server, "doc_service", docs)
        for slug in ("first", "second", "third"):
            await docs.put(DocumentPutRequest(
                vault=vault, collection="notes", slug=slug, title=slug, content="Keep the note here.",
            ))

        holders: list[asyncio.Task] = []
        held, release = asyncio.Event(), asyncio.Event()

        async def hold() -> None:
            async with vault_write_admission(vault_id):
                held.set()
                await release.wait()

        async def fill_the_vault() -> None:
            if not holders:
                holders.append(asyncio.create_task(hold(), context=contextvars.Context()))
                await asyncio.wait_for(held.wait(), timeout=5)

        if backend == "bare_git":
            update = docs.update

            async def update_then_fill(*args, **kwargs):
                result = await update(*args, **kwargs)
                await fill_the_vault()
                return result

            monkeypatch.setattr(docs, "update", update_then_fill)
        else:
            replace_text = NativeRevisionService.replace_text

            async def replace_then_fill(self, **kwargs):
                result = await replace_text(self, **kwargs)
                await fill_the_vault()
                return result

            monkeypatch.setattr(NativeRevisionService, "replace_text", replace_then_fill)
        try:
            result = await server._dispatch(
                "akb_grep", {"vault": vault, "pattern": "note", "replace": "note note"},
                server._MCPUser(str(writer), "grep-writer"),
            )
        finally:
            release.set()
            await asyncio.gather(*holders, return_exceptions=True)

        assert result["code"] == "write_busy", result
        assert result["replacement_complete"] is False and result["replaced_docs"] == 1
        replaced, = result["replacements"]
        details = result["details"]
        assert details["partially_applied"] is True
        assert details["committed_replacements"] == 1
        assert details["failed_uri"] != replaced["uri"]
        hint = result["hint"]
        assert f"{replaced['uri']} ({replaced['commit']})" in hint
        assert "Do not send the same request again" in hint
        assert details["failed_uri"] in hint
        bodies = {}
        for slug in ("first", "second", "third"):
            doc = await docs.get(vault, f"notes/{slug}.md")
            bodies[doc.uri] = doc.content
        assert sorted(bodies.values()) == sorted(["Keep the note note here.", *["Keep the note here."] * 2])
        assert bodies[replaced["uri"]] == "Keep the note note here."


async def test_an_admin_override_recorded_before_a_refused_write_stays(monkeypatch):
    """An unscoped system administrator creates a collection in a
    write-managed Vault. The access check records the override's audit event
    in a transaction of its own, admitted for the Vault, before the write is
    admitted. When the Vault fills up between the two, the write is refused
    with write_busy: neither the collection nor its event is written, and the
    override's audit event stays."""
    from mcp_server import server

    monkeypatch.setattr(settings, "vault_write_concurrency", 1, raising=False)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", 0.3, raising=False)
    async with _fresh_database() as pool:
        vault = "override-then-busy"
        vault_id = await _make_vault(pool, vault)
        admin = await pool.fetchval(
            "INSERT INTO users (id, username, email, password_hash, is_admin) "
            "VALUES ($1, 'override-admin', 'override-admin@example.com', '!fixture!', TRUE) RETURNING id",
            uuid.uuid4(),
        )
        async with pool.acquire() as conn:
            await vault_write_policy_repo.set_policy(vault_id, "collector:test", "tester", conn=conn)

        holders: list[asyncio.Task] = []
        held, release = asyncio.Event(), asyncio.Event()

        async def hold() -> None:
            async with vault_write_admission(vault_id):
                held.set()
                await release.wait()

        check = server.check_vault_access

        async def check_then_fill(*args, **kwargs):
            access = await check(*args, **kwargs)
            holders.append(asyncio.create_task(hold(), context=contextvars.Context()))
            await asyncio.wait_for(held.wait(), timeout=5)
            return access

        monkeypatch.setattr(server, "check_vault_access", check_then_fill)
        try:
            result = await server._dispatch(
                "akb_create_collection", {"vault": vault, "path": "refused"},
                server._MCPUser(str(admin), "override-admin", is_admin=True),
            )
        finally:
            release.set()
            await asyncio.gather(*holders, return_exceptions=True)

        assert result["code"] == "write_busy", result
        kinds = [
            row["kind"]
            for row in await pool.fetch("SELECT kind FROM events WHERE vault_id = $1 ORDER BY id", vault_id)
        ]
        assert kinds == ["vault.write_policy_admin_bypass"]
        assert await pool.fetchval("SELECT count(*) FROM collections WHERE vault_id = $1", vault_id) == 0
