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
     document at the path is proof the path was reused. Be clear about what
     this is: a filter that removes some wrong answers, NOT proof that the
     surviving ones are right. An *older* document moved onto the path after
     the original was deleted would pass it. (The current write path refuses
     a move onto a path some publication still claims, so that shape should
     not be produced any more — but this migration is written for a database
     whose history nobody has re-verified.) The predicate can only refuse a
     binding, never invent one, which is the whole reason to keep it.
     Note that ``documents.created_at`` is written by the application and
     ``publications.created_at`` by the database, so the comparison inherits
     any clock skew between them: a publication created within that skew of
     its own document may be left NULL. That is the safe direction, and a
     NULL row keeps working exactly as it does today.

Everything the parse produced is re-asserted inside the UPDATE — the row's
``resource_uri`` must still be the string that was parsed, and its vault must
still carry the name that string named. A publication moved, or a vault
renamed, between the read and the write therefore leaves the row unbound
rather than bound to what its old URI used to mean.

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
rejected. Adding the FK takes SHARE ROW EXCLUSIVE on both ``publications`` and
``documents`` — measured, not assumed — which blocks writes to both for the
duration of the validation scan. ``NOT VALID`` would take the same lock and
skip the scan, so what it buys is exactly the length of one sequential scan of
``publications``, a table with one row per published link. Against that it
leaves a constraint that is enforced for new rows but unverified for old ones
if the second step never ran, and a schema that looks stronger than it is is
the failure mode this whole change is trying to remove. If ``publications``
ever grows to where that scan is a real outage window, this is the decision to
revisit — the split is mechanical.

Locks and timeouts
------------------
The runner sets ``lock_timeout = '5s'`` and the pool cancels any statement at
30s, and both apply here. ``lock_timeout`` bounds only the wait to ACQUIRE a
lock, not how long one is held once acquired; the 30s cancel is what bounds
the latter.

Every statement in the backfill is bounded to ``_BATCH`` rows, so its cost
does not scale with the table. Two DDL statements do scale:

  * ``ALTER TABLE documents ADD CONSTRAINT documents_id_vault_id_key UNIQUE``
    builds an index over ``documents`` while holding ACCESS EXCLUSIVE. This is
    the worst statement here, and on a large enough table it will be cancelled
    at 30s, roll back, and make no progress across retries.
  * the FK's validation scan over ``publications``, under SHARE ROW EXCLUSIVE
    on both tables.

The index is built plainly, not CONCURRENTLY, because a plain build inside its
own implicit transaction leaves nothing behind when it is cancelled, whereas a
cancelled CONCURRENTLY build leaves an *invalid* index that ``IF NOT EXISTS``
would then skip over forever. Failing loudly and retrying beats a schema that
quietly believes it has an index.

If ``documents`` is ever large enough that the in-line build cannot finish
inside the 30s cancel, the escape hatch is to build the index out of band and
re-run:

    CREATE UNIQUE INDEX CONCURRENTLY documents_id_vault_id_key
        ON documents (id, vault_id);

This migration adopts a pre-existing valid index of that name via
``ADD CONSTRAINT … UNIQUE USING INDEX`` instead of rebuilding it, and drops
one left invalid by an interrupted build before trying again.

init.sql keeps the same shape, and has to stay runnable against a database
that has NOT reached this migration yet: it is re-executed in full on every
boot, before any migration. ``CREATE TABLE IF NOT EXISTS`` is inert on an
existing table, but a bare ``CREATE INDEX`` on the new column is not — it
raises ``UndefinedColumn`` and the boot never reaches the migrations. The
index statement there is therefore guarded on the column's existence.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import uuid as uuid_mod
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import close_pool, get_pool, init_db
from app.services.uri_service import parse_uri

logger = logging.getLogger("akb.migration.053")

_DOC_UNIQUE = "documents_id_vault_id_key"
_PUB_FK = "publications_document_fk"
_PUB_INDEX = "idx_publications_document_id"

