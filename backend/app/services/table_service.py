"""Vault table service — real PostgreSQL tables per vault.

Service composes `table_registry_repo` (the `vault_tables` row) and
`table_data_repo` (the dynamic `vt_***` PG tables) under transaction
boundaries managed here. SQL lives in the repos; this module is
business logic + orchestration only.

Identifier helpers and the SQL-name rewriting used by the
`table_query` share path (publication_service) live in
`table_data_repo`; we re-export them at the historical names so that
caller keeps working. Later commits switch the imports to the public
names.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from difflib import get_close_matches

from app.db.postgres import get_pool
from app.exceptions import ConflictError, NotFoundError
from app.repositories import table_data_repo, table_registry_repo
from app.repositories.document_repo import CollectionRepository
from app.repositories.events_repo import emit_event
from app.services.index_service import (
    build_table_chunk, delete_table_chunks, write_source_chunks,
)
from app.services.uri_service import table_uri

# Re-exported helpers used by publication_service for the
# `table_query` share path. Other modules import directly from
# `table_data_repo`.
from app.repositories.table_data_repo import (  # noqa: F401
    build_table_name_map,
    rewrite_table_names,
)

logger = logging.getLogger("akb.tables")

# Reserved column names that conflict with auto-added bookkeeping columns.
_RESERVED = {"id", "created_at", "updated_at", "created_by"}


# ── Indexing helpers ─────────────────────────────────────────────


async def index_table_metadata(
    table_id: str,
    vault_id: uuid.UUID,
    vault_name: str,
    name: str,
    description: str | None,
    columns: list[dict],
) -> None:
    """Build + upsert the metadata chunk for a table so hybrid search can
    surface it. Safe to call repeatedly — write_source_chunks replaces
    all prior chunks for this table first."""
    chunk = build_table_chunk(
        vault_name=vault_name, name=name,
        description=description, columns=columns,
    )
    pool = await get_pool()
    async with pool.acquire() as conn:
        await write_source_chunks(
            conn, "table", table_id,
            vault_id=vault_id,
            chunks=[chunk],
        )


async def delete_table_index(table_id: str) -> None:
    """Drop the metadata chunk for a table (outbox-driven vector-store delete)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await delete_table_chunks(conn, table_id)


# ── CRUD ─────────────────────────────────────────────────────────


