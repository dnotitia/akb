"""Collection lifecycle: explicit create (and, in a follow-up task, delete).

Reuses `CollectionRepository.create_empty` for the metadata row and emits
a `collection.create` event so subscribers see the change. The event
INSERT is wrapped in `async with conn.transaction()` — same pattern as
`document_service` — so a rollback drops the event with the change.

Only the `create` method lives here for now. `delete` lands in the next
task; keeping this file tight makes the boundary between the two
operations obvious.
"""

from __future__ import annotations

import logging

from app.db.postgres import get_pool
from app.exceptions import NotFoundError
from app.repositories.document_repo import CollectionRepository
from app.repositories.events_repo import emit_event
from app.repositories.vault_repo import VaultRepository

logger = logging.getLogger("akb.collections")


class InvalidPathError(ValueError):
    """Raised when a collection path fails validation.

    Subclasses ValueError so legacy callers that only catch ValueError
    still work; new code should catch InvalidPathError directly.
    """


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
