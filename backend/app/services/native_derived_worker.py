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
from typing import NamedTuple

import asyncpg

from app.db.postgres import get_pool
from app.services import delete_worker
from app.services._backfill import MAX_RETRIES, next_attempt_delay
from app.services.document_service import _parse_markdown
from app.services.kg_service import (
    delete_native_document_edges,
    store_document_relations,
    sync_native_document_edge_uris,
)
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


def _indexable(canonical_text: str) -> str:
    """Drop what PostgreSQL `text` cannot hold from a body already at rest.

    The write boundary removes NUL now (`to_nfc`), so nothing new arrives
    carrying one. Bodies stored before that do, and they live in the payload
    store, which accepts the byte -- so the document reads back intact while
    every indexing attempt raises `CharacterNotInRepertoireError`, retries to
    the ceiling and is abandoned (akb#527). Sanitising here is what lets those
    documents be indexed without anyone having to find and rewrite them.

    Only the byte goes. An index missing eight NULs and a document missing its
    whole entry in ranked search are not comparable losses.
    """
    return canonical_text.replace("\x00", "")


class DocumentRelations(NamedTuple):
    """The graph inputs one document body carries."""

    depends_on: list[str]
    related_to: list[str]
    implements: list[str]
    body: str


def build_native_document_relations(canonical_text: str) -> DocumentRelations:
    """Frontmatter relation lists + the body the link scanner reads.

    Kept beside the chunk builder because both are pure parses of the same
    verified Head body and both belong to one derived rewrite.
    """
    metadata, body = _parse_markdown(_indexable(canonical_text))

    def refs(key: str) -> list[str]:
        value = metadata.get(key)
        return [str(ref) for ref in value] if isinstance(value, list) else []

    return DocumentRelations(
        depends_on=refs("depends_on"),
        related_to=refs("related_to"),
        implements=refs("implements"),
        body=body,
    )


def build_native_document_chunks(
    *,
    vault_name: str,
    path: str,
    canonical_text: str,
) -> list[Chunk]:
    """Build the real AKB chunk representation from one verified native body."""
    metadata, body = _parse_markdown(_indexable(canonical_text))
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
    canonical_text = _indexable(canonical_text)
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


async def _pending_stats(
    pool: asyncpg.Pool,
    namespace_id: uuid.UUID | None = None,
) -> dict[str, int | str]:
    """Return the durable derived-indexing queue state for an operator.

    ``abandoned`` is the count that matters and the reason this is reported at
    all: each one is a Resource revision that spent its whole retry budget and
    will never be chunked, embedded, or returned by ranked search.  Nothing
    else says so — the Resource stays readable and greppable, and ``pending``
    drains to zero exactly as it would have if the work had succeeded.  So
    ``status`` refuses to say ``ok`` while it is non-zero, even with an empty
    queue: a backfill that reached 100% having dropped documents is not a
    finished backfill.

    It is a ledger, though, and a ledger never forgets.  A revision that was
    abandoned and then superseded by one that indexed cleanly leaves its entry
    behind forever, so a verdict taken from it reports a loss that has already
    been repaired and keeps reporting it.  ``*_at_head`` is the same count
    narrowed to intents whose revision is still the Resource's Head — the ones
    that describe a document missing from ranked search *now*.  The verdict
    reads those; the cumulative counts stay exactly as they were, because the
    question "what has this queue ever given up on" is also worth answering.
    They come from ``_current_head_stats``, a second query rather than a join
    on this one.  The counters here read every row, and carrying the Head test
    on that scan doubled it; the Head query only needs the rows that are not
    settled or were given up on, which is a small fraction of any healthy
    ledger.  Two narrow scans, one definition of "at the Head", and the vault
    surface keeps the exact query whose plan it was measured on.

    ``exhausted`` is deliberately separate from terminal ``abandoned``: it is
    the final claimed attempt while its lease is still in force.  The claim
    query skips ``retry_count >= MAX_RETRIES``, so a process killed exactly
    there leaves a row nothing will pick up until ``queue_rescuer`` stamps it
    terminal — visible here rather than counted as ordinary retrying work.
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
                   COUNT(*) FILTER (WHERE delivery_outcome = 'abandoned')::int AS abandoned,
                   COUNT(*) FILTER (WHERE delivery_outcome = 'applied')::int AS applied,
                   COUNT(*) FILTER (WHERE delivery_outcome = 'superseded')::int AS superseded,
                   COUNT(*) FILTER (WHERE delivery_outcome = 'deleted')::int AS deleted,
                   -- Pre-parity rows only; nothing produces 'direct_grep'
                   -- since text Files took the document-parity path. The
                   -- counter stays so history is reported, not rewritten.
                   COUNT(*) FILTER (WHERE delivery_outcome = 'direct_grep')::int AS direct_grep
              FROM native_invalidation_intents
             WHERE TRUE {scope}
            """,
            *params,
        )
    at_head = await _current_head_stats(pool, namespace_id)  # defined below
    return {
        "pending": int(row["pending"]),
        "retrying": int(row["retrying"]),
        "exhausted": int(row["exhausted"]),
        "abandoned": int(row["abandoned"]),
        **{f"{key}_at_head": value for key, value in at_head.items()},
        "applied": int(row["applied"]),
        "superseded": int(row["superseded"]),
        "deleted": int(row["deleted"]),
        "direct_grep": int(row["direct_grep"]),
        "status": (
            "degraded" if at_head["exhausted"] or at_head["abandoned"]
            else "reconciling" if int(row["pending"])
            else "ok"
        ),
    }


