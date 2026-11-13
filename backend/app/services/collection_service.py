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

from app.services.role_authority import role_authority_transaction
from app.db.postgres import get_pool
from app.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.repositories import table_data_repo, table_registry_repo, vault_files_repo
from app.repositories.document_repo import (
    CollectionRepository,
    DocumentRepository,
    lock_collection_rows,
)
from app.repositories.events_repo import EventBuffer, emit_event
from app.repositories.vault_repo import VaultRepository, lock_vault_for_child_write
from app.services.git_service import GitService
from app.services.index_service import (
    delete_document_chunks, delete_file_chunks, delete_table_chunks,
)
from app.services.kg_service import delete_document_relations, delete_native_document_edges
from app.services.m1_file_measurement import _tombstone_native_text_file
from app.services.publication_service import delete_publications_for_file
from app.services.s3_delete_worker import enqueue_delete as _enqueue_s3_delete
from app.services import skill_policy
from app.services.uri_service import doc_uri, file_uri, table_uri
from app.services.vault_write_admission import vault_write_admission
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


class _CollectionsChanged(Exception):
    """A collection appeared under the prefix between reading and locking it."""


class _DocumentsRemain(Exception):
    """A live Native document is under the prefix when the collections go."""


# How many times a recursive delete deletes the prefix's Native documents and
# then tries its collections, when documents keep arriving meanwhile.
_NATIVE_DELETE_ROUNDS = 3

# The hint of a delete that left collections in place after a Native cutover.
RETAINED_COLLECTIONS_HINT = (
    "The collections in retained_collections keep their rows: documents preserved from before "
    "this Vault was cut over to the Native authority still reference them, and that history is "
    "kept as it is. They hold no live documents, and deleting them again leaves them in place."
)


def _under(path: str, prefix: str) -> bool:
    bare = prefix.rstrip("/")
    return path == bare or path.startswith(bare + "/")


async def _live_native_documents_under(conn, vault_id: uuid.UUID, prefix: str) -> list[dict]:
    bare = prefix.rstrip("/")
    rows = await conn.fetch(
        "SELECT resource_id, current_path AS path "
        "FROM native_resources "
        "WHERE namespace_id = $1 AND surface = 'document' "
        "AND lifecycle = 'live' "
        "AND (current_path = $2 OR current_path LIKE $3 ESCAPE '\\') "
        "ORDER BY current_path, resource_id",
        vault_id,
        bare,
        CollectionRepository._like_escape(bare) + "/%",
    )
    return [dict(r) for r in rows]


