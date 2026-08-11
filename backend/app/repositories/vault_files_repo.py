"""Repository for `vault_files` — metadata for binary files stored in S3.

The actual file bytes never touch this layer; S3 access lives in
`services/adapters/s3_adapter.py`. Module-level functions take an
explicit `conn` so the caller controls the transaction boundary.

Collection membership is normalized via the FK `vault_files.collection_id`
referencing `collections.id`. NULL == vault root. The legacy free-form
`vault_files.collection` TEXT column was removed in migration 020.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime


# The measurement File reads resolve the placement of the body a confirmed
# native-text row is pinned to. The join is on the row's own Head identity
# (`native_resource_id` + `native_revision_id`), so it names the manifest that
# revision published and nothing later. It yields the placement STRING only —
# `private_locator` / `payload_manifest_id` are internal addresses and are
# never selected here. Binary rows have no native identity and get NULL.
_MEASUREMENT_PLACEMENT_JOIN = """
          LEFT JOIN native_revisions nr
                 ON nr.resource_id = vf.native_resource_id
                AND nr.revision_id = vf.native_revision_id
          LEFT JOIN native_payload_manifests pm
                 ON pm.payload_manifest_id = nr.payload_manifest_id
"""

_SQL_ALIAS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_S3_KEY_LOCK_NAMESPACE = 1_735_359_043


async def lock_s3_key_for_mutation(conn, s3_key: str) -> None:
    """Take the blocking transaction lock used by the final metadata insert.

    New File uploads first use ``s3_key_available_for_registration`` so they
    never wait behind object-store I/O. This blocking form remains the final
    insert guard and keeps older callers safe during a rolling deployment.
    """
    await conn.fetchval(
        "SELECT pg_advisory_xact_lock($1::int, hashtext($2))",
        _S3_KEY_LOCK_NAMESPACE,
        s3_key,
    )


async def try_lock_s3_key_for_cleanup(conn, s3_key: str) -> bool:
    """Hold one session lock across a cleanup probe and remote delete.

    The lock is session-scoped rather than transaction-scoped: object-store
    latency must not leave a PostgreSQL transaction open. An unprocessed
    outbox row remains the durable barrier if this connection is lost.
    """
    return bool(await conn.fetchval(
        "SELECT pg_try_advisory_lock($1::int, hashtext($2))",
        _S3_KEY_LOCK_NAMESPACE,
        s3_key,
    ))


async def unlock_s3_key_after_cleanup(conn, s3_key: str) -> None:
    await conn.fetchval(
        "SELECT pg_advisory_unlock($1::int, hashtext($2))",
        _S3_KEY_LOCK_NAMESPACE,
        s3_key,
    )


async def s3_key_available_for_registration(
    conn,
    *,
    vault_id: uuid.UUID,
    s3_key: str,
) -> bool:
    """Reserve a key for this transaction without waiting on remote cleanup.

    An unprocessed delete intent is a durable key barrier. If cleanup already
    owns the session lock, or the barrier exists without a same-vault live row,
    the caller selects a fresh random key. A same-vault row remains adoptable;
    cleanup will observe that live reference and retire its stale intent.
    """
    locked = await conn.fetchval(
        "SELECT pg_try_advisory_xact_lock($1::int, hashtext($2))",
        _S3_KEY_LOCK_NAMESPACE,
        s3_key,
    )
    if not locked:
        return False

    live_key = await conn.fetchrow(
        """
        SELECT EXISTS (
                   SELECT 1 FROM vault_files WHERE s3_key = $1
               ) AS any_vault,
               EXISTS (
                   SELECT 1
                     FROM vault_files
                    WHERE s3_key = $1 AND vault_id = $2
               ) AS same_vault
        """,
        s3_key,
        vault_id,
    )
    if live_key["any_vault"]:
        return bool(live_key["same_vault"])

    pending_delete = await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1
              FROM s3_delete_outbox
             WHERE s3_key = $1 AND processed_at IS NULL
        )
        """,
        s3_key,
    )
    return not pending_delete


def confirmed_file_predicate(alias: str = "vf") -> str:
    """Trusted SQL fragment for a user-visible standalone File row."""
    if not _SQL_ALIAS_RE.fullmatch(alias):
        raise ValueError(f"Invalid SQL alias: {alias!r}")
    return f"{alias}.kind = 'file' AND {alias}.upload_state = 'confirmed'"


