---
status: accepted
stage: implementation
created: 2026-09-17
updated: 2026-09-23
issue: dnotitia/akb#433
baseline: d25d6ee
---

# Column logical/physical name split

## Context

`_COLUMN_NAME_RE = ^[a-z][a-z0-9_]*$`
(`backend/app/services/table_service.py:85`) rejects every column name that is
not lowercase ASCII. For a schema authored in code that is a good rule. For a
table whose column names come **from a document**, it is a wall: over a corpus
of 74 Korean-language PDFs, 1,450 structurally clean tables were extracted and
**zero** have column names the rule accepts — 94% carry Korean headers
(`분류`, `중요도`, `모델`, `추론`), the rest fail on spaces, parentheses or
capitals. PostgreSQL itself would take these as quoted identifiers; the
restriction is AKB's (#433).

The rule exists for a sound reason, stated in its own docstring: it "keeps the
registry name identical to its `safe_ident` PG identity". One string in two
places, no mapping that can drift. Any widening has to answer what replaces
that guarantee — and the answer cannot be "widen the regex", because
`safe_ident` maps every non-`[a-zA-Z0-9_]` character to `_`: `분류` and `중요도`
would both become `__`. Widening the regex without replacing the identity
mapping turns a clean 422 into silent column collisions.

The blast radius is four layers sharing one assumption, "registry name == PG
identifier":

1. **Declaration** (`table_service.py`): `_validate_column_name` /
   `_TABLE_NAME_RE` on create and alter; the registry `columns` JSON carries
   the name that becomes the PG column.
2. **DDL** (`table_data_repo.column_definition` and friends): `safe_ident`
   interpolated unquoted. No quoted-identifier path exists anywhere.
3. **Constraints and enforcement**: unique-key / index resolution keyed on
   `.lower()` (`_check_key_column`, `_declared_column_lookup`),
   `generate_constraint_name` (underscore-flatten + digest),
   `role_sync._VT_TABLE_RE = ^vt_[a-z0-9_]+__[a-z0-9_]+$` in front of raw SQL.
