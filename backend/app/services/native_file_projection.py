"""Eventual Native text projection for ordinary S3-backed File mutations.

The File catalogue and S3 remain the public authority.  A row written in the
same PostgreSQL transaction as each confirmed File mutation makes the additive
Native searchable projection crash-resumable without pretending S3 and the
Native ledger share one transaction.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg

from app.config import settings
from app.db.postgres import get_pool
from app.services._backfill import MAX_RETRIES, next_attempt_delay
from app.services.adapters import s3_adapter
from app.services.m1_pg_body_store import M1PgBodyStore, M1_PG_TEXT_MAX_BYTES
from app.services.native_revision_service import NativeRevisionService

logger = logging.getLogger("akb.native_file_projection")

_CLAIM_LEASE = timedelta(minutes=10)
_MUTATION_NAMESPACE = uuid.UUID("3be5871b-8618-48e1-9c68-66db495e9703")


def _logical_path(*, collection: str | None, name: str) -> str:
    return f"{collection}/{name}" if collection else name


def is_native_text_file(*, mime_type: str, byte_size: int) -> bool:
    """Return whether a confirmed File may have an additive text projection."""
    return mime_type.lower().startswith("text/") and 0 <= byte_size <= M1_PG_TEXT_MAX_BYTES


def validate_native_text_file(payload: bytes, *, digest: str, byte_size: int) -> str:
    """Verify the exact S3 bytes admitted to the Native text surface."""
    if len(payload) != byte_size:
        raise ValueError("File byte size changed before Native projection")
    if hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError("File digest changed before Native projection")
    if len(payload) > M1_PG_TEXT_MAX_BYTES or b"\x00" in payload:
        raise ValueError("File is not eligible Native searchable text")
    try:
        return payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("File is not valid UTF-8 Native searchable text") from exc


async def _pending_stats(
    pool: asyncpg.Pool,
    namespace_id: uuid.UUID | None = None,
) -> dict[str, int | str]:
    """Return the durable projection queue state without inferring freshness.

    ``exhausted`` is deliberately separate from terminal ``abandoned``: it is
    the final claimed attempt while its lease is still in force.  That makes a
    process death at the retry boundary visible before ``queue_rescuer`` turns
    it into the terminal, operator-requeueable state.
    """
    params: list[object] = [MAX_RETRIES]
    scope = ""
    if namespace_id is not None:
        params.append(namespace_id)
        scope = "AND namespace_id = $2"
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            SELECT COUNT(*) FILTER (WHERE completed_at IS NULL)::int AS pending,
                   COUNT(*) FILTER (
                       WHERE completed_at IS NULL
                         AND retry_count > 0
                         AND retry_count < $1
                   )::int AS retrying,
                   COUNT(*) FILTER (
                       WHERE completed_at IS NULL
                         AND retry_count >= $1
                   )::int AS exhausted,
                   COUNT(*) FILTER (
                       WHERE completed_at IS NOT NULL
                         AND outcome = 'abandoned'
                   )::int AS abandoned
              FROM native_file_projection_outbox
             WHERE TRUE {scope}
            """,
            *params,
        )
    pending = int(row["pending"])
    exhausted = int(row["exhausted"])
    abandoned = int(row["abandoned"])
    return {
        "pending": pending,
        "retrying": int(row["retrying"]),
        "exhausted": exhausted,
        "abandoned": abandoned,
        # A requeued intent is still awaiting an authority-checked worker pass;
        # only an empty durable queue is actually healthy/fresh.
        "status": "degraded" if exhausted or abandoned else "reconciling" if pending else "ok",
    }


async def pending_stats(namespace_id: uuid.UUID | None = None) -> dict[str, int | str]:
    """Operator-facing projection state for global and vault health surfaces."""
    return await _pending_stats(await get_pool(), namespace_id)


