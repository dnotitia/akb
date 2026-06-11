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

import re


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
            replacement = lowered.get(text.lower())
            out.append(replacement if replacement is not None else text)
        else:
            out.append(text)
        pos = m.end()
    return "".join(out)
