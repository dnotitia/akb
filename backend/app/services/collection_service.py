"""Collection lifecycle: explicit create + delete.

`create` is idempotent and emits `collection.create`. `delete` removes
the row outright (with an optional recursive cascade over docs + files)
and emits `collection.delete`. Anything that would mutate git happens
*outside* the PG cleanup transaction — same ordering as
`document_service.delete`.
"""

from __future__ import annotations

import logging
import uuid

import asyncpg

from app.db.postgres import get_pool
from app.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.repositories import table_data_repo, table_registry_repo, vault_files_repo
from app.repositories.document_repo import CollectionRepository, DocumentRepository
from app.repositories.events_repo import emit_event
from app.repositories.vault_repo import VaultRepository, lock_vault_for_child_write
from app.services.git_service import GitService
from app.services.index_service import (
    delete_document_chunks, delete_file_chunks, delete_table_chunks,
)
from app.services.kg_service import delete_document_relations
from app.services.m1_file_measurement import _tombstone_native_text_file
from app.services.publication_service import delete_publications_for_file
from app.services.s3_delete_worker import enqueue_delete as _enqueue_s3_delete
from app.services import skill_policy
from app.services.uri_service import file_uri, table_uri
from app.services.write_lane import run_git_write, write_lane

logger = logging.getLogger("akb.collections")


class InvalidPathError(ValueError):
    """Raised when a collection path fails validation.

    Subclasses ValueError so legacy callers that only catch ValueError
    still work; new code should catch InvalidPathError directly.
    """


class CollectionNotEmptyError(Exception):
    """Raised by `CollectionService.delete` when the target path still
    has docs, files, or sub-collections under it (prefix semantics) and
    the caller did not pass `recursive=True`.

    Carries `doc_count`, `file_count`, and `sub_collection_count` so the
    HTTP layer can surface them in a structured 409 response (see
    Task 5). `sub_collection_count` covers the nested-parent case: a
    user with only `test/test` who deletes `test` (no row at `test`)
    will see this exception with `sub_collection_count=1`.
    """

    def __init__(
        self,
        doc_count: int,
        file_count: int,
        sub_collection_count: int = 0,
        table_count: int = 0,
    ):
        parts: list[str] = []
        if doc_count:
            parts.append(f"{doc_count} document(s)")
        if file_count:
            parts.append(f"{file_count} file(s)")
        if sub_collection_count:
            parts.append(f"{sub_collection_count} sub-collection(s)")
        if table_count:
            parts.append(f"{table_count} table(s)")
        super().__init__(
            f"Collection has {', '.join(parts) or 'content'}"
        )
        self.doc_count = doc_count
        self.file_count = file_count
        self.sub_collection_count = sub_collection_count
        self.table_count = table_count


def _normalize_path(path: str) -> str:
    """Validate a non-empty collection path via the canonical normalizer
    in `app.util.text`. Wraps the generic `ValueError` into the
    domain-specific `InvalidPathError` so the HTTP layer can map it to
    a 400 without leaking implementation details. Empty input is
    rejected here (collection-management endpoints demand a named
    target) while service callers that treat empty as "vault root"
    keep using the helper with `allow_empty=True`.
    """
    from app.util.text import normalize_collection_path
    try:
        return normalize_collection_path(path, allow_empty=False)
    except ValueError as exc:
        raise InvalidPathError(str(exc)) from exc


