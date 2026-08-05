"""Migration 053: give document publications a database-enforced identity.

What this adds
--------------
    publications.document_id UUID                       -- nullable, see below
    documents  UNIQUE (id, vault_id)                    -- FK target
    publications FOREIGN KEY (document_id, vault_id)
        REFERENCES documents (id, vault_id) ON DELETE CASCADE
    partial index on publications (document_id, vault_id)

Migration 022 collapsed ``publications.document_id`` and
``publications.file_id`` into a single path-shaped ``resource_uri`` and, with
them, dropped the cascade the database used to enforce. Everything that keeps
a publication and its document consistent has been Python-level since: one
repository method deletes documents and their publications together, a write
onto a path some publication still claims is refused, and resolution is bound
to the publication's own vault. Those hold for callers that go through the
application. A direct ``psql`` session, a future migration, or an admin script
that talks to the table does not go through the application. ``FOREIGN KEY …
ON DELETE CASCADE`` does, because there is no code path that can forget it.

Files are out of scope. ``resource_uri`` stays: ``table_query`` publications
have no resource id at all, the column is part of the API response shape, and
the ``move``-time URI rewrite still keeps it current.

Why the FK is composite, and why a simple one would be wrong
------------------------------------------------------------
``FOREIGN KEY (document_id) REFERENCES documents (id)`` would guarantee only
that the document exists. A publication in vault X could point at a document
in vault Y and satisfy it. Pairing the vault into the key makes "the document
lives in the publication's own vault" a structural property of the row rather
than another rule every future write has to remember — which is the whole
reason for preferring a constraint to a check in code.

``documents.id`` is already the primary key, so ``UNIQUE (id, vault_id)`` adds
no new guarantee about documents; PostgreSQL simply requires a unique
constraint on exactly the referenced column pair before it will accept the
composite reference.

The match type is the default (MATCH SIMPLE) and that matters: a row with
``document_id IS NULL`` is exempt from the constraint even though ``vault_id``
is NOT NULL. MATCH FULL would forbid the NULL entirely and turn every
un-bindable legacy row into a migration failure. Do not "tighten" it.

What the backfill binds — and what it deliberately leaves alone
---------------------------------------------------------------
The only handle an existing publication has on its document is
``resource_uri``, which names a path, and paths are reusable
(``documents`` is ``UNIQUE (vault_id, path)``). Binding a row to "whatever
document occupies that path now" would, for any row that had drifted, have the
new FK certify the drift permanently. So the backfill binds only rows where
the answer is unambiguous, and leaves ``document_id`` NULL for everything
else. It never deletes a row: the publication carries its slug, creator,
creation time, restrictions and view count, and that record is the input a
human needs to decide what to do with it.

A row is bound only when all of the following hold:

  1. ``resource_type = 'document'`` and ``resource_uri`` parses as a document
     URI. Parsing goes through ``uri_service.parse_uri`` — the same function
     the resolver uses — so the backfill cannot bind a row the resolver would
     not serve, and a URI shape neither can read stays NULL.
  2. The vault named in the URI equals the name of the vault the publication
     belongs to. A row whose two vault identifiers disagree is NOT bound, no
     matter what document exists at that path anywhere; it is logged as its
     own category. Resolution already refuses to serve such a row.
  3. Exactly one document sits at that path in the publication's own vault
     (guaranteed to be at most one by ``UNIQUE (vault_id, path)``).
  4. That document is not newer than the publication. A publication cannot
     have been created for a document that did not exist yet, so a newer
     document at the path means the path was reused and the original target
     is gone. This predicate can only refuse a binding, never invent one.
     Note that ``documents.created_at`` is written by the application and
     ``publications.created_at`` by the database, so the comparison inherits
     any clock skew between them: a publication created within that skew of
     its own document may be left NULL. That is the safe direction, and a
     NULL row keeps working exactly as it does today.

Consequence, stated plainly: because NULL is allowed, "every document
publication points at a document" is NOT a schema invariant after this
migration. It becomes one only for rows created once the publish path records
the id. Nothing here should be read as proving the column is populated.

Ordering: constrain BEFORE backfill
-----------------------------------
The FK is added while every ``document_id`` is still NULL, which makes its
initial validation trivially satisfiable, and — the actual point — it means
the database checks every binding the backfill writes, at the moment it is
written. The classical expand → backfill → constrain order would have the
backfill write bindings nothing had verified and then certify them wholesale
in one statement. That is the failure mode this migration exists to avoid, so
the order is inverted on purpose.

For the same reason this is one migration and not three. The steps have a
fixed order, the runner applies them back-to-back at boot, and splitting them
would add ledger entries without giving an operator any new place to
intervene. Each step is independently idempotent instead: an interrupted run
re-enters and completes, and a partially-bound table is a legitimate resting
state, not damage.

``ADD CONSTRAINT … NOT VALID`` + ``VALIDATE CONSTRAINT`` was considered and
rejected. It would save the initial scan of ``publications`` — a table with
one row per published link — while still taking the same ACCESS EXCLUSIVE
lock to write the catalog entry, and it would leave behind a constraint that
is enforced but unverified if the second step never ran. The saving is not
worth a state where the schema looks stronger than it is. (If ``publications``
ever grew to a size where that scan mattered, this is the decision to revisit.)

Locks and timeouts
------------------
The runner sets ``lock_timeout = '5s'`` and the pool cancels any statement at
30s, and both apply here. The one statement that does real work proportional
to table size is the unique index behind ``documents_id_vault_id_key``; it is
built plainly, not CONCURRENTLY, because a plain build inside its own implicit
transaction leaves nothing behind when it is cancelled, whereas a cancelled
CONCURRENTLY build leaves an *invalid* index that ``IF NOT EXISTS`` would then
skip over forever. Failing loudly and retrying beats a schema that quietly
believes it has an index.

If ``documents`` is ever large enough that the in-line build cannot finish
inside the 30s cancel, the escape hatch is to build the index out of band and
re-run:

    CREATE UNIQUE INDEX CONCURRENTLY documents_id_vault_id_key
        ON documents (id, vault_id);

This migration adopts a pre-existing valid index of that name via
``ADD CONSTRAINT … UNIQUE USING INDEX`` instead of rebuilding it, and drops
one left invalid by an interrupted build before trying again.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import close_pool, get_pool, init_db
from app.services.uri_service import parse_uri

logger = logging.getLogger("akb.migration.053")

_DOC_UNIQUE = "documents_id_vault_id_key"
_PUB_FK = "publications_document_fk"
_PUB_INDEX = "idx_publications_document_id"

# Rows per UPDATE. Keeps any single statement well inside the pool's 30s
# cancel regardless of how many publications a deployment has accumulated.
_BATCH = 500


async def migrate(conn=None):
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _column_exists(conn, table: str, column: str) -> bool:
    return bool(await conn.fetchval(
        """
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = 'public'
           AND table_name = $1
           AND column_name = $2
        """,
        table, column,
    ))


async def _constraint_exists(conn, table: str, name: str) -> bool:
    return bool(await conn.fetchval(
        """
        SELECT 1 FROM pg_constraint
         WHERE conname::text = $2::text
           AND conrelid = to_regclass('public.' || $1::text)
        """,
        table, name,
    ))


async def _ensure_documents_identity_unique(conn) -> None:
    """``documents UNIQUE (id, vault_id)`` — the composite FK's target.

    Three states to handle, because the index build is the one step an
    operator may have run ahead of time (or had cancelled underneath them):
    no index, a valid index of the right shape, or an invalid leftover.
    """
    if await _constraint_exists(conn, "documents", _DOC_UNIQUE):
        return

    existing = await conn.fetchrow(
        """
        SELECT i.indisvalid                AS is_valid,
               i.indisunique               AS is_unique,
               i.indpred IS NOT NULL       AS is_partial,
               i.indexprs IS NOT NULL      AS is_expression,
               i.indkey::text              AS indkey,
               (SELECT a.attnum FROM pg_attribute a
                 WHERE a.attrelid = i.indrelid AND a.attname = 'id') AS id_attnum,
               (SELECT a.attnum FROM pg_attribute a
                 WHERE a.attrelid = i.indrelid AND a.attname = 'vault_id') AS vault_attnum
          FROM pg_index i
          JOIN pg_class c ON c.oid = i.indexrelid
         WHERE c.relname = $1
           AND c.relnamespace = 'public'::regnamespace
        """,
        _DOC_UNIQUE,
    )

    if existing is not None:
        expected_key = f"{existing['id_attnum']} {existing['vault_attnum']}"
        right_shape = (
            existing["is_unique"]
            and not existing["is_partial"]
            and not existing["is_expression"]
            and existing["indkey"] == expected_key
        )
        if not right_shape:
            raise RuntimeError(
                f"An index named {_DOC_UNIQUE} already exists on documents but is "
                "not a plain UNIQUE index on exactly (id, vault_id). Migration 053 "
                "will not repurpose it — inspect it, then either drop it or add the "
                "constraint by hand."
            )
        if existing["is_valid"]:
            # Adopt it: the constraint takes the index's name and no rebuild
            # happens. This is the out-of-band build path documented above.
            logger.info(
                "Migration 053: adopting the pre-existing valid index %s as the "
                "documents identity constraint (no rebuild)", _DOC_UNIQUE,
            )
            await conn.execute(
                f"ALTER TABLE documents ADD CONSTRAINT {_DOC_UNIQUE} "
                f"UNIQUE USING INDEX {_DOC_UNIQUE}"
            )
            return
        # Invalid: the remains of a cancelled CONCURRENTLY build. It enforces
        # nothing and `IF NOT EXISTS` would step over it silently, so drop it
        # and build for real below.
        logger.warning(
            "Migration 053: found an INVALID index named %s (a cancelled "
            "CREATE INDEX CONCURRENTLY leaves one behind); dropping it before "
            "building the constraint", _DOC_UNIQUE,
        )
        await conn.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_DOC_UNIQUE}")

    await conn.execute(
        f"ALTER TABLE documents ADD CONSTRAINT {_DOC_UNIQUE} UNIQUE (id, vault_id)"
    )
    logger.info("Migration 053: added %s on documents (id, vault_id)", _DOC_UNIQUE)


async def _ensure_publication_document_column(conn) -> None:
    if await _column_exists(conn, "publications", "document_id"):
        return
    await conn.execute("ALTER TABLE publications ADD COLUMN document_id UUID")
    logger.info("Migration 053: added publications.document_id (nullable)")


async def _ensure_publication_document_fk(conn) -> None:
    if await _constraint_exists(conn, "publications", _PUB_FK):
        return
    await conn.execute(
        f"ALTER TABLE publications ADD CONSTRAINT {_PUB_FK} "
        "FOREIGN KEY (document_id, vault_id) REFERENCES documents (id, vault_id) "
        "ON DELETE CASCADE"
    )
    logger.info(
        "Migration 053: added %s — (document_id, vault_id) → documents (id, vault_id) "
        "ON DELETE CASCADE", _PUB_FK,
    )


async def _ensure_publication_document_index(conn) -> None:
    """Index the referencing side of the FK.

    Without it every document delete seq-scans ``publications`` to find rows
    to cascade. Partial on ``document_id IS NOT NULL`` because most rows are
    NULL after this migration and the cascade probe (``document_id = $1``)
    implies NOT NULL, so the planner can still use it.
    """
    await conn.execute(
        f"CREATE INDEX IF NOT EXISTS {_PUB_INDEX} "
        "ON publications (document_id, vault_id) WHERE document_id IS NOT NULL"
    )


async def _backfill(conn) -> None:
    """Bind existing document publications to their document, where that is
    unambiguous. See the module docstring for the four conditions.

    Re-runnable: only rows with ``document_id IS NULL`` are considered, so a
    second run binds nothing and reports the already-bound rows under their
    own count instead of adding them to this run's total.
    """
    already_bound = await conn.fetchval(
        "SELECT COUNT(*) FROM publications "
        "WHERE resource_type = 'document' AND document_id IS NOT NULL"
    )
    skipped_by_design = await conn.fetchval(
        "SELECT COUNT(*) FROM publications WHERE resource_type <> 'document'"
    )

    rows = await conn.fetch(
        """
        SELECT p.id, p.resource_uri, v.name AS vault_name
          FROM publications p
          JOIN vaults v ON v.id = p.vault_id
         WHERE p.resource_type = 'document'
           AND p.document_id IS NULL
        """
    )

    candidate_ids: list = []
    candidate_paths: list[str] = []
    unreadable_uri = 0
    vault_mismatch: list = []

    for row in rows:
        uri = row["resource_uri"]
        parsed = parse_uri(uri) if uri else None
        if parsed is None or parsed.kind != "doc" or not parsed.identifier:
            unreadable_uri += 1
            continue
        if parsed.vault != row["vault_name"]:
            # The URI names one vault and the row belongs to another. Do not
            # bind it to anything: resolution already refuses to serve this
            # row, and "some document exists at that path somewhere" is not
            # evidence about which document this publication was made for.
            vault_mismatch.append(row["id"])
            continue
        candidate_ids.append(row["id"])
        candidate_paths.append(parsed.identifier)

    bound = 0
    unbound_ids: list = []
    unbound_paths: list[str] = []
    for start in range(0, len(candidate_ids), _BATCH):
        ids = candidate_ids[start:start + _BATCH]
        paths = candidate_paths[start:start + _BATCH]
        updated = await conn.fetch(
            """
            WITH candidate AS (
                SELECT pub_id, doc_path
                  FROM unnest($1::uuid[], $2::text[]) AS t(pub_id, doc_path)
            )
            UPDATE publications p
               SET document_id = d.id
              FROM candidate c, documents d
             WHERE p.id = c.pub_id
               AND p.document_id IS NULL
               AND p.resource_type = 'document'
               AND d.vault_id = p.vault_id
               AND d.path = c.doc_path
               AND d.created_at <= p.created_at
            RETURNING p.id
            """,
            ids, paths,
        )
        # `updated_at` is deliberately not touched: this is a schema backfill,
        # not an edit anyone made to the publication.
        bound += len(updated)
        done = {r["id"] for r in updated}
        for pub_id, path in zip(ids, paths, strict=True):
            if pub_id not in done:
                unbound_ids.append(pub_id)
                unbound_paths.append(path)

    # Split the leftovers: a path with no document at all versus a path now
    # occupied by a document that postdates the publication.
    path_reused = 0
    for start in range(0, len(unbound_ids), _BATCH):
        ids = unbound_ids[start:start + _BATCH]
        paths = unbound_paths[start:start + _BATCH]
        path_reused += await conn.fetchval(
            """
            SELECT COUNT(*)
              FROM unnest($1::uuid[], $2::text[]) AS c(pub_id, doc_path)
              JOIN publications p ON p.id = c.pub_id
              JOIN documents d
                ON d.vault_id = p.vault_id AND d.path = c.doc_path
            """,
            ids, paths,
        )
    no_document = len(unbound_ids) - path_reused

    logger.info(
        "Migration 053 backfill: bound=%d, no_document=%d, path_reused=%d, "
        "unreadable_uri=%d, vault_mismatch=%d, already_bound=%d, "
        "non_document_publications=%d (of %d document publications examined "
        "this run)",
        bound, no_document, path_reused, unreadable_uri, len(vault_mismatch),
        already_bound or 0, skipped_by_design or 0, len(rows),
    )
    if vault_mismatch:
        # The most interesting rows in the table if they exist: the vault the
        # URI names is not the vault the row belongs to. Ids only — enough to
        # pull the rows with a SELECT, without putting names or paths in a log.
        shown = [str(i) for i in vault_mismatch[:20]]
        logger.warning(
            "Migration 053: %d document publication(s) name a vault other than "
            "their own and were left unbound. publication id(s): %s%s",
            len(vault_mismatch), ", ".join(shown),
            "" if len(vault_mismatch) <= 20 else " (first 20 shown)",
        )
    if no_document or path_reused or unreadable_uri:
        logger.warning(
            "Migration 053: %d document publication(s) left unbound "
            "(no_document=%d, path_reused=%d, unreadable_uri=%d). They keep "
            "working through resource_uri; document_id stays NULL until they "
            "are re-published.",
            no_document + path_reused + unreadable_uri,
            no_document, path_reused, unreadable_uri,
        )


async def _run(conn):
    # No enclosing transaction. Each step is idempotent on its own, so a run
    # cancelled between steps re-enters and finishes; wrapping them together
    # would instead throw away a completed index build because a later lock
    # wait timed out.
    await _ensure_publication_document_column(conn)
    await _ensure_documents_identity_unique(conn)
    await _ensure_publication_document_fk(conn)
    await _ensure_publication_document_index(conn)
    await _backfill(conn)


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
