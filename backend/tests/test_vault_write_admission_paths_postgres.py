"""Every path that writes a Vault's events waits for the Vault's admission.

Each case below holds the only admission slot of a Vault in another task and
then drives one write of that Vault through its service. The write must fail
with a retryable `write_busy` within the admission deadline, before it changed
anything: no event, no row, no prepared payload, no File lease, no Git commit.
While it waits for admission it must hold no more pool connections than before
it started. A path that took its pool connection without admission would
instead go ahead (or wait on the database), and one that took it before
admission would hold it while waiting, so a missing path fails its case. Once
the slot is free the same write succeeds and records an event, which shows the
case drives a real event write.

`test_every_vault_event_writer_is_registered` keeps the list complete: it scans
the application for every place that writes a Vault's events and fails when one
is not listed in `_EMITTERS` with the case that covers it.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import hashlib
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from app.config import settings
from app.exceptions import WriteBusyError
from app.models.document import DocumentPutRequest, DocumentUpdateRequest
from app.repositories import table_data_repo, vault_write_policy_repo
from app.repositories.document_repo import CollectionRepository
from app.services import (
    access_service,
    app_rollout_worker,
    document_counters,
    file_service,
    table_migration_service,
    table_row_write,
    table_service,
    user_sql_executor,
)
from app.services import vault_write_admission as admission
from app.services.collection_service import CollectionService
from app.services.document_service import DocumentService
from app.services.external_git_service import ExternalGitService
from app.services.git_service import GitService
from app.services.m1_pg_body_store import M1PgBodyStore
from app.services.native_revision_service import NativeRevisionService
from app.services.vault_write_admission import vault_write_admission
from tests.test_events_commit_order_postgres import _fresh_database, _make_vault

_DEADLINE = 0.3


class _AnyRoleSync:
    """Role DDL is not what these cases check."""

    def __getattr__(self, _name: str) -> Callable[..., Awaitable[None]]:
        async def nothing(*_args: Any, **_kwargs: Any) -> None:
            return None

        return nothing


@dataclass
class _Case:
    vault_id: uuid.UUID
    write: Callable[[], Awaitable[object]]
    # Whether the write can run once more as a positive control.
    control: bool = True
    # State a refused write may still have changed, by design.
    allowed_changes: frozenset[str] = frozenset()


@dataclass
class _Context:
    pool: Any
    tmp_path: Path
    monkeypatch: pytest.MonkeyPatch

    async def user(self, name: str, *, admin: bool = False) -> uuid.UUID:
        return await self.pool.fetchval(
            "INSERT INTO users (id, username, email, password_hash, is_admin) "
            "VALUES ($1, $2, $3, '!fixture!', $4) RETURNING id",
            uuid.uuid4(), name, f"{name}@example.com", admin,
        )

    async def owned_vault(self, name: str) -> tuple[uuid.UUID, uuid.UUID]:
        vault_id = await _make_vault(self.pool, name)
        owner = await self.user(f"{name}-owner")
        await self.pool.execute("UPDATE vaults SET owner_id = $1 WHERE id = $2", owner, vault_id)
        return vault_id, owner

    def git_documents(self, vault: str) -> DocumentService:
        git = GitService(storage_path=str(self.tmp_path))
        git.init_vault(vault)
        return DocumentService(git=git)

    def native(self) -> NativeRevisionService:
        return NativeRevisionService(self.pool, payload_store=M1PgBodyStore(self.pool))

    async def table(self, vault: str, vault_id: uuid.UUID, name: str = "items") -> str:
        pg_name = table_data_repo.pg_table_name(vault, name)
        columns = [{"name": "value", "type": "text"}]
        async with self.pool.acquire() as conn:
            await table_data_repo.create_dynamic_table(
                conn, pg_name, columns, vault_name=vault, vault_id=vault_id,
                resource_uri=f"akb://{vault}/table/{name}",
            )
            await conn.execute(
                "INSERT INTO vault_tables (id, vault_id, name, description, columns, unique_keys, indexes) "
                "VALUES ($1, $2, $3, '', $4::jsonb, '[]'::jsonb, '[]'::jsonb)",
                uuid.uuid4(), vault_id, name, json.dumps(columns),
            )
        return pg_name

    def fake_objects(self) -> dict[str, bytes]:
        blobs: dict[str, bytes] = {}

        def head(key: str) -> dict:
            body = blobs[key]
            return {"ContentLength": len(body), "ETag": hashlib.sha256(body).hexdigest(),
                    "ContentType": "application/octet-stream"}

        def copy(source: str, target: str) -> dict:
            blobs[target] = blobs[source]
            return head(target)

        s3 = file_service.s3_adapter
        self.monkeypatch.setattr(s3, "head", head)
        self.monkeypatch.setattr(s3, "copy", copy)
        self.monkeypatch.setattr(s3, "iter_chunks", lambda key: iter([blobs[key]]))
        self.monkeypatch.setattr(s3, "delete", lambda key: blobs.pop(key, None))
        self.monkeypatch.setattr(s3, "ensure_bucket", lambda *_: None)
        self.monkeypatch.setattr(
            s3, "presign_put", lambda *_args, **_kwargs: "https://objects.invalid/put", raising=False,
        )

        async def no_index(*_args: Any, **_kwargs: Any) -> None:
            return None

        self.monkeypatch.setattr(file_service, "index_file_metadata", no_index)
        return blobs

    async def file(self, vault_id: uuid.UUID, blobs: dict[str, bytes], *, state: str) -> uuid.UUID:
        fid = uuid.uuid4()
        body = b"original file"
        key = f"files/{fid}.bin"
        digest = hashlib.sha256(body).hexdigest()
        confirmed = state == "confirmed"
        await self.pool.execute(
            """INSERT INTO vault_files
                (id, vault_id, kind, upload_state, name, s3_key, mime_type, size_bytes,
                 content_hash, hash_algorithm, hash_verified_at, etag, created_by)
               VALUES ($1, $2, 'file', $3, 'original.bin', $4, 'application/octet-stream', $5,
                       $6, 'sha256', CASE WHEN $7 THEN NOW() END, $6, 'tester')""",
            fid, vault_id, state, key, len(body), digest if confirmed else None, confirmed,
        )
        blobs[key] = body
        return fid


# ── Cases ────────────────────────────────────────────────────────────────


async def _git_document(ctx: _Context, operation: str) -> _Case:
    vault = f"admit-git-{operation}"
    vault_id = await _make_vault(ctx.pool, vault)
    documents = ctx.git_documents(vault)
    doc = await documents.put(DocumentPutRequest(
        vault=vault, collection="notes", title="First", content="first body",
    ))
    writes = {
        "put": lambda: documents.put(DocumentPutRequest(
            vault=vault, collection="notes", title="Second", content="second body",
        )),
        "update": lambda: documents.update(vault, doc.path, DocumentUpdateRequest(title="Renamed")),
        "edit": lambda: documents.edit(vault, doc.path, "first body", "edited body"),
        "move": lambda: documents.move(vault, doc.path, collection="moved"),
        "delete": lambda: documents.delete(vault, doc.path),
    }
    return _Case(vault_id, writes[operation])


async def _native_document(ctx: _Context, operation: str) -> _Case:
    vault_id = await _make_vault(ctx.pool, f"admit-native-{operation}")
    native = ctx.native()
    doc = await native.create_text(
        namespace_id=vault_id, surface="document", path="notes/first.md",
        payload="first body", actor="tester", mutation_id=uuid.uuid4(),
    )
    gone = await native.create_text(
        namespace_id=vault_id, surface="document", path="notes/gone.md",
        payload="gone body", actor="tester", mutation_id=uuid.uuid4(),
    )
    tombstone = await native.delete_resource(
        namespace_id=vault_id, surface="document", path=gone.path, actor="tester",
        mutation_id=uuid.uuid4(), expected_revision_id=gone.revision_id,
    )
    writes = {
        "create": lambda: native.create_text(
            namespace_id=vault_id, surface="document", path="notes/second.md",
            payload="second body", actor="tester", mutation_id=uuid.uuid4(),
        ),
        "restore": lambda: native.restore_text(
            namespace_id=vault_id, surface="document", path=gone.path, payload="restored body",
            actor="tester", mutation_id=uuid.uuid4(), expected_revision_id=tombstone.revision_id,
            expected_resource_id=gone.resource_id,
        ),
        "replace": lambda: native.replace_text(
            namespace_id=vault_id, surface="document", path=doc.path, payload="changed body",
            actor="tester", mutation_id=uuid.uuid4(), expected_revision_id=doc.revision_id,
        ),
        "move": lambda: native.move_text(
            namespace_id=vault_id, surface="document", path=doc.path, path_to="moved/first.md",
            actor="tester", mutation_id=uuid.uuid4(), expected_revision_id=doc.revision_id,
        ),
        "delete": lambda: native.delete_resource(
            namespace_id=vault_id, surface="document", path=doc.path, actor="tester",
            mutation_id=uuid.uuid4(), expected_revision_id=doc.revision_id,
        ),
    }
    return _Case(vault_id, writes[operation])


async def _collection(ctx: _Context, operation: str) -> _Case:
    vault = f"admit-collection-{operation}"
    vault_id = await _make_vault(ctx.pool, vault)
    await CollectionRepository(ctx.pool).get_or_create(vault_id, "existing")
    collections = CollectionService(git=object())
    if operation == "native_delete":
        # Each Native document goes in a transaction of its own.
        ctx.monkeypatch.setattr(document_counters, "native_documents_are_authoritative", lambda: True)
        await ctx.native().create_text(
            namespace_id=vault_id, surface="document", path="existing/first.md",
            payload="first body", actor="tester", mutation_id=uuid.uuid4(),
        )
    if operation == "create":
        return _Case(vault_id, lambda: collections.create(
            vault=vault, path="fresh", summary=None, agent_id="tester",
        ))
    return _Case(vault_id, lambda: collections.delete(
        vault=vault, path="existing", recursive=True, agent_id="tester",
    ))


async def _file(ctx: _Context, operation: str) -> _Case:
    vault = f"admit-file-{operation}"
    vault_id = await _make_vault(ctx.pool, vault)
    blobs = ctx.fake_objects()
    files = file_service.FileService()
    if operation == "confirm":
        fid = await ctx.file(vault_id, blobs, state="pending")
        return _Case(vault_id, lambda: files.confirm_upload(vault_id, str(fid), actor_id="tester"))
    fid = await ctx.file(vault_id, blobs, state="confirmed")
    if operation == "delete":
        return _Case(vault_id, lambda: files.delete(vault_id, str(fid), actor_id="tester"))
    replacement = b"replacement bytes"
    digest = hashlib.sha256(replacement).hexdigest()

    async def replace() -> object:
        prepared = await files.initiate_replace(
            vault, vault_id, str(fid), actor_id="tester", content_hash=digest,
        )
        rid = uuid.UUID(prepared["replacement_id"])
        blobs[file_service._replacement_staging_key(vault, fid, rid)] = replacement
        return await files.confirm_replace(
            vault, vault_id, str(fid), str(rid), actor_id="tester", content_hash=digest,
        )

    # The replacement copies its bytes and schedules the copy's cleanup before
    # its publication transaction; a refused publication leaves that cleanup.
    return _Case(vault_id, replace, allowed_changes=frozenset({"outbox"}))


async def _table(ctx: _Context, operation: str) -> _Case:
    vault = f"admit-table-{operation}"
    vault_id = await _make_vault(ctx.pool, vault)
    ctx.monkeypatch.setattr(table_service, "get_role_sync", lambda: _AnyRoleSync())
    await ctx.table(vault, vault_id)
    admin = await ctx.user(f"{vault}-admin", admin=True)
    ctx.monkeypatch.setattr(user_sql_executor, "_executor", user_sql_executor.UserSqlExecutor(ctx.pool))
    pg_name = table_data_repo.pg_table_name(vault, "items")
    writes = {
        "create": lambda: table_service.create_table(
            vault_id, "fresh", [{"name": "value", "type": "text"}], actor_id="tester",
        ),
        "drop": lambda: table_service.drop_table(vault_id, "items", actor_id="tester"),
        "alter": lambda: table_service.alter_table(
            vault_id, "items", actor_id="tester", add_columns=[{"name": "extra", "type": "text"}],
        ),
        "migration": lambda: table_migration_service.apply_table_migration(
            vault_id, actor_id="tester", idempotency_key=str(uuid.uuid4()),
            operations=[{"op": "add_column", "table": "items", "name": "more", "type": "text"}],
        ),
        "rows": lambda: table_row_write.insert_rows(
            vault_name=vault, vault_id=vault_id, table_name="items", user_id=str(admin),
            actor_id="tester", body=[{"value": "row"}], is_admin=True,
        ),
        "sql": lambda: table_service.execute_sql(
            vault_names=[vault], user_id=str(admin), actor_id="tester",
            sql=f"INSERT INTO {pg_name} (value) VALUES ('row')", is_admin=True,
        ),
    }
    return _Case(vault_id, writes[operation])


async def _sql_elsewhere(ctx: _Context, operation: str) -> _Case:
    """SQL declared for one Vault whose plan writes another Vault's table:
    the write waits for the Vault it writes."""
    declared = "admit-sql-declared"
    target = f"admit-sql-{operation}"
    await _make_vault(ctx.pool, declared)
    target_id = await _make_vault(ctx.pool, target)
    pg_name = await ctx.table(target, target_id)
    admin = await ctx.user(f"{target}-admin", admin=True)
    ctx.monkeypatch.setattr(user_sql_executor, "_executor", user_sql_executor.UserSqlExecutor(ctx.pool))
    sql = f"INSERT INTO {pg_name} (value) VALUES ('row')"
    return _Case(target_id, lambda: table_service.execute_sql(
        vault_names=[declared], user_id=str(admin), actor_id="tester", sql=sql, is_admin=True,
    ))


async def _access(ctx: _Context, operation: str) -> _Case:
    vault = f"admit-access-{operation}"
    vault_id, owner = await ctx.owned_vault(vault)
    ctx.monkeypatch.setattr(access_service, "get_role_sync", lambda: _AnyRoleSync())
    member = await ctx.user(f"{vault}-member")
    admin = await ctx.user(f"{vault}-admin", admin=True)
    token = await ctx.pool.fetchval(
        "INSERT INTO tokens (user_id, name, token_hash, token_prefix) "
        "VALUES ($1, 'service', $2, 'akb_test') RETURNING id",
        member, uuid.uuid4().hex,
    )
    # A metadata change records an event only for an admin override, on a
    # write-managed Vault.
    if operation in {"revoke", "check_override", "metadata", "remove_policy", "add_grant", "remove_grant"}:
        async with ctx.pool.acquire() as conn:
            if operation == "revoke":
                await conn.execute(
                    "INSERT INTO vault_access (vault_id, user_id, role) VALUES ($1, $2, 'reader')",
                    vault_id, member,
                )
            else:
                await vault_write_policy_repo.set_policy(vault_id, "collector:test", "tester", conn=conn)
            if operation == "remove_grant":
                await conn.execute(
                    "INSERT INTO vault_write_grants (vault_id, token_id, granted_by) VALUES ($1, $2, 'tester')",
                    vault_id, token,
                )
    username = f"{vault}-member"
    writes = {
        "grant": lambda: access_service.grant_access(str(owner), vault, username, "reader"),
        "revoke": lambda: access_service.revoke_access(str(owner), vault, username),
        "transfer": lambda: access_service.transfer_ownership(str(owner), vault, username),
        "metadata": lambda: access_service.update_vault_metadata(str(admin), vault, description="changed"),
        "public": lambda: access_service.set_public_access(str(owner), vault, "reader"),
        "set_policy": lambda: access_service.set_vault_write_policy("tester", vault, "collector:test"),
        "bootstrap_policy": lambda: access_service.bootstrap_vault_write_policy(
            "tester", vault, "collector:test", grants=[{"token_id": str(token)}],
        ),
        "remove_policy": lambda: access_service.remove_vault_write_policy("tester", vault),
        "add_grant": lambda: access_service.add_vault_write_grant("tester", vault, str(token)),
        "remove_grant": lambda: access_service.remove_vault_write_grant("tester", vault, str(token)),
        "check_override": lambda: access_service.check_vault_access(str(admin), vault, "writer"),
    }
    return _Case(vault_id, writes[operation])


async def _app_rollout(ctx: _Context, _operation: str) -> _Case:
    vault_id = await _make_vault(ctx.pool, "admit-app-rollout")
    target = {"vault_id": vault_id, "installation_id": uuid.uuid4(), "job_id": uuid.uuid4()}
    return _Case(vault_id, lambda: app_rollout_worker._process_target(target), control=False)


async def _external_git(ctx: _Context, operation: str) -> _Case:
    vault = f"admit-external-{operation}"
    vault_id = await _make_vault(ctx.pool, vault)

    class _Git:
        def cat_blob(self, _vault: str, _sha: str) -> bytes:
            return b"# Mirrored\n\nbody\n"

        def last_commit_for_path(self, *_args: object) -> str:
            return "c" * 40

    service = ExternalGitService(git=_Git())  # type: ignore[arg-type]
    if operation == "reindex":
        return _Case(vault_id, lambda: service._reindex_file(
            vault_id=vault_id, vault_name=vault, path="docs/mirrored.md", blob_sha="b" * 40,
            remote_url="https://example.invalid/repo.git", tip_sha="c" * 40,
        ), control=False)
    return _Case(vault_id, lambda: service._delete_external_path(
        vault_id=vault_id, vault_name=vault, path="docs/mirrored.md",
    ), control=False)


_CASES: dict[str, tuple[Callable[[_Context, str], Awaitable[_Case]], str]] = {
    **{f"git.{op}": (_git_document, op) for op in ("put", "update", "edit", "move", "delete")},
    **{f"native.{op}": (_native_document, op) for op in ("create", "replace", "restore", "move", "delete")},
    **{f"collection.{op}": (_collection, op) for op in ("create", "delete", "native_delete")},
    **{f"file.{op}": (_file, op) for op in ("confirm", "replace", "delete")},
    **{f"table.{op}": (_table, op) for op in ("create", "drop", "alter", "migration", "rows", "sql")},
    "table.sql_elsewhere": (_sql_elsewhere, "plan"),
    **{f"access.{op}": (_access, op) for op in (
        "grant", "revoke", "transfer", "metadata", "public", "set_policy", "bootstrap_policy",
        "remove_policy", "add_grant", "remove_grant", "check_override",
    )},
    "app_rollout.target": (_app_rollout, "target"),
    **{f"external_git.{op}": (_external_git, op) for op in ("reindex", "delete")},
}


@contextlib.asynccontextmanager
async def _vault_full(vault_id: uuid.UUID):
    """Hold the Vault's only admission slot in another task."""
    held, release = asyncio.Event(), asyncio.Event()

    async def hold() -> None:
        async with vault_write_admission(vault_id):
            held.set()
            await release.wait()

    holder = asyncio.create_task(hold())
    await asyncio.wait_for(held.wait(), timeout=5)
    try:
        yield
    finally:
        release.set()
        await holder


