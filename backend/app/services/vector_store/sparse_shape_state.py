"""Which sparse shape a pgvector database serves: decided once, then recorded.

`vector_store_sparse_shape: auto` is the default. Startup decides it before the
store is built (`factory.decide_sparse_shape_for_settings`), and the first
successful schema setup records the shape it set up in `<schema>.install_state`.
Both run for API and worker processes alike, and both read only this database,
so every process that points at it arrives at the same answer.

The order matters more than any single rule in it:

1. **A recorded shape.** The last schema setup that succeeded, including one
   under an explicitly configured shape, so a later `auto` follows the
   operator's last explicit choice.
2. **A `posting` table.** An installation that has served `posting` moves to
   `vchord` only through `scripts/backfill_bm25_vector.py` and an explicit
   setting. During that runbook both tables exist and the BM25 index is built
   before the switch, so the index is not a signal that the switch happened.
3. **The BM25 index**, then the `arrays` columns.
4. **Populated chunks with none of those signatures:** refused by name. Guessing
   `posting` would serve an empty side table; guessing `vchord` would trip the
   populated-table guard in `_do_ensure`. Either way search would come back
   empty while readiness stayed green.
5. **A new database:** `vchord` when the server can provide `vchord_bm25` to
   this role, `posting` otherwise. The extension's control file says
   `superuser = true`, so "can provide" means already created and usable by
   this role, or available and this role is a superuser. Created is not
   usable: the extension's schema carries no USAGE for other roles, and a plain
   role then fails the schema setup with "permission denied for schema
   bm25_catalog". An extension somebody created on purpose, but that this role
   cannot use, is refused with the GRANT that fixes it rather than quietly
   recorded as `posting`.

Deliberately free of `app.config`, like the driver that records through it: the
database and the configured value arrive as arguments.
"""

from __future__ import annotations

from typing import cast

from app.services.sparse_shapes import (
    SPARSE_SHAPES,
    SparseShape,
    SparseShapeDecision,
    SparseShapeSetting,
)

#: One row per decided property of this installation's vector schema.
STATE_TABLE = "install_state"
SPARSE_SHAPE_KEY = "sparse_shape"
VCHORD_EXTENSION = "vchord_bm25"


class SparseShapeUndecidable(RuntimeError):
    """No shape can be decided safely; the message names what to change."""


async def recorded_sparse_shape(conn, *, schema: str) -> SparseShape | None:
    """The shape a previous schema setup recorded, or None."""
    if not await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", f'"{schema}".{STATE_TABLE}'):
        return None
    value = await conn.fetchval(
        f'SELECT value FROM "{schema}".{STATE_TABLE} WHERE key = $1', SPARSE_SHAPE_KEY
    )
    if value is None:
        return None
    if value not in SPARSE_SHAPES:
        raise SparseShapeUndecidable(
            f'"{schema}".{STATE_TABLE} records sparse shape {value!r}, which this version '
            "does not know. Set vector_store_sparse_shape explicitly."
        )
    return cast(SparseShape, value)


async def record_sparse_shape(conn, *, schema: str, shape: SparseShape) -> None:
    """Record the shape a schema setup just set up. Idempotent."""
    await conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS "{schema}".{STATE_TABLE} (
            key          TEXT PRIMARY KEY,
            value        TEXT NOT NULL,
            recorded_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    await conn.execute(
        f"""
        INSERT INTO "{schema}".{STATE_TABLE} (key, value) VALUES ($1, $2)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, recorded_at = now()
         WHERE {STATE_TABLE}.value IS DISTINCT FROM EXCLUDED.value
        """,
        SPARSE_SHAPE_KEY,
        shape,
    )


async def decide_sparse_shape(
    conn, *, schema: str, configured: SparseShapeSetting
) -> SparseShapeDecision:
    """The shape this database serves, and whether it has a `posting` table."""
    facts = await conn.fetchrow(
        """
        SELECT to_regclass($1) IS NOT NULL AS has_chunks,
               to_regclass($2) IS NOT NULL AS has_posting,
               to_regclass($3) IS NOT NULL AS has_bm25_index,
               EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_schema = $4 AND table_name = 'chunks'
                          AND column_name = 'sparse_terms') AS has_arrays,
               EXISTS (SELECT 1 FROM pg_extension WHERE extname = $5) AS vchord_created,
               (SELECT has_schema_privilege(current_user, e.extnamespace, 'USAGE')
                  FROM pg_extension e WHERE e.extname = $5) AS vchord_usable,
               (SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
                 WHERE e.extname = $5) AS vchord_schema,
               EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = $5) AS vchord_available,
               COALESCE((SELECT rolsuper FROM pg_roles WHERE rolname = current_user), false) AS superuser,
               current_user AS role
        """,
        f'"{schema}".chunks',
        f'"{schema}".posting',
        f'"{schema}".idx_vi_chunks_bm25',
        schema,
        VCHORD_EXTENSION,
    )
    posting = bool(facts["has_posting"])

    def decided(shape: SparseShape, decided_by, note: str = "") -> SparseShapeDecision:
        return SparseShapeDecision(shape=shape, decided_by=decided_by, posting_table_present=posting, note=note)

    if configured != "auto":
        return decided(configured, "configured")

    recorded = await recorded_sparse_shape(conn, schema=schema)
    if recorded is not None:
        return decided(recorded, "recorded")
    if posting:
        return decided(
            "posting", "existing_posting",
            "this database already serves posting; moving it to vchord is "
            "scripts/backfill_bm25_vector.py and then an explicit vchord setting",
        )
    if facts["has_bm25_index"]:
        return decided("vchord", "existing_bm25_index")
    if facts["has_arrays"]:
        return decided("arrays", "existing_arrays")
    if facts["has_chunks"] and await conn.fetchval(f'SELECT EXISTS (SELECT 1 FROM "{schema}".chunks)'):
        raise SparseShapeUndecidable(
            f'"{schema}".chunks holds rows but no posting table, BM25 index or arrays '
            "columns, so the shape it was written under cannot be told. Set "
            "vector_store_sparse_shape explicitly."
        )

    if facts["vchord_created"]:
        if not facts["vchord_usable"]:
            raise SparseShapeUndecidable(
                f"{VCHORD_EXTENSION} is installed in this database, but role {facts['role']} has "
                f"no USAGE on its schema {facts['vchord_schema']}. For the default vchord shape, "
                f"have a superuser run GRANT USAGE ON SCHEMA {facts['vchord_schema']} TO {facts['role']}; "
                "otherwise set vector_store_sparse_shape: posting."
            )
        return decided("vchord", "new_database", f"{VCHORD_EXTENSION} is installed in this database")
    if facts["vchord_available"] and facts["superuser"]:
        return decided("vchord", "new_database", f"the server provides {VCHORD_EXTENSION} and this role can create it")
    if facts["vchord_available"]:
        return decided(
            "posting", "new_database",
            f"the server provides {VCHORD_EXTENSION}, but it needs a superuser to create and "
            f"role {facts['role']} is not one. For the vchord shape, have a superuser run "
            f"CREATE EXTENSION {VCHORD_EXTENSION} in this database and GRANT USAGE ON SCHEMA "
            f"bm25_catalog TO {facts['role']} before AKB first starts on it.",
        )
    return decided(
        "posting", "new_database",
        f"the server does not provide {VCHORD_EXTENSION}. A server that does gives "
        "databases created on it the vchord shape.",
    )