class CollectionService:
    def __init__(self, *, git: GitService | None = None) -> None:
        # Constructor injection mirrors `DocumentService` / `ExternalGitService`.
        # Held lazily so a test that passes `git=<fake>` avoids the
        # `GitService()` ctor's `mkdir` on `/data/vaults` — important on
        # hosts where that path is read-only or absent.
        self._git: GitService | None = git

    @property
    def git(self) -> GitService:
        if self._git is None:
            self._git = GitService()
        return self._git

    async def _repos(self) -> tuple[VaultRepository, CollectionRepository]:
        pool = await get_pool()
        return VaultRepository(pool), CollectionRepository(pool)

    async def create(
        self,
        *,
        vault: str,
        path: str,
        summary: str | None,
        agent_id: str | None,
    ) -> dict:
        """Idempotently create a collection row and emit `collection.create`.

        Returns the canonical envelope used by the MCP layer. `created`
        distinguishes a fresh insert from a no-op so the caller can
        decide whether to surface the event externally.
        """
        norm = _normalize_path(path)
        # On the NORMALIZED path, same as the delete guard, so `/overview/`
        # and `overview` are one case. The vault-skill seed does not come
        # through here (it goes through put() → coll_repo.get_or_create), so
        # this cannot block vault creation.
        skill_policy.check_collection_create(norm)
        vault_repo, coll_repo = await self._repos()
        vault_id = await vault_repo.get_id_by_name(vault)
        if not vault_id:
            raise NotFoundError("Vault", vault)

        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                if not await lock_vault_for_child_write(conn, vault_id):
                    raise ConflictError("Vault was deleted during collection creation")
                # Return the current row state on an idempotent no-op, and keep
                # the row plus its event in one transaction.
                (
                    _cid, created, name, cur_summary, cur_doc_count,
                ) = await coll_repo.create_empty(
                    vault_id, norm, summary=summary, conn=conn,
                )
                # Collections are not URI-addressable resources (the URI
                # scheme is doc/table/file only), so resource_uri stays
                # None. Subscribers reconstruct identity from payload.path.
                await emit_event(
                    conn,
                    "collection.create",
                    vault_id=vault_id,
                    resource_uri=None,
                    actor_id=agent_id,
                    payload={"vault": vault, "path": norm, "created": created},
                )

        logger.info(
            "Collection create: vault=%s path=%s created=%s", vault, norm, created
        )
        return {
            "ok": True,
            "created": created,
            "collection": {
                "path": norm,
                "name": name,
                "summary": cur_summary,
                "doc_count": cur_doc_count,
            },
        }

    async def delete(
        self,
        *,
        vault: str,
        path: str,
        recursive: bool,
        agent_id: str | None,
        allow_table_delete: bool = False,
    ) -> dict:
        """Delete a collection using **prefix semantics** over the path.

        The supplied path `P` is treated as a prefix: it matches the
        exact row at `P` (if one exists) plus every collection row,
        document, and file under `P/`. This fixes the nested-parent
        delete case where the user has, e.g., only `test/test` but the
        client tree synthesizes a `test` parent: deleting `test` must
        find the `test/test` sub-collection (no row at `test`) and
        cascade properly when `recursive=True`.

        Contract:

        - Truly empty under `P` (no row at `P`, no sub-collection rows
          under `P`, no docs, no files) → `NotFoundError`.
        - Empty mode (`recursive=False`): succeed only if the row at
          `P` exists AND there are zero sub-collections, docs, or
          files. Otherwise `CollectionNotEmptyError(doc_count,
          file_count, sub_collection_count)`.
        - Cascade mode (`recursive=True`): delete everything at and
          under `P` — all sub-collection rows + all docs (one git
          commit) + all files (s3 outbox) + the row at `P` if it
          exists. If the prefix contains a table, the caller must pass
          the admin-derived `allow_table_delete` capability. Returns
          `{ok, collection, deleted_docs, deleted_files,
          deleted_sub_collections, deleted_tables}`.

        Race-safety: the target row (if any) and all sub-collection
        rows are locked `FOR UPDATE` inside the same transaction, so a
        concurrent `akb_put` that calls
        `CollectionRepository.get_or_create` against any of those paths
        blocks until our TX commits. After we commit, the racer sees
        the rows gone and re-inserts with fresh ids — never reusing a
        doomed id, never leaving a doc pointing at a deleted
        collection.
        """
        norm = _normalize_path(path)
        skill_policy.check_collection_delete(norm)
        vault_repo, coll_repo = await self._repos()
        vault_id = await vault_repo.get_id_by_name(vault)
        if not vault_id:
            raise NotFoundError("Vault", vault)

        pool = await get_pool()
        doc_repo = DocumentRepository(pool)
        docs_count = 0
        files_count = 0
        sub_count = 0
        tables_count = 0
        # Snapshot for the post-commit git cleanup. Captured inside
        # the TX (so the doc paths reflect exactly the rows we
        # deleted) and consumed after commit so the git call never
        # runs while we hold FOR UPDATE row locks. A crash between
        # commit and the git call leaves the worktree carrying files
        # for already-deleted DB rows — an operator can reconcile via
        # `git rm` and the chunks/embeddings are already gone, so
        # search is correct either way.
        doc_paths_for_git: list[str] = []
        commit_msg_for_git: str = ""

        async with pool.acquire() as conn:
            async with conn.transaction():
                if not await lock_vault_for_child_write(conn, vault_id):
                    raise ConflictError("Vault was deleted during collection deletion")
                # ── Lock + snapshot under prefix ────────────────
                # Target row may or may not exist (nested-parent
                # case). FOR UPDATE on a missing row is a no-op; we
                # only lock if the row is there.
                target_row = await conn.fetchrow(
                    "SELECT id FROM collections "
                    "WHERE vault_id = $1 AND path = $2 FOR UPDATE",
                    vault_id, norm,
                )

                # Sub-collection rows (strictly under `norm/`). We lock
                # them with a separate `FOR UPDATE` listing so a racing
                # `akb_put` can't slip a doc under one of them between
                # our snapshot and our cleanup.
                sub_rows_locked = await conn.fetch(
                    "SELECT id, path FROM collections "
                    "WHERE vault_id = $1 AND path LIKE $2 ESCAPE '\\' "
                    "FOR UPDATE",
                    vault_id,
                    CollectionRepository._like_escape(norm.rstrip("/")) + "/%",
                )
                sub_rows = [dict(r) for r in sub_rows_locked]

                docs = await coll_repo.list_docs_under(vault_id, norm, conn=conn)
                files = await coll_repo.list_files_under(vault_id, norm, conn=conn)
                # Tables living in this collection (FK collection_id is
                # ON DELETE SET NULL, so deleting the collection rows
                # would otherwise silently re-home them to vault root and
                # a table-only collection would pass the empty check).
                tables = await coll_repo.list_tables_under(vault_id, norm, conn=conn)

                # ── Total-empty check ───────────────────────────
                # Nothing at or under the prefix → genuine 404.
                if (
                    target_row is None
                    and not sub_rows
                    and not docs
                    and not files
                    and not tables
                ):
                    raise NotFoundError("Collection", norm)

                # ── Empty-mode reject ───────────────────────────
                # `recursive=False` succeeds ONLY when the target row
                # exists and nothing else lives under the prefix.
                if not recursive and (sub_rows or docs or files or tables):
                    raise CollectionNotEmptyError(
                        len(docs), len(files), len(sub_rows), len(tables),
                    )

                # A table DROP is admin-only on the dedicated table endpoint.
                # Enforce the same boundary here *after* taking the collection
                # snapshot and *before* any mutation so a writer cannot bypass
                # it by recursively deleting the containing collection. The
                # capability is resolved by the authenticated boundary; this
                # service never trusts a client-supplied role string.
                if recursive and tables and not allow_table_delete:
                    raise ForbiddenError(
                        "Deleting a collection that contains tables requires "
                        f"'admin' role on vault '{vault}'"
                    )

                # ── PG cleanup (same TX, locks still held) ──────
                # Git happens AFTER the TX commits — see the
                # comment above. Doing it here would mean we hold
                # FOR UPDATE row locks across `delete_paths_bulk`,
                # which acquires the per-vault threading lock and
                # blocks on multi-second worktree I/O. Under load
                # that pins connection-pool slots and starves
                # concurrent writers across the entire vault.

                # The row delete goes through the repository chokepoint, which
                # carries the publication cascade with it. Migration 058's FK
                # cascades only publications that carry a `document_id`, so a
                # legacy row the backfill left NULL still needs the explicit
                # cleanup — and this loop, deleting the rows by hand, is where that was
                # first forgotten: the orphaned publication's slug still
                # resolved by path, and since `documents` is
                # UNIQUE(vault_id, path), the next document created (or moved)
                # onto that path would be reached through the old public
                # link.
                for d in docs:
                    await delete_document_chunks(conn, str(d["id"]))
                    await delete_document_relations(conn, vault, d["path"])
                    # The URI for the publication cleanup is derived from
                    # the row under its lock, NOT from `d["path"]` — this
                    # snapshot was taken without a document row lock
                    # (`list_docs_under`), so a concurrent move could have
                    # changed the path since. See the method's docstring.
                    await doc_repo.delete_with_publications(
                        conn, doc_id=d["id"], vault_id=vault_id,
                    )

                # Per-file cost: edges + chunk outbox + s3 outbox +
                # vault_files row delete = ~4 round-trips. Acceptable
                # for typical collection sizes; for >1k files consider
                # batching (and Task 5 should soft-cap doc+file count
                # in the HTTP handler).
                for f in files:
                    file_id = str(f["id"])
                    storage_driver = f.get("storage_driver")
                    native_text = await _tombstone_native_text_file(
                        conn,
                        storage_driver=storage_driver,
                        vault_id=f["vault_id"],
                        native_resource_id=f.get("native_resource_id"),
                        native_revision_id=f.get("native_revision_id"),
                        collection=f.get("collection"),
                        name=f["name"],
                        actor_id=agent_id or "unknown",
                    )
                    # Canonical URI for the file, including its
                    # collection prefix — `f["collection"]` comes from
                    # the JOIN in `list_files_under` and is the path
                    # this file lives under (always non-empty here:
                    # we are iterating files at-or-under a known
                    # collection).
                    f_uri = file_uri(vault, file_id, collection=f.get("collection"))
                    await conn.execute(
                        "DELETE FROM edges WHERE source_uri = $1 OR target_uri = $1",
                        f_uri,
                    )
                    # Same cascade gap as the document loop above. A file
                    # URI carries a UUID, so a stale row here cannot be reoccupied
                    # onto a new resource — but it stays live in the
                    # owner's publication list pointing at nothing. Must
                    # run BEFORE `vault_files_repo.delete`: the helper
                    # re-reads the file's collection to build the
                    # canonical URI and sees no row once it is gone.
                    await delete_publications_for_file(
                        file_id, vault, expected_vault_id=vault_id, conn=conn,
                    )
                    try:
                        await delete_file_chunks(conn, file_id)
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "file chunk delete failed for %s: %s", file_id, e,
                        )
                    # A NULL driver is the legacy S3 path. Native text owns no
                    # binary object, while immutable FS/S3 CAS bytes follow the
                    # same shared-retention policy as direct File deletion.
                    # Never enqueue the synthetic measurement `s3_key` into the
                    # legacy delete worker.
                    if storage_driver is None:
                        await _enqueue_s3_delete(conn, f["s3_key"])
                    elif not native_text and storage_driver not in {"fscas", "s3cas"}:
                        raise RuntimeError(
                            f"unsupported File storage driver during collection delete: {storage_driver}"
                        )
                    await vault_files_repo.delete(conn, uuid.UUID(file_id))

                # Tear down tables BEFORE the collection-row DELETE so
                # the FK SET NULL never fires on a still-registered table.
                # Mirror drop_table's internals (dynamic PG table + chunk
                # outbox + registry row + edges) inside this TX so the
                # table either disappears completely or not at all.
                for t in tables:
                    pg_name = table_data_repo.pg_table_name(vault, t["name"])
                    try:
                        await table_data_repo.drop_dynamic_table(conn, pg_name)
                    except asyncpg.DependentObjectsStillExistError as e:
                        raise ConflictError(
                            f"Cannot delete collection {path!r}: table {t['name']!r} "
                            "is referenced by another vault table. Drop dependent "
                            "tables or columns first."
                        ) from e
                    await delete_table_chunks(conn, str(t["id"]))
                    await table_registry_repo.delete(conn, t["id"])
                    t_uri = table_uri(vault, t["name"], collection=t.get("collection"))
                    await conn.execute(
                        "DELETE FROM edges WHERE source_uri = $1 OR target_uri = $1",
                        t_uri,
                    )

                # Delete the union of sub-collection ids and the
                # target row (if it exists). Empty-mode success has
                # no sub_rows and reaches here only when target_row
                # is non-None and nothing else lives under it.
                ids_to_delete: list[uuid.UUID] = [r["id"] for r in sub_rows]
                if target_row is not None:
                    ids_to_delete.append(target_row["id"])
                if ids_to_delete:
                    await conn.execute(
                        "DELETE FROM collections WHERE id = ANY($1::uuid[])",
                        ids_to_delete,
                    )

                await emit_event(
                    conn,
                    "collection.delete",
                    vault_id=vault_id,
                    resource_uri=None,
                    actor_id=agent_id,
                    payload={
                        "vault": vault,
                        "path": norm,
                        "deleted_docs": len(docs),
                        "deleted_files": len(files),
                        "deleted_sub_collections": len(sub_rows),
                        "deleted_tables": len(tables),
                    },
                )
                docs_count = len(docs)
                files_count = len(files)
                sub_count = len(sub_rows)
                tables_count = len(tables)

                # Snapshot for post-commit git work.
                doc_paths_for_git = [d["path"] for d in docs]
                if doc_paths_for_git:
                    commit_msg_for_git = (
                        f"[delete-collection] {norm}\n\n"
                        f"{len(docs)} docs, {len(files)} files\n"
                        f"agent: {agent_id or 'unknown'}\n"
                        f"action: delete-collection"
                    )

        # ── Git cleanup AFTER the TX commits ────────────────────
        # PG is the source of truth and is now consistent. Any
        # failure here leaves orphan files in the worktree; the
        # chunks/embeddings are already gone so search results stay
        # correct. Logged loudly so an operator can reconcile.
        if doc_paths_for_git:
            try:
                # Write-lane gate + commit executor: this commit contends on
                # the vault git lock like any writer, and must never wait for
                # it inside a shared executor thread. It holds no PG
                # connection here (TX already committed), so a lane timeout
                # (WriteBusyError) simply falls into the operator-reconcile
                # path below — the API call still succeeds.
                async with write_lane(vault):
                    await run_git_write(
                        self.git.delete_paths_bulk,
                        vault_name=vault,
                        file_paths=doc_paths_for_git,
                        message=commit_msg_for_git,
                    )
            except FileNotFoundError:
                # No bare repo (test fixtures, fresh vault) — DB is
                # already consistent, nothing to clean up.
                logger.warning(
                    "Vault %s has no git repo — DB-only cleanup completed",
                    vault,
                )
            except Exception as e:  # noqa: BLE001
                # PG already committed the deletes; surface the git
                # failure for operator reconciliation but don't roll
                # back (we can't).
                logger.error(
                    "post-commit git cleanup failed for vault=%s path=%s "
                    "(%d docs orphaned in worktree, DB is consistent): %s",
                    vault, norm, len(doc_paths_for_git), e,
                )

        logger.info(
            "Collection delete: vault=%s path=%s docs=%d files=%d sub=%d tables=%d",
            vault, norm, docs_count, files_count, sub_count, tables_count,
        )
        return {
            "ok": True,
            "collection": norm,
            "deleted_docs": docs_count,
            "deleted_files": files_count,
            "deleted_sub_collections": sub_count,
            "deleted_tables": tables_count,
        }