async def _state(pool, vault_id: uuid.UUID) -> dict:
    """Everything a write of the Vault could have changed."""
    tables = (
        "events", "documents", "collections", "vault_tables", "vault_access",
        "vault_write_policy", "vault_write_grants",
    )
    state = {table: await pool.fetchval(f"SELECT count(*) FROM {table} WHERE vault_id = $1", vault_id)
             for table in tables}
    for table in ("native_resources", "native_revisions", "m1_reference_payloads"):
        state[table] = await pool.fetchval(f"SELECT count(*) FROM {table} WHERE namespace_id = $1", vault_id)
    # Every column, so a lease that only moved `updated_at` shows too.
    state["vault_files"] = await pool.fetchval(
        "SELECT coalesce(string_agg(f::text, '|' ORDER BY f.id), '') FROM vault_files f WHERE vault_id = $1",
        vault_id,
    )
    state["vault"] = dict(await pool.fetchrow(
        "SELECT owner_id, description, public_access, updated_at FROM vaults WHERE id = $1", vault_id,
    ))
    state["outbox"] = await pool.fetchval("SELECT count(*) FROM s3_delete_outbox")
    vault_name = await pool.fetchval("SELECT name FROM vaults WHERE id = $1", vault_id)
    state["table_rows"] = {}
    for table in await pool.fetch("SELECT name FROM vault_tables WHERE vault_id = $1 ORDER BY name", vault_id):
        pg_name = table_data_repo.pg_table_name(vault_name, table["name"])
        if await pool.fetchval("SELECT to_regclass($1) IS NOT NULL", pg_name):
            state["table_rows"][table["name"]] = await pool.fetchval(f"SELECT count(*) FROM {pg_name}")
    return state