# What the guards below require of an existing constraint, read out of the
# catalog rather than out of `pg_get_constraintdef`. The deparsed text would be
# easier to compare and is the wrong thing to compare: it is a reconstruction
# whose relation names are qualified according to the session's search_path, so
# the identical constraint can render as `REFERENCES documents(…)` or
# `REFERENCES public.documents(…)` — and a guard that refuses the second one
# would fail a boot over a formatting difference. Catalog columns do not move.
#
# (contype, referenced table, local cols, referenced cols, on-delete,
#  on-update, match type, validated)
_DOC_UNIQUE_SHAPE = ("u", None, ["id", "vault_id"], None, " ", " ", " ", True)
_PUB_FK_SHAPE = (
    "f", "documents", ["document_id", "vault_id"], ["id", "vault_id"],
    # c = CASCADE on delete; a = NO ACTION on update (so the vault half of the
    # key cannot be moved out from under the pairing); s = MATCH SIMPLE (so a
    # NULL document_id is exempt — see the module docstring).
    "c", "a", "s", True,
)
# Substring of `pg_get_indexdef` that pins the cascade index's columns and
# predicate. Only the parts that carry meaning — the access method and schema
# qualification in that string are formatting, and the table it sits on is
# checked against the catalog separately.
_PUB_INDEX_COLS = "(document_id, vault_id) WHERE (document_id IS NOT NULL)"

# Rows per UPDATE. Keeps any single statement well inside the pool's 30s
# cancel regardless of how many publications a deployment has accumulated.
_BATCH = 500

# How many vault-mismatch publication ids the log names individually.
_MISMATCH_SAMPLE = 20


async def migrate(conn=None):
    """Apply the migration. Returns the backfill's per-category counts.

    The runner ignores the return value; it exists so the counts are a value
    the caller can assert on rather than only a line in a log. What an
    operator reads to decide whether a backfill can be trusted should be
    something a test can hold to account.
    """
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            return await _run(new_conn)
    return await _run(conn)


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


async def _constraint_shape(conn, table: str, name: str) -> tuple | None:
    """The constraint's structure as the catalog records it, or None if no
    constraint of that name exists on that table.

    Structure rather than existence on purpose. A name-only check would let a
    CHECK constraint, an unvalidated FK, or an FK over the wrong columns
    wearing the right name satisfy the idempotency guard — the migration would
    return happily, get recorded in the ledger, and leave the guarantee it
    advertises uninstalled.
    """
    row = await conn.fetchrow(
        """
        SELECT c.contype::text                                   AS contype,
               (SELECT relname::text FROM pg_class
                 WHERE oid = NULLIF(c.confrelid, 0))             AS target,
               (SELECT array_agg(a.attname::text ORDER BY k.ord)
                  FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
                  JOIN pg_attribute a
                    ON a.attrelid = c.conrelid AND a.attnum = k.attnum) AS local_cols,
               (SELECT array_agg(a.attname::text ORDER BY k.ord)
                  FROM unnest(c.confkey) WITH ORDINALITY AS k(attnum, ord)
                  JOIN pg_attribute a
                    ON a.attrelid = c.confrelid AND a.attnum = k.attnum) AS target_cols,
               c.confdeltype::text                               AS on_delete,
               c.confupdtype::text                               AS on_update,
               c.confmatchtype::text                             AS match_type,
               c.convalidated                                    AS validated
          FROM pg_constraint c
         WHERE c.conname::text = $2::text
           AND c.conrelid = to_regclass('public.' || $1::text)
        """,
        table, name,
    )
    if row is None:
        return None
    return (
        row["contype"], row["target"], row["local_cols"], row["target_cols"],
        row["on_delete"], row["on_update"], row["match_type"], row["validated"],
    )


async def _already_correct(conn, table: str, name: str, expected: tuple) -> bool:
    """True when `name` is already the constraint we would create; raises when
    something else is wearing the name."""
    found = await _constraint_shape(conn, table, name)
    if found is None:
        return False
    if found != expected:
        raise RuntimeError(
            f"{table} already has a constraint named {name}, but it is not the "
            f"one migration 053 installs.\n  found:    {found}\n  expected: {expected}\n"
            "(fields: contype, referenced table, local columns, referenced "
            "columns, on-delete, on-update, match type, validated)\n"
            "Migration 053 will not silently accept it — inspect the constraint, "
            "then drop or rename it and re-run."
        )
    return True


async def _ensure_documents_identity_unique(conn) -> None:
    """``documents UNIQUE (id, vault_id)`` — the composite FK's target.

    Three states to handle, because the index build is the one step an
    operator may have run ahead of time (or had cancelled underneath them):
    no index, a valid index of the right shape, or an invalid leftover.
    """
    if await _already_correct(conn, "documents", _DOC_UNIQUE, _DOC_UNIQUE_SHAPE):
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
        #
        # A plain DROP, not CONCURRENTLY. Dropping concurrently waits for every
        # transaction that could be using the index — a wait `lock_timeout`
        # does not bound, which makes it the statement here most likely to be
        # cancelled at the pool's 30s limit — and it cannot run inside a
        # transaction at all. All to avoid a brief lock while removing an
        # index that is invalid, and therefore in use by nothing.
        logger.warning(
            "Migration 053: found an INVALID index named %s (a cancelled "
            "CREATE INDEX CONCURRENTLY leaves one behind); dropping it before "
            "building the constraint", _DOC_UNIQUE,
        )
        await conn.execute(f"DROP INDEX IF EXISTS {_DOC_UNIQUE}")

    await conn.execute(
        f"ALTER TABLE documents ADD CONSTRAINT {_DOC_UNIQUE} UNIQUE (id, vault_id)"
    )
    logger.info("Migration 053: added %s on documents (id, vault_id)", _DOC_UNIQUE)