async def _requeue_abandoned(
    pool: asyncpg.Pool,
    *,
    namespace_id: uuid.UUID,
    file_id: uuid.UUID | None = None,
) -> int:
    """Re-open only terminal intents in one explicit namespace.

    This does not read or mutate S3, catalogue rows, or Native resources.  It
    preserves the intent identity so the worker's existing idempotent mutation
    IDs protect re-entry, and lets the normal worker re-check the authoritative
    File catalogue and S3 bytes before it projects anything.
    """
    params: list[object] = [namespace_id]
    scope = ""
    if file_id is not None:
        params.append(file_id)
        scope = "AND file_id = $2"
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            UPDATE native_file_projection_outbox
               SET claimed_at = NULL, retry_count = 0, next_attempt_at = NOW(),
                   completed_at = NULL, outcome = NULL, last_error = NULL
             WHERE namespace_id = $1
               AND completed_at IS NOT NULL
               AND outcome = 'abandoned'
               {scope}
            RETURNING file_id
            """,
            *params,
        )
    return len(rows)


async def requeue_abandoned(
    *,
    namespace_id: uuid.UUID,
    file_id: uuid.UUID | None = None,
) -> int:
    """Operator entry point for idempotently requeueing abandoned intents.

    An explicit namespace is required.  Supplying a file ID narrows recovery
    to one File; omitting it reconciles only the selected vault's terminal
    projection intents.
    """
    return await _requeue_abandoned(
        await get_pool(), namespace_id=namespace_id, file_id=file_id,
    )


async def enqueue_native_file_projection(
    conn: asyncpg.Connection,
    *,
    file_id: uuid.UUID,
    namespace_id: uuid.UUID,
    collection: str | None,
    name: str,
    mime_type: str,
    content_hash: str,
    byte_size: int,
    s3_key: str,
    actor: str,
) -> uuid.UUID | None:
    """Record one confirmed File desired state in its catalogue transaction."""
    if settings.document_revision_backend != "postgres_native":
        return None
    intent_id = uuid.uuid4()
    await conn.execute(
        """
        INSERT INTO native_file_projection_outbox (
            file_id, intent_id, namespace_id, source_present, logical_path,
            mime_type, content_hash, byte_size, s3_key, actor
        )
        VALUES ($1, $2, $3, TRUE, $4, $5, $6, $7, $8, $9)
        ON CONFLICT (file_id) DO UPDATE
            SET intent_id = EXCLUDED.intent_id,
                namespace_id = EXCLUDED.namespace_id,
                source_present = TRUE,
                logical_path = EXCLUDED.logical_path,
                mime_type = EXCLUDED.mime_type,
                content_hash = EXCLUDED.content_hash,
                byte_size = EXCLUDED.byte_size,
                s3_key = EXCLUDED.s3_key,
                actor = EXCLUDED.actor,
                generation = native_file_projection_outbox.generation + 1,
                created_at = NOW(), claimed_at = NULL, retry_count = 0,
                next_attempt_at = NULL, completed_at = NULL,
                outcome = NULL, last_error = NULL
        """,
        file_id,
        intent_id,
        namespace_id,
        _logical_path(collection=collection, name=name),
        mime_type or "application/octet-stream",
        content_hash,
        byte_size,
        s3_key,
        actor,
    )
    return intent_id


async def enqueue_native_file_projection_delete(
    conn: asyncpg.Connection,
    *,
    file_id: uuid.UUID,
    namespace_id: uuid.UUID,
    collection: str | None,
    name: str,
    actor: str,
) -> uuid.UUID | None:
    """Record the absence owed after an ordinary File delete."""
    if settings.document_revision_backend != "postgres_native":
        return None
    intent_id = uuid.uuid4()
    await conn.execute(
        """
        INSERT INTO native_file_projection_outbox (
            file_id, intent_id, namespace_id, source_present, logical_path, actor
        )
        VALUES ($1, $2, $3, FALSE, $4, $5)
        ON CONFLICT (file_id) DO UPDATE
            SET intent_id = EXCLUDED.intent_id,
                namespace_id = EXCLUDED.namespace_id,
                source_present = FALSE,
                logical_path = EXCLUDED.logical_path,
                mime_type = NULL, content_hash = NULL, byte_size = NULL, s3_key = NULL,
                actor = EXCLUDED.actor,
                generation = native_file_projection_outbox.generation + 1,
                created_at = NOW(), claimed_at = NULL, retry_count = 0,
                next_attempt_at = NULL, completed_at = NULL,
                outcome = NULL, last_error = NULL
        """,
        file_id,
        intent_id,
        namespace_id,
        _logical_path(collection=collection, name=name),
        actor,
    )
    return intent_id


class NativeFileProjectionWorker:
    """Consume one durable File desired-state row per embed-worker pass."""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self.native = NativeRevisionService(pool, payload_store=M1PgBodyStore(pool))

    async def pending_stats(self, namespace_id: uuid.UUID | None = None) -> dict[str, int | str]:
        """Return queue diagnostics using this worker's pool (tests/operators)."""
        return await _pending_stats(self.pool, namespace_id)

    async def requeue_abandoned(
        self,
        *,
        namespace_id: uuid.UUID,
        file_id: uuid.UUID | None = None,
    ) -> int:
        """Idempotently requeue terminal intents through the normal worker path."""
        return await _requeue_abandoned(
            self.pool, namespace_id=namespace_id, file_id=file_id,
        )

    async def _claim_one(self) -> dict | None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT *
                      FROM native_file_projection_outbox
                     WHERE completed_at IS NULL
                       AND retry_count < $1
                       AND (next_attempt_at IS NULL OR next_attempt_at <= NOW())
                       AND (claimed_at IS NULL OR claimed_at < NOW() - INTERVAL '10 minutes')
                     ORDER BY created_at, file_id
                     LIMIT 1
                     FOR UPDATE SKIP LOCKED
                    """,
                    MAX_RETRIES,
                )
                if row is None:
                    return None
                claimed = await conn.fetchrow(
                    """
                    UPDATE native_file_projection_outbox
                       SET claimed_at = NOW(), retry_count = retry_count + 1,
                           next_attempt_at = NOW() + INTERVAL '10 minutes'
                     WHERE file_id = $1 AND intent_id = $2
                    RETURNING *
                    """,
                    row["file_id"],
                    row["intent_id"],
                )
                return dict(claimed) if claimed is not None else None

    async def _complete(self, intent: dict, outcome: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE native_file_projection_outbox
                   SET claimed_at = NULL, next_attempt_at = NULL, retry_count = 0,
                       completed_at = NOW(), outcome = $3, last_error = NULL
                 WHERE file_id = $1 AND intent_id = $2 AND completed_at IS NULL
                """,
                intent["file_id"],
                intent["intent_id"],
                outcome,
            )

    async def _failure(self, intent: dict, error: Exception) -> None:
        attempt = int(intent["retry_count"])
        async with self.pool.acquire() as conn:
            if attempt >= MAX_RETRIES:
                await conn.execute(
                    """
                    UPDATE native_file_projection_outbox
                       SET claimed_at = NULL, next_attempt_at = NULL,
                           completed_at = NOW(), outcome = 'abandoned', last_error = $3
                     WHERE file_id = $1 AND intent_id = $2 AND completed_at IS NULL
                    """,
                    intent["file_id"], intent["intent_id"], type(error).__name__,
                )
            else:
                next_at = datetime.now(UTC) + timedelta(
                    seconds=next_attempt_delay(max(0, attempt - 1))
                )
                await conn.execute(
                    """
                    UPDATE native_file_projection_outbox
                       SET claimed_at = NULL, next_attempt_at = $3, last_error = $4
                     WHERE file_id = $1 AND intent_id = $2 AND completed_at IS NULL
                    """,
                    intent["file_id"], intent["intent_id"], next_at, type(error).__name__,
                )

    async def _resource(self, file_id: uuid.UUID) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT r.namespace_id, r.resource_id, r.surface, r.lifecycle,
                       r.current_path, r.head_revision_id, pm.digest, pm.byte_size
                  FROM native_resources r
             LEFT JOIN native_revisions nr
                    ON nr.resource_id = r.resource_id
                   AND nr.revision_id = r.head_revision_id
             LEFT JOIN native_payload_manifests pm
                    ON pm.payload_manifest_id = nr.payload_manifest_id
                 WHERE r.resource_id = $1
                """,
                file_id,
            )
        return dict(row) if row is not None else None

    async def _source(self, intent: dict) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT vf.id, vf.vault_id, vf.kind, vf.upload_state,
                       CASE WHEN c.path IS NULL THEN vf.name
                            ELSE c.path || '/' || vf.name END AS logical_path,
                       COALESCE(NULLIF(vf.mime_type, ''), 'application/octet-stream') AS mime_type,
                       vf.content_hash, vf.size_bytes, vf.s3_key, vf.hash_verified_at
                  FROM vault_files vf
             LEFT JOIN collections c ON c.id = vf.collection_id
                 WHERE vf.id = $1 AND vf.vault_id = $2
                """,
                intent["file_id"], intent["namespace_id"],
            )
        return dict(row) if row is not None else None

    async def _intent_is_current(self, intent: dict) -> bool:
        async with self.pool.acquire() as conn:
            return bool(await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM native_file_projection_outbox
                     WHERE file_id = $1 AND intent_id = $2 AND completed_at IS NULL
                )
                """,
                intent["file_id"], intent["intent_id"],
            ))

    @staticmethod
    def _mutation_id(intent: dict, stage: str) -> uuid.UUID:
        return uuid.uuid5(
            _MUTATION_NAMESPACE,
            f"{intent['intent_id']}:{stage}",
        )

    async def _ensure_absent(self, intent: dict, resource: dict | None) -> str:
        if resource is None or resource["lifecycle"] == "deleted":
            return "already_absent"
        self._validate_resource(intent, resource)
        await self.native.delete_resource(
            namespace_id=intent["namespace_id"],
            surface="file",
            path=resource["current_path"],
            actor=intent["actor"],
            mutation_id=self._mutation_id(intent, "delete"),
            expected_revision_id=resource["head_revision_id"],
            expected_resource_id=intent["file_id"],
            message="S3 File no longer has a searchable text projection",
        )
        return "deleted"

    @staticmethod
    def _validate_resource(intent: dict, resource: dict) -> None:
        if resource["namespace_id"] != intent["namespace_id"]:
            raise RuntimeError("Native File projection namespace drifted")
        if resource["surface"] != "file":
            raise RuntimeError("Native File projection identity belongs to another surface")

    @staticmethod
    def _source_matches(intent: dict, source: dict | None) -> bool:
        return bool(
            source is not None
            and source["kind"] == "file"
            and source["upload_state"] == "confirmed"
            and source["hash_verified_at"] is not None
            and source["logical_path"] == intent["logical_path"]
            and source["mime_type"] == intent["mime_type"]
            and source["content_hash"] == intent["content_hash"]
            and int(source["size_bytes"]) == int(intent["byte_size"])
            and source["s3_key"] == intent["s3_key"]
        )

    @staticmethod
    def _read_payload(intent: dict) -> bytes:
        body = bytearray()
        for chunk in s3_adapter.iter_chunks(intent["s3_key"]):
            body.extend(chunk)
            if len(body) > M1_PG_TEXT_MAX_BYTES:
                raise ValueError("File exceeds Native searchable text limit")
        return bytes(body)

    async def _apply(self, intent: dict) -> str:
        if not await self._intent_is_current(intent):
            return "superseded"
        resource = await self._resource(intent["file_id"])
        if not intent["source_present"]:
            return await self._ensure_absent(intent, resource)

        source = await self._source(intent)
        if not self._source_matches(intent, source):
            raise RuntimeError("File catalogue drifted from its Native projection intent")
        if not is_native_text_file(
            mime_type=intent["mime_type"], byte_size=int(intent["byte_size"]),
        ):
            return await self._ensure_absent(intent, resource)

        try:
            payload = await asyncio.to_thread(self._read_payload, intent)
            validate_native_text_file(
                payload,
                digest=intent["content_hash"],
                byte_size=int(intent["byte_size"]),
            )
        except ValueError:
            # MIME is advisory. Invalid UTF-8/NUL/oversize content remains a
            # valid S3 File but must not masquerade as Native text.
            return await self._ensure_absent(intent, resource)

        if not await self._intent_is_current(intent):
            return "superseded"
        source = await self._source(intent)
        if not self._source_matches(intent, source):
            return "superseded"

        if resource is None:
            await self.native.create_text(
                namespace_id=intent["namespace_id"], surface="file",
                path=intent["logical_path"], payload=payload, actor=intent["actor"],
                mutation_id=self._mutation_id(intent, "create"),
                resource_id=intent["file_id"],
                message="S3 File searchable-text projection",
                expected_digest=intent["content_hash"],
                expected_size=int(intent["byte_size"]),
            )
            return "created"

        self._validate_resource(intent, resource)
        if resource["lifecycle"] == "deleted":
            await self.native.restore_text(
                namespace_id=intent["namespace_id"], surface="file",
                path=resource["current_path"], payload=payload, actor=intent["actor"],
                mutation_id=self._mutation_id(intent, "restore"),
                expected_revision_id=resource["head_revision_id"],
                expected_resource_id=intent["file_id"],
                message="S3 File searchable-text projection restored",
                expected_digest=intent["content_hash"],
                expected_size=int(intent["byte_size"]),
            )
            resource = await self._resource(intent["file_id"])
            assert resource is not None
            outcome = "restored"
        else:
            outcome = "unchanged"

        if resource["current_path"] != intent["logical_path"]:
            await self.native.move_text(
                namespace_id=intent["namespace_id"], surface="file",
                path=resource["current_path"], path_to=intent["logical_path"],
                actor=intent["actor"], mutation_id=self._mutation_id(intent, "move"),
                expected_revision_id=resource["head_revision_id"],
                expected_resource_id=intent["file_id"],
                message="S3 File searchable-text path synchronized",
            )
            resource = await self._resource(intent["file_id"])
            assert resource is not None
            outcome = "moved"

        if resource["digest"] != intent["content_hash"]:
            await self.native.replace_text(
                namespace_id=intent["namespace_id"], surface="file",
                path=intent["logical_path"], payload=payload, actor=intent["actor"],
                mutation_id=self._mutation_id(intent, "replace"),
                expected_revision_id=resource["head_revision_id"],
                expected_resource_id=intent["file_id"],
                message="S3 File searchable-text projection replaced",
                expected_digest=intent["content_hash"],
                expected_size=int(intent["byte_size"]),
            )
            outcome = "replaced"
        return outcome

    async def process_once(self) -> int:
        intent = await self._claim_one()
        if intent is None:
            return 0
        try:
            outcome = await self._apply(intent)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Native File projection failed for %s: %s",
                intent["file_id"], type(exc).__name__,
            )
            await self._failure(intent, exc)
            return 0
        await self._complete(intent, outcome)
        return 1