async def pending_stats(namespace_id: uuid.UUID | None = None) -> dict[str, int | str]:
    """Operator-facing derived-index queue state for the health surfaces."""
    return await _pending_stats(await get_pool(), namespace_id)


async def _current_head_stats(
    pool: asyncpg.Pool,
    namespace_id: uuid.UUID | None = None,
) -> dict[str, int]:
    """Count only intents for the current Head, including deletion Heads.

    The historical ledger remains available through ``pending_stats``. Its
    abandoned revisions must not become permanent current-resource warnings.
    Require the Resource and Head revision keys, and the namespace key too
    when the caller named one.

    Every counter here is a subset of "not settled, or given up on", so the
    join reads only those rows.  The restriction changes no count and is what
    makes the unscoped form affordable on ``/health``: measured against a
    deployment ledger it costs less than the cumulative counters beside it,
    where the same join over the whole table costs several times more.
    """
    params: list[object] = [MAX_RETRIES]
    scope = ""
    if namespace_id is not None:
        params.append(namespace_id)
        scope = "AND i.namespace_id = $2"
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            SELECT COUNT(*) FILTER (WHERE i.completed_at IS NULL) AS pending,
                   COUNT(*) FILTER (
                       WHERE i.completed_at IS NULL
                         AND i.retry_count > 0 AND i.retry_count < $1
                   ) AS retrying,
                   COUNT(*) FILTER (
                       WHERE i.completed_at IS NULL AND i.retry_count >= $1
                   ) AS exhausted,
                   COUNT(*) FILTER (WHERE i.delivery_outcome = 'abandoned') AS abandoned
              FROM native_invalidation_intents i
              JOIN native_resources r
                ON r.namespace_id = i.namespace_id
               AND r.resource_id = i.resource_id
               AND r.head_revision_id = i.revision_id
             WHERE (i.completed_at IS NULL OR i.delivery_outcome = 'abandoned')
               {scope}
            """,
            *params,
        )
    return {key: int(row[key]) for key in ("pending", "retrying", "exhausted", "abandoned")}


async def current_head_stats(namespace_id: uuid.UUID) -> dict[str, int]:
    """Vault-scoped current preparation observations for versioned health."""
    return await _current_head_stats(await get_pool(), namespace_id)


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
        terminal = attempt_count >= MAX_RETRIES
        async with self.pool.acquire() as conn:
            if terminal:
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
        if terminal:
            await self._log_abandonment(intent, error, attempt_count)

    async def _log_abandonment(
        self, intent: dict, error: Exception, attempts: int,
    ) -> None:
        """Say once, loudly, which Resource just stopped being retried.

        ``process_once`` logs the same line for attempt 1 and attempt
        ``MAX_RETRIES``, so the moment a Resource is given up on reads in a log
        exactly like the transient failures before it.  The counters on the
        health surfaces say how MANY were lost; this says WHICH, because a
        count tells an operator that something is wrong and only the path tells
        them what to fix.

        Best effort on purpose: the row is already stamped terminal before this
        runs, so a failed lookup costs a name in a message, never the state.
        Only the exception CLASS is reported, as in ``last_error`` — an
        exception message can quote the body that failed to store.
        """
        vault_name: str | None = None
        path: str | None = None
        try:
            async with self.pool.acquire() as conn:
                located = await conn.fetchrow(
                    """
                    SELECT v.name AS vault_name, r.current_path
                      FROM native_resources r
                      JOIN vaults v ON v.id = r.namespace_id
                     WHERE r.resource_id = $1
                    """,
                    intent["resource_id"],
                )
            if located is not None:
                vault_name = located["vault_name"]
                path = located["current_path"]
        except Exception:  # noqa: BLE001 — diagnostics must not mask the failure
            logger.debug("could not name the abandoned resource", exc_info=True)
        logger.error(
            "native derived delivery ABANDONED after %d attempts: this %s will not be "
            "indexed and ranked search will not return it until a new revision "
            "supersedes it. vault=%s path=%s resource_id=%s revision_id=%s error=%s",
            attempts,
            intent["surface"],
            vault_name or "<unresolved>",
            path or "<unresolved>",
            intent["resource_id"],
            intent["revision_id"],
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
                    """
                    SELECT r.lifecycle, r.head_revision_id, r.current_path, r.surface,
                           v.name AS vault_name
                      FROM native_resources r
                      JOIN vaults v ON v.id = r.namespace_id
                     WHERE r.resource_id = $1 FOR UPDATE OF r
                    """,
                    intent["resource_id"],
                )
                if resource is None or resource["head_revision_id"] != intent["revision_id"]:
                    await conn.execute(
                        """
                        UPDATE native_invalidation_intents
                           SET completed_at = NOW(), delivery_outcome = 'superseded',
                               retry_count = 0, claimed_at = NULL,
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
                               retry_count = 0, claimed_at = NULL,
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
                if resource["surface"] == "document":
                    # A deleted document is not a graph endpoint any more, in
                    # either direction — the legacy delete clears the same rows.
                    #
                    # Keyed on the resource, not on the path it used to hold.
                    # This intent can be applied long after the commit that
                    # raised it, and by then a DIFFERENT document may own that
                    # path; clearing by URI erased ITS links (akb#654).
                    await delete_native_document_edges(
                        conn,
                        intent["namespace_id"],
                        resource["vault_name"],
                        intent["resource_id"],
                        resource["current_path"],
                    )
                await conn.execute(
                    "DELETE FROM native_derived_heads WHERE resource_id = $1",
                    intent["resource_id"],
                )
                await conn.execute(
                    """
                    UPDATE native_invalidation_intents
                       SET completed_at = NOW(), delivery_outcome = 'deleted',
                           retry_count = 0, claimed_at = NULL,
                           next_attempt_at = NULL, last_error = NULL
                     WHERE intent_id = $1
                    """,
                    intent["intent_id"],
                )

    async def _apply_live(self, intent: dict, head: dict) -> int:
        source_type = source_type_for_surface(intent["surface"])

        def prepare() -> tuple[str, list[Chunk], DocumentRelations | None]:
            canonical = verify_native_head_body(head)
            canonical_text = canonical.decode("utf-8", errors="strict")
            if intent["surface"] == "file":
                chunks = build_native_file_chunks(
                    vault_name=head["vault_name"],
                    path=head["current_path"],
                    resource_id=head["resource_id"],
                    canonical_text=canonical_text,
                )
                relations = None
            else:
                chunks = build_native_document_chunks(
                    vault_name=head["vault_name"],
                    path=head["current_path"],
                    canonical_text=canonical_text,
                )
                relations = build_native_document_relations(canonical_text)
            return hashlib.sha256(canonical).hexdigest(), chunks, relations

        digest, chunks, relations = await asyncio.to_thread(
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
                               retry_count = 0, claimed_at = NULL,
                               next_attempt_at = NULL, last_error = NULL
                         WHERE intent_id = $1
                        """,
                        intent["intent_id"],
                    )
                    return 0
                await self._drop_chunks(conn, intent["resource_id"], source_type)
                if relations is not None:
                    # Durable recovery for the endpoints. The facade's move hook
                    # runs after the authoritative commit and outside it, so it
                    # can be lost entirely — a crash between commit and hook
                    # leaves the explicit `akb_link` rows naming a path nothing
                    # writes to, and nothing retries. This does retry: the
                    # intent is durable, and the sync writes the head path this
                    # transaction already locked, so running it here is
                    # convergent with the hook rather than a second opinion.
                    await sync_native_document_edge_uris(
                        conn,
                        intent["namespace_id"],
                        head["vault_name"],
                        intent["resource_id"],
                        resource["current_path"],
                    )
                    # The graph half of the same rewrite. Passing the resource
                    # identity scopes the implicit clear to THIS document's
                    # rows, wherever they currently point — which subsumes the
                    # separate previous-path sweep this used to do. That sweep
                    # deleted by URI, so a delayed move rewrite erased the
                    # implicit edges of whichever document had since taken the
                    # freed path. Explicit `akb_link` rows are never touched
                    # here; the move carries them.
                    await store_document_relations(
                        conn,
                        intent["namespace_id"],
                        head["vault_name"],
                        resource["current_path"],
                        relations.depends_on,
                        relations.related_to,
                        relations.implements,
                        relations.body,
                        intent["resource_id"],
                    )
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
                           retry_count = 0, claimed_at = NULL,
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

    async def pending_stats(self, namespace_id: uuid.UUID | None = None) -> dict[str, int | str]:
        """Return queue diagnostics using this worker's pool (tests/operators)."""
        return await _pending_stats(self.pool, namespace_id)

    async def settle(
        self,
        *,
        namespace_id: uuid.UUID,
        timeout_seconds: float,
        poll_interval_seconds: float = 0.05,
    ) -> dict[str, int | float | str]:
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