def confirmed_attachment_predicate(alias: str = "vf") -> str:
    """Trusted SQL fragment for a readable hidden document attachment."""
    if not _SQL_ALIAS_RE.fullmatch(alias):
        raise ValueError(f"Invalid SQL alias: {alias!r}")
    return (
        f"{alias}.kind = 'attachment' "
        f"AND {alias}.upload_state = 'confirmed' "
        f"AND {alias}.hash_verified_at IS NOT NULL"
    )


_ATTACHMENT_SELECT = """
        SELECT vf.id, vf.vault_id, v.name AS vault_name, vf.kind, vf.name,
               vf.s3_key, vf.mime_type, vf.size_bytes, vf.content_hash,
               vf.hash_verified_at
          FROM vault_files vf
          JOIN vaults v ON v.id = vf.vault_id
"""


async def insert_or_adopt(
    conn,
    *,
    file_id: uuid.UUID,
    vault_id: uuid.UUID,
    name: str,
    s3_key: str,
    mime_type: str,
    size_bytes: int,
    description: str,
    created_by: str,
    collection_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Insert a file row, or adopt the row already holding `(vault_id, s3_key)`.

    Returns the id of the row the caller should now use: `file_id` when a new
    row was inserted, the pre-existing row's id when the key was already
    taken. Callers tell the two apart by comparing against the `file_id` they
    passed in.

    Only reachable for a deterministic (content-addressed) key — a random key
    cannot collide — so an adopted row is by construction the same bytes under
    the same vault/collection/filename.

    `ON CONFLICT ... DO UPDATE` rather than `DO NOTHING` on purpose. DO UPDATE
    is the form PostgreSQL guarantees to be atomic insert-or-update: it always
    returns a row, and against a *concurrent uncommitted* insert of the same
    key it waits and then returns the winner. DO NOTHING returns no row on
    conflict, forcing a follow-up SELECT that races the other transaction.

    Re-adoption refreshes ``updated_at`` as an upload lease. Otherwise a
    previously abandoned pending row could remain past the GC cutoff while a
    new transfer is actively using it. Descriptive fields are left alone —
    first writer wins — so a repeat upload cannot silently rewrite metadata
    that something else may already be reading.
    """
    row = await conn.fetchrow(
        """
        WITH key_lock AS (
            SELECT pg_advisory_xact_lock($10::int, hashtext($5)) AS held
        )
        INSERT INTO vault_files
            (id, vault_id, collection_id, kind, upload_state, name, s3_key,
             mime_type, size_bytes, description, created_by)
        SELECT $1, $2, $3, 'file', 'pending', $4, $5, $6, $7, $8, $9
          FROM key_lock
        ON CONFLICT (vault_id, s3_key) DO UPDATE
            SET collection_id = EXCLUDED.collection_id,
                updated_at = CASE
                    WHEN vault_files.upload_state = 'pending' THEN NOW()
                    ELSE vault_files.updated_at
                END
        RETURNING id
        """,
        file_id, vault_id, collection_id, name, s3_key,
        mime_type, size_bytes, description, created_by,
        _S3_KEY_LOCK_NAMESPACE,
    )
    return row["id"]


async def insert_pending_attachment(
    conn,
    *,
    file_id: uuid.UUID,
    vault_id: uuid.UUID,
    name: str,
    s3_key: str,
    mime_type: str,
    size_bytes: int,
    content_hash: str,
    created_by: str,
) -> None:
    """Record a validated editor image before its object-store PUT.

    The row remains unreadable while ``hash_verified_at`` is NULL. Persisting
    it before the remote PUT gives the lifecycle worker an authoritative key to
    collect if the process exits between S3 and finalization. There is no
    collection: document images are authorized through explicit live/revision
    reference tables.
    """
    await conn.execute(
        """
        INSERT INTO vault_files (
            id, vault_id, collection_id, kind, upload_state, name, s3_key, mime_type,
            size_bytes, content_hash, hash_algorithm,
            description, created_by
        )
        VALUES ($1, $2, NULL, 'attachment', 'pending', $3, $4, $5, $6, $7,
                'sha256', 'Document image', $8)
        """,
        file_id, vault_id, name, s3_key, mime_type, size_bytes,
        content_hash, created_by,
    )


async def finalize_attachment(
    conn,
    *,
    file_id: uuid.UUID,
    vault_id: uuid.UUID,
    s3_key: str,
) -> bool:
    """Make one pending attachment readable after its immutable PUT succeeds."""
    row = await conn.fetchrow(
        """
        UPDATE vault_files
           SET upload_state = 'confirmed', hash_verified_at = NOW(), updated_at = NOW()
         WHERE id = $1
           AND vault_id = $2
           AND kind = 'attachment'
           AND s3_key = $3
           AND hash_verified_at IS NULL
        RETURNING id
        """,
        file_id, vault_id, s3_key,
    )
    return row is not None


async def insert_or_adopt_measurement_confirmed(
    conn, *, file_id: uuid.UUID, vault_id: uuid.UUID, collection_id: uuid.UUID | None,
    name: str, mime_type: str, description: str, created_by: str, driver: str,
    locator: str, digest: str, size_bytes: int, native_resource_id: uuid.UUID | None,
    native_revision_id: str | None,
) -> dict:
    """Atomically publish a confirmed File or return its exact-content peer."""
    await conn.execute(
        """
        INSERT INTO vault_files (
            id, vault_id, collection_id, kind, upload_state,
            name, s3_key, mime_type, size_bytes,
            content_hash, hash_algorithm, hash_verified_at, description, created_by,
            storage_driver, storage_locator, native_resource_id, native_revision_id
        )
        VALUES ($1, $2, $3, 'file', 'confirmed', $4, $5, $6, $7, $8, 'sha256', NOW(), $9, $10,
                $11, $12, $13, $14)
        ON CONFLICT DO NOTHING
        """,
        file_id, vault_id, collection_id, name, f"m1-logical/{file_id}", mime_type,
        size_bytes, digest, description, created_by, driver, locator,
        native_resource_id, native_revision_id,
    )
    row = await conn.fetchrow(
        """
        SELECT vf.id, vf.vault_id, v.name AS vault_name, vf.collection_id, c.path AS collection,
               vf.name, vf.mime_type, vf.size_bytes, vf.description,
               vf.content_hash, vf.storage_driver, vf.storage_locator,
               vf.native_resource_id, vf.native_revision_id,
               pm.selected_placement AS payload_placement
          FROM vault_files vf JOIN vaults v ON v.id = vf.vault_id
          LEFT JOIN collections c ON c.id = vf.collection_id
        """ + _MEASUREMENT_PLACEMENT_JOIN + """
         WHERE vf.vault_id = $1
           AND vf.collection_id IS NOT DISTINCT FROM $2
           AND vf.name = $3 AND vf.content_hash = $4
           AND vf.storage_driver IS NOT NULL
         ORDER BY vf.created_at ASC LIMIT 1
        """, vault_id, collection_id, name, digest,
    )
    if row is None:
        raise RuntimeError("confirmed File publication did not produce an exact row")
    return dict(row)


async def list_measurement_confirmed(
    conn, vault_id: uuid.UUID, *, collection: str | None, limit: int,
) -> list[dict]:
    params: list = [vault_id]
    collection_clause = ""
    if collection is not None:
        if collection == "":
            collection_clause = " AND vf.collection_id IS NULL"
        else:
            params.append(collection)
            collection_clause = f" AND c.path = ${len(params)}"
    params.append(limit)
    rows = await conn.fetch(
        """
        SELECT vf.id, vf.vault_id, v.name AS vault_name, c.path AS collection, vf.name, vf.mime_type,
               vf.size_bytes, vf.content_hash, vf.hash_algorithm, vf.storage_driver,
               vf.storage_locator, vf.native_resource_id, vf.native_revision_id,
               pm.selected_placement AS payload_placement
          FROM vault_files vf JOIN vaults v ON v.id = vf.vault_id
          LEFT JOIN collections c ON c.id = vf.collection_id
        """ + _MEASUREMENT_PLACEMENT_JOIN + """
         WHERE vf.vault_id = $1 AND vf.storage_driver IS NOT NULL
        """ + collection_clause + f" ORDER BY vf.created_at DESC LIMIT ${len(params)}",
        *params,
    )
    return [dict(row) for row in rows]


async def find_by_id(
    conn,
    vault_id: uuid.UUID,
    file_id: uuid.UUID,
) -> dict | None:
    """Returns the file row joined with its collection.path. The
    `collection` field on the result dict is the human-readable path
    (or None for vault root), used by event payloads + browse renderers
    that need the path string."""
    row = await conn.fetchrow(
        """
        SELECT vf.id, vf.vault_id, vf.collection_id, vf.kind, c.path AS collection,
               vf.name, vf.s3_key, vf.mime_type, vf.size_bytes,
               vf.description, vf.created_by, vf.created_at, vf.updated_at,
               vf.content_hash, vf.hash_algorithm, vf.etag,
               vf.storage_version, vf.hash_verified_at, vf.upload_state
          FROM vault_files vf
          LEFT JOIN collections c ON c.id = vf.collection_id
         WHERE vf.id = $1 AND vf.vault_id = $2
        """,
        file_id, vault_id,
    )
    return dict(row) if row else None


async def lease_file_upload_confirmation(
    conn,
    vault_id: uuid.UUID,
    file_id: uuid.UUID,
) -> dict | None:
    """Refresh a File's confirmation lease and return its scoped metadata.

    The short UPDATE lock serializes with stale-pending GC without retaining a
    database connection while S3 bytes are hashed.
    """
    row = await conn.fetchrow(
        """
        WITH leased AS (
            UPDATE vault_files
               SET updated_at = NOW()
             WHERE id = $1
               AND vault_id = $2
               AND kind = 'file'
            RETURNING *
        )
        SELECT leased.id, leased.vault_id, leased.collection_id, leased.kind,
               c.path AS collection, leased.name, leased.s3_key,
               leased.mime_type, leased.size_bytes, leased.description,
               leased.created_by, leased.created_at, leased.updated_at,
               leased.content_hash, leased.hash_algorithm, leased.etag,
               leased.storage_version, leased.hash_verified_at,
               leased.upload_state
          FROM leased
          LEFT JOIN collections c ON c.id = leased.collection_id
        """,
        file_id, vault_id,
    )
    return dict(row) if row else None


async def find_attachment_by_id(
    conn,
    vault_id: uuid.UUID,
    file_id: uuid.UUID,
) -> dict | None:
    """Find a confirmed document image inside one authorization scope.

    Callers must resolve the vault from their authenticated document or public
    publication context first.  Keeping that predicate in the lookup avoids a
    global asset-id existence probe before authorization.
    """
    row = await conn.fetchrow(
        _ATTACHMENT_SELECT + f"""
         WHERE vf.id = $1
           AND vf.vault_id = $2
           AND {confirmed_attachment_predicate("vf")}
        """,
        file_id, vault_id,
    )
    return dict(row) if row else None


async def find_authorized_attachment(
    conn,
    *,
    vault_id: uuid.UUID,
    file_id: uuid.UUID,
    created_by: str,
    document_path: str | None = None,
    commit_prefix: str | None = None,
) -> dict | None:
    """Resolve one attachment only through an authorized reachability edge.

    A reader may load an image that is referenced by a current document.  The
    uploader may also preview an unclaimed upload before the first save.  A
    historical read must provide the exact document path and a Git commit
    prefix that is still present in the bounded revision manifest.  Keeping
    every branch in this single vault-scoped query avoids an asset-existence
    probe before authorization.
    """
    row = await conn.fetchrow(
        _ATTACHMENT_SELECT + f"""
         WHERE vf.id = $1
           AND vf.vault_id = $2
           AND {confirmed_attachment_predicate("vf")}
           AND (
                (vf.attachment_claimed_at IS NULL AND vf.created_by = $3)
                OR EXISTS (
                    SELECT 1
                      FROM document_asset_refs live
                     WHERE live.asset_id = vf.id
                       AND live.vault_id = vf.vault_id
                )
                OR (
                    $4::text IS NOT NULL
                    AND $5::text IS NOT NULL
                    AND EXISTS (
                        SELECT 1
                          FROM document_asset_revision_refs rev
                         WHERE rev.asset_id = vf.id
                           AND rev.vault_id = vf.vault_id
                           AND rev.document_path = $4
                           AND rev.commit_hash LIKE $5 || '%'
                           AND rev.retain_until > NOW()
                    )
                )
           )
        """,
        file_id, vault_id, created_by, document_path, commit_prefix,
    )
    return dict(row) if row else None


async def claim_attachment_references(
    conn,
    vault_id: uuid.UUID,
    file_ids: set[uuid.UUID],
    *,
    strict: bool = True,
) -> set[uuid.UUID]:
    """Lock and claim valid attachment ids from a document body.

    In strict mode every id must be a confirmed attachment in ``vault_id``.
    Explicit import/mirror paths use non-strict mode so text-only archives can
    preserve source-vault links without making them readable in the target;
    ordinary creates stay strict. Valid local assets still gain historical-
    revision retention.
    """
    if not file_ids:
        return set()
    rows = await conn.fetch(
        f"""
        SELECT id
          FROM vault_files
         WHERE vault_id = $1
           AND id = ANY($2::uuid[])
           AND {confirmed_attachment_predicate("vault_files")}
         ORDER BY id
         FOR UPDATE
        """,
        vault_id, list(file_ids),
    )
    found = {row["id"] for row in rows}
    if strict and found != file_ids:
        return found
    if found:
        await conn.execute(
            """
            UPDATE vault_files
               SET attachment_claimed_at = COALESCE(attachment_claimed_at, NOW()),
                   updated_at = NOW()
             WHERE vault_id = $1 AND id = ANY($2::uuid[])
            """,
            vault_id, list(found),
        )
    return found


async def sync_document_asset_references(
    conn,
    *,
    document_id: uuid.UUID,
    vault_id: uuid.UUID,
    document_path: str,
    commit_hash: str,
    asset_ids: set[uuid.UUID],
    retain_until: datetime,
    previous_commit: str | None = None,
    previous_path: str | None = None,
) -> None:
    """Replace live refs and record bounded manifests for adjacent commits.

    The caller has already locked/validated ``asset_ids`` with
    :func:`claim_attachment_references`.  Before replacing the live set, the
    prior HEAD's refs are extended from *this* superseding write.  Thus an
    image removed from a long-lived document still gets a complete retention
    window instead of expiring immediately because its original commit is old.
    """
    if previous_commit:
        await conn.execute(
            """
            INSERT INTO document_asset_revision_refs (
                vault_id, document_path, commit_hash, asset_id, retain_until
            )
            SELECT live.vault_id, $3, $4, live.asset_id, $5
              FROM document_asset_refs live
             WHERE live.document_id = $1 AND live.vault_id = $2
            ON CONFLICT (vault_id, document_path, commit_hash, asset_id)
            DO UPDATE SET retain_until = GREATEST(
                document_asset_revision_refs.retain_until,
                EXCLUDED.retain_until
            )
            """,
            document_id, vault_id, previous_path or document_path,
            previous_commit, retain_until,
        )

    await conn.execute(
        """
        DELETE FROM document_asset_refs
         WHERE document_id = $1
           AND vault_id = $2
           AND NOT (asset_id = ANY($3::uuid[]))
        """,
        document_id, vault_id, list(asset_ids),
    )
    if asset_ids:
        await conn.execute(
            """
            INSERT INTO document_asset_refs (document_id, vault_id, asset_id)
            SELECT $1, $2, asset_id
              FROM unnest($3::uuid[]) AS asset_id
            ON CONFLICT (document_id, asset_id) DO NOTHING
            """,
            document_id, vault_id, list(asset_ids),
        )
        await conn.execute(
            """
            INSERT INTO document_asset_revision_refs (
                vault_id, document_path, commit_hash, asset_id, retain_until
            )
            SELECT $1, $2, $3, asset_id, $5
              FROM unnest($4::uuid[]) AS asset_id
            ON CONFLICT (vault_id, document_path, commit_hash, asset_id)
            DO UPDATE SET retain_until = GREATEST(
                document_asset_revision_refs.retain_until,
                EXCLUDED.retain_until
            )
            """,
            vault_id, document_path, commit_hash, list(asset_ids), retain_until,
        )


async def list_live_document_asset_ids(
    conn,
    *,
    document_id: uuid.UUID,
    vault_id: uuid.UUID,
) -> set[uuid.UUID]:
    rows = await conn.fetch(
        """
        SELECT asset_id
          FROM document_asset_refs
         WHERE document_id = $1 AND vault_id = $2
        """,
        document_id, vault_id,
    )
    return {row["asset_id"] for row in rows}


async def retain_current_document_assets(
    conn,
    *,
    document_id: uuid.UUID,
    vault_id: uuid.UUID,
    document_path: str,
    commit_hash: str | None,
    retain_until: datetime,
) -> None:
    """Extend the current HEAD manifest before a document row is deleted."""
    if not commit_hash:
        return
    await conn.execute(
        """
        INSERT INTO document_asset_revision_refs (
            vault_id, document_path, commit_hash, asset_id, retain_until
        )
        SELECT live.vault_id, $3, $4, live.asset_id, $5
          FROM document_asset_refs live
         WHERE live.document_id = $1 AND live.vault_id = $2
        ON CONFLICT (vault_id, document_path, commit_hash, asset_id)
        DO UPDATE SET retain_until = GREATEST(
            document_asset_revision_refs.retain_until,
            EXCLUDED.retain_until
        )
        """,
        document_id, vault_id, document_path, commit_hash, retain_until,
    )


async def delete_unclaimed_attachment(
    conn,
    *,
    vault_id: uuid.UUID,
    file_id: uuid.UUID,
    created_by: str,
) -> dict | None:
    """Delete one finalized caller upload only if no document claimed it."""
    row = await conn.fetchrow(
        """
        DELETE FROM vault_files
         WHERE id = $1
           AND vault_id = $2
           AND kind = 'attachment'
           AND upload_state = 'confirmed'
           AND attachment_claimed_at IS NULL
           AND created_by = $3
        RETURNING id, s3_key
        """,
        file_id, vault_id, created_by,
    )
    return dict(row) if row else None


async def find_owned_attachment_for_discard(
    conn,
    *,
    vault_id: uuid.UUID,
    file_id: uuid.UUID,
    created_by: str,
) -> dict | None:
    """Lock one caller-owned attachment so discard can be idempotent safely.

    A missing/foreign id is deliberately indistinguishable. Claimed and active
    uploads are returned only to their uploader, allowing the API to report
    that no deletion occurred without exposing another user's asset state.
    """
    row = await conn.fetchrow(
        """
        SELECT id, s3_key, upload_state, attachment_claimed_at
          FROM vault_files
         WHERE id = $1
           AND vault_id = $2
           AND kind = 'attachment'
           AND created_by = $3
         FOR UPDATE
        """,
        file_id, vault_id, created_by,
    )
    return dict(row) if row else None


async def delete_failed_pending_attachment(
    conn,
    *,
    vault_id: uuid.UUID,
    file_id: uuid.UUID,
    created_by: str,
) -> dict | None:
    """Remove a failed pending upload after its object write has settled.

    This is intentionally separate from the user-facing discard predicate:
    callers must not remove metadata while an accepted object write may still
    be in flight.
    """
    row = await conn.fetchrow(
        """
        DELETE FROM vault_files
         WHERE id = $1
           AND vault_id = $2
           AND kind = 'attachment'
           AND upload_state = 'pending'
           AND attachment_claimed_at IS NULL
           AND created_by = $3
        RETURNING id, s3_key
        """,
        file_id, vault_id, created_by,
    )
    return dict(row) if row else None


async def find_measurement_by_id(conn, vault_id: uuid.UUID, file_id: uuid.UUID) -> dict | None:
    row = await conn.fetchrow(
        """
        SELECT vf.id, vf.vault_id, v.name AS vault_name, c.path AS collection, vf.name, vf.mime_type,
               vf.size_bytes, vf.description, vf.content_hash,
               vf.storage_driver, vf.storage_locator, vf.native_resource_id,
               vf.native_revision_id, pm.selected_placement AS payload_placement
          FROM vault_files vf JOIN vaults v ON v.id = vf.vault_id
          LEFT JOIN collections c ON c.id = vf.collection_id
        """ + _MEASUREMENT_PLACEMENT_JOIN + """
         WHERE vf.id = $1 AND vf.vault_id = $2 AND vf.storage_driver IS NOT NULL
        """, file_id, vault_id,
    )
    return dict(row) if row else None


async def find_measurement_exact(
    conn, *, vault_id: uuid.UUID, collection_id: uuid.UUID | None,
    name: str, digest: str,
) -> dict | None:
    row = await conn.fetchrow(
        """
        SELECT vf.id, vf.vault_id, v.name AS vault_name, c.path AS collection,
               vf.name, vf.mime_type, vf.size_bytes, vf.description,
               vf.content_hash, vf.storage_driver, vf.storage_locator,
               vf.native_resource_id, vf.native_revision_id,
               pm.selected_placement AS payload_placement
          FROM vault_files vf JOIN vaults v ON v.id = vf.vault_id
          LEFT JOIN collections c ON c.id = vf.collection_id
        """ + _MEASUREMENT_PLACEMENT_JOIN + """
         WHERE vf.vault_id = $1
           AND vf.collection_id IS NOT DISTINCT FROM $2
           AND vf.name = $3 AND vf.content_hash = $4
           AND vf.storage_driver IS NOT NULL
         ORDER BY vf.created_at ASC LIMIT 1
        """, vault_id, collection_id, name, digest,
    )
    return dict(row) if row else None


async def update_size(conn, file_id: uuid.UUID, size_bytes: int) -> None:
    await conn.execute(
        "UPDATE vault_files SET size_bytes = $1, updated_at = NOW() WHERE id = $2",
        size_bytes, file_id,
    )


async def confirm_file_upload_metadata(
    conn,
    file_id: uuid.UUID,
    vault_id: uuid.UUID,
    *,
    size_bytes: int,
    content_hash: str,
    hash_algorithm: str,
    etag: str | None,
    storage_version: str | None,
) -> bool:
    row = await conn.fetchrow(
        """
        UPDATE vault_files SET
            size_bytes = $1,
            content_hash = $2,
            hash_algorithm = $3,
            etag = $4,
            storage_version = $5,
            upload_state = 'confirmed',
            hash_verified_at = NOW(),
            updated_at = NOW()
        WHERE id = $6
          AND vault_id = $7
          AND kind = 'file'
          AND upload_state IN ('pending', 'confirmed')
        RETURNING id
        """,
        size_bytes, content_hash, hash_algorithm, etag, storage_version, file_id,
        vault_id,
    )
    return row is not None


async def repair_confirmed_file_metadata(
    conn,
    file_id: uuid.UUID,
    *,
    size_bytes: int,
    content_hash: str,
    hash_algorithm: str,
    etag: str | None,
    storage_version: str | None,
) -> bool:
    """Backfill integrity metadata without performing an upload transition.

    Operator repair may certify bytes belonging to an already-published legacy
    File, but it must never turn a pending presigned upload into a readable
    resource. Keeping repair and confirmation as distinct repository methods
    makes that state boundary structural rather than caller convention.
    """
    row = await conn.fetchrow(
        f"""
        UPDATE vault_files SET
            size_bytes = $1,
            content_hash = $2,
            hash_algorithm = $3,
            etag = $4,
            storage_version = $5,
            hash_verified_at = COALESCE(hash_verified_at, NOW()),
            updated_at = NOW()
        WHERE id = $6
          AND {confirmed_file_predicate("vault_files")}
        RETURNING id
        """,
        size_bytes, content_hash, hash_algorithm, etag, storage_version, file_id,
    )
    return row is not None


async def delete(conn, file_id: uuid.UUID) -> None:
    """Delete the metadata row only. Caller is responsible for S3 object
    lifecycle (s3_delete_outbox is enqueued by file_service in the same
    TX so the worker drains S3 deletions atomically with the DB write)."""
    await conn.execute("DELETE FROM vault_files WHERE id = $1", file_id)


async def list_for_vault(
    conn,
    vault_id: uuid.UUID,
    *,
    collection_id: uuid.UUID | None = None,
    scoped: bool = False,
    max_depth: int | None = None,
    prefix: str = "",
    limit: int = 50,
) -> list[dict]:
    """List files in a vault.

    Three filtering modes — pick one:

    * ``scoped=True`` — equality on ``collection_id``
      (``None`` ⇒ ``IS NULL``). Files directly inside that collection
      (or vault root).

    * ``max_depth`` is not ``None`` — tree-depth filter from ``prefix``.
      A file at collection ``X/Y`` has depth 2 from vault root, depth 1
      from prefix ``X``. NULL collection ⇒ depth 0. ``max_depth < 0``
      disables the depth filter (entire subtree). Used by the unified
      vault browse to honor ``depth=N``.

    Default (no flags) — every file regardless of collection. Preserved
    so legacy callers see no behaviour change.
    """
    if scoped:
        if collection_id is None:
            rows = await conn.fetch(
                f"""
                SELECT vf.id, vf.collection_id, c.path AS collection, vf.name,
                       vf.mime_type, vf.size_bytes, vf.description,
                       vf.created_by, vf.created_at, vf.content_hash,
                       vf.hash_algorithm, vf.etag, vf.storage_version,
                       vf.hash_verified_at
                  FROM vault_files vf
                  LEFT JOIN collections c ON c.id = vf.collection_id
                 WHERE vf.vault_id = $1 AND {confirmed_file_predicate("vf")}
                   AND vf.collection_id IS NULL
                 ORDER BY vf.created_at DESC
                 LIMIT $2
                """,
                vault_id, limit,
            )
        else:
            rows = await conn.fetch(
                f"""
                SELECT vf.id, vf.collection_id, c.path AS collection, vf.name,
                       vf.mime_type, vf.size_bytes, vf.description,
                       vf.created_by, vf.created_at, vf.content_hash,
                       vf.hash_algorithm, vf.etag, vf.storage_version,
                       vf.hash_verified_at
                  FROM vault_files vf
                  LEFT JOIN collections c ON c.id = vf.collection_id
                 WHERE vf.vault_id = $1 AND {confirmed_file_predicate("vf")}
                   AND vf.collection_id = $2
                 ORDER BY vf.created_at DESC
                 LIMIT $3
                """,
                vault_id, collection_id, limit,
            )
    elif max_depth is not None:
        params: list = [vault_id]
        prefix_clause = ""
        if prefix:
            from app.util.text import like_escape
            safe_prefix = like_escape(prefix)
            params.append(safe_prefix)
            params.append(safe_prefix + "/%")
            prefix_clause = (
                f" AND (c.path = ${len(params)-1} "
                f"OR c.path LIKE ${len(params)} ESCAPE '\\')"
            )
            depth_offset = prefix.count("/") + 1
        else:
            depth_offset = 0

        if max_depth < 0:
            depth_clause = ""
        else:
            params.append(max_depth + depth_offset)
            depth_clause = (
                f" AND COALESCE("
                f"length(c.path) - length(replace(c.path, '/', '')) + 1, 0"
                f") <= ${len(params)}"
            )
        params.append(limit)
        sql = (
            "SELECT vf.id, vf.collection_id, c.path AS collection, vf.name, "
            "       vf.mime_type, vf.size_bytes, vf.description, "
            "       vf.created_by, vf.created_at, vf.content_hash, "
            "       vf.hash_algorithm, vf.etag, vf.storage_version, "
            "       vf.hash_verified_at "
            "  FROM vault_files vf "
            "  LEFT JOIN collections c ON c.id = vf.collection_id "
            f" WHERE vf.vault_id = $1 AND {confirmed_file_predicate('vf')}"
            + prefix_clause
            + depth_clause
            + f" ORDER BY vf.created_at DESC LIMIT ${len(params)}"
        )
        rows = await conn.fetch(sql, *params)
    else:
        rows = await conn.fetch(
            f"""
            SELECT vf.id, vf.collection_id, c.path AS collection, vf.name,
                   vf.mime_type, vf.size_bytes, vf.description,
                   vf.created_by, vf.created_at, vf.content_hash,
                   vf.hash_algorithm, vf.etag, vf.storage_version,
                   vf.hash_verified_at
              FROM vault_files vf
              LEFT JOIN collections c ON c.id = vf.collection_id
             WHERE vf.vault_id = $1 AND {confirmed_file_predicate("vf")}
             ORDER BY vf.created_at DESC
             LIMIT $2
            """,
            vault_id, limit,
        )
    return [dict(r) for r in rows]
