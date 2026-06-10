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
import re
import uuid
from datetime import datetime, timezone

import asyncpg

from app.db.postgres import get_pool
from app.exceptions import ConflictError, NotFoundError, ValidationError
from app.repositories import table_data_repo, table_registry_repo
from app.repositories.document_repo import CollectionRepository
from app.repositories.events_repo import emit_event
from app.services.index_service import (
    build_table_chunk, delete_table_chunks, write_source_chunks,
)
from app.services.role_sync import get_role_sync
from app.services.uri_service import table_uri
from app.services.user_sql_executor import PermissionDeniedError, get_user_sql_executor
from app.util.errors import (
    err,
    METHOD_NOT_ALLOWED,
    MULTI_STATEMENT,
    PERMISSION_DENIED,
    SQL_ERROR,
    UNDEFINED_COLUMN,
    UNDEFINED_TABLE,
    VAULT_ARCHIVED,
)
from app.util.text import fuzzy_hint

# Re-exported helpers used by publication_service for the
# `table_query` share path. Other modules import directly from
# `table_data_repo`.
from app.repositories.table_data_repo import (  # noqa: F401
    build_table_name_map,
    count_statement_separators,
    rewrite_table_names,
)

logger = logging.getLogger("akb.tables")

# Reserved column names that conflict with auto-added bookkeeping columns.
_RESERVED = {"id", "created_at", "updated_at", "created_by"}

# Column-name shape — same grammar as table names. Enforced on
# create AND alter so the value stored in the registry cannot diverge
# from `safe_ident(name)` (which silently maps punctuation to `_`).
_COLUMN_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _validate_column_name(name) -> None:
    """Reject reserved + malformed column names (raises ValueError).

    Shared by create_table and alter_table so the two paths stay
    consistent: reserved names collide with the auto-added bookkeeping
    columns (id/created_at/updated_at/created_by), and the shape check
    keeps the registry name identical to its `safe_ident` PG identity.
    """
    if not isinstance(name, str) or not name:
        raise ValueError("Column name must be a non-empty string.")
    if name.lower() in _RESERVED:
        raise ValueError(
            f"Column name '{name}' is reserved (auto-added by AKB). "
            f"Reserved names: {sorted(_RESERVED)}. Choose a different name."
        )
    if not _COLUMN_NAME_RE.fullmatch(name):
        raise ValueError(
            f"Invalid column name {name!r}: must match {_COLUMN_NAME_RE.pattern} "
            f"(lowercase letter then letters/digits/underscores)."
        )


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
    # pg_table_name maps any punctuation to underscore — allowing hyphens
    # would let `mcp-items` and `mcp_items` collide on the PG side.
    if not _TABLE_NAME_RE.fullmatch(name):
        raise ValidationError(
            f"Invalid table name {name!r}: must match {_TABLE_NAME_RE.pattern}"
        )
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
                _validate_column_name(col["name"])

            collection_id = None
            if collection_path:
                coll_repo = CollectionRepository(pool)
                collection_id = await coll_repo.get_or_create(
                    vault_id, collection_path, conn=conn,
                )

            pg_name = table_data_repo.pg_table_name(vault["name"], name)
            # Refuse names whose PG identifier would overflow NAMEDATALEN.
            # PG truncates silently, and role_sync then refuses to GRANT on
            # the over-long name (raising deep in the stack as a 500). Catch
            # it here as a clean 422 that names the culprit, before any DDL.
            if len(pg_name) > table_data_repo.PG_IDENT_MAX_LEN:
                raise ValidationError(
                    f"Table name too long: the PostgreSQL identifier "
                    f"{pg_name!r} is {len(pg_name)} chars, over the "
                    f"{table_data_repo.PG_IDENT_MAX_LEN}-char limit. Shorten "
                    f"the vault name ({vault['name']!r}) or table name "
                    f"({name!r})."
                )
            try:
                await table_data_repo.create_dynamic_table(conn, pg_name, columns)
                await table_registry_repo.insert(
                    conn,
                    table_id=tid, vault_id=vault_id, name=name,
                    description=description, columns=columns,
                    created_by=actor_id, now=now,
                    collection_id=collection_id,
                )
            except asyncpg.UniqueViolationError as e:
                # Concurrent create past the find_by_name check races on
                # UNIQUE(vault_tables) or pg_type. Surface as 409.
                raise ConflictError(f"Table already exists: {name}") from e
            # Grant inside the TX so akb_sql can address the table the
            # instant the create commits (no "exists but 42501" window).
            await get_role_sync().grant_table_in_conn(conn, vault_id, pg_name)
            await emit_event(
                conn, "table.create",
                vault_id=vault_id,
                resource_uri=table_uri(vault["name"], name, collection=collection_path),
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
        "uri": table_uri(vault["name"], name, collection=collection_path),
        "vault": vault["name"],
        "collection": collection_path or None,
        "name": name,
        "columns": columns,
    }