@pytest.mark.parametrize("case", sorted(_CASES))
async def test_a_write_waits_for_its_vaults_admission(case, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_write_concurrency", 1)
    monkeypatch.setattr(settings, "vault_write_queue_timeout_secs", _DEADLINE)
    arrange, operation = _CASES[case]
    async with _fresh_database() as pool:
        ctx = _Context(pool=pool, tmp_path=tmp_path, monkeypatch=monkeypatch)
        prepared = await arrange(ctx, operation)
        before = await _state(pool, prepared.vault_id)
        heads_before = _git_heads(tmp_path)
        async with _vault_full(prepared.vault_id):
            started = time.monotonic()
            held_while_waiting = await _refused_holding_no_more_connections(pool, prepared.write)
            waited = time.monotonic() - started
        assert waited < _DEADLINE + 2, waited
        assert held_while_waiting <= 0, (
            f"held {held_while_waiting} more pool connection(s) while waiting for admission"
        )
        after = await _state(pool, prepared.vault_id)
        for key in prepared.allowed_changes:
            after.pop(key), before.pop(key)
        assert after == before
        assert _git_heads(tmp_path) == heads_before
        if prepared.control:
            await asyncio.wait_for(prepared.write(), timeout=30)
            assert await pool.fetchval(
                "SELECT count(*) FROM events WHERE vault_id = $1", prepared.vault_id,
            ) > before["events"]


async def _refused_holding_no_more_connections(pool, write: Callable[[], Awaitable[object]]) -> int:
    """Run a write that its Vault's admission must refuse, and return the most
    pool connections it held above the starting count while it waited for
    admission."""
    def in_use() -> int:
        return pool.get_size() - pool.get_idle_size()

    baseline = in_use()
    peak = None
    task = asyncio.ensure_future(write())
    deadline = time.monotonic() + _DEADLINE + 5
    try:
        while not task.done() and time.monotonic() < deadline:
            # The loop runs one task at a time: while the write is registered
            # as a waiter, it is suspended in the admission wait.
            if admission.snapshot()["waiting"]:
                peak = max(peak if peak is not None else in_use(), in_use())
            await asyncio.sleep(0.002)
    finally:
        if not task.done():
            task.cancel()
    with pytest.raises(WriteBusyError):
        await task
    assert peak is not None, "the write never waited for admission"
    return peak - baseline


def _git_heads(storage: Path) -> dict[str, str]:
    heads = {}
    for repo in storage.glob("*.git"):
        ref = repo / "refs" / "heads" / "main"
        heads[repo.name] = ref.read_text() if ref.exists() else ""
    return heads


# ── Every writer is registered ─────────────────────────────────────────────

# Each place in the application that writes a Vault's events (or a dynamic
# table's rows, whose trigger records `table.rows_changed`), with the case that
# shows its path waits for admission.
_EMITTERS: dict[str, str] = {
    "services/access_service.py:_check_vault_access_impl": "access.check_override",
    "services/access_service.py:grant_access": "access.grant",
    "services/access_service.py:revoke_access": "access.revoke",
    "services/access_service.py:transfer_ownership": "access.transfer",
    "services/access_service.py:update_vault_metadata": "access.metadata",
    "services/access_service.py:set_public_access": "access.public",
    "services/access_service.py:set_vault_write_policy": "access.set_policy",
    "services/access_service.py:bootstrap_vault_write_policy": "access.bootstrap_policy",
    "services/access_service.py:remove_vault_write_policy": "access.remove_policy",
    "services/access_service.py:add_vault_write_grant": "access.add_grant",
    "services/access_service.py:remove_vault_write_grant": "access.remove_grant",
    "services/access_service.py:check_vault_access": "access.check_override",
    "services/app_rollout_worker.py:_process_target": "app_rollout.target",
    "services/collection_service.py:CollectionService.create": "collection.create",
    "services/collection_service.py:CollectionService._delete_collections": "collection.delete",
    "services/collection_service.py:_delete_native_document": "collection.native_delete",
    "services/document_service.py:DocumentService._put_locked": "git.put",
    "services/document_service.py:DocumentService._update_locked": "git.update",
    "services/document_service.py:DocumentService._edit_locked": "git.edit",
    "services/document_service.py:DocumentService._delete_locked": "git.delete",
    "services/document_service.py:DocumentService.move": "git.move",
    "services/external_git_service.py:ExternalGitService._reindex_file": "external_git.reindex",
    "services/external_git_service.py:ExternalGitService._delete_external_path": "external_git.delete",
    "services/file_service.py:FileService._record_upload_confirmation": "file.confirm",
    "services/file_service.py:FileService.confirm_replace": "file.replace",
    "services/file_service.py:FileService.delete": "file.delete",
    "services/native_revision_service.py:NativeRevisionService._publish_create": "native.create",
    "services/native_revision_service.py:NativeRevisionService._publish_replace": "native.replace",
    "services/native_revision_service.py:NativeRevisionService._publish_restore": "native.restore",
    "services/native_revision_service.py:NativeRevisionService._publish_move": "native.move",
    "services/native_revision_service.py:NativeRevisionService._publish_delete": "native.delete",
    "services/table_migration_service.py:apply_table_migration": "table.migration",
    "services/table_service.py:create_table": "table.create",
    "services/table_service.py:drop_table": "table.drop",
    "services/table_service.py:alter_table": "table.alter",
    "services/table_service.py:execute_sql": "table.sql",
    "services/table_row_write.py:_execute_mutation": "table.rows",
    "services/table_row_query.py:select_rows": "read-only: a compiled SELECT runs read-only",
    "services/table_row_query.py:query_rows": "read-only: a compiled SELECT runs read-only",
    "services/user_sql_executor.py:_check_vault_write_policy": "table.sql",
    "services/user_sql_executor.py:UserSqlExecutor._run": "table.sql",
}

# The generic helpers: callers own the transaction and its admission.
_HELPERS = {
    "repositories/events_repo.py:emit_event",
    "repositories/events_repo.py:EventBuffer.flush",
    "repositories/events_repo.py:deferred_events",
    "services/native_revision_service.py:NativeRevisionService._emit_document_change",
}


def _vault_event_writers() -> set[str]:
    app_dir = Path(__file__).resolve().parents[1] / "app"
    found: set[str] = set()
    for path in sorted(app_dir.rglob("*.py")):
        if "migrations" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        scopes: dict[ast.AST, str] = {}

        def visit(node: ast.AST, scope: str) -> None:
            for child in ast.iter_child_nodes(node):
                child_scope = scope
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    child_scope = f"{scope}.{child.name}" if scope else child.name
                scopes[child] = child_scope
                visit(child, child_scope)

        visit(tree, "")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            keywords = {k.arg: k.value for k in node.keywords}
            vault = keywords.get("vault_id")
            writes = (
                (name == "emit_event" and vault is not None and not (
                    isinstance(vault, ast.Constant) and vault.value is None))
                or (name == "add" and "vault_id" in keywords)
                or name in {"EventBuffer", "deferred_events", "lock_vault_events", "_emit_document_change"}
                or (name == "execute" and isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Call)
                    and getattr(func.value.func, "id", None) == "get_user_sql_executor")
            )
            if writes:
                scope = scopes.get(node, "")
                found.add(f"{path.relative_to(app_dir)}:{scope.rsplit('.<locals>', 1)[0]}")
    return found


def test_every_vault_event_writer_is_registered():
    found = _vault_event_writers() - _HELPERS
    unregistered = sorted(found - set(_EMITTERS))
    assert not unregistered, (
        "These write a Vault's events without a registered admission case. Enter "
        "vault_write_admission before their pool connection and add a case: "
        f"{unregistered}"
    )
    stale = sorted(set(_EMITTERS) - found)
    assert not stale, f"Registered writers that no longer write events: {stale}"
    covered = {case for case in _EMITTERS.values() if not case.startswith("read-only")}
    assert covered <= set(_CASES), sorted(covered - set(_CASES))
