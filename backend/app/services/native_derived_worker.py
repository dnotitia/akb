"""Measurement-only consumer for native searchable derived state.

The worker consumes durable native invalidation intents, but it never trusts an
intent as read authority: immediately before replacing chunks it locks and
rechecks the native Resource Head.  Chunks and their Revision mapping commit in
one PostgreSQL transaction; vector upsert/delete continues through AKB's
existing embed and delete workers.

Both admitted surfaces take this path.  Text Files used to be closed on an
explicit ``direct_grep`` delivery that produced nothing — a measurement
bookkeeping device that kept W3b from sitting permanently pending, and which
had the consequence that a text File could be grepped but never embedded (the
embed pipeline consumes ``chunks``, and File Revisions produced no chunks).
The frozen P0 specification requires searchable *and embeddable* text Files,
entering the chunk/index/embedding boundary "on the same Resource/Revision
basis as Documents", so the surfaces differ only in how a body is chunked and
which discriminator the derived rows carry.

Derived output is still never a Head and never an exact-grep oracle:
``M1NativeGrepService`` reads verified Head bytes and never touches ``chunks``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg

from app.services import delete_worker
from app.services._backfill import MAX_RETRIES, next_attempt_delay
from app.services.document_service import _parse_markdown
from app.services.index_service import (
    Chunk,
    build_doc_metadata_header,
    build_file_metadata_header,
    chunk_markdown,
    chunk_text_body,
)
from app.services.native_payload_verification import verify_native_head_body
from app.services.uri_service import file_uri

logger = logging.getLogger("akb.native_derived_worker")

NATIVE_DOCUMENT_SOURCE = "native_document"
NATIVE_FILE_SOURCE = "native_file"
# One delivery name for both surfaces: `selected_delivery` records the delivery
# *mechanism*, and after parity there is literally one — the same code path,
# the same `chunks` + `native_derived_chunks` + `native_derived_heads` rows,
# the same invalidation contract. The surface is not delivery identity; it
# stays recoverable by joining `native_resources`.
SELECTED_DELIVERY = "native-searchable-derived-v1"
# Historical only. Text File intents closed under this delivery before the
# document-parity path existed. Nothing produces it now; it stays defined (and
# `pending_stats` keeps counting it) so pre-parity rows can still be read and
# reported instead of being rewritten out of the ledger.
DIRECT_GREP_DELIVERY = "native-direct-pg-grep-v1"

_SOURCE_TYPE_BY_SURFACE = {
    "document": NATIVE_DOCUMENT_SOURCE,
    "file": NATIVE_FILE_SOURCE,
}


def source_type_for_surface(surface: str) -> str:
    """Map an admitted native surface to its derived chunk discriminator."""
    try:
        return _SOURCE_TYPE_BY_SURFACE[surface]
    except KeyError:
        raise ValueError(f"unsupported native derived surface: {surface}") from None


def build_native_document_chunks(
    *,
    vault_name: str,
    path: str,
    canonical_text: str,
) -> list[Chunk]:
    """Build the real AKB chunk representation from one verified native body."""
    metadata, body = _parse_markdown(canonical_text)
    if not body.strip():
        return []
    title = str(metadata.get("title") or path.rsplit("/", 1)[-1])
    tags = metadata.get("tags")
    header = build_doc_metadata_header(
        vault_name=vault_name,
        path=path,
        title=title,
        summary=metadata.get("summary"),
        tags=list(tags) if isinstance(tags, list) else [],
        doc_type=metadata.get("type") or "note",
    )
    return chunk_markdown(body, metadata_header=header)


def build_native_file_chunks(
    *,
    vault_name: str,
    path: str,
    resource_id: uuid.UUID,
    canonical_text: str,
) -> list[Chunk]:
    """Build the real AKB chunk representation from one verified File body.

    A text File has no frontmatter to strip and no markdown structure to trust,
    so the whole verified body is chunked on size alone. The header carries
    File addressing (``akb://…/file/<uuid>``), not a Document path.
    """
    if not canonical_text.strip():
        return []
    collection = path.rsplit("/", 1)[0] if "/" in path else None
    header = build_file_metadata_header(
        vault_name=vault_name,
        path=path,
        uri=file_uri(vault_name, str(resource_id), collection=collection),
        size_bytes=len(canonical_text.encode("utf-8")),
    )
    return chunk_text_body(canonical_text, metadata_header=header)


class NativeDerivedWorker:
    """One-batch native invalidation consumer with durable retry state."""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def _claim_one(self) -> dict | None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Every Head mutation publishes an intent in the authority
                # transaction. Older pending intents for the same Resource can
                # therefore be closed without materializing stale revisions.
                await conn.execute(
                    """
                    WITH ranked AS (
                        SELECT i.intent_id, r.surface,
                               row_number() OVER (
                                   PARTITION BY i.resource_id
                                   ORDER BY i.occurred_at DESC, i.intent_id DESC
                               ) AS position
                          FROM native_invalidation_intents i
                          JOIN native_resources r ON r.resource_id = i.resource_id
                         WHERE i.completed_at IS NULL
                    )
                    UPDATE native_invalidation_intents i
                       SET completed_at = NOW(),
                           delivery_outcome = 'superseded',
                           selected_delivery = $1,
                           retry_count = 0,
                           claimed_at = NULL,
                           next_attempt_at = NULL,
                           last_error = NULL
                      FROM ranked r
                     WHERE i.intent_id = r.intent_id AND r.position > 1
                    """,
                    SELECTED_DELIVERY,
                )
                row = await conn.fetchrow(
                    """
                    SELECT i.intent_id, i.namespace_id, i.resource_id,
                           i.revision_id, i.reason, i.retry_count, r.surface
                      FROM native_invalidation_intents i
                      JOIN native_resources r ON r.resource_id = i.resource_id
                     WHERE i.completed_at IS NULL
                       AND (i.next_attempt_at IS NULL OR i.next_attempt_at <= NOW())
                       AND i.retry_count < $1
                       AND r.surface IN ('document', 'file')
                     ORDER BY i.occurred_at, i.intent_id
                     LIMIT 1
                     FOR UPDATE OF i SKIP LOCKED
                    """,
                    MAX_RETRIES,
                )
                if row is None:
                    return None
                claimed = await conn.fetchrow(
                    """
                    UPDATE native_invalidation_intents
                       SET claimed_at = NOW(),
                           next_attempt_at = NOW() + INTERVAL '10 minutes',
                           retry_count = retry_count + 1,
                           selected_delivery = $2
                     WHERE intent_id = $1
                    RETURNING retry_count, claimed_at
                    """,
                    row["intent_id"],
                    SELECTED_DELIVERY,
                )
                intent = dict(row)
                intent.update(dict(claimed))
                return intent

    async def _complete(
        self,
        intent_id: uuid.UUID,
        outcome: str,
        *,
        selected_delivery: str = SELECTED_DELIVERY,
    ) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE native_invalidation_intents
                   SET completed_at = NOW(), delivery_outcome = $2,
                       selected_delivery = $3,
                       retry_count = 0, claimed_at = NULL,
                       next_attempt_at = NULL, last_error = NULL
                 WHERE intent_id = $1
                """,
                intent_id,
                outcome,
                selected_delivery,
            )

    async def _failure(self, intent: dict, error: Exception) -> None:
        attempt_count = int(intent["retry_count"])
        delay = next_attempt_delay(max(0, attempt_count - 1))
        next_at = datetime.now(UTC) + timedelta(seconds=delay)
        async with self.pool.acquire() as conn:
            if attempt_count >= MAX_RETRIES:
                await conn.execute(
                    """
                    UPDATE native_invalidation_intents
                       SET claimed_at = NULL,
                           completed_at = NOW(), delivery_outcome = 'abandoned',
                           next_attempt_at = NULL, last_error = $2
                     WHERE intent_id = $1 AND completed_at IS NULL
                    """,
                    intent["intent_id"],
                    type(error).__name__,
                )
            else:
                await conn.execute(
                    """
                    UPDATE native_invalidation_intents
                       SET claimed_at = NULL,
                           next_attempt_at = $2, last_error = $3
                     WHERE intent_id = $1 AND completed_at IS NULL
                    """,
                    intent["intent_id"],
                    next_at,
                    type(error).__name__,
                )

    async def _head(self, resource_id: uuid.UUID) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT r.namespace_id, r.resource_id, r.lifecycle, r.current_path,
                       r.head_revision_id, v.name AS vault_name,
                       pm.digest, pm.byte_size, pm.encoding,
                       pm.selected_placement, pm.verification_profile,
                       p.payload_id, p.content_profile, p.canonical_bytes
                  FROM native_resources r
                  JOIN vaults v ON v.id = r.namespace_id
                  LEFT JOIN native_revisions nr
                    ON nr.resource_id = r.resource_id
                   AND nr.revision_id = r.head_revision_id
                  LEFT JOIN native_payload_manifests pm
                    ON pm.payload_manifest_id = nr.payload_manifest_id
                  LEFT JOIN m1_reference_payloads p
                    ON p.payload_id = pm.private_locator
                 WHERE r.resource_id = $1
                """,
                resource_id,
            )
        return dict(row) if row is not None else None

    async def _drop_chunks(self, conn, resource_id: uuid.UUID, source_type: str) -> None:
        # Outbox first, in the caller's transaction: the chunk ids must reach
        # `vector_delete_outbox` before `chunks` forgets them, or the derived
        # vector points outlive the Revision that produced them. Identical for
        # both surfaces — only the discriminator differs.
        await delete_worker.enqueue_source_deletes(
            source_type,
            str(resource_id),
            conn=conn,
        )
        await conn.execute(
            "DELETE FROM chunks WHERE source_type = $1 AND source_id = $2",
            source_type,
            resource_id,
        )

    async def _apply_delete(self, intent: dict) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                resource = await conn.fetchrow(
                    "SELECT lifecycle, head_revision_id FROM native_resources WHERE resource_id = $1 FOR UPDATE",
                    intent["resource_id"],
                )
                if resource is None or resource["head_revision_id"] != intent["revision_id"]:
                    await conn.execute(
                        """
                        UPDATE native_invalidation_intents
                           SET completed_at = NOW(), delivery_outcome = 'superseded',
                               next_attempt_at = NULL, last_error = NULL
                         WHERE intent_id = $1
                        """,
                        intent["intent_id"],
                    )
                    return
                if resource["lifecycle"] != "deleted":
                    await conn.execute(
                        """
                        UPDATE native_invalidation_intents
                           SET completed_at = NOW(), delivery_outcome = 'superseded',
                               next_attempt_at = NULL, last_error = NULL
                         WHERE intent_id = $1
                        """,
                        intent["intent_id"],
                    )
                    return
                await self._drop_chunks(
                    conn,
                    intent["resource_id"],
                    source_type_for_surface(intent["surface"]),
                )
                await conn.execute(
                    "DELETE FROM native_derived_heads WHERE resource_id = $1",
                    intent["resource_id"],
                )
                await conn.execute(
                    """
                    UPDATE native_invalidation_intents
                       SET completed_at = NOW(), delivery_outcome = 'deleted',
                           next_attempt_at = NULL, last_error = NULL
                     WHERE intent_id = $1
                    """,
                    intent["intent_id"],
                )

    async def _apply_live(self, intent: dict, head: dict) -> int:
        source_type = source_type_for_surface(intent["surface"])

        def prepare() -> tuple[str, list[Chunk]]:
            canonical = verify_native_head_body(head)
            canonical_text = canonical.decode("utf-8", errors="strict")
            if intent["surface"] == "file":
                chunks = build_native_file_chunks(
                    vault_name=head["vault_name"],
                    path=head["current_path"],
                    resource_id=head["resource_id"],
                    canonical_text=canonical_text,
                )
            else:
                chunks = build_native_document_chunks(
                    vault_name=head["vault_name"],
                    path=head["current_path"],
                    canonical_text=canonical_text,
                )
            return hashlib.sha256(canonical).hexdigest(), chunks

        digest, chunks = await asyncio.to_thread(
            prepare,
        )
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                resource = await conn.fetchrow(
                    """
                    SELECT lifecycle, head_revision_id, current_path
                      FROM native_resources
                     WHERE resource_id = $1 FOR UPDATE
                    """,
                    intent["resource_id"],
                )
                if (
                    resource is None
                    or resource["lifecycle"] != "live"
                    or resource["head_revision_id"] != intent["revision_id"]
                ):
                    await conn.execute(
                        """
                        UPDATE native_invalidation_intents
                           SET completed_at = NOW(), delivery_outcome = 'superseded',
                               next_attempt_at = NULL, last_error = NULL
                         WHERE intent_id = $1
                        """,
                        intent["intent_id"],
                    )
                    return 0
                await self._drop_chunks(conn, intent["resource_id"], source_type)
                await conn.execute(
                    """
                    INSERT INTO native_derived_heads (
                        resource_id, namespace_id, revision_id, intent_id,
                        path, content_digest, chunk_count, settled_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
                    ON CONFLICT (resource_id) DO UPDATE SET
                        namespace_id = EXCLUDED.namespace_id,
                        revision_id = EXCLUDED.revision_id,
                        intent_id = EXCLUDED.intent_id,
                        path = EXCLUDED.path,
                        content_digest = EXCLUDED.content_digest,
                        chunk_count = EXCLUDED.chunk_count,
                        settled_at = NOW()
                    """,
                    intent["resource_id"],
                    intent["namespace_id"],
                    intent["revision_id"],
                    intent["intent_id"],
                    resource["current_path"],
                    digest,
                    len(chunks),
                )
                for chunk in chunks:
                    chunk_id = uuid.uuid4()
                    await conn.execute(
                        """
                        INSERT INTO chunks (
                            id, source_type, source_id, vault_id, section_path,
                            content, chunk_index, char_start, char_end
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                        """,
                        chunk_id,
                        source_type,
                        intent["resource_id"],
                        intent["namespace_id"],
                        chunk.section_path,
                        chunk.content,
                        chunk.chunk_index,
                        chunk.char_start,
                        chunk.char_end,
                    )
                    await conn.execute(
                        """
                        INSERT INTO native_derived_chunks (
                            chunk_id, namespace_id, resource_id, revision_id, intent_id
                        ) VALUES ($1, $2, $3, $4, $5)
                        """,
                        chunk_id,
                        intent["namespace_id"],
                        intent["resource_id"],
                        intent["revision_id"],
                        intent["intent_id"],
                    )
                await conn.execute(
                    """
                    UPDATE native_invalidation_intents
                       SET completed_at = NOW(), delivery_outcome = 'applied',
                           next_attempt_at = NULL, last_error = NULL
                     WHERE intent_id = $1
                    """,
                    intent["intent_id"],
                )
        return len(chunks)

    async def process_once(self) -> int:
        intent = await self._claim_one()
        if intent is None:
            return 0
        try:
            head = await self._head(intent["resource_id"])
            if head is None or head["head_revision_id"] != intent["revision_id"]:
                await self._complete(intent["intent_id"], "superseded")
                return 1
            if head["lifecycle"] == "deleted":
                await self._apply_delete(intent)
            else:
                await self._apply_live(intent, head)
            return 1
        except Exception as exc:
            await self._failure(intent, exc)
            logger.warning("native derived delivery failed: %s", type(exc).__name__)
            return 0

    async def pending_stats(self, namespace_id: uuid.UUID | None = None) -> dict[str, int]:
        params: list[object] = []
        where = ""
        if namespace_id is not None:
            params.append(namespace_id)
            where = "AND namespace_id = $1"
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT COUNT(*) FILTER (WHERE completed_at IS NULL)::int AS pending,
                       COUNT(*) FILTER (WHERE delivery_outcome = 'applied')::int AS applied,
                       COUNT(*) FILTER (WHERE delivery_outcome = 'superseded')::int AS superseded,
                       COUNT(*) FILTER (WHERE delivery_outcome = 'deleted')::int AS deleted,
                       -- Pre-parity rows only; nothing produces 'direct_grep'
                       -- since text Files took the document-parity path. The
                       -- counter stays so history is reported, not rewritten.
                       COUNT(*) FILTER (WHERE delivery_outcome = 'direct_grep')::int AS direct_grep,
                       COUNT(*) FILTER (WHERE delivery_outcome = 'abandoned')::int AS abandoned,
                       COUNT(*) FILTER (
                           WHERE completed_at IS NULL AND retry_count > 0
                       )::int AS retrying
                  FROM native_invalidation_intents
                 WHERE TRUE {where}
                """,
                *params,
            )
        return dict(row)

    async def settle(
        self,
        *,
        namespace_id: uuid.UUID,
        timeout_seconds: float,
        poll_interval_seconds: float = 0.05,
    ) -> dict[str, int | float]:
        started = asyncio.get_running_loop().time()
        polls = 0
        while True:
            polls += 1
            stats = await self.pending_stats(namespace_id)
            if stats["pending"] == 0:
                return {**stats, "polls": polls, "elapsed_seconds": asyncio.get_running_loop().time() - started}
            if asyncio.get_running_loop().time() - started >= timeout_seconds:
                raise TimeoutError("native derived settlement timed out")
            await self.process_once()
            await asyncio.sleep(poll_interval_seconds)
