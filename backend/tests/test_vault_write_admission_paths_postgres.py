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
import functools
import hashlib
import json
import sys
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
    """SQL declaring two Vaults whose plan writes the second one's table: the
    write waits for every Vault it declares, the one it writes included."""
    declared = "admit-sql-declared"
    target = f"admit-sql-{operation}"
    await _make_vault(ctx.pool, declared)
    target_id = await _make_vault(ctx.pool, target)
    pg_name = await ctx.table(target, target_id)
    admin = await ctx.user(f"{target}-admin", admin=True)
    ctx.monkeypatch.setattr(user_sql_executor, "_executor", user_sql_executor.UserSqlExecutor(ctx.pool))
    sql = f"INSERT INTO {pg_name} (value) VALUES ('row')"
    return _Case(target_id, lambda: table_service.execute_sql(
        vault_names=[declared, target], user_id=str(admin), actor_id="tester", sql=sql, is_admin=True,
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


# ── Connections taken inside an admission ──────────────────────────────────

_APP_DIR = Path(__file__).resolve().parents[1] / "app"
# Entering one of these holds an admission for the rest of its `with`: the
# items after it and the body. `vault_write_admission` and `vault_gate` hold
# it without a connection; `admitted_connection` with the transaction's own,
# which the code inside passes on rather than take another.
_ADMISSION_CONTEXTS = frozenset({
    "vault_write_admission", "_vault_write_admission", "vault_gate",
    "admitted_connection", "_admitted_connection",
})
# A method this decorates runs inside the admission the decorator enters.
_ADMISSION_DECORATORS = frozenset({"_admitted_before_preparing"})
# The helpers that give connections back (`admitted_connection`,
# `acquire_given_back`) live here; they are the rule, not what it checks.
_HELPERS_MODULE = "services/vault_write_admission.py"
# A pool's methods that take one of its connections.
_POOL_METHODS = frozenset({"acquire", "execute", "executemany", "fetch", "fetchrow", "fetchval"})
# What the walk must reach from the roots, so that it cannot pass by
# resolving nothing: the payload preparation and revision id of the Native
# mutations, the repositories and role hooks of Vault creation, and the
# connection-taking helpers that admitted transactions pass their own
# connection to.
_MUST_REACH = frozenset({
    "services/access_service.py:_access_connection",
    "repositories/vault_write_policy_repo.py:get_policy",
    "repositories/document_repo.py:CollectionRepository.list_docs_under",
    "services/m1_pg_body_store.py:M1PgBodyStore.prepare_text",
    "services/m1_reference_payload_store.py:M1ReferencePayloadStore.prepare_text",
    "services/native_revision_service.py:NativeRevisionService._allocate_revision_id",
    "repositories/vault_repo.py:VaultRepository.create",
    "repositories/vault_repo.py:VaultRepository.get_id_by_name",
    "repositories/document_repo.py:CollectionRepository.get_or_create",
    "services/role_sync.py:RoleSync.on_vault_create",
    "services/role_sync.py:RoleSync.on_public_access_change",
    "services/document_service.py:DocumentService._rollback_vault_rows",
})


def _name_of(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _module_path(dotted: str) -> str | None:
    """`app.services.x` as a path under `app/`, when it is a module there."""
    if dotted != "app" and not dotted.startswith("app."):
        return None
    path = _APP_DIR.joinpath(*dotted.split(".")[1:])
    if path.with_suffix(".py").is_file():
        return str(path.with_suffix(".py").relative_to(_APP_DIR))
    if (path / "__init__.py").is_file():
        return str((path / "__init__.py").relative_to(_APP_DIR))
    return None


class _Module:
    """An application module: its top-level functions and classes, and the
    application modules and names its imports bind."""

    def __init__(self, rel: str, tree: ast.Module) -> None:
        self.rel = rel
        self.functions = {
            n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
        self.imports: dict[str, tuple[str, str | None]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    package = ["app", *Path(rel).parent.parts]
                    package = package[: len(package) - node.level + 1]
                    base = ".".join(package + ([base] if base else []))
                owner = _module_path(base)
                for alias in node.names:
                    local = alias.asname or alias.name
                    submodule = _module_path(f"{base}.{alias.name}")
                    if submodule is not None:
                        self.imports[local] = (submodule, None)
                    elif owner is not None:
                        self.imports[local] = (owner, alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    target = _module_path(alias.name)
                    if target is not None and alias.asname:
                        self.imports[alias.asname] = (target, None)


@functools.cache
def _app_modules() -> dict[str, _Module]:
    modules = {}
    for path in sorted(_APP_DIR.rglob("*.py")):
        if "migrations" in path.parts:
            continue
        rel = str(path.relative_to(_APP_DIR))
        modules[rel] = _Module(rel, ast.parse(path.read_text(encoding="utf-8")))
    return modules


def _lookup(rel: str, name: str, seen: frozenset = frozenset()) -> tuple | None:
    """What `name` is in module `rel`: ("class", rel, node), ("func", rel,
    node) or ("module", rel), following imports."""
    module = _app_modules()[rel]
    if name in module.classes:
        return ("class", rel, module.classes[name])
    if name in module.functions:
        return ("func", rel, module.functions[name])
    imported = module.imports.get(name)
    if imported is None or (rel, name) in seen:
        return None
    target, attribute = imported
    if attribute is None:
        return ("module", target)
    if target not in _app_modules():
        return None
    return _lookup(target, attribute, seen | {(rel, name)})


# A type is ("class", module, class name) or ("tuple", (types of each item)).
def _class_named(rel: str, node: ast.AST) -> tuple:
    found = None
    if isinstance(node, ast.Name):
        found = _lookup(rel, node.id)
    elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        owner = _lookup(rel, node.value.id)
        if owner is not None and owner[0] == "module" and owner[1] in _app_modules():
            found = _lookup(owner[1], node.attr)
    if found is not None and found[0] == "class":
        return (("class", found[1], found[2].name),)
    return ()


def _annotation(rel: str, node: ast.AST | None) -> tuple:
    if node is None:
        return ()
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            node = ast.parse(node.value, mode="eval").body
        except SyntaxError:
            return ()
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _annotation(rel, node.left) + _annotation(rel, node.right)
    if isinstance(node, ast.Subscript):
        head = _name_of(node.value)
        if head in {"tuple", "Tuple"} and isinstance(node.slice, ast.Tuple):
            return (("tuple", tuple(_annotation(rel, item) for item in node.slice.elts)),)
        return _annotation(rel, node.slice) if head == "Optional" else ()
    return _class_named(rel, node)


def _constructed(rel: str, node: ast.AST) -> tuple:
    if isinstance(node, ast.Await):
        node = node.value
    return _class_named(rel, node.func) if isinstance(node, ast.Call) else ()


def _returns(rel: str, fn: ast.AST) -> tuple:
    """The types a function returns: its annotation, else the constructors
    its return statements call."""
    declared = _annotation(rel, fn.returns)
    if declared:
        return declared
    found: tuple = ()
    for node in ast.walk(fn):
        if isinstance(node, ast.Return) and node.value is not None:
            if isinstance(node.value, ast.Tuple):
                found += (("tuple", tuple(_constructed(rel, item) for item in node.value.elts)),)
            else:
                found += _constructed(rel, node.value)
    return found


def _class(entry: tuple) -> ast.ClassDef:
    return _app_modules()[entry[1]].classes[entry[2]]


def _bases(entry: tuple) -> tuple:
    return tuple(t for base in _class(entry).bases for t in _class_named(entry[1], base))


def _is_protocol(entry: tuple) -> bool:
    return any(_name_of(base) == "Protocol" for base in _class(entry).bases) or any(
        _is_protocol(base) for base in _bases(entry)
    )


def _method(entry: tuple, name: str) -> tuple | None:
    for node in _class(entry).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return (entry[1], entry[2], node)
    for base in _bases(entry):
        found = _method(base, name)
        if found is not None:
            return found
    return None


def _method_names(entry: tuple) -> set[str]:
    names = {n.name for n in _class(entry).body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for base in _bases(entry):
        names |= _method_names(base)
    return names


@functools.cache
def _subclasses(entry: tuple) -> tuple:
    direct = tuple(
        ("class", rel, name)
        for rel, module in _app_modules().items()
        for name in module.classes
        if entry in _bases(("class", rel, name))
    )
    return direct + tuple(t for sub in direct for t in _subclasses(sub))


def _instances(types: tuple) -> tuple:
    """The classes an object of these types may be an instance of: for a
    protocol, every application class that has all its methods; for a class,
    itself and its subclasses."""
    found: tuple = ()
    for entry in types:
        if entry[0] != "class":
            continue
        if _is_protocol(entry):
            wanted = _method_names(entry)
            found += tuple(
                ("class", rel, cls)
                for rel, module in _app_modules().items()
                for cls in module.classes
                if not _is_protocol(("class", rel, cls)) and wanted <= _method_names(("class", rel, cls))
            )
        else:
            found += (entry, *_subclasses(entry))
    return tuple(dict.fromkeys(found))


def _dispatch(instances: tuple, name: str) -> list[tuple]:
    """(module, defining class, method, instances it runs for) for each
    method `name` runs on instances of these classes."""
    found: dict[int, tuple] = {}
    for entry in instances:
        method = _method(entry, name)
        if method is not None:
            rel, owner, fn, selves = found.get(id(method[2]), (*method, ()))
            found[id(method[2])] = (rel, owner, fn, (*selves, entry))
    return list(found.values())


def _attribute_types(entry: tuple, attribute: str) -> tuple:
    """The types `self.<attribute>` holds in a class: its annotations and
    what its methods assign, the constructors called or the annotated
    parameters passed."""
    rel, found = entry[1], ()
    for node in _class(entry).body:
        if isinstance(node, ast.AnnAssign) and _name_of(node.target) == attribute:
            found += _annotation(rel, node.annotation)
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        arguments = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
        parameters = {a.arg: _annotation(rel, a.annotation) for a in arguments}
        for statement in ast.walk(node):
            if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
                target, value, annotation = statement.targets[0], statement.value, None
            elif isinstance(statement, ast.AnnAssign):
                target, value, annotation = statement.target, statement.value, statement.annotation
            else:
                continue
            if not (isinstance(target, ast.Attribute) and _name_of(target.value) == "self"
                    and target.attr == attribute):
                continue
            found += _annotation(rel, annotation)
            for part in value.values if isinstance(value, ast.BoolOp) else [value]:
                if isinstance(part, ast.Name):
                    found += parameters.get(part.id, ())
                elif part is not None:
                    found += _constructed(rel, part)
    for base in _bases(entry):
        found += _attribute_types(base, attribute)
    return found


def _known(test: ast.AST, present: frozenset[str]) -> bool | None:
    """What an `if` test is when the names in `present` hold objects (not
    None, and true), if that settles it."""
    if isinstance(test, ast.Name):
        return True if test.id in present else None
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        inner = _known(test.operand, present)
        return None if inner is None else not inner
    if (isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.left, ast.Name)
            and test.left.id in present and isinstance(test.ops[0], (ast.Is, ast.IsNot))
            and isinstance(test.comparators[0], ast.Constant) and test.comparators[0].value is None):
        return isinstance(test.ops[0], ast.IsNot)
    if isinstance(test, ast.BoolOp):
        values = [_known(value, present) for value in test.values]
        settles, otherwise = (False, True) if isinstance(test.op, ast.And) else (True, False)
        if settles in values:
            return settles
        if all(value is otherwise for value in values):
            return otherwise
    return None


def _live(nodes: list, present: frozenset[str]):
    """The nodes in and under `nodes` that can run when the names in
    `present` hold objects: an `if` on them keeps only the branch they
    leave, and that branch ending in `return` or `raise` drops what follows
    it. A connection a caller passes in thus rules out the branch that takes
    one when none was passed."""
    for node in nodes:
        if isinstance(node, ast.If):
            known = _known(node.test, present)
            if known is not None:
                branch = node.body if known else node.orelse
                yield from _live(branch, present)
                if branch and isinstance(branch[-1], (ast.Return, ast.Raise)):
                    return
                continue
        yield node
        for _field, value in ast.iter_fields(node):
            if isinstance(value, list):
                yield from _live([item for item in value if isinstance(item, ast.AST)], present)
            elif isinstance(value, ast.AST):
                yield from _live([value], present)


class _Reader:
    """One function read under an admission: the classes `self` may be an
    instance of, the types its names hold, as its annotations, assignments
    and its caller's arguments tell, the names that hold objects (`present`:
    `with` targets, and parameters the caller passed one for), and the
    application functions each of its calls may run."""

    def __init__(
        self, rel: str, owner: str | None, selves: tuple, fn: ast.AST, bound: dict | None = None,
        passed: frozenset[str] = frozenset(),
    ) -> None:
        self.rel, self.owner, self.selves, self.fn = rel, owner, selves, fn
        self.present = passed | {
            item.optional_vars.id
            for node in ast.walk(fn) if isinstance(node, (ast.With, ast.AsyncWith))
            for item in node.items if isinstance(item.optional_vars, ast.Name)
        }
        arguments = fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs
        self.names: dict[str, tuple] = {a.arg: _annotation(rel, a.annotation) for a in arguments}
        self.names.update({name: types for name, types in (bound or {}).items() if types})
        # Twice: an assignment may use a name assigned further down.
        for _ in range(2):
            for node in ast.walk(fn):
                if isinstance(node, ast.Assign) and len(node.targets) == 1:
                    self._bind(node.targets[0], self.type_of(node.value))
                elif isinstance(node, ast.AnnAssign):
                    self._bind(node.target, _annotation(rel, node.annotation) or self.type_of(node.value))

    def _bind(self, target: ast.AST, types: tuple) -> None:
        if isinstance(target, ast.Name) and types:
            self.names[target.id] = types
        elif isinstance(target, ast.Tuple):
            for entry in types:
                if entry[0] == "tuple" and len(entry[1]) == len(target.elts):
                    for item, item_types in zip(target.elts, entry[1]):
                        self._bind(item, item_types)

    def type_of(self, node: ast.AST | None) -> tuple:
        if isinstance(node, ast.Await):
            return self.type_of(node.value)
        if isinstance(node, ast.Name):
            if node.id in {"self", "cls"}:
                return self.selves
            return self.names.get(node.id, ())
        if isinstance(node, ast.BoolOp):
            return tuple(t for value in node.values for t in self.type_of(value))
        if isinstance(node, ast.Attribute):
            return tuple(
                t for entry in self.type_of(node.value) if entry[0] == "class"
                for t in _attribute_types(entry, node.attr)
            )
        if isinstance(node, ast.Call):
            return _class_named(self.rel, node.func) or tuple(
                t for rel, _owner, fn, _selves in self.targets(node) for t in _returns(rel, fn)
            )
        return ()

    def targets(self, call: ast.Call) -> list[tuple]:
        """(module, defining class or None, function, instances `self` may be
        there) for each function the call may run."""
        func = call.func
        if isinstance(func, ast.Name):
            for node in ast.walk(self.fn):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func.id \
                        and node is not self.fn:
                    return [(self.rel, self.owner, node, self.selves)]
            found = _lookup(self.rel, func.id)
            return [(found[1], None, found[2], ())] if found is not None and found[0] == "func" else []
        if not isinstance(func, ast.Attribute):
            return []
        receiver = func.value
        if isinstance(receiver, ast.Name) and receiver.id in {"self", "cls"}:
            # Each class `self` may be runs the method its own lookup finds.
            return _dispatch(self.selves, func.attr)
        if isinstance(receiver, ast.Name) and receiver.id not in self.names:
            found = _lookup(self.rel, receiver.id)
            if found is not None and found[0] == "module":
                inner = _lookup(found[1], func.attr) if found[1] in _app_modules() else None
                return [(inner[1], None, inner[2], ())] if inner is not None and inner[0] == "func" else []
            if found is not None and found[0] == "class":
                return _dispatch(_instances((("class", found[1], found[2].name),)), func.attr)
        if isinstance(receiver, ast.Call) and _name_of(receiver.func) == "super" and self.owner:
            return [
                (rel, owner, fn, self.selves)
                for base in _bases(("class", self.rel, self.owner))
                for rel, owner, fn, _ in _dispatch((base,), func.attr)
            ]
        return _dispatch(_instances(self.type_of(receiver)), func.attr)

    def bound(self, call: ast.Call, fn: ast.AST) -> dict[str, tuple]:
        """The types the call passes for the function's parameters."""
        names = [a.arg for a in fn.args.posonlyargs + fn.args.args]
        if names and names[0] in {"self", "cls"}:
            names = names[1:]
        bound = {
            name: self.type_of(argument)
            for name, argument in zip(names, call.args) if not isinstance(argument, ast.Starred)
        }
        bound.update({kw.arg: self.type_of(kw.value) for kw in call.keywords if kw.arg is not None})
        return bound

    def passed(self, call: ast.Call, fn: ast.AST) -> frozenset[str]:
        """The function's parameters the call passes an object for: a name
        that holds one here, or a true constant."""
        names = [a.arg for a in fn.args.posonlyargs + fn.args.args]
        if names and names[0] in {"self", "cls"}:
            names = names[1:]

        def holds(argument: ast.AST) -> bool:
            return (isinstance(argument, ast.Name) and argument.id in self.present) or (
                isinstance(argument, ast.Constant) and bool(argument.value)
            )

        found = {
            name for name, argument in zip(names, call.args)
            if not isinstance(argument, ast.Starred) and holds(argument)
        }
        found |= {kw.arg for kw in call.keywords if kw.arg is not None and holds(kw.value)}
        return frozenset(found)


def _admission_roots():
    """Code that runs inside an admission entered without a connection: the
    methods `_ADMISSION_DECORATORS` decorate, and what follows an
    `_ADMISSION_CONTEXTS` item in a `with`; with the classes `self` may be
    an instance of there (the class and the subclasses that inherit the
    method)."""
    for rel, module in _app_modules().items():
        if rel == _HELPERS_MODULE:
            continue
        functions = [(None, (), fn) for fn in module.functions.values()]
        for cls in module.classes.values():
            entry = ("class", rel, cls.name)
            for fn in cls.body:
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    selves = tuple(
                        sub for sub in (entry, *_subclasses(entry))
                        if (method := _method(sub, fn.name)) is not None and method[2] is fn
                    )
                    functions.append((cls.name, selves, fn))
        for owner, selves, fn in functions:
            if any(_name_of(d) in _ADMISSION_DECORATORS for d in fn.decorator_list):
                yield rel, owner, selves, fn, fn.body, f"{rel}:{fn.lineno} @{fn.name}"
            for node in ast.walk(fn):
                if not isinstance(node, (ast.With, ast.AsyncWith)):
                    continue
                for index, item in enumerate(node.items):
                    if any(isinstance(c, ast.Call) and _name_of(c.func) in _ADMISSION_CONTEXTS
                           for c in ast.walk(item.context_expr)):
                        later = [i.context_expr for i in node.items[index + 1:]]
                        yield rel, owner, selves, fn, later + node.body, f"{rel}:{node.lineno} in {fn.name}"
                        break


def _connections_taken_under_admission() -> tuple[dict[str, str], set[str]]:
    """Each pool connection taken other than through the helpers by code that
    runs inside an admission entered without a connection, directly or
    through the application functions it calls, with a path that reaches it;
    and the functions reached."""
    taken: dict[str, str] = {}
    reached: set[str] = set()
    seen: set = set()

    def walk(reader: _Reader, nodes: list, path: str) -> None:
        for node in _live(nodes, reader.present):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in _POOL_METHODS \
                    and "pool" in ast.unparse(func.value).lower():
                taken.setdefault(f"{reader.rel}:{node.lineno}", path)
                continue
            for rel, owner, fn, selves in reader.targets(node):
                if rel == _HELPERS_MODULE:
                    continue
                bound = reader.bound(node, fn)
                passed = reader.passed(node, fn)
                key = (
                    rel, fn.lineno, selves, passed,
                    tuple(sorted((k, v) for k, v in bound.items() if v)),
                )
                if key in seen:
                    continue
                seen.add(key)
                name = f"{rel}:{owner + '.' if owner else ''}{fn.name}"
                reached.add(name)
                walk(_Reader(rel, owner, selves, fn, bound, passed), fn.body, f"{path} -> {name}")

    for rel, owner, selves, fn, nodes, where in _admission_roots():
        walk(_Reader(rel, owner, selves, fn), nodes, where)
    return taken, reached


def _with_statement_in_an_admitted_transaction(rel: str, function: str, statement: str) -> dict:
    """The application's modules, with `statement` added at the top of the
    first `admitted_connection` block of `function` in module `rel`."""
    tree = ast.parse((_APP_DIR / rel).read_text(encoding="utf-8"))
    fn = next(
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function
    )
    block = next(
        node for node in ast.walk(fn)
        if isinstance(node, ast.AsyncWith) and any(
            isinstance(call, ast.Call) and _name_of(call.func) == "admitted_connection"
            for item in node.items for call in ast.walk(item.context_expr)
        )
    )
    added = ast.parse(f"async def _():\n    {statement}\n").body[0].body
    for node in added:
        ast.increment_lineno(node, block.lineno - node.lineno)
    block.body[:0] = added
    modules = dict(_app_modules())
    modules[rel] = _Module(rel, tree)
    return modules


@pytest.mark.parametrize("statement, site", [
    # A second connection of the admitted transaction.
    ("async with pool.acquire() as extra: await extra.execute('SELECT 1')", None),
    # A helper that takes a connection when it is not given one.
    ("await write_policy_repo.get_policy(admitted_id)", "repositories/vault_write_policy_repo.py"),
])
def test_a_connection_taken_inside_an_admitted_transaction_fails_the_check(monkeypatch, statement, site):
    """`set_public_access` with a connection taken inside its admitted
    transaction other than through the transaction's own: a plain
    `pool.acquire()` there, or a helper called without the connection, which
    then takes one. The check names it."""
    rel = "services/access_service.py"
    modules = _with_statement_in_an_admitted_transaction(rel, "set_public_access", statement)
    monkeypatch.setattr(sys.modules[__name__], "_app_modules", lambda: modules)
    taken, _reached = _connections_taken_under_admission()
    flagged = [where for where, path in taken.items() if " in set_public_access" in path.split(" -> ")[0]]
    assert flagged, taken
    assert all(where.startswith(f"{site or rel}:") for where in flagged), flagged


def test_every_admitted_connection_is_given_back_before_its_admission():
    """A connection taken inside a Vault's admission goes back to the pool
    before the admission does. A transaction takes it with
    ``admitted_connection``; code that runs inside an admission entered
    without a connection takes each with ``acquire_given_back``: what a
    Native mutation admitted before it prepares its payload runs, and what
    Vault creation runs inside the new Vault's gate and admission, through
    the application functions they call, as far as annotations, assignments
    and arguments tell what those calls reach. A plain ``pool.acquire()``
    there, or one in the same ``async with`` as the admission, gave the
    admission back while a cancelled release still reset the connection.
    Inside an ``admitted_connection`` block the transaction passes its own
    connection on, and a helper given it does not take another: the walk
    leaves out the branch a passed connection rules out."""
    taken, reached = _connections_taken_under_admission()
    unreached = sorted(_MUST_REACH - reached)
    assert not unreached, f"The walk no longer reaches these, so it cannot check them: {unreached}"
    assert not taken, "Connections taken inside an admission without acquire_given_back:\n" + "\n".join(
        f"  {site}  (reached from {path})" for site, path in sorted(taken.items())
    )


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
