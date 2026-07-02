"""Repository for vault-scoped dynamic tables (the `vt_***` PG tables
that hold actual row data).

Owns identifier sanitisation, DDL primitives, and the SQL-name
rewriting used by `execute_sql` and the `table_query` share path.
The registry row in `vault_tables` lives in `table_registry_repo`.

Module-level functions take an explicit `conn` so the caller controls
the transaction boundary.

Row-level DML (INSERT/UPDATE/DELETE on a single row) is intentionally
not exposed here: all row-level mutations happen through
`execute_sql`, which gives operators raw SQL with proper read-only /
write enforcement at the PG transaction level. If a structured
row-CRUD API is added later it will live in this module.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from typing import Any
from uuid import UUID

from app.exceptions import InvalidColumnTypeError, ValidationError
from app.util.text import fuzzy_hint


TYPE_MAP = {
    "text": "TEXT",
    "int": "BIGINT",
    "float": "DOUBLE PRECISION",
    "numeric": "NUMERIC",
    "number": "NUMERIC",
    "boolean": "BOOLEAN",
    "uuid": "UUID",
    "date": "DATE",
    "timestamp": "TIMESTAMPTZ",
    "jsonb": "JSONB",
    "json": "JSONB",
    "text[]": "TEXT[]",
    "enum": "TEXT",
}

CANONICAL_TYPE_ALIASES = {
    "text": "text",
    "int": "int",
    "float": "float",
    "numeric": "numeric",
    "number": "numeric",
    "boolean": "boolean",
    "uuid": "uuid",
    "date": "date",
    "timestamp": "timestamp",
    "jsonb": "jsonb",
    "json": "jsonb",
    "text[]": "text[]",
    "enum": "enum",
}

CANONICAL_TYPES = [
    "text",
    "int",
    "float",
    "numeric",
    "boolean",
    "uuid",
    "date",
    "timestamp",
    "jsonb",
    "text[]",
    "enum",
]

_DEFAULT_FUNCTIONS = {
    "timestamp": {"now()"},
    "uuid": {"gen_random_uuid()"},
}

_CHECK_OPERATORS = {
    "eq": "=",
    "ne": "<>",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
}

_LENGTH_CHECK_OPERATORS = {
    "len_lt": "<",
    "len_lte": "<=",
    "len_gt": ">",
    "len_gte": ">=",
}

_REFERENCE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

_FK_ON_DELETE_SQL = {
    "cascade": "CASCADE",
    "set null": "SET NULL",
    "restrict": "RESTRICT",
    "no action": "NO ACTION",
}


# ── Identifier helpers ───────────────────────────────────────────


def _sanitize_pg_part(s: str) -> str:
    """Single source of truth for the vault-name / table-name → PG-part
    transformation. Lowercase, hyphens to underscores, then any
    remaining non-alphanumeric replaced with underscore. Idempotent.

    Non-ASCII inputs (Korean / Japanese / Chinese / symbol-only names)
    collapse to all-underscore tokens (`______`). PG accepts those, but
    the caller still needs to know what they sanitized to — see
    ``pg_short_name`` below.
    """
    return re.sub(r"[^a-z0-9]", "_", s.lower().replace("-", "_"))


# PostgreSQL truncates identifiers past NAMEDATALEN-1 (63 bytes by
# default) *silently*. A truncated `vt_*` name could collide with a
# different table — so we refuse, rather than truncate, names that
# don't fit. `role_sync._is_safe_pg_table_name` enforces the same bound
# as defense-in-depth; this constant is the single source for both.
# Note the bound is in *bytes*: `_sanitize_pg_part` maps every non-ASCII
# character to `_`, so the identifier is pure ASCII and a `len()`
# char-count equals PG's byte count — the equivalence breaks if a future
# change ever lets multibyte characters through.
PG_IDENT_MAX_LEN = 63


def pg_table_name(vault_name: str, table_name: str) -> str:
    """Return the PG table name for a vault-scoped table:
    `vt_{sanitised_vault}__{sanitised_table}`."""
    return f"vt_{_sanitize_pg_part(vault_name)}__{_sanitize_pg_part(table_name)}"


def pg_short_name(table_name: str) -> str:
    """SQL-safe bare identifier the caller should pass to ``akb_sql``.

    This is the right-hand side of ``pg_table_name``'s ``vt_<v>__<t>``;
    it is what the rewriter actually keys off. ``akb_browse`` surfaces
    it as ``sql_name`` so clients don't have to re-derive the
    sanitisation rule (issue #110).
    """
    return _sanitize_pg_part(table_name)


def safe_ident(name: str) -> str:
    """Sanitise a column / table name for use as a SQL identifier."""
    return re.sub(r"[^a-zA-Z0-9_]", "_", name)


def normalize_column_type(logical_type: Any) -> str:
    """Return the canonical AKB logical column type or raise a stable error."""
    raw = "text" if logical_type in (None, "") else logical_type
    if not isinstance(raw, str):
        raise InvalidColumnTypeError(
            f"Invalid column type {raw!r}: type must be a string.",
            hint=f"Available column types: {', '.join(CANONICAL_TYPES)}",
            details={"available_types": CANONICAL_TYPES},
        )
    key = raw.strip().lower()
    canonical = CANONICAL_TYPE_ALIASES.get(key)
    if canonical is None:
        raise InvalidColumnTypeError(
            f"Unsupported column type {raw!r}.",
            hint=fuzzy_hint(key, CANONICAL_TYPES + ["number", "json"], label="column types"),
            details={"available_types": CANONICAL_TYPES, "aliases": {"number": "numeric", "json": "jsonb"}},
        )
    return canonical


def normalize_column_spec(col: dict) -> dict:
    """Copy a caller column spec and normalize its logical type."""
    out = dict(col)
    out["type"] = normalize_column_type(out.get("type", "text"))
    if out.get("references") is not None:
        refs, on_delete = normalize_reference_spec(out["references"], out.get("on_delete"))
        out["references"] = refs
        out["on_delete"] = on_delete
    if out["type"] == "enum":
        values = _normalize_enum_values(out.get("enum"))
        if out.get("check") is not None:
            raise ValidationError(
                "Enum columns derive their CHECK constraint from `enum`; omit `check`."
            )
        out["enum"] = values
        out["check"] = {"op": "in", "values": values}
        if "default" in out and out.get("default") is not None and out["default"] not in values:
            raise ValidationError(
                f"Default {out['default']!r} is not one of enum values {values!r}."
            )
    # Compile once here so invalid default/check specs fail before any DDL.
    _column_default_sql(out)
    _column_check_sql(out)
    return out


def column_type_sql(logical_type: Any) -> str:
    return TYPE_MAP[normalize_column_type(logical_type)]


def column_definition(col: dict, *, include_check: bool = True) -> str:
    """Build one validated user-column definition for CREATE/ALTER TABLE."""
    col_name = safe_ident(col["name"])
    logical_type = normalize_column_type(col.get("type", "text"))
    parts = [col_name, TYPE_MAP[logical_type]]
    if col.get("required"):
        parts.append("NOT NULL")
    if default_sql := _column_default_sql(col):
        parts.append(f"DEFAULT {default_sql}")
    if include_check and logical_type != "enum" and (check_sql := _column_check_sql(col)):
        parts.append(f"CHECK ({check_sql})")
    return " ".join(parts)


def _column_default_sql(col: dict) -> str | None:
    if "default" not in col or col.get("default") is None:
        return None
    logical_type = normalize_column_type(col.get("type", "text"))
    value = col["default"]
    if isinstance(value, str) and value.strip().lower() in {"now()", "gen_random_uuid()"}:
        func = value.strip().lower()
        if func not in _DEFAULT_FUNCTIONS.get(logical_type, set()):
            raise ValidationError(
                f"Default function {func!r} is not allowed for {logical_type} columns."
            )
        return func
    return _literal_sql(value, logical_type, ctx="default")


def _column_check_sql(col: dict) -> str | None:
    spec = col.get("check")
    if spec is None:
        return None
    if not isinstance(spec, dict):
        raise ValidationError("check must be an object like {op, value}; raw SQL is not allowed.")
    op = spec.get("op")
    if not isinstance(op, str):
        raise ValidationError("check.op must be a string.")
    op = op.lower()
    col_name = safe_ident(col["name"])
    logical_type = normalize_column_type(col.get("type", "text"))
    if op in _CHECK_OPERATORS:
        if "value" not in spec:
            raise ValidationError(f"check.{op} requires a value.")
        return f"{col_name} {_CHECK_OPERATORS[op]} {_literal_sql(spec['value'], logical_type, ctx='check.value')}"
    if op in {"in", "not_in"}:
        values = spec.get("value", spec.get("values"))
        if not isinstance(values, list) or not values:
            raise ValidationError(f"check.{op} requires a non-empty value list.")
        literals = ", ".join(_literal_sql(v, logical_type, ctx="check.value") for v in values)
        neg = "NOT " if op == "not_in" else ""
        return f"{col_name} {neg}IN ({literals})"
    if op in _LENGTH_CHECK_OPERATORS:
        if logical_type != "text":
            raise ValidationError(f"check.{op} is only supported for text columns.")
        value = spec.get("value")
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValidationError(f"check.{op} requires a non-negative integer value.")
        return f"char_length({col_name}) {_LENGTH_CHECK_OPERATORS[op]} {value}"
    raise ValidationError(
        "Unsupported check.op "
        f"{op!r}. Available ops: {sorted([*_CHECK_OPERATORS, 'in', 'not_in', *_LENGTH_CHECK_OPERATORS])}."
    )


def _literal_sql(value: Any, logical_type: str, *, ctx: str) -> str:
    if logical_type in {"text", "enum"}:
        if not isinstance(value, str):
            raise ValidationError(f"{ctx} for {logical_type} columns must be a string literal.")
        return _quote(value)
    if logical_type == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValidationError(f"{ctx} for int columns must be an integer literal.")
        return str(value)
    if logical_type in {"float", "numeric"}:
        if not isinstance(value, int | float) or isinstance(value, bool):
            raise ValidationError(f"{ctx} for {logical_type} columns must be a numeric literal.")
        return repr(value)
    if logical_type == "boolean":
        if not isinstance(value, bool):
            raise ValidationError(f"{ctx} for boolean columns must be a boolean literal.")
        return "TRUE" if value else "FALSE"
    if logical_type == "uuid":
        if not isinstance(value, str):
            raise ValidationError(f"{ctx} for uuid columns must be a UUID string literal.")
        try:
            UUID(value)
        except ValueError as e:
            raise ValidationError(f"{ctx} for uuid columns must be a valid UUID string.") from e
        return _quote(value)
    if logical_type == "date":
        if not isinstance(value, str):
            raise ValidationError(f"{ctx} for date columns must be an ISO date string.")
        try:
            date.fromisoformat(value)
        except ValueError as e:
            raise ValidationError(f"{ctx} for date columns must be an ISO date string.") from e
        return _quote(value)
    if logical_type == "timestamp":
        if not isinstance(value, str):
            raise ValidationError(f"{ctx} for timestamp columns must be an ISO timestamp string.")
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as e:
            raise ValidationError(f"{ctx} for timestamp columns must be an ISO timestamp string.") from e
        return _quote(value)
    if logical_type == "jsonb":
        return f"{_quote(json.dumps(value, ensure_ascii=False, separators=(',', ':')))}::jsonb"
    if logical_type == "text[]":
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValidationError(f"{ctx} for text[] columns must be a list of string literals.")
        return "ARRAY[" + ", ".join(_quote(v) for v in value) + "]::TEXT[]"
    raise InvalidColumnTypeError(
        f"Unsupported column type {logical_type!r}.",
        hint=fuzzy_hint(logical_type, CANONICAL_TYPES, label="column types"),
        details={"available_types": CANONICAL_TYPES},
    )


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _normalize_enum_values(values: Any) -> list[str]:
    if not isinstance(values, list) or not values:
        raise ValidationError("Enum columns require a non-empty `enum` value list.")
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value:
            raise ValidationError("Enum values must be non-empty strings.")
        if value in seen:
            raise ValidationError(f"Duplicate enum value {value!r}.")
        seen.add(value)
        out.append(value)
    return out


def normalize_on_delete(value: Any) -> str:
    if value is None or value == "":
        return "no action"
    if not isinstance(value, str):
        raise ValidationError("references.on_delete must be a string when provided.")
    key = value.strip().lower().replace("_", " ").replace("-", " ")
    key = " ".join(key.split())
    if key not in _FK_ON_DELETE_SQL:
        raise ValidationError(
            "references.on_delete must be one of "
            f"{sorted(_FK_ON_DELETE_SQL)}; got {value!r}."
        )
    return key


def normalize_reference_spec(refs: Any, on_delete: Any = None) -> tuple[dict, str]:
    if not isinstance(refs, dict):
        raise ValidationError("references must be an object like {table, column?, on_delete?}.")
    table = refs.get("table") or refs.get("referenced_table")
    if not isinstance(table, str) or not table:
        raise ValidationError("references.table must be a non-empty table name.")
    if not _REFERENCE_NAME_RE.fullmatch(table):
        raise ValidationError(
            f"Invalid references.table {table!r}: must match {_REFERENCE_NAME_RE.pattern}."
        )
    column = refs.get("column") or refs.get("referenced_column") or "id"
    if not isinstance(column, str) or not column:
        raise ValidationError("references.column must be a non-empty column name.")
    if column != "id" and not _REFERENCE_NAME_RE.fullmatch(column):
        raise ValidationError(
            f"Invalid references.column {column!r}: must be 'id' or match "
            f"{_REFERENCE_NAME_RE.pattern}."
        )
    nested_on_delete = refs.get("on_delete")
    if nested_on_delete is not None and on_delete is not None:
        nested = normalize_on_delete(nested_on_delete)
        top_level = normalize_on_delete(on_delete)
        if nested != top_level:
            raise ValidationError(
                "references.on_delete and column on_delete disagree; provide only one value."
            )
        normalized_on_delete = nested
    else:
        normalized_on_delete = normalize_on_delete(
            nested_on_delete if nested_on_delete is not None else on_delete
        )
    return {"table": table, "column": column}, normalized_on_delete


def reference_on_delete_sql(value: Any) -> str:
    return _FK_ON_DELETE_SQL[normalize_on_delete(value)]


def enum_constraint_name(pg_name: str, col_name: str) -> str:
    return generate_constraint_name(pg_name, [col_name], kind="enum")


def check_constraint_name(pg_name: str, col_name: str) -> str:
    return generate_constraint_name(pg_name, [col_name], kind="check")


def check_constraint_definition(pg_name: str, col: dict) -> str:
    if normalize_column_type(col.get("type", "text")) == "enum":
        raise ValidationError("Enum columns derive their CHECK constraint from `enum`.")
    check_sql = _column_check_sql(col)
    if not check_sql:
        raise ValidationError(f"Column {col.get('name')!r} has no check spec.")
    name = safe_ident(check_constraint_name(pg_name, col["name"]))
    return f"CONSTRAINT {name} CHECK ({check_sql})"


def enum_check_sql(col: dict) -> str:
    if normalize_column_type(col.get("type", "text")) != "enum":
        raise ValidationError(f"Column {col.get('name')!r} is not an enum column.")
    return _column_check_sql(col) or "TRUE"


def enum_constraint_definition(pg_name: str, col: dict) -> str:
    name = safe_ident(enum_constraint_name(pg_name, col["name"]))
    return f"CONSTRAINT {name} CHECK ({enum_check_sql(col)})"


def foreign_key_constraint_name(pg_name: str, col_name: str) -> str:
    return generate_constraint_name(pg_name, [col_name], kind="fkey")


def _same_vault_reference_pg_name(pg_name: str, table_name: str) -> str:
    vault_prefix, _sep, _table_part = pg_name.rpartition("__")
    if not vault_prefix:
        raise ValidationError(f"Invalid dynamic table name {pg_name!r}.")
    return f"{vault_prefix}__{pg_short_name(table_name)}"


def foreign_key_constraint_definition(
    pg_name: str, col: dict, *, vault_name: str | None = None,
) -> str:
    refs = col.get("references")
    if refs is None:
        raise ValidationError(f"Column {col.get('name')!r} has no references spec.")
    refs, on_delete = normalize_reference_spec(refs, col.get("on_delete"))
    name = safe_ident(foreign_key_constraint_name(pg_name, col["name"]))
    source_col = safe_ident(col["name"])
    target_table = (
        pg_table_name(vault_name, refs["table"])
        if vault_name is not None
        else _same_vault_reference_pg_name(pg_name, refs["table"])
    )
    target_col = safe_ident(refs["column"])
    return (
        f"CONSTRAINT {name} FOREIGN KEY ({source_col}) "
        f"REFERENCES {target_table} ({target_col}) "
        f"ON DELETE {reference_on_delete_sql(on_delete)}"
    )


# ── DDL ──────────────────────────────────────────────────────────


async def create_dynamic_table(
    conn, pg_name: str, columns: list[dict], *, vault_name: str | None = None,
) -> None:
    """Create the data-bearing PG table for a vault table. Caller is
    responsible for sanitising `pg_name` (use `pg_table_name`)."""
    col_defs = ["id UUID PRIMARY KEY DEFAULT uuid_generate_v4()"]
    for col in columns:
        col_defs.append(column_definition(col, include_check=False))
    for col in columns:
        if col.get("check") is not None and normalize_column_type(col.get("type", "text")) != "enum":
            col_defs.append(check_constraint_definition(pg_name, col))
    for col in columns:
        if normalize_column_type(col.get("type", "text")) == "enum":
            col_defs.append(enum_constraint_definition(pg_name, col))
    for col in columns:
        if col.get("references") is not None:
            col_defs.append(
                foreign_key_constraint_definition(pg_name, col, vault_name=vault_name)
            )
    col_defs.append("created_by TEXT")
    col_defs.append("created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()")
    col_defs.append("updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()")
    await conn.execute(f'CREATE TABLE {pg_name} ({", ".join(col_defs)})')
    # Auto-bump `updated_at` on every UPDATE. PG has no MySQL-style
    # `ON UPDATE CURRENT_TIMESTAMP`, and user SQL reaches the table verbatim
    # through `execute_sql` (no app-layer `SET updated_at = NOW()` hook), so a
    # BEFORE UPDATE trigger is the only robust place to enforce it. The shared
    # `akb_set_updated_at()` function is created by migration 038 (and exists
    # before any runtime create, since migrations run at startup).
    await conn.execute(
        f"CREATE TRIGGER akb_set_updated_at_trigger "
        f"BEFORE UPDATE ON {pg_name} "
        f"FOR EACH ROW EXECUTE FUNCTION akb_set_updated_at()"
    )


async def drop_dynamic_table(conn, pg_name: str) -> None:
    await conn.execute(f"DROP TABLE IF EXISTS {pg_name}")


async def count_rows(conn, pg_name: str) -> int:
    """Returns 0 if the table does not exist (used by list_tables on a
    registry row whose data table was already dropped)."""
    try:
        return int(await conn.fetchval(f"SELECT COUNT(*) FROM {pg_name}") or 0)
    except Exception:  # noqa: BLE001 — table-missing is the usual case here
        return 0


async def add_column(
    conn, pg_name: str, col: dict, *, vault_name: str | None = None,
) -> None:
    """Add a column to the dynamic table. Column DDL is validated here."""
    await conn.execute(
        f"ALTER TABLE {pg_name} ADD COLUMN IF NOT EXISTS "
        f"{column_definition(col, include_check=False)}"
    )
    if col.get("check") is not None and normalize_column_type(col.get("type", "text")) != "enum":
        await create_check_constraint(conn, pg_name, col)
    if normalize_column_type(col.get("type", "text")) == "enum":
        await create_enum_constraint(conn, pg_name, col)
    if col.get("references") is not None:
        await create_foreign_key_constraint(conn, pg_name, col, vault_name=vault_name)


async def drop_column(conn, pg_name: str, col_name: str) -> None:
    await conn.execute(
        f"ALTER TABLE {pg_name} DROP COLUMN IF EXISTS {col_name}"
    )


async def rename_column(conn, pg_name: str, old_name: str, new_name: str) -> None:
    await conn.execute(
        f"ALTER TABLE {pg_name} RENAME COLUMN {old_name} TO {new_name}"
    )


# ── Constraint / index DDL (AKB #215) ────────────────────────────


# Closed enum for index column ordering. Never interpolate a raw user
# value into DDL — map through this dict so only ASC/DESC can ever
# reach the SQL string.
_ORDER_SQL = {"asc": "ASC", "desc": "DESC"}


def generate_constraint_name(pg_name: str, columns: list[str], *, kind: str) -> str:
    """Deterministic, schema-global-safe name for a generated UNIQUE
    constraint (``kind='uk'``) or index (``kind='idx'``).

    Index names — and a UNIQUE constraint's implicit index name — are
    SCHEMA-GLOBAL in PostgreSQL, so the generated name is namespaced by
    the physical table (``pg_name``) to avoid cross-table collisions.

    Shape: ``{pg_name}__{cols joined by _}_{digest}__{kind}``. ``digest``
    is the first 8 hex chars of a ``hashlib.sha1`` over the NUL-joined
    ``(pg_name, kind, *columns)`` tuple — pure ``hashlib`` (no randomness,
    stable across calls/processes). It is ALWAYS present, for two reasons:
    (1) it disambiguates column lists whose underscore-flattened forms
    collide (``["a","b"]`` and ``["a_b"]`` both flatten to ``a_b``) — NUL
    can never occur in a column name, so distinct lists always hash
    differently; (2) when the readable part would exceed
    ``PG_IDENT_MAX_LEN`` bytes it is truncated and the digest tail keeps
    the result collision-safe.
    """
    suffix = f"__{kind}"
    cols_part = "_".join(safe_ident(c) for c in columns)
    # usedforsecurity=False: this digest is a collision-avoidance tag for a
    # PG identifier, not a security primitive — SHA-1 is fine and the flag
    # keeps static analysis (bandit B324) from flagging it as weak crypto.
    digest = hashlib.sha1(
        "\x00".join([pg_name, kind, *columns]).encode(), usedforsecurity=False
    ).hexdigest()[:8]
    logical = f"{pg_name}__{cols_part}_{digest}{suffix}"
    if len(logical.encode()) <= PG_IDENT_MAX_LEN:
        return logical
    tail = f"_{digest}{suffix}"
    # Keep the leading readable bytes; reserve room for the digest tail.
    budget = PG_IDENT_MAX_LEN - len(tail.encode())
    readable = f"{pg_name}__{cols_part}".encode()[:budget].decode(errors="ignore")
    return f"{readable}{tail}"


async def create_unique_constraint(
    conn, pg_name: str, name: str, columns: list[str],
) -> None:
    """``ALTER TABLE {pg} ADD CONSTRAINT {name} UNIQUE ({cols})``.

    Every identifier flows through ``safe_ident``; caller is expected to
    have validated/resolved ``name`` already (see service layer)."""
    safe_name = safe_ident(name)
    cols = ", ".join(safe_ident(c) for c in columns)
    await conn.execute(
        f"ALTER TABLE {pg_name} ADD CONSTRAINT {safe_name} UNIQUE ({cols})"
    )


async def drop_constraint(conn, pg_name: str, name: str) -> None:
    safe_name = safe_ident(name)
    await conn.execute(
        f"ALTER TABLE {pg_name} DROP CONSTRAINT IF EXISTS {safe_name}"
    )


async def create_enum_constraint(conn, pg_name: str, col: dict) -> None:
    await conn.execute(
        f"ALTER TABLE {pg_name} ADD {enum_constraint_definition(pg_name, col)}"
    )


async def create_check_constraint(conn, pg_name: str, col: dict) -> None:
    await conn.execute(
        f"ALTER TABLE {pg_name} ADD {check_constraint_definition(pg_name, col)}"
    )


async def drop_column_check_constraints(conn, pg_name: str, col_name: str) -> None:
    """Drop all CHECK constraints on a column.

    AKB now creates deterministic named checks, but older scalar checks were
    emitted inline and PostgreSQL assigned auto names. Discovering by conkey
    lets set_check/drop_check migrate those legacy constraints cleanly.
    """
    safe_col = safe_ident(col_name)
    rows = await conn.fetch(
        """
        SELECT con.conname AS name
          FROM pg_constraint con
          JOIN pg_class cls ON cls.oid = con.conrelid
          JOIN pg_attribute att
            ON att.attrelid = con.conrelid
           AND att.attnum = ANY(con.conkey)
         WHERE cls.relname = $1
           AND con.contype = 'c'
           AND att.attname = $2
        """,
        pg_name,
        safe_col,
    )
    for row in rows:
        await drop_constraint(conn, pg_name, row["name"])


async def replace_check_constraint(conn, pg_name: str, col: dict) -> None:
    await drop_column_check_constraints(conn, pg_name, col["name"])
    if col.get("check") is not None:
        await create_check_constraint(conn, pg_name, col)


async def replace_enum_constraint(conn, pg_name: str, col: dict) -> None:
    await drop_constraint(conn, pg_name, enum_constraint_name(pg_name, col["name"]))
    await create_enum_constraint(conn, pg_name, col)


async def create_foreign_key_constraint(
    conn, pg_name: str, col: dict, *, vault_name: str | None = None,
) -> None:
    await conn.execute(
        f"ALTER TABLE {pg_name} ADD "
        f"{foreign_key_constraint_definition(pg_name, col, vault_name=vault_name)}"
    )


async def replace_foreign_key_constraint(
    conn,
    pg_name: str,
    old_col_name: str,
    col: dict,
    *,
    vault_name: str | None = None,
) -> None:
    await drop_constraint(conn, pg_name, foreign_key_constraint_name(pg_name, old_col_name))
    await create_foreign_key_constraint(conn, pg_name, col, vault_name=vault_name)


async def alter_column_default(conn, pg_name: str, col: dict) -> None:
    safe_col = safe_ident(col["name"])
    default_sql = _column_default_sql(col)
    if default_sql is None:
        await conn.execute(f"ALTER TABLE {pg_name} ALTER COLUMN {safe_col} DROP DEFAULT")
    else:
        await conn.execute(
            f"ALTER TABLE {pg_name} ALTER COLUMN {safe_col} SET DEFAULT {default_sql}"
        )


async def alter_column_required(conn, pg_name: str, col_name: str, required: bool) -> None:
    safe_col = safe_ident(col_name)
    op = "SET NOT NULL" if required else "DROP NOT NULL"
    await conn.execute(f"ALTER TABLE {pg_name} ALTER COLUMN {safe_col} {op}")


async def rename_enum_values(
    conn, pg_name: str, col_name: str, renames: dict[str, str],
) -> None:
    if not renames:
        return
    safe_col = safe_ident(col_name)
    params: list[Any] = []
    cases: list[str] = []
    for old_value, new_value in renames.items():
        params.extend([old_value, new_value])
        cases.append(f"WHEN ${len(params) - 1} THEN ${len(params)}")
    params.append(list(renames))
    await conn.execute(
        f"UPDATE {pg_name} "
        f"SET {safe_col} = CASE {safe_col} {' '.join(cases)} ELSE {safe_col} END "
        f"WHERE {safe_col} = ANY(${len(params)}::text[])",
        *params,
    )


async def create_index(
    conn, pg_name: str, name: str, cols_with_order: list[tuple[str, str]],
) -> None:
    """``CREATE INDEX {name} ON {pg} ({col [ASC|DESC], ...})``.

    ``cols_with_order`` is a list of ``(column, order)`` where order is
    ``'asc'`` / ``'desc'`` (the closed enum). Identifiers via
    ``safe_ident``; order via the ``_ORDER_SQL`` map — an unknown order
    raises ``ValidationError`` rather than reaching the DDL string."""
    safe_name = safe_ident(name)
    parts = []
    for col, order in cols_with_order:
        order_sql = _ORDER_SQL.get((order or "asc").lower())
        if order_sql is None:
            raise ValidationError(
                f"Invalid index order {order!r}: must be 'asc' or 'desc'."
            )
        parts.append(f"{safe_ident(col)} {order_sql}")
    await conn.execute(
        f"CREATE INDEX {safe_name} ON {pg_name} ({', '.join(parts)})"
    )


async def drop_index(conn, name: str) -> None:
    safe_name = safe_ident(name)
    await conn.execute(f"DROP INDEX IF EXISTS {safe_name}")


async def unique_key_duplicates(
    conn, pg_name: str, columns: list[str], limit: int = 5,
) -> list[dict]:
    """Preflight a candidate UNIQUE key against EXISTING data.

    Returns up to ``limit`` groups of the *key columns only* (no other
    columns leaked) that already have COUNT(*) > 1 — i.e. rows that
    would violate the constraint. An empty list means the key can be
    added safely.

    PostgreSQL ``UNIQUE`` defaults to ``NULLS DISTINCT`` — a row whose key has
    ANY NULL value never conflicts. The preflight mirrors that (``WHERE <each
    col> IS NOT NULL``) so it is not STRICTER than the constraint it guards:
    otherwise multiple legitimately-NULL rows would group together and falsely
    block a valid ``ADD CONSTRAINT``.

    ``SELECT {cols}, COUNT(*) FROM {pg} WHERE {cols all NOT NULL}
    GROUP BY {cols} HAVING COUNT(*) > 1 LIMIT {n}`` — identifiers via
    ``safe_ident``; ``limit`` is coerced to ``int``."""
    safe_cols = [safe_ident(c) for c in columns]
    col_list = ", ".join(safe_cols)
    not_null = " AND ".join(f"{c} IS NOT NULL" for c in safe_cols)
    lim = int(limit)
    rows = await conn.fetch(
        f"SELECT {col_list}, COUNT(*) AS dup_count "
        f"FROM {pg_name} "
        f"WHERE {not_null} "
        f"GROUP BY {col_list} "
        f"HAVING COUNT(*) > 1 "
        f"LIMIT {lim}"
    )
    return [dict(r) for r in rows]


# ── SQL rewriting (for execute_sql + table_query share path) ─────


async def build_table_name_map(conn, vault_names: list[str]) -> dict[str, str]:
    """Map friendly table aliases → real PG names.

    Single vault: bare ('pipeline') and prefixed ('sales__pipeline')
    forms both accepted. Multi-vault: only prefixed form, to avoid
    ambiguity. Raises NotFoundError on a missing vault.
    """
    from app.exceptions import NotFoundError

    table_map: dict[str, str] = {}
    for vname in vault_names:
        vault_row = await conn.fetchrow("SELECT id FROM vaults WHERE name = $1", vname)
        if not vault_row:
            raise NotFoundError("Vault", vname)
        tables = await conn.fetch(
            "SELECT name FROM vault_tables WHERE vault_id = $1",
            vault_row["id"],
        )
        sanitized_vault = _sanitize_pg_part(vname)
        for t in tables:
            pg_name = pg_table_name(vname, t["name"])
            # The fully-sanitised short form (e.g. ``______`` for a
            # Korean-named table) is what ``akb_browse`` now advertises
            # as ``sql_name``. Keying off it makes that contract
            # actually queryable; without it the rewriter dropped
            # non-ASCII tables on the floor (issue #111).
            short = pg_short_name(t["name"])
            table_map[f"{sanitized_vault}__{short}"] = pg_name
            if len(vault_names) == 1:
                table_map[short] = pg_name
    return table_map


# Token kinds emitted by `_tokenize_sql`. Only ``id`` tokens are eligible
# for rewriting; everything else (literals, comments, punctuation) is
# emitted verbatim so the rewriter cannot corrupt string contents.
#
# Naive regex rewriting (`re.sub(r"\bname\b", ..., flags=IGNORECASE)`)
# matched inside single-quoted strings, double-quoted identifiers,
# comments, and column aliases — silently corrupting query results
#. The tokenizer makes the rewrite scope-aware:
# strings/comments/quoted-idents pass through untouched.
_SQL_TOKEN_RE = re.compile(
    r"""
      (?P<str>'(?:[^']|'')*')                # single-quoted string (PG escapes '' inside)
    | (?P<qid>"(?:[^"]|"")+")                # double-quoted identifier
    | (?P<line_comment>--[^\n]*)             # line comment
    | (?P<block_comment>/\*[\s\S]*?\*/)      # block comment (non-greedy)
    | (?P<num>[0-9]+(?:\.[0-9]+)?)           # numeric literal
    | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)      # bare identifier or keyword
    | (?P<ws>\s+)                            # whitespace
    | (?P<sym>.)                             # any other single char
    """,
    re.VERBOSE | re.DOTALL,
)


def _scan_dollar_quote(sql: str, start: int) -> int | None:
    """If sql[start:] begins with a PG dollar-quote `$tag$ ... $tag$`,
    return the index just past the closing tag. Otherwise None.
    """
    if sql[start] != "$":
        return None
    m = re.match(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$", sql[start:])
    if not m:
        return None
    tag = m.group(0)
    end = sql.find(tag, start + len(tag))
    if end == -1:
        return None
    return end + len(tag)


def count_statement_separators(sql: str) -> int:
    """Count the ``;`` characters in ``sql`` that act as statement
    separators — i.e. appear OUTSIDE string literals, quoted
    identifiers, comments, and dollar-quoted blocks.

    Shares the tokenizer (``_SQL_TOKEN_RE`` + ``_scan_dollar_quote``)
    with ``rewrite_table_names`` so the multi-statement guard in
    ``table_service.execute_sql`` classifies semicolons with exactly
    the same scope-awareness as the rewriter. The previous guard was a
    literal-blind ``";" in sql`` membership test, which rejected single
    statements like ``VALUES ('Fix bug; refactor')`` (issue #180).

    Tolerance for trailing semicolons is the caller's policy: this
    helper reports every separator it sees.
    """
    count = 0
    pos = 0
    n = len(sql)
    while pos < n:
        # Same walk order as `rewrite_table_names`: manual dollar-quote
        # scan first, then the token regex.
        end = _scan_dollar_quote(sql, pos)
        if end is not None:
            pos = end
            continue
        m = _SQL_TOKEN_RE.match(sql, pos)
        if not m:
            # Should not happen — `sym` catches any character. Safety net.
            pos += 1
            continue
        if m.lastgroup == "sym" and m.group() == ";":
            count += 1
        pos = m.end()
    return count


def contains_set_config_call(sql: str) -> bool:
    """Return True when SQL calls PostgreSQL's ``set_config`` function.

    ``request.jwt.claims`` is reserved for service-key claim injection. Since
    callers can otherwise spoof that custom GUC from inside their own SQL,
    ``akb_sql`` rejects all user-authored ``set_config(...)`` calls. The scan
    is token-aware so string literals, comments, quoted identifiers, and
    dollar-quoted bodies do not create false positives.
    """
    pos = 0
    n = len(sql)
    while pos < n:
        end = _scan_dollar_quote(sql, pos)
        if end is not None:
            pos = end
            continue
        m = _SQL_TOKEN_RE.match(sql, pos)
        if not m:
            pos += 1
            continue
        if _token_identifier_name(m.lastgroup, m.group()) == "set_config":
            if _next_significant_token_is_open_paren(sql, m.end()):
                return True
        pos = m.end()
    return False


def contains_unicode_escaped_identifier(sql: str) -> bool:
    """Return True when SQL uses PostgreSQL ``U&"..."`` identifiers.

    Dynamic table identifiers exposed by AKB are already sanitized ASCII names,
    and Unicode-escaped identifiers create a second spelling for protected
    function names such as ``set_config``. Keep this raw SQL surface simple:
    callers may use normal quoted identifiers, but not ``U&`` escapes.
    """
    pos = 0
    n = len(sql)
    while pos < n:
        end = _scan_dollar_quote(sql, pos)
        if end is not None:
            pos = end
            continue
        m = _SQL_TOKEN_RE.match(sql, pos)
        if not m:
            pos += 1
            continue
        if (
            m.lastgroup == "ident"
            and m.group().lower() == "u"
            and m.end() + 1 < n
            and sql[m.end()] == "&"
            and sql[m.end() + 1] == '"'
        ):
            return True
        pos = m.end()
    return False


def contains_pg_settings_identifier(sql: str) -> bool:
    """Return True when SQL references PostgreSQL's ``pg_settings`` view."""
    pos = 0
    n = len(sql)
    while pos < n:
        end = _scan_dollar_quote(sql, pos)
        if end is not None:
            pos = end
            continue
        m = _SQL_TOKEN_RE.match(sql, pos)
        if not m:
            pos += 1
            continue
        if _token_identifier_name(m.lastgroup, m.group()) == "pg_settings":
            return True
        pos = m.end()
    return False


def _token_identifier_name(kind: str | None, text: str) -> str | None:
    if kind == "ident":
        return text.lower()
    if kind == "qid":
        return text[1:-1].replace('""', '"').lower()
    return None


def _next_significant_token_is_open_paren(sql: str, pos: int) -> bool:
    n = len(sql)
    while pos < n:
        end = _scan_dollar_quote(sql, pos)
        if end is not None:
            return False
        m = _SQL_TOKEN_RE.match(sql, pos)
        if not m:
            pos += 1
            continue
        if m.lastgroup in {"ws", "line_comment", "block_comment"}:
            pos = m.end()
            continue
        return m.lastgroup == "sym" and m.group() == "("
    return False


# PostgreSQL keywords must not be rewritten as bare table aliases. The
# rewriter is token-aware but not grammar-aware, so even non-reserved
# context-sensitive keywords like BETWEEN, EXISTS, and OVER can be SQL
# syntax in one position and identifiers in another. A keyword-shaped
# table remains reachable through the non-keyword vault-prefixed alias
# (`<vault>__<table>`).
#
# Source: PostgreSQL 16.14 Appendix C, PostgreSQL-keyword column.
# Frozen deliberately: reading pg_get_keywords() at runtime would make
# the MCP SQL contract depend on the connected server version.
_PG_KEYWORDS = frozenset(
    """
    abort absent absolute access action add admin after aggregate all also alter always
    analyse analyze and any array as asc asensitive assertion assignment asymmetric at
    atomic attach attribute authorization backward before begin between bigint binary
    bit boolean both breadth by cache call called cascade cascaded case cast catalog
    chain char character characteristics check checkpoint class close cluster coalesce
    collate collation column columns comment comments commit committed compression
    concurrently configuration conflict connection constraint constraints content
    continue conversion copy cost create cross csv cube current current_catalog
    current_date current_role current_schema current_time current_timestamp
    current_user cursor cycle data database day deallocate dec decimal declare default
    defaults deferrable deferred definer delete delimiter delimiters depends depth desc
    detach dictionary disable discard distinct do document domain double drop each else
    enable encoding encrypted end enum escape event except exclude excluding exclusive
    execute exists explain expression extension external extract false family fetch
    filter finalize first float following for force foreign format forward freeze from
    full function functions generated global grant granted greatest group grouping
    groups handler having header hold hour identity if ilike immediate immutable
    implicit import in include including increment indent index indexes inherit
    inherits initially inline inner inout input insensitive insert instead int integer
    intersect interval into invoker is isnull isolation join json json_array
    json_arrayagg json_object json_objectagg key keys label language large last lateral
    leading leakproof least left level like limit listen load local localtime
    localtimestamp location lock locked logged mapping match matched materialized
    maxvalue merge method minute minvalue mode month move name names national natural
    nchar new next nfc nfd nfkc nfkd no none normalize normalized not nothing notify
    notnull nowait null nullif nulls numeric object of off offset oids old on only
    operator option options or order ordinality others out outer over overlaps overlay
    overriding owned owner parallel parameter parser partial partition passing password
    placing plans policy position preceding precision prepare prepared preserve primary
    prior privileges procedural procedure procedures program publication quote range
    read real reassign recheck recursive ref references referencing refresh reindex
    relative release rename repeatable replace replica reset restart restrict return
    returning returns revoke right role rollback rollup routine routines row rows rule
    savepoint scalar schema schemas scroll search second security select sequence
    sequences serializable server session session_user set setof sets share show
    similar simple skip smallint snapshot some sql stable standalone start statement
    statistics stdin stdout storage stored strict strip subscription substring support
    symmetric sysid system system_user table tables tablesample tablespace temp
    template temporary text then ties time timestamp to trailing transaction transform
    treat trigger trim true truncate trusted type types uescape unbounded uncommitted
    unencrypted union unique unknown unlisten unlogged until update user using vacuum
    valid validate validator value values varchar variadic varying verbose version view
    views volatile when where whitespace window with within without work wrapper write
    xml xmlattributes xmlconcat xmlelement xmlexists xmlforest xmlnamespaces xmlparse
    xmlpi xmlroot xmlserialize xmltable year yes zone
    """.split()
)


def rewrite_table_names(sql: str, table_map: dict[str, str]) -> str:
    """Replace short table names in ``sql`` with their pg-qualified
    names, but ONLY for bare identifiers — never inside string literals,
    quoted identifiers, or comments.

    The map is matched case-insensitively (PG identifiers are
    case-folded for unquoted refs), so ``SELECT * FROM PIPELINE`` and
    ``FROM pipeline`` both rewrite. A quoted identifier ``"Pipeline"``
    is left alone because PG treats it as case-sensitive — rewriting it
    would change semantics.

    Longest key first so ``"sales_v2"`` doesn't get partially clobbered
    by a shorter ``"sales"`` entry.
    """
    if not table_map:
        return sql

    # Pre-sort once so each lookup is deterministic. Lower-case the keys
    # for the case-insensitive match.
    lowered = {k.lower(): v for k, v in table_map.items()}

    out: list[str] = []
    pos = 0
    n = len(sql)
    while pos < n:
        # Manual dollar-quote scan first; the regex below can't match
        # arbitrary tags with backreferences via a single alternation.
        end = _scan_dollar_quote(sql, pos)
        if end is not None:
            out.append(sql[pos:end])
            pos = end
            continue
        m = _SQL_TOKEN_RE.match(sql, pos)
        if not m:
            # Should not happen — `sym` catches any character. Safety net.
            out.append(sql[pos])
            pos += 1
            continue
        kind = m.lastgroup
        text = m.group()
        if kind == "ident":
            low = text.lower()
            # PostgreSQL keywords may be syntax in this position. Keyword-shaped
            # tables are available through their prefixed alias instead.
            if low in _PG_KEYWORDS:
                out.append(text)
            else:
                replacement = lowered.get(low)
                out.append(replacement if replacement is not None else text)
        else:
            out.append(text)
        pos = m.end()
    return "".join(out)