async def create_table(
    vault_id: uuid.UUID,
    name: str,
    columns: list[dict],
    *,
    actor_id: str,
    description: str = "",
    collection: str | None = None,
) -> dict:
    """Create a vault-scoped table inside an optional collection.

    `collection` is a path string (e.g. "specs" or "sessions/learnings").
    NULL / empty → vault root. The path is normalized and the matching
    `collections` row is auto-created via `CollectionRepository.get_or_create`
    if it doesn't exist yet.
    """
    pool = await get_pool()
    tid = uuid.uuid4()
    now = datetime.now(timezone.utc)
    collection_path = _normalize_collection_path(collection)

    async with pool.acquire() as conn:
        async with conn.transaction():
            vault = await conn.fetchrow("SELECT name FROM vaults WHERE id = $1", vault_id)
            if not vault:
                raise NotFoundError("Vault", str(vault_id))

            existing = await table_registry_repo.find_by_name(conn, vault_id, name)
            if existing:
                raise ConflictError(f"Table already exists: {name}")

            for col in columns:
                if col["name"].lower() in _RESERVED:
                    raise ValueError(
                        f"Column name '{col['name']}' is reserved (auto-added by AKB). "
                        f"Reserved names: {sorted(_RESERVED)}. Choose a different name."
                    )

            collection_id = None
            if collection_path:
                coll_repo = CollectionRepository(pool)
                collection_id = await coll_repo.get_or_create(
                    vault_id, collection_path, conn=conn,
                )

            pg_name = table_data_repo.pg_table_name(vault["name"], name)
            await table_data_repo.create_dynamic_table(conn, pg_name, columns)
            await table_registry_repo.insert(
                conn,
                table_id=tid, vault_id=vault_id, name=name,
                description=description, columns=columns,
                created_by=actor_id, now=now,
                collection_id=collection_id,
            )
            await emit_event(
                conn, "table.create",
                vault_id=vault_id,
                resource_uri=table_uri(vault["name"], name),
                actor_id=actor_id,
                payload={
                    "vault": vault["name"],
                    "table_name": name,
                    "collection": collection_path or None,
                    "columns_count": len(columns),
                    "description": description,
                },
            )

    # Outside the create transaction on purpose: the embedding call can
    # be slow and we'd rather not hold a DB connection on it.
    try:
        await index_table_metadata(
            str(tid),
            vault_id=vault_id,
            vault_name=vault["name"],
            name=name,
            description=description,
            columns=columns,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("table metadata indexing failed for %s: %s", name, e)

    logger.info("Table created: %s → %s (collection=%s)", name, pg_name, collection_path or "<root>")
    return {
        "kind": "table",
        "uri": table_uri(vault["name"], name),
        "vault": vault["name"],
        "collection": collection_path or None,
        "name": name,
        "columns": columns,
    }


from app.util.text import normalize_collection_path as _normalize_collection_path  # noqa: E402


async def list_tables(vault_id: uuid.UUID) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        vault = await conn.fetchrow("SELECT name FROM vaults WHERE id = $1", vault_id)
        if not vault:
            return []

        rows = await table_registry_repo.list_for_vault(conn, vault_id)

        results: list[dict] = []
        for r in rows:
            pg_name = table_data_repo.pg_table_name(vault["name"], r["name"])
            count = await table_data_repo.count_rows(conn, pg_name)
            results.append({
                "kind": "table",
                "uri": table_uri(vault["name"], r["name"]),
                "vault": vault["name"],
                "collection": r["collection"],
                "name": r["name"],
                "description": r["description"],
                "columns": table_registry_repo.parse_columns(r["columns"]),
                "row_count": count,
                "created_at": r["created_at"].isoformat(),
            })
    return results


async def drop_table(
    vault_id: uuid.UUID,
    table_name: str,
    *,
    actor_id: str,
) -> dict:
    """Drop a vault-scoped table: registry row + dynamic PG table +
    edges referencing the table URI + metadata chunk."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            vault = await conn.fetchrow("SELECT name FROM vaults WHERE id = $1", vault_id)
            if not vault:
                raise NotFoundError("Vault", str(vault_id))

            table = await table_registry_repo.find_by_name(conn, vault_id, table_name)
            if not table:
                raise NotFoundError("Table", table_name)

            table_id = table["id"]
            pg_name = table_data_repo.pg_table_name(vault["name"], table_name)
            await table_data_repo.drop_dynamic_table(conn, pg_name)
            await table_registry_repo.delete(conn, table_id)

            # Clean up edges referencing this table.
            t_uri = f"akb://{vault['name']}/table/{table_name}"
            await conn.execute(
                "DELETE FROM edges WHERE source_uri = $1 OR target_uri = $1",
                t_uri,
            )

            await emit_event(
                conn, "table.drop",
                vault_id=vault_id,
                resource_uri=table_uri(vault["name"], table_name),
                actor_id=actor_id,
                payload={
                    "vault": vault["name"],
                    "collection": table.get("collection"),
                    "table_name": table_name,
                },
            )

    # Outside the TX: drop the metadata chunk via the vector-store
    # outbox (same pattern as file deletion).
    try:
        await delete_table_index(str(table_id))
    except Exception as e:  # noqa: BLE001
        logger.warning("table chunk delete failed for %s: %s", table_name, e)

    logger.info("Table dropped: %s (%s)", table_name, pg_name)
    return {
        "kind": "table",
        "uri": table_uri(vault["name"], table_name),
        "vault": vault["name"],
        "collection": table.get("collection"),
        "name": table_name,
        "deleted": True,
    }


async def alter_table(
    vault_id: uuid.UUID,
    table_name: str,
    *,
    actor_id: str,
    add_columns: list[dict] | None = None,
    drop_columns: list[str] | None = None,
    rename_columns: dict[str, str] | None = None,
) -> dict:
    """Apply schema changes to a vault table:
       - add_columns: [{name, type}, ...]
       - drop_columns: ["name", ...]
       - rename_columns: {"old": "new", ...}

    All three are optional and combine in one TX. Emits `table.alter`
    after the writes commit.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            vault = await conn.fetchrow("SELECT name FROM vaults WHERE id = $1", vault_id)
            if not vault:
                raise NotFoundError("Vault", str(vault_id))

            table = await table_registry_repo.find_by_name(conn, vault_id, table_name)
            if not table:
                raise NotFoundError("Table", table_name)

            columns = table_registry_repo.parse_columns(table["columns"])
            pg_name = table_data_repo.pg_table_name(vault["name"], table_name)

            added: list[str] = []
            dropped: list[str] = []
            renamed: dict[str, str] = {}

            if add_columns:
                for col in add_columns:
                    if any(c["name"] == col["name"] for c in columns):
                        continue
                    col_type = table_data_repo.TYPE_MAP.get(col.get("type", "text"), "TEXT")
                    safe_name = table_data_repo.safe_ident(col["name"])
                    await table_data_repo.add_column(conn, pg_name, safe_name, col_type)
                    columns.append(col)
                    added.append(col["name"])

            if drop_columns:
                for col_name in drop_columns:
                    safe_name = table_data_repo.safe_ident(col_name)
                    await table_data_repo.drop_column(conn, pg_name, safe_name)
                    dropped.append(col_name)
                columns = [c for c in columns if c["name"] not in drop_columns]

            if rename_columns:
                for old_name, new_name in rename_columns.items():
                    old_safe = table_data_repo.safe_ident(old_name)
                    new_safe = table_data_repo.safe_ident(new_name)
                    await table_data_repo.rename_column(conn, pg_name, old_safe, new_safe)
                    for c in columns:
                        if c["name"] == old_name:
                            c["name"] = new_name
                    renamed[old_name] = new_name

            await table_registry_repo.update_columns(conn, table["id"], columns)

            await emit_event(
                conn, "table.alter",
                vault_id=vault_id,
                resource_uri=table_uri(vault["name"], table_name),
                actor_id=actor_id,
                payload={
                    "vault": vault["name"],
                    "table_name": table_name,
                    "added": added,
                    "dropped": dropped,
                    "renamed": renamed,
                },
            )

    return {
        "kind": "table",
        "uri": table_uri(vault["name"], table_name),
        "vault": vault["name"],
        "name": table_name,
        "columns": columns,
    }


import re as _re

# Statement keywords allowed via `akb_sql`. Everything else (DDL like
# CREATE/DROP/ALTER, DCL like GRANT/REVOKE, TCL like BEGIN/COMMIT/
# SAVEPOINT/RESET, plus informational SHOW/EXPLAIN) is delegated to
# specific tools or simply has no business in this entry-point.
_ALLOWED_FIRST_KEYWORDS = ("SELECT", "WITH", "INSERT", "UPDATE", "DELETE")

# Identifiers that MUST NOT appear in user-supplied SQL. PG accepts
# them via `akbuser`'s broad privileges and would happily return
# rows, but they cross trust boundaries (other vaults' vt_* tables,
# AKB's own bookkeeping, PG system catalogs).
_FORBIDDEN_TOKEN = _re.compile(
    r"\b(?:"
    # PG system catalogs / metadata
    r"pg_catalog|information_schema|pg_authid|pg_user|pg_shadow|"
    r"pg_proc|pg_class|pg_namespace|pg_database|pg_attribute|"
    r"pg_roles|pg_settings|pg_stat_\w+|pg_tables|pg_views|"
    # AKB internal bookkeeping. These are managed by the service
    # layer; userland SQL must not read or write them directly.
    r"users|vaults|documents|chunks|tokens|vault_access|"
    r"bm25_vocab|bm25_stats|edges|events|publications|todos|"
    r"vault_tables|vault_files|collections|memories|"
    r"vault_external_git|vector_delete_outbox"
    r")\b",
    _re.IGNORECASE,
)
_VT_IDENTIFIER = _re.compile(r"\bvt_[a-z0-9_]+__[a-z0-9_]+\b", _re.IGNORECASE)


def _validate_sql_surface(sql: str, allowed_pg_tables: set[str]) -> str | None:
    """Return an error message if `sql` references anything outside the
    caller's table whitelist or runs a non-DML statement type. None
    means the surface is safe to hand to PG.

    `allowed_pg_tables` is the set of fully-qualified `vt_<vault>__<t>`
    names the caller can legitimately reach (i.e. `table_map.values()`).
    """
    stripped = sql.strip()
    upper = stripped.upper()
    # Statement-type whitelist: only DML (and SELECT/WITH).
    if not upper.startswith(_ALLOWED_FIRST_KEYWORDS):
        return (
            "Only SELECT / WITH / INSERT / UPDATE / DELETE are allowed via "
            "akb_sql. Use akb_create_table / akb_alter_table / "
            "akb_drop_table for schema changes."
        )
    # Foreign vt_* table references.
    allowed_lower = {t.lower() for t in allowed_pg_tables}
    for m in _VT_IDENTIFIER.finditer(sql):
        if m.group(0).lower() not in allowed_lower:
            return f"Reference to '{m.group(0)}' is not allowed."
    # PG system catalogs + AKB internal tables.
    bad = _FORBIDDEN_TOKEN.search(sql)
    if bad:
        return f"Reference to '{bad.group(0)}' is not allowed."
    return None


async def execute_sql(
    vault_names: list[str],
    sql: str,
    read_only: bool = False,
) -> dict:
    """Execute raw SQL scoped to vault tables.

    Table references are rewritten:
      - Single vault: 'pipeline' → 'vt_sales__pipeline'
      - Cross-vault: 'sales__pipeline' → 'vt_sales__pipeline'
    Only tables registered in vault_tables are accessible.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        table_map = await table_data_repo.build_table_name_map(conn, vault_names)
        rewritten = table_data_repo.rewrite_table_names(sql, table_map)

        sql_check = rewritten.rstrip(";").strip()
        if ";" in sql_check:
            return {"error": "Multi-statement SQL is not allowed. Send one statement at a time."}

        # Sandbox the SQL surface to a tight whitelist before PG ever sees
        # it. Two classes of bypass exist without this gate:
        #
        # (1) Foreign-vault data exfiltration. `rewrite_table_names` only
        #     rewrites identifiers in `table_map` (the caller's vault's
        #     tables). User-supplied SQL can name another vault's PG
        #     table directly (`vt_<other>__<t>`) — those identifiers
        #     reach PG untouched and `akbuser` (the backend's
        #     superuser-class role) returns the rows. ALSO catches
        #     reads against AKB internal tables (`users`, `vaults`,
        #     `tokens`, `chunks`, `bm25_*`, `edges`, ...) and PG
        #     system catalogs (`pg_catalog.*`, `information_schema.*`,
        #     `pg_user`, `pg_authid`, ...).
        #
        # (2) DDL-driven side channels. Even a writer must not be able
        #     to `CREATE VIEW`, `CREATE FUNCTION`, etc. — those bypass
        #     the table whitelist (a view can SELECT from anywhere)
        #     and outlive the request. DDL is delegated to specific
        #     tools (`akb_create_table` / `akb_alter_table` /
        #     `akb_drop_table`). `akb_sql` is DML-only.
        deny = _validate_sql_surface(rewritten, set(table_map.values()))
        if deny:
            return {"error": deny}

        is_select = rewritten.strip().upper().startswith(("SELECT", "WITH"))

        # Read-only access must reject anything that isn't a SELECT or
        # WITH query. PG's `SET TRANSACTION READ ONLY` blocks data
        # mutations (INSERT/UPDATE/DELETE/DDL) but transaction-control
        # statements (SET/BEGIN/RESET/COMMIT/ROLLBACK/SAVEPOINT) and
        # informational statements (SHOW/EXPLAIN) slip through. None of
        # them are useful to a reader and several change session state.
        if read_only and not is_select:
            return {"error": "Read-only access: only SELECT / WITH queries are allowed."}

        try:
            async with conn.transaction():
                # PostgreSQL enforces read-only: blocks INSERT/UPDATE/DELETE/
                # TRUNCATE/DROP/ALTER/CREATE regardless of SQL tricks
                if read_only:
                    await conn.execute("SET TRANSACTION READ ONLY")

                if is_select:
                    rows = await conn.fetch(rewritten)
                    result_rows = []
                    for r in rows:
                        row: dict = {}
                        for k, v in dict(r).items():
                            if isinstance(v, uuid.UUID):
                                row[k] = str(v)
                            elif hasattr(v, "isoformat"):
                                row[k] = v.isoformat()
                            elif isinstance(v, (int, float, str, bool, type(None))):
                                row[k] = v
                            else:
                                row[k] = str(v)
                        result_rows.append(row)
                    return {
                        "kind": "table_query",
                        "vaults": vault_names,
                        "columns": list(dict(rows[0]).keys()) if rows else [],
                        "items": result_rows,
                        "total": len(result_rows),
                    }
                else:
                    result = await conn.execute(rewritten)
                    return {
                        "kind": "table_sql",
                        "vaults": vault_names,
                        "result": result,
                    }
        except Exception as e:
            msg = str(e)
            if read_only and "read-only transaction" in msg:
                return {"error": "Write operation denied. You have read-only access to this vault."}
            # Column/table-not-exist errors: enrich with fuzzy-match hint
            # so agents can self-correct in a single retry instead of
            # falling back to `information_schema` lookups or
            # `akb_search(limit=10)` (see issues #34 / #35 / #36).
            #
            # Safe re-use of `conn` after the failed transaction —
            # `async with conn.transaction()` rolls back on exception,
            # leaving conn in healthy idle state.
            enriched = await _enrich_undefined_error(
                conn, msg, allowed_pg_tables=set(table_map.values())
            )
            if enriched:
                return enriched
            return {"error": msg}


# Max items listed in fallback hints (when no fuzzy suggestion strong enough).
_HINT_LIST_LIMIT = 15

_COLUMN_NOT_EXIST = _re.compile(r'column "([^"]+)" does not exist')
_RELATION_NOT_EXIST = _re.compile(r'relation "([^"]+)" does not exist')


async def _enrich_undefined_error(
    conn,
    err_msg: str,
    *,
    allowed_pg_tables: set[str],
) -> dict | None:
    """Turn a column/table-not-exist error into an actionable hint.

    Backend role queries `pg_attribute` / `pg_class` directly — only the
    user-supplied SQL is sandboxed. `allowed_pg_tables` is the caller's
    rewritten table list (e.g. ``{"vt_sales__pipeline"}``) — we never
    suggest names from other vaults.

    Returns None when the error isn't a recoverable shape, letting the
    caller fall through to the verbatim PG message.
    """
    if not allowed_pg_tables:
        return None

    # ── Column not exist ────────────────────────────────────────
    if m := _COLUMN_NOT_EXIST.search(err_msg):
        bad_col = m.group(1)
        col_meta = await _fetch_column_meta(conn, allowed_pg_tables)
        if not col_meta:
            return None
        hint = _fuzzy_hint(bad_col, list(col_meta.keys()), label="columns")
        jsonb_cols = [c for c, t in col_meta.items() if t == "jsonb"]
        if jsonb_cols:
            hint += (
                f"  (jsonb columns — use `<col>::text ILIKE '%X%'`: "
                f"{', '.join(jsonb_cols)})"
            )
        return {
            "error": err_msg,
            "hint": hint,
            "available_columns": list(col_meta.keys()),
        }

    # ── Relation/table not exist ────────────────────────────────
    if m := _RELATION_NOT_EXIST.search(err_msg):
        bad_rel = m.group(1)
        # Only consult the caller's allowed tables — never leak other
        # vaults' table names via fuzzy suggestion.
        short_names = sorted({
            t.split("__", 1)[1] for t in allowed_pg_tables if "__" in t
        })
        if not short_names:
            return None
        hint = _fuzzy_hint(bad_rel, short_names, label="tables")
        hint += (
            "  (Reference vault tables by their short name — the rewriter "
            "prefixes them with `vt_<vault>__` automatically.)"
        )
        return {
            "error": err_msg,
            "hint": hint,
            "available_tables": short_names,
        }

    return None


async def _fetch_column_meta(conn, table_names: set[str]) -> dict[str, str]:
    """{column_name: pg_type} for the union of `table_names`.

    Backend pool is unsandboxed — `pg_attribute` / `pg_class` reads are
    fine here; the protected surface is the user-supplied SQL only.
    """
    rows = await conn.fetch(
        """
        SELECT a.attname AS name, format_type(a.atttypid, a.atttypmod) AS type
          FROM pg_attribute a
          JOIN pg_class c ON c.oid = a.attrelid
         WHERE c.relname = ANY($1::text[])
           AND a.attnum > 0
           AND NOT a.attisdropped
         ORDER BY a.attname
        """,
        list(table_names),
    )
    return {r["name"]: r["type"] for r in rows}


def _fuzzy_hint(bad: str, candidates: list[str], *, label: str) -> str:
    """Top-3 close matches → 'Did you mean…?', else first N as fallback."""
    suggestions = get_close_matches(bad, candidates, n=3, cutoff=0.6)
    if suggestions:
        return f"Did you mean: {', '.join(suggestions)}?"
    truncated = candidates[:_HINT_LIST_LIMIT]
    suffix = " …" if len(candidates) > _HINT_LIST_LIMIT else ""
    return f"Available {label}: {', '.join(truncated)}{suffix}"
