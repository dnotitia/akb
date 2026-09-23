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

Implemented on `feat/akb-433-column-logical-names`. The six gates above were
committed red first and are green on the branch; gate 6 was restated for
sparse storage (decision 11) and was committed red again before that change.
Where the build had to be more specific than this proposal, these decisions
hold:

1. **Logical name.** `columns[].name` is NFC-normalized and otherwise verbatim.
   Refused (422): non-string, empty or whitespace-only; any control character,
   NUL included (NUL is the digest separator); a lone surrogate, which has no
   UTF-8 form; more than 255 characters; the bookkeeping names
   `id`/`created_at`/`updated_at`/`created_by`/`row_commit` compared by
   casefold.
2. **Physical name.** `columns[].pg_name` is server-derived and a caller-supplied
   one is a 422. A logical name is **plain** when it matches `^[a-z][a-z0-9_]*$`
   within 63 bytes and is not one of the 101 words PostgreSQL refuses as a
   column name (`user`, `order`, `group`, …: `pg_get_keywords()` catcode R or
   T, frozen from PostgreSQL 16). A plain name is its own physical name;
   anything else is `c_<ordinal>_<digest8>`, ordinal =
   1-based position among user columns when added, digest = first 8 hex of
   `sha1(table_pg_name NUL logical_name)`. The digest is over the table's
   physical `vt_…` name rather than `table_id` (as first proposed) so that
   re-creating a table from the same document yields the same registry row
   (gate 3). The other keywords (`name`, `type`, `value`, …) work unquoted as
   column names and stay plain, so `SELECT name FROM products` keeps working.
3. **Uniqueness.** Logical names are unique by `casefold(NFC(name))`; physical
   names are unique and never a bookkeeping name, 422 otherwise (a clash needs a
   caller's plain name to equal a derived one).
4. **Resolution.** Every name a caller gives — unique keys, indexes, alter
   operations, row API select/filter/order/insert/update keys and the AST,
   `on_conflict`, `references.column` — resolves by `casefold(NFC(input))`
   against logical names through one helper (`table_data_repo.column_key`).
   A drop, like a rename or an alter, must name a declared column; any other
   name is a 422 and never reaches `DROP COLUMN`, because a plain name can be
   another column's physical name (a header's `c_1_…`, or `age` after a
   logical rename to `나이`). A rename map whose keys name one column twice
   (`age` and `AGE`, or NFC and NFD spellings) is a 422.
5. **SQL.** Only `pg_name` is interpolated: DDL, CHECK/enum/FK/UNIQUE/INDEX
   definitions, the duplicate preflight, `pg_attribute` comparison.
   `table_data_repo.column_pg_name(col)` returns the stored `pg_name`, or, when
   none is stored, `safe_ident(name).lower()` — the identifier DDL before the
   split made. That fallback is the contract (decision 11), not a transition
   shim. RoleSync grants tables and never names a column.
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
   on commas. A query-string key is a filter or a control, never both, and a
   header may be spelled like a control (`Limit`, `Order`, lowercase `order`):
   on a read, `select`/`order`/`limit`/`offset` is a filter only when it names
   a column and its value is a filter (`<op>.<value>`), so the web UI's
   `limit=50&offset=0&order=created_at.desc,id.desc` still pages and sorts. On
   PATCH/DELETE a control naming a column is a filter unless the mutation
   reads it and the value is one it takes — `select` a column list, `all` a
   yes/no; `expected_row_commit` is always the CAS token — so a filter on such
   a column is never dropped (8d04a2aa), in any spelling. Controls a mutation
   never reads (`count`, `order`, `limit`, `offset`, `resolution`,
   `on_conflict`) are filters on a column of that name, and a malformed one is
   a 400, not ignored.
9. **`akb_sql` and read surfaces.** `akb_sql` spells columns physically, with no
   column rewriting; a logical name in SQL gets a hint naming the `pg_name`.
   Every read of a table's columns — schema reads, table lists, `akb_browse`
   on both the Git and the Native arm, vault info, create/alter responses, MCP — reports `name` and `pg_name`,
   computed through `column_pg_name` whether or not it is stored; MCP tool text
   explains both.