async def _delete_native_document(
    native, *, vault: str, vault_id: uuid.UUID, prefix: str, resource_id: uuid.UUID, actor: str,
) -> str | None:
    """Delete one live Native document under ``prefix``, with its event, and
    return the path it was deleted at.

    The delete, the document's graph edges and its ``document.delete`` commit
    together in one short transaction that locks only this document's Resource
    (and its path), so it never waits for a second Resource while it holds one.
    The edges go while the Resource is locked: a document restored afterwards
    starts without the links it had when its delete committed, and keeps the
    ones made after its restore (a link racing the delete is a known limit;
    see docs/operations/change-events.md). A document deleted, or moved out of
    the prefix, by
    another request since it was listed is left as it is, and None is
    returned.

    Its chunks are removed by the derived worker, from the invalidation intent
    the delete records; the worker locks the Resource and skips the intent once
    the Resource has a later Revision.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await native.repository.lock_resource(conn, resource_id)
            if (
                row is None
                or row["namespace_id"] != vault_id
                or row["surface"] != "document"
                or row["lifecycle"] != "live"
                or not _under(row["current_path"], prefix)
            ):
                return None
            events = EventBuffer()
            await native.delete_resource(
                namespace_id=vault_id,
                surface="document",
                path=row["current_path"],
                actor=actor,
                mutation_id=uuid.uuid4(),
                expected_revision_id=row["head_revision_id"],
                expected_resource_id=resource_id,
                retain_document_assets=False,
                message=f"[delete-collection] {prefix}\n\nagent: {actor}\naction: delete-collection",
                subject=f"[delete-collection] {prefix}",
                conn=conn,
                events=events,
            )
            # Keyed on the resource, and on the path read under its row lock.
            await delete_native_document_edges(conn, vault_id, vault, resource_id, row["current_path"])
            # The event is the transaction's last statement.
            await events.flush(conn)
    return row["current_path"]


async def _delete_native_documents_under(
    *, vault: str, vault_id: uuid.UUID, prefix: str, actor: str, deleted: list[str],
) -> None:
    """Delete the live Native documents under ``prefix`` one by one, appending
    the path of each one this call deletes to ``deleted``. A failure part-way
    leaves the documents deleted so far deleted, each with its event; running
    the delete again continues from the documents that remain."""
    from app.services.native_document_service import NativeDocumentService

    native = await NativeDocumentService()._native()
    pool = await get_pool()
    async with pool.acquire() as conn:
        listed = await _live_native_documents_under(conn, vault_id, prefix)
    for doc in listed:
        path = await _delete_native_document(
            native, vault=vault, vault_id=vault_id, prefix=prefix,
            resource_id=doc["resource_id"], actor=actor,
        )
        if path is not None:
            deleted.append(path)


def _tables_refused_part_way(
    refused: ForbiddenError, *, vault: str, prefix: str, deleted: list[str],
) -> ForbiddenError:
    """The table refusal of a recursive delete that already deleted documents.

    A caller who may not delete tables is refused before any document goes
    when the prefix holds a table. A table created under the prefix by
    another request after that check is found by the last transaction, which
    checks again under its locks, once the documents are deleted. They stay
    deleted, and the refusal says so, as a busy Vault's ``write_busy`` does:
    ``partially_applied`` and the deleted documents' URIs in its details. The
    same request from this caller is refused before deleting anything else;
    only a caller allowed to delete the table can finish the delete.
    """
    uris = [doc_uri(vault, path) for path in deleted]
    return ForbiddenError(
        f"{refused.message}; {len(uris)} document(s) under {prefix!r} were deleted "
        "before the table was found",
        code=refused.code,
        hint=(
            f"The documents in details.deleted_uris were deleted before a table appeared under "
            f"{prefix!r}, and stay deleted. Only a caller with the 'admin' role on the Vault can "
            "finish this delete: the same request from such a caller deletes the rest, the table "
            "included. Sent again without that role, it is refused before anything else is deleted."
        ),
        details={"partially_applied": True, "deleted_docs": len(uris), "deleted_uris": uris},
    )


async def _earlier_recursive_delete(conn, vault_id: uuid.UUID, prefix: str) -> bool:
    """Whether a recursive delete of ``prefix`` ever deleted a Native document.

    A recursive delete gives each Native document it deletes a Revision with
    the subject ``[delete-collection] <prefix>``. Revisions are kept, so this
    tells a prefix an earlier delete emptied, and that delete may have stopped
    before recording ``collection.delete``, from one that never held anything.
    """
    return bool(await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM native_revisions "
        "WHERE namespace_id = $1 AND action = 'delete' AND subject = $2)",
        vault_id, f"[delete-collection] {prefix}",
    ))


async def _legacy_documents_fenced(conn) -> bool:
    """Whether a Native cutover has fenced the `documents` rows.

    After an existing-database cutover the rows stay as immutable history: a
    trigger refuses every insert, update and delete of them, including the
    `SET NULL` that deleting a collection they reference would perform.
    """
    if await conn.fetchval("SELECT to_regclass('public.native_revision_legacy_write_fence')") is None:
        return False
    return bool(await conn.fetchval(
        "SELECT state <> 'open' FROM native_revision_legacy_write_fence WHERE fence_key = TRUE",
    ))


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
        async with vault_write_admission(vault_id, vault=vault), pool.acquire() as conn:
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
          under `P` — all sub-collection rows + all docs (one Git
          commit on Bare Git installations) + all files (s3 outbox) + the row at `P` if it
          exists. If the prefix contains a table, the caller must pass
          the admin-derived `allow_table_delete` capability. Returns
          `{ok, collection, deleted_docs, deleted_files,
          deleted_sub_collections, deleted_tables}`.

        On the Native authority the documents go first, one per short
        transaction with its `document.delete` event, and the collections,
        files and tables with `collection.delete` in a last transaction once
        no live document is left under `P`. A failure part-way leaves the
        documents deleted so far deleted and returns the error; running the
        same delete again continues with the rest. When nothing is left under
        `P` because an earlier recursive delete of `P` deleted its documents,
        the delete succeeds with zero counts instead of `NotFoundError` and
        records `collection.delete` (see `_earlier_recursive_delete`); the
        event is at least once, so it can be recorded more than once for one
        prefix. The counts are what this call deleted.

        Race-safety: the target row (if any) and all sub-collection
        rows are locked `FOR UPDATE` inside the same transaction, so a
        concurrent `akb_put` that calls
        `CollectionRepository.get_or_create` against any of those paths
        blocks until our TX commits. After we commit, the racer sees
        the rows gone and re-inserts with fresh ids — never reusing a
        doomed id, never leaving a doc pointing at a deleted
        collection. A sub-collection created under the prefix meanwhile
        is either deleted with the rest or left whole (see the lock step
        below).
        """
        norm = _normalize_path(path)
        skill_policy.check_collection_delete(norm)
        vault_repo, _ = await self._repos()
        vault_id = await vault_repo.get_id_by_name(vault)
        if not vault_id:
            raise NotFoundError("Vault", vault)
        # Native-authority emptiness check (#543): on `postgres_native` the
        # native path never writes `documents`, so a collection whose live
        # documents were all created after the cutover presents as empty to
        # the legacy repository — and `delete(recursive=False)` would succeed
        # instead of refusing with CollectionNotEmptyError. The native ledger
        # (same query shape as the browse view) is the authority there.
        # Module-level import (not function-local): tests patch this symbol
        # and #525's counter module is the single authority selector.
        from app.services import document_counters as _counters

        native_authority = _counters.native_documents_are_authoritative()
        native_deleted: list[str] = []
        try:
            for _ in range(_NATIVE_DELETE_ROUNDS):
                if native_authority and recursive:
                    if not allow_table_delete:
                        # Refuse before any document goes; the last
                        # transaction checks the tables again under its locks.
                        await self._refuse_tables_under(vault, vault_id, norm)
                    await _delete_native_documents_under(
                        vault=vault, vault_id=vault_id, prefix=norm,
                        actor=agent_id or "unknown", deleted=native_deleted,
                    )
                try:
                    return await self._delete_collections(
                        vault=vault, vault_id=vault_id, norm=norm, path=path,
                        recursive=recursive, agent_id=agent_id,
                        allow_table_delete=allow_table_delete,
                        native_authority=native_authority, native_deleted=len(native_deleted),
                    )
                except _DocumentsRemain:
                    continue
        except ForbiddenError as exc:
            # A table appeared under the prefix after some documents went.
            if native_deleted:
                raise _tables_refused_part_way(
                    exc, vault=vault, prefix=norm, deleted=native_deleted,
                ) from exc
            raise
        raise ConflictError(
            f"Documents kept arriving under {norm!r} during the delete; retry it"
        )

    async def _refuse_tables_under(self, vault: str, vault_id: uuid.UUID, norm: str) -> None:
        _, coll_repo = await self._repos()
        if await coll_repo.list_tables_under(vault_id, norm):
            raise ForbiddenError(
                "Deleting a collection that contains tables requires "
                f"'admin' role on vault '{vault}'"
            )

    async def _delete_collections(
        self,
        *,
        vault: str,
        vault_id: uuid.UUID,
        norm: str,
        path: str,
        recursive: bool,
        agent_id: str | None,
        allow_table_delete: bool,
        native_authority: bool,
        native_deleted: int,
    ) -> dict:
        """The delete's last transaction: the collections, files, tables and
        legacy documents under ``norm``, and the `collection.delete` event.
        Raises ``_DocumentsRemain`` when a live Native document is still under
        the prefix of a recursive delete."""
        _, coll_repo = await self._repos()
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
        retained_paths: list[str] = []

        # Admitted for the transaction only: the Git cleanup after it takes
        # the write lane, which an admitted transaction must not wait for.
        async with vault_write_admission(vault_id, vault=vault), pool.acquire() as conn:
            async with role_authority_transaction(conn):
                if not await lock_vault_for_child_write(conn, vault_id):
                    raise ConflictError("Vault was deleted during collection deletion")
                # ── Lock + snapshot under prefix ────────────────
                # The target row (absent in the nested-parent case) and every
                # sub-collection row strictly under `norm/` are locked
                # together, in id order like every other multi-collection
                # lock (lock_collection_rows): a document move between two of
                # them locks them in that order, and a parent-first order here
                # would deadlock with it. Locked rows keep a racing `akb_put`
                # from slipping a doc under one of them between our snapshot
                # and our cleanup. The set is read without a lock first; if it
                # grew before the lock, the attempt's savepoint is rolled
                # back, releasing its locks, and the set is read again, so the
                # locks are still taken in one id-ordered pass.
                #
                # A transaction that creates a sub-collection locks its
                # existing ancestors FOR KEY SHARE (CollectionRepository), so
                # with the target row locked a creation under it waits for
                # this delete, and one in progress makes the lock above wait
                # for its commit and then shows up in the second read. A
                # creation with no ancestor row to lock (no row at `norm`, or
                # an intermediate row created meanwhile) is caught by reading
                # the set once more after the documents, files and tables:
                # one that committed before those reads has its contents among
                # them, and deleting them while its row stays would leave an
                # emptied collection with a stale count. Its row then appears
                # in that read and the attempt starts over. One that commits
                # later is not among them and stays whole.
                under_prefix = (
                    "SELECT id, path FROM collections "
                    "WHERE vault_id = $1 AND (path = $2 OR path LIKE $3 ESCAPE '\\')"
                )
                prefix_pattern = CollectionRepository._like_escape(norm.rstrip("/")) + "/%"

                async def _all_locked(locked: set[uuid.UUID]) -> bool:
                    current = await conn.fetch(under_prefix, vault_id, norm, prefix_pattern)
                    return {r["id"] for r in current} <= locked

                for _ in range(3):
                    try:
                        async with conn.transaction():
                            candidates = await conn.fetch(under_prefix, vault_id, norm, prefix_pattern)
                            locked = await lock_collection_rows(
                                conn, [r["id"] for r in candidates], strength="update",
                            )
                            if not await _all_locked(locked):
                                raise _CollectionsChanged
                            legacy_docs = await coll_repo.list_docs_under(vault_id, norm, conn=conn)
                            # After a Native cutover the `documents` rows are
                            # immutable history: they are neither deleted nor
                            # counted, and a collection they reference keeps
                            # its row (deleting it would set theirs to NULL).
                            fenced = await _legacy_documents_fenced(conn)
                            preserved = legacy_docs if fenced else []
                            if fenced:
                                legacy_docs = []
                            native_docs = (
                                await _live_native_documents_under(conn, vault_id, norm)
                                if native_authority else []
                            )
                            if native_docs and recursive:
                                # The documents go one by one before this
                                # transaction; one written meanwhile is
                                # deleted in another round.
                                raise _DocumentsRemain
                            docs = legacy_docs + native_docs
                            files = await coll_repo.list_files_under(
                                vault_id, norm, conn=conn, for_update=True,
                            )
                            # Tables living in this collection (FK collection_id is
                            # ON DELETE SET NULL, so deleting the collection rows
                            # would otherwise silently re-home them to vault root and
                            # a table-only collection would pass the empty check).
                            tables = await coll_repo.list_tables_under(vault_id, norm, conn=conn)
                            if not await _all_locked(locked):
                                raise _CollectionsChanged
                    except _CollectionsChanged:
                        continue
                    break
                else:
                    raise ConflictError(
                        f"Collections under {norm!r} kept changing during the delete; retry it"
                    )
                target_row = next(
                    (dict(r) for r in candidates if r["path"] == norm and r["id"] in locked),
                    None,
                )
                sub_rows = sorted(
                    (dict(r) for r in candidates if r["path"] != norm and r["id"] in locked),
                    key=lambda r: (r["path"], r["id"]),
                )

                # ── Total-empty check ───────────────────────────
                # Nothing at or under the prefix → genuine 404, unless an
                # earlier recursive delete of this prefix deleted its Native
                # documents and stopped before this point: then the prefix is
                # deleted already, and this one completes it.
                if (
                    target_row is None
                    and not sub_rows
                    and not docs
                    and not files
                    and not tables
                    and not native_deleted
                ):
                    if not (native_authority and recursive):
                        raise NotFoundError("Collection", norm)
                    if not await _earlier_recursive_delete(conn, vault_id, norm):
                        raise NotFoundError("Collection", norm)
                    # `collection.delete` is at least once: it is recorded by
                    # every delete that finds the prefix empty, whether or
                    # not an earlier one recorded it already.
                    nothing = {
                        "deleted_docs": 0,
                        "deleted_files": 0,
                        "deleted_sub_collections": 0,
                        "deleted_tables": 0,
                    }
                    completion = EventBuffer()
                    completion.add(
                        "collection.delete",
                        vault_id=vault_id,
                        resource_uri=None,
                        actor_id=agent_id,
                        payload={"vault": vault, "path": norm, **nothing},
                    )
                    await completion.flush(conn)
                    logger.info("Collection delete: vault=%s path=%s already emptied", vault, norm)
                    return {"ok": True, "collection": norm, **nothing}

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
                #
                # Events are collected and inserted as the transaction's last
                # statements: inserting one takes the Vault's event lock, and
                # this transaction still has rows to lock and delete.
                events = EventBuffer()
                # Native-authority documents (#543) are deleted through the
                # native revision service before this transaction, one by
                # one (`_delete_native_documents_under`); none is left here.
                for d in legacy_docs:
                    await delete_document_chunks(conn, str(d["id"]))
                    await delete_document_relations(conn, vault, d["path"])
                    # The URI for the publication cleanup is derived from
                    # the row under its lock, NOT from `d["path"]` — this
                    # snapshot was taken without a document row lock
                    # (`list_docs_under`), so a concurrent move could have
                    # changed the path since. See the method's docstring.
                    deleted = await doc_repo.delete_with_publications(
                        conn, doc_id=d["id"], vault_id=vault_id,
                    )
                    if deleted:
                        from app.services.notification_producer import enqueue_document_change
                        await enqueue_document_change(
                            conn, "document.delete", source_key=f"collection-delete:{uuid.uuid4()}",
                            vault_id=vault_id, resource_id=d["id"], actor_username=agent_id,
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
                retained: set[uuid.UUID] = set()
                if preserved and ids_to_delete:
                    retained = {
                        r["collection_id"] for r in await conn.fetch(
                            "SELECT DISTINCT collection_id FROM documents "
                            "WHERE vault_id = $1 AND collection_id = ANY($2::uuid[])",
                            vault_id, ids_to_delete,
                        )
                    }
                    ids_to_delete = [cid for cid in ids_to_delete if cid not in retained]
                    sub_rows = [r for r in sub_rows if r["id"] not in retained]
                if ids_to_delete:
                    await conn.execute(
                        "DELETE FROM collections WHERE id = ANY($1::uuid[])",
                        ids_to_delete,
                    )

                # Only the documents this call deleted are counted; one that
                # was deleted or moved away meanwhile is not.
                docs_count = len(legacy_docs) + native_deleted
                payload = {
                    "vault": vault,
                    "path": norm,
                    "deleted_docs": docs_count,
                    "deleted_files": len(files),
                    "deleted_sub_collections": len(sub_rows),
                    "deleted_tables": len(tables),
                }
                retained_paths = sorted(r["path"] for r in candidates if r["id"] in retained)
                if retained_paths:
                    payload["retained_collections"] = retained_paths
                events.add(
                    "collection.delete",
                    vault_id=vault_id,
                    resource_uri=None,
                    actor_id=agent_id,
                    payload=payload,
                )
                await events.flush(conn)
                files_count = len(files)
                sub_count = len(sub_rows)
                tables_count = len(tables)

                # Only Bare Git installations own repository files. Native
                # authority must not enter the Git lane, even when historical
                # legacy catalog rows are included in the PG cleanup.
                doc_paths_for_git = [] if native_authority else [d["path"] for d in legacy_docs]
                if doc_paths_for_git:
                    commit_msg_for_git = (
                        f"[delete-collection] {norm}\n\n"
                        f"{len(legacy_docs)} docs, {len(files)} files\n"
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
        result: dict = {
            "ok": True,
            "collection": norm,
            "deleted_docs": docs_count,
            "deleted_files": files_count,
            "deleted_sub_collections": sub_count,
            "deleted_tables": tables_count,
        }
        if retained_paths:
            # After a Native cutover: these rows stay (see the retention step).
            result["retained_collections"] = retained_paths
            result["hint"] = RETAINED_COLLECTIONS_HINT
        return result
