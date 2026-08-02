"""Measurement-only consumer for native searchable-document derived state.

The worker consumes durable native invalidation intents, but it never trusts an
intent as read authority: immediately before replacing chunks it locks and
rechecks the native Resource Head.  Chunks and their Revision mapping commit in
one PostgreSQL transaction; vector upsert/delete continues through AKB's
existing embed and delete workers.
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
from app.services.index_service import Chunk, build_doc_metadata_header, chunk_markdown
from app.services.m1_pg_body_store import M1PgBodyStore

logger = logging.getLogger("akb.native_derived_worker")

NATIVE_DOCUMENT_SOURCE = "native_document"
SELECTED_DELIVERY = "native-searchable-derived-v1"
DIRECT_GREP_DELIVERY = "native-direct-pg-grep-v1"


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
                           selected_delivery = CASE
                               WHEN r.surface = 'file' THEN $2 ELSE $1
                           END,
                           last_error = NULL
                      FROM ranked r
                     WHERE i.intent_id = r.intent_id AND r.position > 1
                    """,
                    SELECTED_DELIVERY,
                    DIRECT_GREP_DELIVERY,
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
                await conn.execute(
                    """
                    UPDATE native_invalidation_intents
                       SET claimed_at = NOW(),
                           next_attempt_at = NOW() + INTERVAL '10 minutes',
                           selected_delivery = CASE
                               WHEN $3 = 'file' THEN $4 ELSE $2
                           END
                     WHERE intent_id = $1
                    """,
                    row["intent_id"],
                    SELECTED_DELIVERY,
                    row["surface"],
                    DIRECT_GREP_DELIVERY,
                )
                return dict(row)

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
                       next_attempt_at = NULL, last_error = NULL
                 WHERE intent_id = $1
                """,
                intent_id,
                outcome,
                selected_delivery,
            )

    async def _failure(self, intent: dict, error: Exception) -> None:
        delay = next_attempt_delay(int(intent["retry_count"]))
        next_at = datetime.now(UTC) + timedelta(seconds=delay)
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE native_invalidation_intents
                   SET claimed_at = NULL, retry_count = retry_count + 1,
                       next_attempt_at = $2, last_error = $3
                 WHERE intent_id = $1 AND completed_at IS NULL
                """,
                intent["intent_id"],
                next_at,
                f"{type(error).__name__}: {error}"[:500],
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

    async def _drop_chunks(self, conn, resource_id: uuid.UUID) -> None:
        await delete_worker.enqueue_source_deletes(
            NATIVE_DOCUMENT_SOURCE,
            str(resource_id),
            conn=conn,
        )
        await conn.execute(
            "DELETE FROM chunks WHERE source_type = $1 AND source_id = $2",
            NATIVE_DOCUMENT_SOURCE,
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
                await self._drop_chunks(conn, intent["resource_id"])
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
        canonical = M1PgBodyStore._verify_row(head)
        digest = hashlib.sha256(canonical).hexdigest()
        chunks = build_native_document_chunks(
            vault_name=head["vault_name"],
            path=head["current_path"],
            canonical_text=canonical.decode("utf-8", errors="strict"),
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
                await self._drop_chunks(conn, intent["resource_id"])
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
                        NATIVE_DOCUMENT_SOURCE,
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
            if intent["surface"] == "file":
                # Searchable text Files are read directly from the verified
                # current Head by M1NativeGrepService; no chunk/vector copy is
                # created. Closing the intent records that explicit delivery
                # choice instead of leaving W3b permanently pending.
                await self._complete(
                    intent["intent_id"],
                    "direct_grep",
                    selected_delivery=DIRECT_GREP_DELIVERY,
                )
                return 1
            if head["lifecycle"] == "deleted":
                await self._apply_delete(intent)
            else:
                await self._apply_live(intent, head)
            return 1
        except Exception as exc:
            await self._failure(intent, exc)
            logger.warning("native derived intent %s failed: %s", intent["intent_id"], exc)
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
                       COUNT(*) FILTER (WHERE delivery_outcome = 'direct_grep')::int AS direct_grep,
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
