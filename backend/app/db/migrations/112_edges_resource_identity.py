"""Migration 112: bind graph edges to the native ledger's stable resource identity.

`edges` has always keyed an endpoint by its path-derived URI. On the bare-Git
arm that was sound: a `documents` row is deleted synchronously with the path it
owns, so "the edges at this URI" and "this document's edges" are the same set.

The `postgres_native` arm broke the equivalence. There, identity is
`native_resources.resource_id` and `current_path` is a mutable attribute that a
DIFFERENT resource can take over the moment the previous owner is moved away or
deleted — and the graph work runs asynchronously (derived worker) or after the
authoritative commit (facade hook), so it can land in a window where the path
has already changed hands. Every lifecycle operation keyed on the path then
acts on "whoever owns this path now" instead of "this resource":

- a delayed delete erased the replacement document's links (akb#654),
- a delayed move rewrite erased the new path owner's implicit links,
- two moves whose post-commit hooks finished out of order left the edge on the
  intermediate path (akb#655).

The columns added here are the anchor: maintenance finds rows by resource
identity and repoints their URI at the resource's CURRENT head path, which also
makes the operation idempotent and order-independent.

Scope of the identity column, deliberately narrow:

- It holds a `native_resources.resource_id` and NEVER a legacy `documents.id`.
  One column carrying two id spaces would be indistinguishable at the point of
  use, which is the same class of mistake this migration exists to repair. The
  legacy arm needs no anchor — its delete is synchronous with the catalog row.
- It is NULL for every endpoint the native ledger does not own (legacy
  documents, tables, files). Nothing reads it as "this edge is valid".

Backfill matches live native document resources to the edges naming their
current URI. It is computed in Python through `uri_service.doc_uri` rather than
restated as a SQL expression, so the URI grammar keeps exactly one definition.
A pre-existing edge whose URI names a path that has ALREADY changed hands is
matched to the path's current owner — this migration repairs nothing that was
corrupted before it ran, and does not pretend to.
"""

from __future__ import annotations

import logging


logger = logging.getLogger("akb.migration.112")


async def migrate(conn) -> None:
    async with conn.transaction():
        await conn.execute(
            """
            ALTER TABLE edges
                ADD COLUMN IF NOT EXISTS source_resource_id UUID,
                ADD COLUMN IF NOT EXISTS target_resource_id UUID
            """
        )
        await conn.execute(
            "COMMENT ON COLUMN edges.source_resource_id IS "
            "'native_resources.resource_id of the source endpoint, or NULL when "
            "the native ledger does not own it. Never a legacy documents.id.'"
        )
        await conn.execute(
            "COMMENT ON COLUMN edges.target_resource_id IS "
            "'native_resources.resource_id of the target endpoint, or NULL when "
            "the native ledger does not own it. Never a legacy documents.id.'"
        )
        # Partial: the great majority of rows on a legacy installation carry
        # NULL, and every lookup that uses these columns asks for a specific
        # non-NULL id.
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_edges_source_resource "
            "ON edges(vault_id, source_resource_id) "
            "WHERE source_resource_id IS NOT NULL"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_edges_target_resource "
            "ON edges(vault_id, target_resource_id) "
            "WHERE target_resource_id IS NOT NULL"
        )

        # `native_resources` is created by migration 048, so the constraint
        # lives here rather than in init.sql (which builds `edges` before the
        # native ledger exists). MATCH SIMPLE leaves a NULL identity
        # unchecked, which is exactly the legacy-endpoint case.
        #
        # NOT VALID on purpose: adding a validated foreign key scans `edges`,
        # and at this instant every value in both columns is NULL, so the scan
        # can only confirm what the ALTER already knows. It still enforces
        # every row inserted or updated from here on — including the backfill
        # below.
        #
        # It does NOT buy a gentler lock here, and planning a production window
        # on the assumption that it does would be wrong. `_run_one_migration` runs every
        # migration inside its own transaction so the effects and the ledger
        # receipt commit as one unit, which makes the `conn.transaction()` above
        # a savepoint rather than a top-level transaction. The VALIDATE below is
        # therefore still inside that outer transaction and inherits the ALTER's
        # ACCESS EXCLUSIVE lock. Budget for `edges` being held under ACCESS
        # EXCLUSIVE for this whole migration, backfill included.
        native_present = await conn.fetchval(
            "SELECT to_regclass('public.native_resources') IS NOT NULL"
        )
        added: list[str] = []
        if native_present:
            for column, constraint in _FOREIGN_KEYS:
                exists = await conn.fetchval(
                    "SELECT 1 FROM pg_constraint WHERE conname = $1", constraint,
                )
                if exists:
                    continue
                await conn.execute(
                    f"""
                    ALTER TABLE edges ADD CONSTRAINT {constraint}
                        FOREIGN KEY (vault_id, {column})
                        REFERENCES native_resources(namespace_id, resource_id)
                        ON DELETE CASCADE
                        NOT VALID
                    """
                )
                added.append(constraint)

        stamped = 0
        if native_present:
            stamped = await _backfill(conn)

    for constraint in added:
        # Outside the savepoint, not outside a transaction — see above. Kept
        # separate so this becomes the cheap SHARE UPDATE EXCLUSIVE validate the
        # moment a migration can opt out of the runner's transaction.
        await conn.execute(f"ALTER TABLE edges VALIDATE CONSTRAINT {constraint}")

    logger.info(
        "Migration 112 applied: edges resource-identity columns + indexes; "
        "%d endpoint(s) stamped from the native ledger",
        stamped,
    )


