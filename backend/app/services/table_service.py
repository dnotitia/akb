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
from app.services.role_sync import get_role_sync
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

    # PG-native RBAC: grant SELECT/INSERT/UPDATE/DELETE/ALL on the new
    # vt_* table to the vault's reader/writer/admin group roles. Tables
    # without these grants are invisible to akb_user_<uid> roles (PG
    # returns "relation does not exist" or 42501).
    await get_role_sync().on_table_create(vault_id, pg_name)

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

    # PG-native RBAC: DROP TABLE has already cascaded the GRANTs;
    # this hook exists for symmetry + audit (logs at DEBUG).
    await get_role_sync().on_table_drop(vault_id, pg_name)

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

# Statement keywords allowed via `akb_sql`. Kept as a friendly
# pre-flight check: PG would also reject non-DML at the role level
# (akb_user_* roles have no CREATE/ALTER/DROP/GRANT privilege), but
# the PG error ("permission denied for schema public") is less
# actionable than "akb_sql is DML-only; use akb_create_table for
# schema changes."
_ALLOWED_FIRST_KEYWORDS = ("SELECT", "WITH", "INSERT", "UPDATE", "DELETE")


async def execute_sql(
    *,
    vault_names: list[str],
    user_id: str,
    sql: str,
    is_admin: bool = False,
) -> dict:
    """Execute raw SQL scoped to vault tables.

    Vault isolation is enforced by PostgreSQL ACL: the caller's
    ``akb_user_<uid>`` role has GRANTs only on tables in vaults
    they have access to (via ``akb_vault_<vid>_<scope>`` group role
    membership). Cross-vault references return PG ``42501``; the
    application no longer inspects user SQL for forbidden identifiers.

    Table references are rewritten for UX before execution:
      - Single vault: 'pipeline' → 'vt_sales__pipeline'
      - Cross-vault: 'sales__pipeline' → 'vt_sales__pipeline'

    System admins (``users.is_admin = TRUE``) bypass the per-user PG
    role and run as the backend service role — matching the existing
    system-admin trust model. For everyone else, PG is the authority.
    """
    from app.services.user_sql_executor import PermissionDeniedError, get_user_sql_executor

    pool = await get_pool()
    async with pool.acquire() as conn:
        table_map = await table_data_repo.build_table_name_map(conn, vault_names)
        rewritten = table_data_repo.rewrite_table_names(sql, table_map)

        sql_check = rewritten.rstrip(";").strip()
        if ";" in sql_check:
            return {"error": "Multi-statement SQL is not allowed. Send one statement at a time."}

        if not rewritten.strip().upper().startswith(_ALLOWED_FIRST_KEYWORDS):
            return {
                "error": (
                    "Only SELECT / WITH / INSERT / UPDATE / DELETE are allowed via "
                    "akb_sql. Use akb_create_table / akb_alter_table / "
                    "akb_drop_table for schema changes."
                )
            }

    try:
        return await get_user_sql_executor().execute(
            user_id=user_id,
            sql=rewritten,
            is_admin=is_admin,
            vault_names=vault_names,
        )
    except PermissionDeniedError as e:
        # PG ACL denied — the boundary working as designed. Surface
        # the PG error verbatim so callers know it came from PG, not
        # from application validation.
        return {
            "error": str(e),
            "code": "permission_denied",
            "pg_sqlstate": e.pg_sqlstate,
        }
    except Exception as e:  # noqa: BLE001 — fall through to enrichment
        msg = str(e)
        # Try to enrich column/table-not-exist errors with fuzzy-match
        # hints (issues #34 / #35 / #36). The enrichment query needs
        # superuser-class privileges on pg_attribute/pg_class — we
        # acquire a fresh connection (default role) for it; the SET
        # LOCAL ROLE from the failed user query is already reset.
        async with pool.acquire() as conn:
            enriched = await _enrich_undefined_error(
                conn, msg, allowed_pg_tables=set(table_map.values()),
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

    Runs under the connection's default role (the backend service
    role), NOT under the caller's `akb_user_<uid>` — `SET LOCAL ROLE`
    inside `UserSqlExecutor` is reset on tx rollback, so by the time
    we're here we have full read access to `pg_attribute` /
    `pg_class`. The protected surface is the user-supplied SQL only;
    this enrichment query is application-controlled.
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
