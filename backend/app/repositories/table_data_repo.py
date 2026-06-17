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
import re

from app.exceptions import ValidationError


TYPE_MAP = {
    "text": "TEXT",
    "number": "NUMERIC",
    "boolean": "BOOLEAN",
    "date": "DATE",
    "json": "JSONB",
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


# ── DDL ──────────────────────────────────────────────────────────


async def create_dynamic_table(conn, pg_name: str, columns: list[dict]) -> None:
    """Create the data-bearing PG table for a vault table. Caller is
    responsible for sanitising `pg_name` (use `pg_table_name`)."""
    col_defs = ["id UUID PRIMARY KEY DEFAULT uuid_generate_v4()"]
    for col in columns:
        col_name = safe_ident(col["name"])
        col_type = TYPE_MAP.get(col.get("type", "text"), "TEXT")
        not_null = " NOT NULL" if col.get("required") else ""
        col_defs.append(f"{col_name} {col_type}{not_null}")
    col_defs.append("created_by TEXT")
    col_defs.append("created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()")
    col_defs.append("updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()")
    await conn.execute(f'CREATE TABLE {pg_name} ({", ".join(col_defs)})')


async def drop_dynamic_table(conn, pg_name: str) -> None:
    await conn.execute(f"DROP TABLE IF EXISTS {pg_name}")


async def count_rows(conn, pg_name: str) -> int:
    """Returns 0 if the table does not exist (used by list_tables on a
    registry row whose data table was already dropped)."""
    try:
        return int(await conn.fetchval(f"SELECT COUNT(*) FROM {pg_name}") or 0)
    except Exception:  # noqa: BLE001 — table-missing is the usual case here
        return 0


async def add_column(conn, pg_name: str, col_name: str, col_type: str) -> None:
    """Add a column to the dynamic table. Caller sanitises name/type."""
    await conn.execute(
        f"ALTER TABLE {pg_name} ADD COLUMN IF NOT EXISTS {col_name} {col_type}"
    )


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

    ``SELECT {cols}, COUNT(*) FROM {pg} GROUP BY {cols} HAVING COUNT(*) > 1 LIMIT {n}``
    — identifiers via ``safe_ident``; ``limit`` is coerced to ``int``."""
    safe_cols = [safe_ident(c) for c in columns]
    col_list = ", ".join(safe_cols)
    lim = int(limit)
    rows = await conn.fetch(
        f"SELECT {col_list}, COUNT(*) AS dup_count "
        f"FROM {pg_name} "
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
