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
    """Raised by `CollectionService.delete` when the target collection
    still has documents or files and the caller did not pass
    `recursive=True`.

    Carries `doc_count` and `file_count` so the HTTP layer can surface
    them in a structured 409 response (see Task 5).
    """

    def __init__(self, doc_count: int, file_count: int):
        super().__init__(
            f"Collection has {doc_count} documents and {file_count} files"
        )
        self.doc_count = doc_count
        self.file_count = file_count


_MAX_PATH_BYTES = 1024


def _normalize_path(path: str) -> str:
    """Normalize and validate a collection path.

    Strips surrounding whitespace and leading/trailing slashes, then
    inspects each remaining segment. The rules mirror
    `_normalize_collection` in `document_service` (no empty / `.` / `..`
    segments, no control characters) so collection paths produced here
    are interchangeable with those a `put` call would generate.
    """
    if not isinstance(path, str):
        raise InvalidPathError("path must be a string")
    s = path.strip().strip("/")
    if not s:
        raise InvalidPathError("path is empty")
    if len(s.encode("utf-8")) > _MAX_PATH_BYTES:
        raise InvalidPathError("path is too long")
    for seg in s.split("/"):
        if seg in ("", ".", ".."):
            raise InvalidPathError(f"invalid path segment: {seg!r}")
        if any(ord(ch) < 32 for ch in seg):
            raise InvalidPathError("control characters not allowed")
    return s


class CollectionService:
    def __init__(self) -> None:
        # `git` is lazy: the GitService constructor calls `mkdir` on
        # `git_storage_path`, which is `/data/vaults` in production but
        # may not exist on test hosts. Holding it as a property means a
        # unit test that swaps `self.git = <fake>` before any delete
        # call never triggers the filesystem touch.
        self._git: GitService | None = None

    @property
    def git(self) -> GitService:
        if self._git is None:
            self._git = GitService()
        return self._git

    @git.setter
    def git(self, value: GitService) -> None:
        self._git = value

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
        """Delete a collection, optionally cascading over docs + files.

        Algorithm:

        1. Normalize the path; resolve the vault.
        2. Open a short snapshot TX that `SELECT … FOR UPDATE`s the
           `collections` row and reads every doc + file under it. If
           the collection is non-empty and `recursive=False`, raise
           `CollectionNotEmptyError`. The TX commits at the end of the
           `async with` block — the row is still in place at that point;
           the snapshot we carry forward is the docs/files list.
        3. **Git first**, OUTSIDE any PG TX, mirroring the ordering in
           `document_service.delete`. One bulk commit per delete.
           Files are S3-only, so only document paths go to git.
        4. Single PG cleanup TX:
             • per doc: chunks + edges + DELETE FROM documents
             • per file: edges + chunks + s3_delete_outbox + vault_files
             • DELETE FROM collections (the row we locked in step 2)
             • emit `collection.delete`

        Race-safety: between step 2's TX commit and step 4 a concurrent
        `akb_put` may re-create the row via `get_or_create` — that's
        the documented behavior (see spec § Race safety). Step 4's
        final `DELETE FROM collections WHERE id = $cid` removes the
        snapshot's id specifically, so a re-created sibling with a
        fresh id keeps living.
        """
        norm = _normalize_path(path)
        vault_repo, coll_repo = await self._repos()
        vault_id = await vault_repo.get_id_by_name(vault)
        if not vault_id:
            raise NotFoundError("Vault", vault)

        pool = await get_pool()

        # ── 1. Lock + snapshot ──────────────────────────────────
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT id FROM collections "
                    "WHERE vault_id = $1 AND path = $2 FOR UPDATE",
                    vault_id, norm,
                )
                if row is None:
                    raise NotFoundError("Collection", norm)
                collection_id = row["id"]
                docs = await coll_repo.list_docs_under(vault_id, norm, conn=conn)
                files = await coll_repo.list_files_under(vault_id, norm, conn=conn)
                if (docs or files) and not recursive:
                    raise CollectionNotEmptyError(len(docs), len(files))

        # ── 2. Git first (outside PG TX) ────────────────────────
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
                # The bare repo doesn't exist (test fixtures, fresh
                # vault with no commits yet, etc.) — fall through to
                # DB-only cleanup, same idempotency stance as
                # `document_service.delete`.
                logger.warning(
                    "Vault %s has no git repo — proceeding with DB-only cleanup",
                    vault,
                )

        # ── 3. PG cleanup TX ────────────────────────────────────
        async with pool.acquire() as conn:
            async with conn.transaction():
                for d in docs:
                    await delete_document_chunks(conn, str(d["id"]))
                    await delete_document_relations(conn, vault, d["path"])
                    await conn.execute(
                        "DELETE FROM documents WHERE id = $1", d["id"],
                    )

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

                await conn.execute(
                    "DELETE FROM collections WHERE id = $1", collection_id,
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
                    },
                )

        logger.info(
            "Collection delete: vault=%s path=%s docs=%d files=%d",
            vault, norm, len(docs), len(files),
        )
        return {
            "ok": True,
            "collection": norm,
            "deleted_docs": len(docs),
            "deleted_files": len(files),
        }