10. **Search.** The table metadata chunk keeps logical names.
11. **Sparse storage, no backfill.** Supersedes "Compatibility and migration"
    above. A registry column stores `pg_name` only where it differs from
    `safe_ident(name).lower()`; otherwise the key is absent. The rule lives at
    the one place columns are written (`table_registry_repo.storable_columns`,
    used by `insert` and `update_columns`), so create, idempotent create, alter
    add, rename and app rollout all store sparsely; a rename that brings a name
    back to its physical name drops the key again. `parse_columns` fills
    `pg_name` back in on read. No migration runs and nothing is backfilled: a
    table whose column names are all plain — every table that exists, and any
    created with plain names — is stored exactly as before, byte for byte.
12. **`if_not_exists`.** The spec comparison ignores `pg_name`.
13. **App manifests.** A manifest keeps the plain column grammar,
    `references.column` included; the fingerprint ignores `pg_name`. The
    rollout's create step derives physical names as the table service does
    (a reserved word gets `c_…`) and resolves a referenced column's physical
    name through its table's registry row; `backfill_column` and the
    `set_not_null` precheck resolve their column through the registry, since
    a table adopted after logical renames can hold one column's name on
    another's physical column.

Gate 2 is exercised with `분류`/`모델`, which `safe_ident` really does send to
the same `__` (`중요도` becomes `___`, so the pair in the gate's text does not
quite fuse).

Table and vault names, the app-manifest column grammar (decision 13), `akb_sql`'s
Unicode-escape and `pg_settings` defenses, row-level policy and column ACLs are
unchanged.

### Rolling deploy and rollback

Nothing is migrated, so there is no window to protect: every table that exists
before this change keeps its registry row byte for byte — no column stores
`pg_name` — and code from before #433 goes on using it as it did. That code
addresses a column as `safe_ident(name)` and accepts only names matching
`^[a-z][a-z0-9_]*$`, so a table this code creates or alters stays fully
readable and writable by it — during a rolling deploy and after a rollback —
while every column name is plain and no column stores `pg_name`: created with
plain names and renamed, if at all, only physically (plain to plain). Anything
else is new-code-only, whether or not it stores `pg_name`. A header can store
none — a logical rename of `age` to `Age`, or of `a_b` to `A B`, leaves the
physical name `safe_ident` gives — and older code still refuses or misreads it.
A name in the old grammar can store one — a reserved word such as `order`,
whose physical name is `c_…`, or plain names moved onto one another's physical
columns by logical renames — and older code addresses the wrong column, or
none.

### Known limits

Behaviour left as it is, on purpose:

- **NUL.** A column name containing NUL is a 422 over MCP. Over REST the request
  model (`NFCModel`) strips NUL from every string before the service sees it,
  so `"분\x00류"` arrives as `"분류"` and is accepted. That is the pre-existing
  REST normalization, not a column-name rule.
- **Commas.** A header containing a comma cannot be named in a query-string
  `select`, `order` or `on_conflict`, which split on commas. The JSON AST can:
  a `select`/`returning` array is not re-split, and `order` objects and filters
  take one name each. PostgREST-style double quoting is not implemented.
- **Rename onto a name held physically.** Renaming a column to a plain name
  that another column already uses as its physical name is a logical-only
  rename (decision 7): `RENAME COLUMN` would fail, and the logical rename is
  still what was asked.

Follow-ups in the web UI, which renders and edits rows keyed by logical names
but does not yet let a header be authored, sorted or published:

- **URL sort/filter state.** `frontend/src/lib/table-query-state.ts`
  (`parseSort`, `parseFilter`) accepts only `^[a-z][a-z0-9_]*$`, so sorting or
  filtering by a header column is dropped from the URL and falls back to the
  default sort.
- **Create dialog.** `frontend/src/components/table-create-dialog.tsx` still
  refuses a non-plain column name.
- **Table publication.** `frontend/src/lib/table-publication.ts` refuses to
  publish a non-plain column; it would need to select the `pg_name` and alias
  it back to the header.