_FOREIGN_KEYS = (
    ("source_resource_id", "edges_source_native_resource_fkey"),
    ("target_resource_id", "edges_target_native_resource_fkey"),
)

_BACKFILL_BATCH = 1000


async def _backfill(conn) -> int:
    """Stamp identity on edges whose URI names a live native document.

    Set-based, in batches: a vault can hold a six-figure document count, and a
    statement pair per document would make this migration the slowest thing in
    the boot sequence. The URIs are still built by `uri_service.doc_uri`, so
    the grammar keeps exactly one definition — the batch is carried into SQL as
    parallel arrays rather than restated as a SQL expression.
    """
    from app.services.uri_service import doc_uri

    rows = await conn.fetch(
        """
        SELECT r.resource_id, r.namespace_id, r.current_path, v.name AS vault_name
          FROM native_resources r
          JOIN vaults v ON v.id = r.namespace_id
         WHERE r.surface = 'document' AND r.lifecycle = 'live'
         ORDER BY r.namespace_id, r.resource_id
        """
    )
    if not rows:
        return 0

    stamped = 0
    for start in range(0, len(rows), _BACKFILL_BATCH):
        batch = rows[start:start + _BACKFILL_BATCH]
        resource_ids = [r["resource_id"] for r in batch]
        vault_ids = [r["namespace_id"] for r in batch]
        uris = [doc_uri(r["vault_name"], r["current_path"]) for r in batch]
        for column, uri_column, type_column in (
            ("source_resource_id", "source_uri", "source_type"),
            ("target_resource_id", "target_uri", "target_type"),
        ):
            stamped += _affected(
                await conn.execute(
                    f"""
                    UPDATE edges e SET {column} = m.resource_id
                      FROM unnest($1::uuid[], $2::uuid[], $3::text[])
                        AS m(resource_id, vault_id, uri)
                     WHERE e.vault_id = m.vault_id
                       AND e.{uri_column} = m.uri
                       AND e.{type_column} = 'doc'
                       AND e.{column} IS NULL
                    """,
                    resource_ids, vault_ids, uris,
                )
            )
    return stamped


def _affected(status: str) -> int:
    """Row count from an asyncpg command tag (``UPDATE <n>``)."""
    try:
        return int(status.rsplit(" ", 1)[1])
    except (IndexError, ValueError):
        return 0
