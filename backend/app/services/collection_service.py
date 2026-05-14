"""Collection lifecycle: explicit create + delete.

`create` is idempotent and emits `collection.create`. `delete` removes
the row outright (with an optional recursive cascade over docs + files)
and emits `collection.delete`. Anything that would mutate git happens
*outside* the PG cleanup transaction — same ordering as
`document_service.delete`.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from app.db.postgres import get_pool
from app.exceptions import NotFoundError
from app.repositories import vault_files_repo
from app.repositories.document_repo import CollectionRepository
from app.repositories.events_repo import emit_event
from app.repositories.vault_repo import VaultRepository
from app.services.git_service import GitService
from app.services.index_service import delete_document_chunks, delete_file_chunks
from app.services.kg_service import delete_document_relations
from app.services.s3_delete_worker import enqueue_delete as _enqueue_s3_delete

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
    ):
        parts: list[str] = []
        if doc_count:
            parts.append(f"{doc_count} document(s)")
        if file_count:
            parts.append(f"{file_count} file(s)")
        if sub_collection_count:
            parts.append(f"{sub_collection_count} sub-collection(s)")
        super().__init__(
            f"Collection has {', '.join(parts) or 'content'}"
        )
        self.doc_count = doc_count
        self.file_count = file_count
        self.sub_collection_count = sub_collection_count


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
        vault_repo, coll_repo = await self._repos()
        vault_id = await vault_repo.get_id_by_name(vault)
        if not vault_id:
            raise NotFoundError("Vault", vault)

        # `create_empty` returns the *current* row state, not the
        # caller's inputs. On a no-op (created=False) the stored
        # summary / doc_count win — the contract is "report what's in
        # the DB," so an idempotent re-create against an existing
        # collection with 5 docs surfaces doc_count=5, not 0.
        _cid, created, name, cur_summary, cur_doc_count = await coll_repo.create_empty(
            vault_id, norm, summary=summary,
        )

        # emit_event MUST run inside the same transaction as its
        # domain row so the event is dropped on rollback. The repo
        # insert above is already committed (it owns its own
        # transaction), so this transaction protects only the event
        # write — fine, since the event is the only remaining side
        # effect and the rule we're enforcing is "no event without a
        # successful domain write," which holds either way.
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await emit_event(
                    conn,
                    "collection.create",
                    vault_id=vault_id,
                    ref_type="collection",
                    ref_id=norm,
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
          exists. Returns `{ok, collection, deleted_docs,
          deleted_files, deleted_sub_collections}`.

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
        vault_repo, coll_repo = await self._repos()
        vault_id = await vault_repo.get_id_by_name(vault)
        if not vault_id:
            raise NotFoundError("Vault", vault)

        pool = await get_pool()
        docs_count = 0
        files_count = 0
        sub_count = 0

        async with pool.acquire() as conn:
            async with conn.transaction():
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

                # ── Total-empty check ───────────────────────────
                # Nothing at or under the prefix → genuine 404.
                if (
                    target_row is None
                    and not sub_rows
                    and not docs
                    and not files
                ):
                    raise NotFoundError("Collection", norm)

                # ── Empty-mode reject ───────────────────────────
                # `recursive=False` succeeds ONLY when the target row
                # exists and nothing else lives under the prefix.
                if not recursive and (sub_rows or docs or files):
                    raise CollectionNotEmptyError(
                        len(docs), len(files), len(sub_rows),
                    )

                # ── Git first (cascade only; empty-mode has no docs)
                doc_paths = [d["path"] for d in docs]
                if doc_paths:
                    commit_msg = (
                        f"[delete-collection] {norm}\n\n"
                        f"{len(docs)} docs, {len(files)} files\n"
                        f"agent: {agent_id or 'unknown'}\n"
                        f"action: delete-collection"
                    )
                    try:
                        await asyncio.to_thread(
                            self.git.delete_paths_bulk,
                            vault_name=vault,
                            file_paths=doc_paths,
                            message=commit_msg,
                        )
                    except FileNotFoundError:
                        # No bare repo (test fixtures, fresh vault) —
                        # same idempotency stance as
                        # `document_service.delete`: fall through to
                        # DB-only cleanup.
                        logger.warning(
                            "Vault %s has no git repo — proceeding with "
                            "DB-only cleanup",
                            vault,
                        )

                # ── PG cleanup (same TX, locks still held) ──────
                for d in docs:
                    await delete_document_chunks(conn, str(d["id"]))
                    await delete_document_relations(conn, vault, d["path"])
                    await conn.execute(
                        "DELETE FROM documents WHERE id = $1", d["id"],
                    )

                # Per-file cost: edges + chunk outbox + s3 outbox +
                # vault_files row delete = ~4 round-trips. Acceptable
                # for typical collection sizes; for >1k files consider
                # batching (and Task 5 should soft-cap doc+file count
                # in the HTTP handler).
                for f in files:
                    file_id = str(f["id"])
                    f_uri = f"akb://{vault}/file/{file_id}"
                    await conn.execute(
                        "DELETE FROM edges WHERE source_uri = $1 OR target_uri = $1",
                        f_uri,
                    )
                    try:
                        await delete_file_chunks(conn, file_id)
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "file chunk delete failed for %s: %s", file_id, e,
                        )
                    await _enqueue_s3_delete(conn, f["s3_key"])
                    await vault_files_repo.delete(conn, uuid.UUID(file_id))

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
                    ref_type="collection",
                    ref_id=norm,
                    actor_id=agent_id,
                    payload={
                        "vault": vault,
                        "path": norm,
                        "deleted_docs": len(docs),
                        "deleted_files": len(files),
                        "deleted_sub_collections": len(sub_rows),
                    },
                )
                docs_count = len(docs)
                files_count = len(files)
                sub_count = len(sub_rows)

        logger.info(
            "Collection delete: vault=%s path=%s docs=%d files=%d sub=%d",
            vault, norm, docs_count, files_count, sub_count,
        )
        return {
            "ok": True,
            "collection": norm,
            "deleted_docs": docs_count,
            "deleted_files": files_count,
            "deleted_sub_collections": sub_count,
        }