4. **Reads**: the `akb_sql` table-name rewriter, `_fetch_column_meta` against
   `pg_attribute`, the `sql_name` projection (#110), MCP tool schemas.

Since #433 was filed the terrain moved further in the same direction:
declarative unique keys/indexes (#215 follow-ups) assume the same grammar,
`pg_table_name` fits over-long pairs with a digest tail (#470), and `akb_sql`
explicitly refuses Unicode-escaped identifiers
(`contains_unicode_escaped_identifier`) as a defense line. A quoted-Unicode
DDL would have to re-verify every one of these.

## Decision (proposed)

Split the column name into a **logical name** (human-facing, stored in the
registry, returned by every read surface) and a **physical name** (ASCII-only,
the only thing that ever reaches PostgreSQL):

- `columns[].name` keeps the original header verbatim after NFC
  normalization — `분류`, `TriviaQA(비과학 문헌)`, `w Embedding`. This is
  what search, schema reads, `akb_browse`, and MCP return. The column name —
  the single most useful thing about a table for search and for a human
  reading the schema — stays searchable.
- `columns[].pg_name` is derived once at create, ASCII-only
  (`c_<ordinal>_<digest8>`, digest over the NUL-joined
  `(table_id, logical_name)` tuple in the shape `generate_constraint_name`
  already uses). DDL, constraint definitions, the rewriter, `pg_attribute`
  comparison, and RoleSync interpolate `pg_name` only; `safe_ident` is never
  called on a logical name again.
- The existing grammar stays exactly where it is for one case: a logical
  name that already matches `^[a-z][a-z0-9_]*$` keeps `pg_name == name`, so
  every table that exists today backfills byte-identical and no migration
  rewrites live DDL. This mirrors how #470 fitted pairs without touching
  tables that already fit.

What this explicitly does **not** do:

- No quoted-Unicode DDL. The `PG_IDENT_MAX_LEN = 63` **byte** bound
  (`table_data_repo.py:136`) hits Korean at ~21 characters in UTF-8; quoted
  identifiers do not move that wall, while an ASCII physical name keeps the
  existing `len() == byte-count` equivalence (and its warning comment) intact.
- No transliteration at ingest (#433 option 2). A mapping has to be stored
  either way; a digest-derived physical name is deterministic where
  translation is not — the same table parsed twice yields the same schema.
- No table-name change in this slice. `_TABLE_NAME_RE` shares the grammar,
  but measured demand is at the column layer (1,450 blocked tables) and the
  vault-name grammar (`document_service._VAULT_NAME_RE`) is load-bearing for
  `pg_table_name` framing. Tables follow as a second slice if demand appears.

## Compatibility and migration

- Registry rows gain `pg_name` per column. Backfill: `pg_name = name` where
  the name already matches the grammar (all existing rows, by construction);
  nothing else changes, no DDL rewrite.
- `sql_name` (#110) is a table-name contract and is untouched. Column-level
  SQL spelling is the physical name; read surfaces return the logical name
  alongside it so callers never parse one from the other.
- `safe_ident(logical_name)` call sites (`table_service.py:215/291/1408/
  1449-1450/1714`, `column_definition`, `_column_check_sql`,
  `create_unique_constraint`, enum/check definitions) switch to the stored
  `pg_name`. The function itself stays for genuinely caller-chosen ASCII
  inputs (constraint names).
- Case-insensitive lookups (`_declared_column_lookup`, `_check_key_column`)
  key on the logical name with NFC applied once at the boundary, not per
  call site.

## Acceptance gates (to be red before implementation)

1. A table with Korean headers (`분류`, `중요도`) creates, inserts, alters,
   and drops; every read surface returns the original headers verbatim.
2. Two headers that `safe_ident` would fuse (`분류`/`중요도` → `__`/`__`)
   coexist on one table with distinct physical names.
3. Re-parsing the same document yields the identical registry row
   (deterministic physical names, no drift).
4. A 21+ character Korean header (past the 63-byte bound as UTF-8) is
   accepted; the physical name stays within the bound.
5. `akb_sql`, `pg_attribute` drift detection, RoleSync reconciliation, and
   unique-key/index enforcement all observe the physical name and agree with
   each other after downgrade/revoke-equivalent operations.
6. Existing tables read back byte-identical (`pg_name == name` backfill).

## Explicitly out of scope

- Table-name / vault-name grammar changes (second slice on measured demand).
- Row-level policy (#407 Q3); this slice keeps the Vault/table grain AKB
  enforces.
- Deny semantics, column-level ACLs, retroactive header renames.
- The interim workaround stays valid and should be documented: positional
  names (`col_1`, …) with originals in metadata (#433 option 1) — no AKB
  change, at the cost of searchable headers. Each ingest caller currently
  pays that cost independently and differently; unifying the documented
  form is worthwhile even before this proposal lands.

## Implementation (2026-09-23)

Implemented on `feat/akb-433-column-logical-names` (base `2ed799bf`); the six
gates above were committed red first and are green on the branch. Where the
build had to be more specific than this proposal, these decisions hold:

1. **Logical name.** `columns[].name` is NFC-normalized and otherwise verbatim.
   Refused (422): non-string, empty or whitespace-only; any control character,
   NUL included (NUL is the digest separator); more than 255 characters; the
   bookkeeping names `id`/`created_at`/`updated_at`/`created_by`/`row_commit`
   compared by casefold.
2. **Physical name.** `columns[].pg_name` is server-derived and a caller-supplied
   one is a 422. A logical name matching `^[a-z][a-z0-9_]*$` within 63 bytes is
   its own physical name; anything else is `c_<ordinal>_<digest8>`, ordinal =
   1-based position among user columns when added, digest = first 8 hex of
   `sha1(table_pg_name NUL logical_name)`. The digest is over the table's
   physical `vt_…` name rather than `table_id` (as first proposed) so that
   re-creating a table from the same document yields the same registry row
   (gate 3).
3. **Uniqueness.** Logical names are unique by `casefold(NFC(name))`; physical
   names are unique and never a bookkeeping name, 422 otherwise (a clash needs a
   caller's plain name to equal a derived one).
4. **Resolution.** Every name a caller gives — unique keys, indexes, alter
   operations, row API select/filter/order/insert/update keys and the AST,
   `on_conflict`, `references.column` — resolves by `casefold(NFC(input))`
   against logical names through one helper (`table_data_repo.column_key`).
5. **SQL.** Only `pg_name` is interpolated: DDL, CHECK/enum/FK/UNIQUE/INDEX
   definitions, the duplicate preflight, `pg_attribute` comparison.
   `table_data_repo.column_pg_name(col)` falls back to `safe_ident(name).lower()`
   only for a registry row written before the backfill. RoleSync grants tables
   and never names a column.
6. **Generated names** (unique keys, indexes, check/enum/FK constraints) derive
   from physical column names — identical to before for every existing table.
7. **Rename.** Physical `RENAME COLUMN` only when the column's `pg_name` equals
   its logical name and the new name is plain; otherwise the rename is logical
   and `pg_name` and constraint names stay. A plain target another column
   already holds physically also renames logically, since `RENAME COLUMN`
   would fail.
8. **Row API.** Callers use logical names; response rows and `columns` are keyed
   by logical names, remapped from physical in Python — no quoted-identifier
   aliases in SQL. A JSON-AST `select`/`returning` array is no longer re-joined
   on commas.
9. **`akb_sql` and read surfaces.** `akb_sql` spells columns physically, with no
   column rewriting; a logical name in SQL gets a hint naming the `pg_name`.
   Schema reads return `name` and `pg_name` per column; MCP tool text explains
   both.
10. **Search.** The table metadata chunk keeps logical names.
11. **Backfill.** Migration 113 writes `pg_name = safe_ident(name).lower()` onto
    every registry column lacking one. Registry only, no DDL, idempotent,
    `updated_at` untouched.
12. **`if_not_exists`.** The spec comparison ignores `pg_name`.

Gate 2 is exercised with `분류`/`모델`, which `safe_ident` really does send to
the same `__` (`중요도` becomes `___`, so the pair in the gate's text does not
quite fuse).

Table and vault names, the app-manifest column grammar, `akb_sql`'s
Unicode-escape and `pg_settings` defenses, row-level policy and column ACLs are
unchanged. The web UI's create-table dialog and its sort/filter URL state still
accept only plain column names (rendering rows keyed by logical names works);
that is the next slice if headers are to be authored or sorted in the UI.