from app.util.text import normalize_collection_path as _normalize_collection_path  # noqa: E402

# Table name shape — PG-native identifier grammar. Underscores only
# (no hyphens) so `pg_table_name`'s `[^a-z0-9] → _` sanitiser doesn't
# collapse two distinct user-visible names onto the same PG identifier.
_TABLE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


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
                "uri": table_uri(vault["name"], r["name"], collection=r["collection"]),
                "vault": vault["name"],
                "collection": r["collection"],
                "name": r["name"],
                # SQL-side identifier the caller must pass to akb_sql —
                # mirrors `BrowseItem.sql_name` so REST clients have the
                # same contract MCP clients got in 0.5.5 (issue #110).
                "sql_name": table_data_repo.pg_short_name(r["name"]),
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
            # Chunks + vector outbox enqueue must commit with the DDL/registry
            # delete so a crash mid-drop can't leave orphan chunks.
            from app.services.index_service import delete_table_chunks
            await delete_table_chunks(conn, str(table_id))
            await table_registry_repo.delete(conn, table_id)

            t_uri = table_uri(vault["name"], table_name, collection=table.get("collection"))
            await conn.execute(
                "DELETE FROM edges WHERE source_uri = $1 OR target_uri = $1",
                t_uri,
            )

            await emit_event(
                conn, "table.drop",
                vault_id=vault_id,
                resource_uri=t_uri,
                actor_id=actor_id,
                payload={
                    "vault": vault["name"],
                    "collection": table.get("collection"),
                    "table_name": table_name,
                },
            )

    # PG-native RBAC: DROP TABLE has already cascaded the GRANTs;
    # this hook exists for symmetry + audit (logs at DEBUG).
    await get_role_sync().on_table_drop(vault_id, pg_name)

    logger.info("Table dropped: %s (%s)", table_name, pg_name)
    return {
        "kind": "table",
        "uri": table_uri(vault["name"], table_name, collection=table.get("collection")),
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

            # FOR UPDATE serialises concurrent alters' read-modify-write
            # of vault_tables.columns — without it they last-write-wins.
            table = await conn.fetchrow(
                """
                SELECT * FROM vault_tables
                 WHERE vault_id = $1 AND name = $2
                 FOR UPDATE
                """,
                vault_id, table_name,
            )
            if not table:
                raise NotFoundError("Table", table_name)

            columns = table_registry_repo.parse_columns(table["columns"])
            pg_name = table_data_repo.pg_table_name(vault["name"], table_name)

            # ── Guard rails (mirror create_table) ───────────────────
            # Reject reserved/malformed names BEFORE any DDL so the whole
            # alter is atomic: a bad name rolls back the TX with zero
            # schema change. Covers dropping/renaming the implicit
            # bookkeeping columns (id/created_at/updated_at/created_by) —
            # those are exactly the _RESERVED set — so a client can no
            # longer drop the PK or shadow a reserved name.
            for col in (add_columns or []):
                _validate_column_name(col["name"])
            for col_name in (drop_columns or []):
                if not isinstance(col_name, str) or not col_name:
                    raise ValueError("Drop column name must be a non-empty string.")
                if col_name.lower() in _RESERVED:
                    raise ValueError(
                        f"Column '{col_name}' is a reserved bookkeeping column "
                        f"and cannot be dropped. Reserved: {sorted(_RESERVED)}."
                    )
            for old_name, new_name in (rename_columns or {}).items():
                if not isinstance(old_name, str) or not old_name:
                    raise ValueError("Rename source column must be a non-empty string.")
                if old_name.lower() in _RESERVED:
                    raise ValueError(
                        f"Column '{old_name}' is a reserved bookkeeping column "
                        f"and cannot be renamed. Reserved: {sorted(_RESERVED)}."
                    )
                _validate_column_name(new_name)

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

            t_uri = table_uri(vault["name"], table_name, collection=table.get("collection"))
            await emit_event(
                conn, "table.alter",
                vault_id=vault_id,
                resource_uri=t_uri,
                actor_id=actor_id,
                payload={
                    "vault": vault["name"],
                    "table_name": table_name,
                    "added": added,
                    "dropped": dropped,
                    "renamed": renamed,
                },
            )

    # Refresh the metadata chunk so search reflects the new schema —
    # otherwise the chunk drifts from the ALTER until the table is dropped.
    try:
        await index_table_metadata(
            str(table["id"]),
            vault_id=vault_id,
            vault_name=vault["name"],
            name=table_name,
            description=table["description"] or "",
            columns=columns,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("alter_table chunk reindex failed for %s: %s", table_name, e)

    return {
        "kind": "table",
        "uri": t_uri,
        "vault": vault["name"],
        "name": table_name,
        "columns": columns,
    }


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
    pool = await get_pool()
    async with pool.acquire() as conn:
        table_map = await table_data_repo.build_table_name_map(conn, vault_names)
        rewritten = table_data_repo.rewrite_table_names(sql, table_map)

        # Literal-aware single-statement boundary (issue #180): a `;`
        # only counts as a separator outside string literals, quoted
        # identifiers, comments, and dollar-quoted blocks — same token
        # classes the rewriter above already walks. Trailing-semicolon
        # tolerance (`rstrip(";")`) is preserved as-is.
        sql_check = rewritten.rstrip(";").strip()
        if count_statement_separators(sql_check) > 0:
            return err(
                "Multi-statement SQL is not allowed. Send one statement at a time.",
                code=MULTI_STATEMENT,
            )

        upper = rewritten.strip().upper()
        if not upper.startswith(_ALLOWED_FIRST_KEYWORDS):
            return err(
                "Only SELECT / WITH / INSERT / UPDATE / DELETE are allowed via "
                "akb_sql. Use akb_create_table / akb_alter_table / "
                "akb_drop_table for schema changes.",
                code=METHOD_NOT_ALLOWED,
            )

        # Archived vaults are READ-ONLY. PG ACL has no archive concept
        # (write grants are intentionally preserved so unarchive is
        # instant), so the write block lives here: a non-read statement
        # (anything but SELECT/WITH) against ANY archived referenced
        # vault is refused before it touches PG.
        if not upper.startswith(("SELECT", "WITH")):
            archived = await conn.fetch(
                "SELECT name FROM vaults WHERE name = ANY($1::text[]) "
                "AND status = 'archived'",
                vault_names,
            )
            if archived:
                names = ", ".join(sorted(r["name"] for r in archived))
                return err(
                    f"Vault '{names}' is archived (read-only); writes via "
                    f"akb_sql are not allowed. Unarchive the vault first.",
                    code=VAULT_ARCHIVED,
                )

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
        # from application validation. `pg_sqlstate` lives under
        # `details` per the canonical shape.
        return err(
            str(e),
            code=PERMISSION_DENIED,
            pg_sqlstate=e.pg_sqlstate,
        )
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
        return err(msg, code=SQL_ERROR)


_COLUMN_NOT_EXIST = re.compile(r'column "([^"]+)" does not exist')
_RELATION_NOT_EXIST = re.compile(r'relation "([^"]+)" does not exist')


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

    Returns the canonical 0.5.6 error envelope (``err(...)`` with
    ``code=undefined_column`` or ``undefined_table``, ``hint``, and
    ``details.available_columns`` / ``details.available_tables``), or
    ``None`` when the PG message isn't a recoverable shape — in which
    case the caller falls through to the verbatim PG message wrapped
    in ``err(msg, code=SQL_ERROR)``.
    """
    if not allowed_pg_tables:
        return None

    # ── Column not exist ────────────────────────────────────────
    if m := _COLUMN_NOT_EXIST.search(err_msg):
        bad_col = m.group(1)
        col_meta = await _fetch_column_meta(conn, allowed_pg_tables)
        if not col_meta:
            return None
        hint = fuzzy_hint(bad_col, list(col_meta.keys()), label="columns")
        jsonb_cols = [c for c, t in col_meta.items() if t == "jsonb"]
        if jsonb_cols:
            hint += (
                f"  (jsonb columns — use `<col>::text ILIKE '%X%'`: "
                f"{', '.join(jsonb_cols)})"
            )
        return err(
            err_msg,
            code=UNDEFINED_COLUMN,
            hint=hint,
            available_columns=list(col_meta.keys()),
        )

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
        hint = fuzzy_hint(bad_rel, short_names, label="tables")
        hint += (
            "  (Reference vault tables by their short name — the rewriter "
            "prefixes them with `vt_<vault>__` automatically.)"
        )
        return err(
            err_msg,
            code=UNDEFINED_TABLE,
            hint=hint,
            available_tables=short_names,
        )

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