async def _ensure_publication_identity(conn) -> None:
    """Add the column and its foreign key as ONE unit.

    Together, not one after the other: a column that exists without its FK is
    an unconstrained UUID column on a live table, and anything that wrote a
    dangling or cross-vault value into that window would make every later
    attempt to add the constraint fail. The pair costs nothing to redo — the
    column add is a catalog write and the FK's validation scan runs against
    all-NULL data — so there is no progress worth preserving by splitting
    them, and a great deal worth preserving by not leaving that window open.
    """
    have_column = await _column_exists(conn, "publications", "document_id")
    have_fk = await _already_correct(conn, "publications", _PUB_FK, _PUB_FK_SHAPE)
    if have_column and have_fk:
        return

    async with conn.transaction():
        if not have_column:
            await conn.execute("ALTER TABLE publications ADD COLUMN document_id UUID")
            logger.info("Migration 053: added publications.document_id (nullable)")
        if not have_fk:
            try:
                await conn.execute(
                    f"ALTER TABLE publications ADD CONSTRAINT {_PUB_FK} "
                    "FOREIGN KEY (document_id, vault_id) "
                    "REFERENCES documents (id, vault_id) ON DELETE CASCADE"
                )
            except asyncpg.ForeignKeyViolationError as e:
                raise RuntimeError(
                    "Migration 053 cannot add the publication identity constraint: "
                    "publications already holds a document_id that does not name a "
                    "document in that publication's own vault. Nothing here will "
                    "delete or blank those rows — that is a decision for a human. "
                    "List them with:\n"
                    "  SELECT p.id, p.vault_id, p.document_id FROM publications p\n"
                    "   WHERE p.document_id IS NOT NULL AND NOT EXISTS (\n"
                    "     SELECT 1 FROM documents d\n"
                    "      WHERE d.id = p.document_id AND d.vault_id = p.vault_id);\n"
                    f"underlying error: {e}"
                ) from e
            logger.info(
                "Migration 053: added %s — (document_id, vault_id) → "
                "documents (id, vault_id) ON DELETE CASCADE", _PUB_FK,
            )


async def _ensure_publication_document_index(conn) -> None:
    """Index the referencing side of the FK.

    Without it every document delete seq-scans ``publications`` to find rows
    to cascade. Partial on ``document_id IS NOT NULL`` because most rows are
    NULL after this migration and the cascade probe (``document_id = $1``)
    implies NOT NULL, so the planner can still use it.

    Checked structurally rather than by `IF NOT EXISTS` alone, for the same
    reason as the constraints. Index names are unique per schema, not per
    table, so "a relation of this name exists" says nothing on its own: it
    could be an index on another table, or an invalid leftover from a
    cancelled concurrent build that enforces and accelerates nothing while
    `IF NOT EXISTS` steps over it forever.
    """
    found = await conn.fetchrow(
        """
        SELECT i.indisvalid                          AS is_valid,
               i.indisunique                         AS is_unique,
               i.indrelid = to_regclass('public.publications') AS on_publications,
               pg_get_indexdef(i.indexrelid)         AS definition
          FROM pg_index i
         WHERE i.indexrelid = to_regclass('public.' || $1::text)
        """,
        _PUB_INDEX,
    )
    if found is not None:
        # `is_unique` is checked for the same reason the documents guard checks
        # it, inverted: this one must NOT be unique. Adopting a UNIQUE index of
        # that name would quietly forbid a second publication of one document.
        if (
            not found["on_publications"]
            or found["is_unique"]
            or _PUB_INDEX_COLS not in found["definition"]
        ):
            raise RuntimeError(
                f"An index named {_PUB_INDEX} already exists but is not the "
                f"cascade index migration 053 creates.\n  found: {found['definition']}\n"
                "Inspect it, then drop or rename it and re-run."
            )
        if found["is_valid"]:
            return
        logger.warning(
            "Migration 053: found an INVALID index named %s; dropping it before "
            "rebuilding", _PUB_INDEX,
        )
        await conn.execute(f"DROP INDEX IF EXISTS {_PUB_INDEX}")
    await conn.execute(
        f"CREATE INDEX {_PUB_INDEX} "
        "ON publications (document_id, vault_id) WHERE document_id IS NOT NULL"
    )


async def _backfill(conn) -> dict:
    """Bind existing document publications to their document, where that is
    unambiguous. See the module docstring for the conditions. Returns the
    per-category counts it logs.

    Re-runnable: only rows with ``document_id IS NULL`` are considered, so a
    second run binds nothing and reports the already-bound rows under their
    own count instead of adding them to this run's total.

    Paged by primary key. Every statement here is bounded by ``_BATCH`` rows
    so that neither the pool's 30s cancel nor this process's memory depends on
    how many publications a deployment has accumulated. Rows bound by an
    earlier page drop out of the predicate rather than shifting the window, so
    the key order stays stable across pages.

    Publication ids are random, so a row inserted concurrently below the
    cursor is not visited by this run. That is not a defect to work around:
    an unvisited row is simply an unbound row, which is a state this migration
    already leaves behind by design and which resolution handles the same way
    it does today.
    """
    already_bound = await conn.fetchval(
        "SELECT COUNT(*) FROM publications "
        "WHERE resource_type = 'document' AND document_id IS NOT NULL"
    )
    skipped_by_design = await conn.fetchval(
        "SELECT COUNT(*) FROM publications WHERE resource_type <> 'document'"
    )

    examined = 0
    bound = 0
    unreadable_uri = 0
    path_reused = 0
    no_document = 0
    changed_underfoot = 0
    vault_mismatch = 0
    # Bounded sample, not every id: the log names a handful so an operator can
    # pull the rows, and the count carries the rest. A per-row list would be
    # the one structure here whose size still tracked the table's.
    mismatch_sample: list = []
    after: uuid_mod.UUID | None = None

    while True:
        # `after IS NULL` for the first page rather than starting from the
        # all-zero UUID: nothing generates that id, but the column accepts it,
        # and a sentinel that is also a legal key value would silently skip
        # the one row that used it.
        page = await conn.fetch(
            """
            SELECT p.id, p.resource_uri, v.name AS vault_name
              FROM publications p
              JOIN vaults v ON v.id = p.vault_id
             WHERE p.resource_type = 'document'
               AND p.document_id IS NULL
               AND ($1::uuid IS NULL OR p.id > $1)
             ORDER BY p.id
             LIMIT $2
            """,
            after, _BATCH,
        )
        if not page:
            break
        after = page[-1]["id"]
        examined += len(page)

        ids: list = []
        paths: list[str] = []
        uris: list[str] = []
        vaults: list[str] = []
        for row in page:
            uri = row["resource_uri"]
            parsed = parse_uri(uri) if uri else None
            if parsed is None or parsed.kind != "doc" or not parsed.identifier:
                unreadable_uri += 1
                continue
            if parsed.vault != row["vault_name"]:
                # The URI names one vault and the row belongs to another. Do
                # not bind it to anything: resolution already refuses to serve
                # this row, and "some document exists at that path somewhere"
                # is not evidence about which document this publication was
                # made for.
                vault_mismatch += 1
                if len(mismatch_sample) < _MISMATCH_SAMPLE:
                    mismatch_sample.append(row["id"])
                continue
            ids.append(row["id"])
            paths.append(parsed.identifier)
            uris.append(uri)
            vaults.append(parsed.vault)

        if not ids:
            continue

        # The parse happened in Python a moment ago; the row can have moved
        # since. `p.resource_uri = c.uri` and `v.name = c.uri_vault` re-assert
        # both inputs to that parse inside the same statement that writes the
        # binding, so a URI rewritten by a concurrent move — or a vault renamed
        # underneath it — leaves the row unbound instead of binding it to what
        # its old URI used to mean. A later run re-reads it and decides again.
        updated = await conn.fetch(
            """
            WITH candidate AS (
                SELECT pub_id, doc_path, uri, uri_vault
                  FROM unnest($1::uuid[], $2::text[], $3::text[], $4::text[])
                    AS t(pub_id, doc_path, uri, uri_vault)
            )
            UPDATE publications p
               SET document_id = d.id
              FROM candidate c, documents d, vaults v
             WHERE p.id = c.pub_id
               AND p.document_id IS NULL
               AND p.resource_type = 'document'
               AND p.resource_uri = c.uri
               AND v.id = p.vault_id
               AND v.name = c.uri_vault
               AND d.vault_id = p.vault_id
               AND d.path = c.doc_path
               AND d.created_at <= p.created_at
            RETURNING p.id
            """,
            ids, paths, uris, vaults,
        )
        # `updated_at` is deliberately not touched: this is a schema backfill,
        # not an edit anyone made to the publication.
        bound += len(updated)

        done = {r["id"] for r in updated}
        left = [
            (i, p, u, v)
            for i, p, u, v in zip(ids, paths, uris, vaults, strict=True)
            if i not in done
        ]
        if left:
            # Why each leftover was left. The UPDATE can decline a row for four
            # reasons and the counts are only worth reading if they name the
            # right one, so this re-checks the same predicates rather than
            # asking the weaker question "is anything at that path now" — which
            # would file a row that moved underfoot as a reused path.
            split = await conn.fetchrow(
                """
                WITH candidate AS (
                    SELECT pub_id, doc_path, uri, uri_vault
                      FROM unnest($1::uuid[], $2::text[], $3::text[], $4::text[])
                        AS t(pub_id, doc_path, uri, uri_vault)
                ), state AS (
                    SELECT (p.id IS NOT NULL
                            AND p.document_id IS NULL
                            AND p.resource_uri IS NOT DISTINCT FROM c.uri
                            AND v.name IS NOT DISTINCT FROM c.uri_vault) AS unchanged,
                           d.id IS NOT NULL AS path_occupied
                      FROM candidate c
                      LEFT JOIN publications p ON p.id = c.pub_id
                      LEFT JOIN vaults v ON v.id = p.vault_id
                      LEFT JOIN documents d
                        ON d.vault_id = p.vault_id AND d.path = c.doc_path
                )
                SELECT COUNT(*) FILTER (WHERE NOT unchanged)             AS changed,
                       COUNT(*) FILTER (WHERE unchanged AND NOT path_occupied) AS absent,
                       COUNT(*) FILTER (WHERE unchanged AND path_occupied)     AS reused
                  FROM state
                """,
                [r[0] for r in left], [r[1] for r in left],
                [r[2] for r in left], [r[3] for r in left],
            )
            changed_underfoot += split["changed"]
            no_document += split["absent"]
            path_reused += split["reused"]

    counts = {
        "examined": examined,
        "bound": bound,
        "no_document": no_document,
        "path_reused": path_reused,
        "unreadable_uri": unreadable_uri,
        "vault_mismatch": vault_mismatch,
        "changed_underfoot": changed_underfoot,
        "already_bound": already_bound or 0,
        "non_document_publications": skipped_by_design or 0,
    }
    logger.info(
        "Migration 053 backfill: bound=%d, no_document=%d, path_reused=%d, "
        "unreadable_uri=%d, vault_mismatch=%d, changed_underfoot=%d, "
        "already_bound=%d, non_document_publications=%d (of %d document "
        "publications examined this run)",
        bound, no_document, path_reused, unreadable_uri, vault_mismatch,
        changed_underfoot, already_bound or 0, skipped_by_design or 0, examined,
    )
    if vault_mismatch:
        # The most interesting rows in the table if they exist: the vault the
        # URI names is not the vault the row belongs to. Ids only — enough to
        # pull the rows with a SELECT, without putting names or paths in a log.
        logger.warning(
            "Migration 053: %d document publication(s) name a vault other than "
            "their own and were left unbound. publication id(s): %s%s",
            vault_mismatch, ", ".join(str(i) for i in mismatch_sample),
            "" if vault_mismatch <= _MISMATCH_SAMPLE
            else f" (first {_MISMATCH_SAMPLE} shown)",
        )
    unbound = no_document + path_reused + unreadable_uri + changed_underfoot
    if unbound:
        logger.warning(
            "Migration 053: %d document publication(s) left unbound "
            "(no_document=%d, path_reused=%d, unreadable_uri=%d, "
            "changed_underfoot=%d). They keep working through resource_uri; "
            "document_id stays NULL until they are re-published.",
            unbound, no_document, path_reused, unreadable_uri, changed_underfoot,
        )
    return counts


async def _run(conn):
    # No enclosing transaction over the whole migration. Each step is
    # idempotent on its own, so a run cancelled between steps re-enters and
    # finishes; wrapping everything together would instead throw away a
    # completed index build because a later lock wait timed out.
    #
    # The FK target has to exist before the FK, and the column and the FK are
    # added together (see `_ensure_publication_identity`) so no window opens
    # in which the column exists unconstrained.
    await _ensure_documents_identity_unique(conn)
    await _ensure_publication_identity(conn)
    await _ensure_publication_document_index(conn)
    return await _backfill(conn)


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
