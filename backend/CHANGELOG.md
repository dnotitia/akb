# AKB Backend — Changelog

The AKB backend ships as a Docker image and as the HTTP layer behind
the `akb-mcp` stdio proxy. This changelog tracks the backend
specifically; the proxy has its own log in
`packages/akb-mcp-client/CHANGELOG.md` and a separate version stream.

## 0.6.3 — 2026-06-05  *(patch — 0.6.2 review findings)*

Independent review of the 0.6.2 BM25 fallback surface turned up one
real correctness gap and a handful of hygiene items. Two larger
suggestions (per-row reject signalling, `CONCURRENTLY` HNSW rebuild)
were considered but kept out as over-engineering at the current scale.
See the *Considered and rejected* section below.

### Bug fix

**`embed_worker` could now create useless points** when the embed API
was disabled / failed AND the row's sparse vector was also empty
(whitespace-only or OOV-only content). Before this PR the per-row loop
would call `vector_store.upsert_one(dense=None, sparse_indices=[])`,
producing a pgvector row with `dense IS NULL AND sparse_terms = '{}'`
(invisible to both legs) or a Qdrant `vectors={}` upsert (rejected by
the driver with a less specific error than the actual cause). Now the
worker fails such rows up-front with `"no indexable signal (no dense
embedding + empty sparse)"`; they go through normal retry semantics
and stop accumulating in the index. The pre-0.6.2 atomic-pair check
covered this implicitly — 0.6.2's relaxation re-opened the gap.

### Hygiene

- **`vector_store.base.has_dense(dense)`** — single source of truth
  TypeGuard for "this point has a usable dense vector". All three
  drivers (`pgvector`, `qdrant`, `seahorse`) and `embed_worker` now
  branch on this helper instead of inlining three different falsy
  checks. mypy narrows `dense` to `list[float]` inside the guarded
  block, so call sites no longer need redundant asserts.
- **`embeddings_padded` is now `list[list[float] | None]`**, padded
  with explicit `None` instead of `[]` and a falsy-game comment. The
  three-branch comment block at the top of stage 2 was shortened — the
  "per-row reject" sub-case the 0.6.2 PR drafted around turned out
  not to be distinguishable from a batch outage without a richer
  upstream contract; if that ever becomes a signal we want, surface it
  from `generate_embeddings` directly.
- **Mismatched batch length is now an explicit error log + outage
  promotion** rather than a silent zip-shorter footgun
  (`if embeddings and len(embeddings) != len(batch): logger.error(…)`).
- **`pgvector.ensure_collection` legacy-HNSW check** now inspects
  `pg_index.indpred IS NOT NULL` instead of `"WHERE" in indexdef`. The
  textual probe was correct on PG 16 but brittle across PG version
  upgrades that could re-format `indexdef`; `indpred` is the
  catalog's stable boolean for "this index has a WHERE clause".
- **`config.py` `embed_base_url` comment** said "required" — stale
  since 0.6.2. Now reads "Optional; unset disables the dense leg →
  BM25-only retrieval".
- **`vector_store.base.upsert_one` docstring** was claiming atomicity
  across all drivers ("stores dense + sparse + payload atomically").
  pgvector joins the caller's PG transaction; Qdrant and Seahorse
  ignore `conn` and rely on `chunk_id` idempotence for recovery. Now
  spelled out explicitly.

### Considered and rejected

- **Per-row reject signalling.** The review flagged that a single-row
  `[]` from the embed API would look identical to a batch outage at
  the worker. Investigated and kept out: `generate_embeddings`
  currently has no upstream contract for partial responses (a missing
  row is treated as a batch-level failure inside `index_service`), so
  distinguishing the case at the worker would just be a guard for a
  scenario we can't actually produce. If the upstream ever surfaces
  per-row rejects, the worker should grow a `dense=[]` branch then;
  pre-emptive guards for impossible states age into liabilities.
- **HNSW `CREATE CONCURRENTLY`.** At the current single-digit-K chunks
  scale the rebuild is sub-second; CONCURRENTLY's own failure mode
  (an `INVALID` leftover index that `IF NOT EXISTS` then refuses to
  recreate) is genuinely worse than the brief lock. The doc-comment
  records this trade-off so a future maintainer at a much larger
  chunks count can re-evaluate.

### Verification

- `bash scripts/check.sh` — ruff + mypy + tsc + vitest + secrets pass.
- `bash backend/tests/test_publications_e2e.sh` — 97/97.
- `bash backend/tests/test_mcp_e2e.sh` — 76/76.

---

## 0.6.2 — 2026-06-05  *(patch — BM25 fallback when the embed API is unavailable)*

### Bug fix

Before 0.6.2, `embed_worker` treated dense+sparse as an atomic pair:
if the embedding API was unreachable (or `embed_base_url` empty),
the whole batch was `_mark_failure`'d and nothing landed in
`vector_index`. Result: self-hosted instances that didn't configure
an embedding endpoint, and any temporary upstream outage, dropped to
**0 search hits across the affected vault** — not "embedding leg
degraded, BM25 still works", which is what the search-pipeline
docstrings and the `embed_base_url` doc-comment implied.

The fix removes the all-or-nothing coupling end-to-end so dense is
genuinely optional:

- **`vector_store.upsert_one`**: signature now
  `dense: list[float] | None` across all three drivers. `None` means
  "store this point with sparse only". pgvector writes a NULL column;
  qdrant omits the dense entry from its named-vectors dict; seahorse
  omits the column from the upsert row.
- **`vector_index.chunks.dense`** column is now nullable, and the
  HNSW index is *partial* (`WHERE dense IS NOT NULL`) so sparse-only
  rows don't get indexed for dense KNN and don't appear as
  zero-vector neighbours in the dense leg. The dense leg's
  `ORDER BY dense <=> $1` SQL gains the matching `WHERE dense IS NOT
  NULL` filter. Existing deployments idempotently lose the `NOT NULL`
  constraint and have the legacy full HNSW dropped+rebuilt as partial
  at the next `ensure_collection` (startup).
- **`embed_worker`** distinguishes three cases that used to all
  bucket as failure:
  1. `embed_base_url == ""` → intentional, no embed call attempted,
     log at DEBUG, every row → sparse-only indexed, `succeeded`
     counter increments normally.
  2. embed configured + transient outage → log WARNING once per
     batch, fall through to sparse-only for this batch (do not
     retry-storm). The `_mark_failure` / `vector_retry_count` flow is
     reserved for problems the worker can actually fix on its own
     next pass (sparse encoder failure, vector_store unavailable).
  3. sparse encoder or vector_store failure → real `_mark_failure`,
     normal retry semantics unchanged.

The search side already had the symmetric `query_dense=None` path
in `search_service` and the driver `hybrid_search` implementations,
so the sparse-only fallback became end-to-end correct as soon as
the indexing side stopped withholding rows.

### Manual verification

Reproduces the embed-disabled scenario directly: set `embed_base_url:
""` in `config/app.yaml`, rebuild + restart the backend, create a doc,
wait for `/health` `pending=0`, then search. The doc lands in
`vector_index.chunks` with `dense IS NULL` + posting rows populated,
and the BM25 leg returns it. Confirmed in the docker-compose dev
stack:

```
score=0.0164 title="Symantec DLP issue"
total=1
```

Regression: `bash backend/tests/test_publications_e2e.sh` 97/97,
`bash backend/tests/test_mcp_e2e.sh` 76/76.

### Out of scope (follow-ups)

- **Automatic dense backfill** when an embed endpoint is configured
  *after* sparse-only rows already exist. The current worker keeps
  picking up `vector_indexed_at IS NULL` rows; sparse-only rows have
  it set, so they're considered done. A separate explicit reindex
  tool (or a `dense_pending` flag column) is the cleanest next step.
- pgvector `posting`-shape: this release only validated the `arrays`
  shape (the dev / prod default) end-to-end. The `posting`-shape code
  paths went through the same diff and compile, but were not
  e2e-exercised. Operators on the posting shape should re-run the
  regression suite before pinning `:0.6.2`.

---

## 0.6.1 — 2026-06-04  *(patch — three latent bugs around the 0.6.0 surface, plus envelope close-out)*

Three honest bug fixes that surfaced during the post-0.6.0 review, plus
the leftover piece of the 0.5.6 error-envelope unification. No contract
changes beyond a single new stable error code; no migration needed.

### Bug fixes

1. **`_handle_publication_snapshot` mis-classified non-input failures as
   `INVALID_ARGUMENT`.** The handler caught every `PublicationError` and
   flattened it to `INVALID_ARGUMENT`, even when `create_snapshot` raised
   502 (S3 upload failure — not the caller's fault) or 404 (the row
   vanished between the lookup and the locked re-read). Now maps by
   `e.status_code`: 404 → `NOT_FOUND`, 4xx → `INVALID_ARGUMENT`, 5xx →
   `INTERNAL` (new code, see below). Callers that branched on `code` to
   decide whether to retry vs. fix their input were getting the wrong
   answer for the storage-failure path.

2. **`akb_unpublish(uri=…)` URI branch wasn't vault-bound at the SQL
   layer.** The slug branch already passed `expected_vault_id` to the
   service for belt-and-suspenders IDOR protection; the URI branch
   relied solely on the URI encoding the vault. The URI grammar
   guarantees that holds in practice, but having one model on the slug
   branch and a different model on the URI branch was itself a smell.
   `delete_publications_for_document` / `_for_file` now accept optional
   `expected_vault_id`, and the URI branch threads `access["vault_id"]`
   through. Same explicit binding on both sides.

3. **MCP `call_tool` last-resort `except Exception` broke the 0.5.6
   error envelope.** 0.5.6 (`c00791e`) collapsed every error response
   to `{error, code, hint?, details?}`, but the dispatch-level catch
   was missed and kept returning bare `{error: str(e)}` — no `code`.
   Any handler that `raise`d instead of returning `err(...)` therefore
   shipped a response that broke the canonical envelope contract.
   Catch-all now returns `err(str(e), code=INTERNAL)`, so every
   response path — success, expected failure, and unhandled exception —
   carries the same shape.

### New error code

- `INTERNAL = "internal"` in `app/util/errors.py`. Used by the
  dispatch's last-resort catch and by any handler that needs to surface
  a 5xx-class failure without falsifying caller-side blame.

### Hygiene (no behavior change)

- `Mode.ALL` constant removed — was dead after 0.6.0 dropped the
  publish-time `mode` option.
- `delete_publication()` collapsed to slug-only — no caller passes
  `publication_id` after 0.6.0 (routes use slug, MCP was always
  slug-only); the dual-shape signature was just `# old.` framing.
- `resolve_table_query_publication` no longer says
  `publication.get("mode", Mode.LIVE)` — `mode` is a NOT NULL column
  with `'live'` default, so the `.get` default was a fallback that
  could never fire. Replaced with direct subscript so the code reads
  honestly.
- `publication_meta` had a `elif rt == DOCUMENT: meta["title"] = ...`
  branch that re-assigned the same value `meta` already carried from
  three lines above. Dead elif removed, intent commented.
- `_PUBLIC_FIELDS` renamed to `_PUBLIC_PASSTHROUGH_FIELDS` with a
  docstring naming the two derived keys (`share_url`,
  `password_protected`) that `to_public_dict` adds — single place to
  extend.
- `_row_to_internal_dict` previously accepted an already-parsed dict
  as a silent third branch between the JSON string and None cases.
  Added an explicit `isinstance(qp, dict)` check + clear error for any
  other type.
- `_handle_unpublish` URI branch replaced a `not parsed.identifier`
  defensive guard (unreachable after the kind narrowing above it) with
  `assert parsed.identifier is not None`. Same mypy narrowing, honest
  intent.
- Stale comment on the table_query default title removed.

### Tests

- `backend/tests/test_publications_e2e.sh` — 97/97 (unchanged from 0.6.0;
  the response-shape assertions added in 0.6.0 still pass).
- `backend/tests/test_mcp_e2e.sh` — 76/76.

### Verification

Six agent-driven prod scenarios after each deploy step (doc publish,
file publish, snapshot-rejection-for-doc, `unpublish(uri=<file_uri>)`,
table_query publish→snapshot, list-item-shape-matches-publish) — all
clean. Existing 24 active prod publications keep resolving normally;
no schema migration, contract changes only.

### Migration

None. 0.6.0 callers see the same external shape; only the dispatch
catch-all now returns `code: "internal"` in places that previously
returned `{error: "..."}` with no `code` field. If anything, this is a
contract *strengthening* (envelope is now total).

---

## 0.6.0 — 2026-06-04  *(BREAKING — publication tool surface rewritten)*

The `akb_publish` / `akb_unpublish` / `akb_publications` /
`akb_publication_snapshot` quartet had accumulated several
inconsistencies that confused agent callers: response shapes differed
between the four tools, `publication_id` and `slug` competed for the
"identifier" role, `public_url` / `public_url_full` / `public_base`
all came back together with a null-fallback contract, the `mode`
option overlapped with the dedicated snapshot tool, and
`akb_unpublish(uri=...)` silently rejected file URIs (a bug, not a
design choice). This release replaces the whole surface with a single,
consistent shape.

### One canonical publication dict

Every endpoint (`akb_publish`, `akb_publications`,
`akb_publication_snapshot`, `POST /publications/{vault}/create`,
`GET /publications/{vault}`) now returns the exact same dict shape:

```jsonc
{
  "slug": "...",                  // sole external identifier
  "share_url": "https://...",     // always absolute (see below)
  "resource_type": "document|file|table_query",
  "resource_uri": "akb://...|null",
  "vault": "...",                 // human-readable vault name
  "title": "...|null",
  "mode": "live|snapshot",
  "expires_at": "...|null",
  "max_views": null|N,
  "view_count": 0,
  "allow_embed": true,
  "section_filter": "...|null",
  "password_protected": false,
  "created_at": "...",
  "snapshot_at": "...|null",
  // table_query only:
  "query_sql": "...|null",
  "query_vault_names": [...]|null,
  "query_params": {...}
}
```

**Removed fields** (no longer surfaced to any client):
`publication_id`, `public_url`, `public_url_full`, `public_base`,
`snapshot_s3_key`.

**Internal-vs-public dict boundary**: `publication_service` now has
exactly two helpers — `_row_to_internal_dict(row)` for code that needs
`id` / `password_hash` / `snapshot_s3_key` (route password check,
S3 fetch), and `to_public_dict(internal)` for every response. There
is no `_enrich_publication` post-processing step anymore.

### `AKB_PUBLIC_BASE_URL` is now startup-required

`share_url` is always an absolute URL. The lifespan refuses to start
if `AKB_PUBLIC_BASE_URL` is unset (alongside `AKB_JWT_SECRET` and
`AKB_DB_PASSWORD`). No nullable URL field, no fallback chain, no
client-side string concatenation.

### `akb_unpublish(uri=...)` now accepts file URIs

The old code path called `split_uri(uri, expected_type="doc")`,
silently rejecting file URIs even though `akb_publish(uri=file_uri)`
worked. Now the URI's `kind` drives the cascade:
`doc` → `delete_publications_for_document`,
`file` → `delete_publications_for_file` (previously defined but never
called). table_query publications have no resource URI, so they
remain slug-only.

Response is now `{"deleted": N}` (was a mix of
`{"published": false, "deleted": bool}` and `{"published": false,
"deleted_publications": N}`).

### `mode` is no longer a publish-time option

`akb_publish` and `POST /publications/{vault}/create` no longer accept
a `mode` parameter. Every publication is created with `mode='live'`.
Snapshot is a state transition reached through
`akb_publication_snapshot(slug=...)` — the only path that ever made
sense, since `mode='snapshot'` at create time meant nothing for
document/file publications.

### REST `publication_id` URL params replaced with `slug`

`DELETE /publications/{vault}/{publication_id}` →
`DELETE /publications/{vault}/{slug}`.
`POST /publications/{vault}/{publication_id}/snapshot` →
`POST /publications/{vault}/{slug}/snapshot`.

The internal-only `publication_id` UUID is no longer accepted as a URL
path identifier, no longer returned in responses, and no longer
expected as an unpublish input — `slug` is the single external handle.

### `akb_publication_snapshot` input simplified

Accepts only `{slug}`. The owning vault is resolved from the
publication row and writer access is verified against it (no need
for the caller to pass `vault` — and the prior surface let a writer
on vault A trigger a snapshot of vault B's publication when they
guessed the slug, which is now structurally impossible).

### `section` → `section_filter`

The `akb_publish` / REST publish input now uses `section_filter`,
matching the response field name and the DB column. Document-only.

### Frontend response keys migrated

`frontend/src/lib/api.ts` exports a typed `Publication` interface
matching the new dict. `frontend/src/pages/publications.tsx` uses
`p.slug` for all keying and `p.share_url` for the "Copy link"
button. `frontend/src/components/publish-options-dialog.tsx` already
read `result.slug`, so it was unchanged.

### Migration

Callers using the MCP tools:

| 0.5.x | 0.6.0 |
|---|---|
| `akb_publish(..., mode="snapshot")` | `akb_publish(...)` then `akb_publication_snapshot(slug=...)` |
| `akb_publish(..., section="...")` | `akb_publish(..., section_filter="...")` |
| `akb_publication_snapshot(vault=..., slug=...)` | `akb_publication_snapshot(slug=...)` |
| reading `result.public_url_full` | `result.share_url` |
| reading `result.publication_id` | `result.slug` (it was always the right identifier) |

REST callers:

| 0.5.x | 0.6.0 |
|---|---|
| `DELETE /publications/{vault}/{publication_id}` | `DELETE /publications/{vault}/{slug}` |
| `POST /publications/{vault}/{publication_id}/snapshot` | `POST /publications/{vault}/{slug}/snapshot` |
| `POST /publications/{vault}/create` body `{section: ...}` | `{section_filter: ...}` |
| `POST /publications/{vault}/create` body `{mode: ...}` | (removed — call snapshot endpoint) |

### Tests

`backend/tests/test_publications_e2e.sh` reshaped end-to-end (slug-only
routes, response-shape assertions for the removed fields, new
`akb_unpublish(uri=file_uri)` regression test that exercises the bug
the prior surface couldn't fix). `backend/tests/concurrency/repro_pub_security.sh`
moved off `publication_id` onto `slug`.

---

## 0.5.8 — 2026-06-04  *(minor breaking — `query` alias removed)*

Whole-repo "half-done migration" sweep. The 0.5.4–0.5.7 stream
closed several files cleanly but left a few dead seams behind. This
release retires them.

### `query` alias on `akb_list_vaults` + `akb_browse` removed

The `query` arg was kept "for one minor release" when `filter` became
the canonical name (0.3.x era — `akb_search.query` started to collide
with `query` as filter). That release window expired five versions
ago. Both `tools.py` inputSchemas dropped the `query` property and
`_filter_arg` (`mcp_server/server.py`) no longer reads it.

**Breaking note**: a caller still passing `query="..."` now hits the
0.5.4 unknown-arg gate and gets a fuzzy-hint response pointing at
`filter`. Frontend uses neither; akb-mcp proxy is pass-through. No
known external caller needs migration, but flagged here so anyone who
finds a 4xx in logs can self-diagnose.

### `build_table_name_map` backward-compat keys deleted

`table_data_repo.build_table_name_map` was carrying two extra keys per
table — the raw display name and `replace("-", "_")` — "for pre-fix
callers". After 0.5.5's `pg_short_name` keying landed and the prod
legacy hyphen / non-ASCII tables were renamed (out-of-band rename in
this session), neither key is reachable: the SQL tokenizer accepts
only `[A-Za-z_][A-Za-z0-9_]*`, and no table with a hyphen in its
display name exists any more. Two lines + comment removed.

### Frontend delete-vault dialog text

`delete-vault-dialog.tsx` still promised the dialog would wipe
"sessions, memories" alongside other vault content. Those PG tables
were dropped by migration 031 (0.5.0). Wording updated to match
what's actually deleted.

### Docstring drift

- `ParsedUri` docstring no longer claims "3-tuple unpack backward
  compatibility" — every call site in the codebase reads parsed
  attributes (`parsed.vault`, `parsed.kind`, …), never unpacks.
  Field-order rationale rewritten as plain surface description.
- `frontend/src/lib/api.ts` 7-line "Memory client surface removed in
  v0.5.0" tombstone compressed to a one-liner that describes the
  current state instead of the deleted past.

### Out of scope / deferred

- `_TABLE_NAME_RE` continues to reject hyphen / non-ASCII at create
  time. Existing legacy tables were renamed in this session; making
  create permissive is a separate design (URI escaping, column rules,
  frontend display).
- Boot-time migration list in `db/postgres.py` — 11 entries, all
  idempotent and cheap. Trimming them needs a "schema-state snapshot"
  policy decision (when does an applied migration get folded into
  `init.sql` and dropped from the runner?). Tracked separately.
- `app/main.py:196` `/health` field still emits a raw `{"error": str(e)}`
  — intentional exception per 0.5.7.

### Verified

Unit 16/16; e2e green against 0.5.8 locally on the suites this PR
touches (mcp, security, edit, collection, pg_rbac). Frontend type
check + lint pass.

## 0.5.7 — 2026-06-03

Post-0.5.6 cleanup pass — five findings from a code-quality audit
of the last four releases (0.5.4–0.5.6).

### REST `sql_name` symmetry (issue #110 follow-up)

`GET /api/v1/tables/{vault}` now includes `sql_name` on each table
item, matching what MCP `akb_browse` started returning in 0.5.5. REST
clients were unintentionally excluded from the contract — they had to
guess the sanitisation rule the same way the original seahorse-mcp
table viewer did before #110. Additive field; no breaking change.

### Catalogue-enforcement test (drift gate)

`tests/test_errors_unit.py` gains two AST-level sync tests:

- `test_every_err_call_uses_catalogue_constant` — every `err(code=X)`
  call in `app/` + `mcp_server/` must use a constant from
  `app/util/errors.py`, not an ad-hoc string. Catches the next
  contributor who writes `code="some_new_thing"` inline.
- `test_catalogue_has_no_orphan_constants` — every constant in the
  catalogue must be imported somewhere. Catches forward-declared
  codes that get carried release-to-release without ever shipping a
  call site (`CROSS_VAULT_LINK`, `INTERNAL_ERROR` were exactly this
  in 0.5.6 — see "Dropped" below).

Together these make the 0.5.6 "one shape" promise self-enforcing
rather than depending on review vigilance.

### Dropped

- `CROSS_VAULT_LINK`, `INTERNAL_ERROR` — declared in 0.5.6's
  catalogue but never imported. YAGNI; re-add when the first call
  site lands.

### Internal cleanup (no behaviour change)

- `app/services/table_service.py`: dropped the late `import re as _re`
  alias — the file already has `import re` at the top, and `_re.compile`
  was only used in two patterns that now just say `re.compile`.
- `_enrich_undefined_error` docstring rewritten to spell out the
  canonical envelope it now returns (was accurate but vague — easy to
  mis-read by a future maintainer).

### Tests

Unit: 16/16 (5 envelope shape + 2 catalogue sync + 3 fuzzy_hint
+ 1 TOOLS↔_HANDLERS sync + 5 table identifier).

E2E: re-ran the suites touched by 0.5.4–0.5.6 (mcp, security, edit,
collection_lifecycle, pg_rbac, stdio_files, put_file_param) against
0.5.7 locally — all green.

## 0.5.6 — 2026-06-03  *(breaking error-response shape)*

Error responses across the backend now share one envelope. Until
0.5.5 there were ~6 distinct shapes — bare `{error}`, `{error,
code}`, `{error, code, pg_sqlstate}`, `{error, hint, available_*}`,
`{error, message, hint}` — and every new handler that wanted to
surface a hint or metadata reinvented a slightly different one.
Agents that wanted to auto-recover had to learn each case's field
names; new error paths kept proliferating new shapes.

### Canonical shape

Every error return from `akb_*` MCP tools and the REST surface now
matches:

```json
{
  "error":   "<human-readable message>",
  "code":    "<stable enum>",
  "hint":    "<optional self-correction guidance>",
  "details": { "<optional case-specific metadata>": "..." }
}
```

`error` + `code` are always present. `hint` and `details` are
opt-in. **Top-level meta fields that used to live alongside `error`
have moved into `details`** — `available_columns`,
`available_tables`, `available_arguments`, `pg_sqlstate`,
`doc_count` / `file_count` / etc. on a non-empty-collection error.

### Code catalogue (initial)

Stable strings, defined in `app/util/errors.py`:

| code                | When                                                                     |
|---------------------|--------------------------------------------------------------------------|
| `not_found`         | Resource doesn't exist (vault, doc, table, version, publication, user)    |
| `permission_denied` | PG ACL denied (cross-vault probe); `details.pg_sqlstate` carries SQLSTATE |
| `vault_archived`    | Write attempted against an archived vault                                 |
| `invalid_argument`  | Generic argument-shape problem (missing required, wrong format, …)        |
| `invalid_uri`       | `akb://…` URI couldn't be parsed                                          |
| `invalid_path`      | Collection / file path failed normalisation                               |
| `unknown_argument`  | Tool called with an arg key not in its schema (0.5.4)                     |
| `unknown_tool`      | `_dispatch` got a tool name not in `_HANDLERS`                            |
| `conflict`          | OCC mismatch / non-empty collection delete without `recursive`            |
| `no_op`             | Update request had nothing to update                                      |
| `edit_failed`       | `akb_edit` couldn't apply (no match / non-unique / empty old_string)      |
| `multi_statement`   | `akb_sql` got `;`-separated statements                                    |
| `method_not_allowed`| `akb_sql` got DDL or other non-DML                                        |
| `sql_error`         | Generic PG error after enrichment                                         |
| `undefined_column`  | SQL column doesn't exist; `details.available_columns` + `hint`            |
| `undefined_table`   | SQL relation doesn't exist; `details.available_tables` + `hint`           |
| `cross_vault_link`  | (reserved for future link cross-vault rejection)                          |
| `self_link`         | `akb_link` source == target                                               |
| `internal_error`    | Uncategorised exception fall-through (prefer a specific code over this)   |

### Breaking changes

If you parse error responses programmatically:

1. **`error` is now always a human message**, not sometimes a code.
   The old `{"error": "edit_failed", "message": "..."}` shape is
   gone — use `code == "edit_failed"` and read `error` for the
   message.
2. **Aux fields moved under `details`**. `response.available_columns`
   → `response.details.available_columns`. Same for
   `available_tables`, `available_arguments`, `pg_sqlstate`,
   `doc_count` / `file_count` / `sub_collection_count` / `table_count`
   on `akb_delete_collection`.
3. **`hint` stays top-level** (it's the dominant self-correction
   signal — burying it under `details` would hurt agent UX).

The frontend uses only `error` (verified — `frontend/src/lib/api.ts`
throws `new Error(body.error || body.detail)`) and the akb-mcp
stdio proxy is pass-through, so neither needs changes. Direct REST
clients and agents that inspected the aux fields by their old
top-level names need to look one level deeper.

### Tests

- `test_errors_unit.py` (new, 5 cases): envelope shape — minimal,
  with hint, with details kwargs, `code` always present.
- E2E migrated: `test_mcp_e2e.sh` (`available_columns` →
  `details.available_columns`), `test_edit_e2e.sh` and
  `test_security_edge_e2e.sh` (`error == "edit_failed"` →
  `code == "edit_failed"`, `message` → `error`).
- Full regression: unit 30 passed; main / security / pg_rbac e2e
  green against the 0.5.6 backend.

## 0.5.5 — 2026-06-03

Vault tables with hyphenated or non-ASCII display names are now
reachable via `akb_sql`, and `akb_browse` advertises the SQL identifier
each table actually responds to. Fixes paired issues #110 + #111
surfaced by the seahorse-mcp-agent-server table-viewer modal.

### What went wrong

Three call sites — `pg_table_name` (DDL), `build_table_name_map`
(rewriter map), and the browse table-item builder — each ran their
own slightly-different sanitisation of the display name. The
mismatches:

- `pg_table_name` collapsed every non-`[a-z0-9]` character to `_`, so a
  Korean name like `공공사업기획` became the PG table
  `vt_<vault>__________`.
- `build_table_name_map` only stripped hyphens, keeping the original
  Korean characters as map keys. The rewriter's tokenizer accepts only
  `[A-Za-z_][A-Za-z0-9_]*`, so those Korean keys never matched and the
  table was unreachable.
- `akb_browse` surfaced the display name as `name` with no field that
  exposed the SQL-side identifier, leaving clients to guess
  AKB's sanitisation rule. The seahorse-mcp PR #193 was shipping a
  hyphen-to-underscore client-side guess as a workaround.

### Fix

Single sanitisation function `_sanitize_pg_part` in
`table_data_repo`; both `pg_table_name` and the new
`pg_short_name` (the right half of `vt_<v>__<t>`) call it. The
rewriter map now keys off the fully-sanitised short form, and
`akb_browse` table items expose it as `sql_name`. Backward-compat
keys for the previous display-name lookup are preserved.

After the fix:

- `akb_browse(vault="demo")` returns `{name: "pipeline-snapshots",
  sql_name: "pipeline_snapshots", ...}` and `{name: "공공사업기획",
  sql_name: "______", ...}`.
- Both `SELECT * FROM pipeline_snapshots` and `SELECT * FROM ______`
  resolve through the rewriter to the real PG table.

### Tests

- Unit (`test_table_identifier_unit.py`, 5 cases): pg_short_name ↔
  pg_table_name agree on every input shape; sanitiser idempotent;
  Korean → all-underscore; rewriter resolves all-underscore; quoted
  identifiers untouched.
- E2E (`test_mcp_e2e.sh` §11b, 4 cases): create hyphen + Korean
  tables, browse exposes correct `sql_name`, round-trip the value
  through `akb_sql` and confirm both queries return rows.

## 0.5.4 — 2026-06-03

MCP `_dispatch` now rejects unknown tool arguments with a fuzzy hint
instead of silently letting them through.

Before: a typo like `akb_activity(user="someone")` (real arg name:
`author`) fell through `args.get("author")` with no signal, the filter
quietly disabled, and the unfiltered commit list came back looking
correct. An agent that trusts its own argument spelling has no way to
notice. This was the exact failure mode that motivated the gate — a
session ran a vault-permission audit, two `user=...` calls returned
identical author-unfiltered output, and the agent reported the result
as if the filter had applied.

Fix: build `_TOOL_ARG_NAMES` from the `TOOLS` schema list at import
time, and in `_dispatch` reject any argument key that isn't in the
allowed set. The response uses the same `{error, hint,
available_arguments}` shape that `table_service._enrich_undefined_error`
already returns for SQL column / table not-exist errors, so the
fuzzy-hint tone is uniform across the API surface.

The `fuzzy_hint` helper (Top-3 close matches via `difflib`, capped
fallback list) moves from `table_service` to `app.util.text` so both
callers share a single implementation.

New tests:
- Unit: `fuzzy_hint` shapes (close match / fallback / truncation) and
  AST-based `TOOLS ↔ _HANDLERS` sync assertion (no heavy imports).
- E2E: `test_security_edge_e2e.sh` §10 covers the activity
  `user`→`author` typo path plus a regression guard that a valid call
  still passes through the gate.

Follow-up not in this release: error-response shape standardization
across handlers (currently ~6 distinct shapes — `{error}`,
`{error, code}`, `{error, code, hint, available_*}`, …). Tracked
separately; this PR deliberately reuses the existing
`_enrich_undefined_error` shape rather than introducing a seventh.

## 0.5.3 — 2026-06-02

Document `status` coherence: leaned the lifecycle to 3 states and gave
`archived` a real effect, after an audit found status was 100%
descriptive (it gated nothing) and the 4th state was vestigial.

### `superseded` state + `supersedes` column removed (3-state lifecycle)

The 4-state model (`draft`/`active`/`archived`/`superseded`) was never
operationalized: no code transitioned states, the `superseded` value and
its paired `documents.supersedes` UUID FK were never read or written.
Leaned down to **`draft` → `active` → `archived`**. `superseded` is no
longer accepted (`akb_put`/`akb_update` return 422; both now validate the
status enum — previously only `put` did). Migration 032 drops the unused
`documents.supersedes` column (idempotent `DROP COLUMN IF EXISTS`).

### `archived` is now hidden from default search + browse

Previously `status` did not affect any read path. Now `archived` means
something: `akb_search` and `akb_browse` (REST + MCP) **exclude archived
documents by default**, with an opt-in `include_archived: true`. The
default `draft` on create is unchanged — the intended flow stays
draft → promote.

Concretely: a SQL `status != 'archived'` predicate on the search
document-candidate prefilter and the browse depth query; `include_archived`
threaded through `SearchService.search`, `DocumentService.browse` /
`_browse_docs`, and `DocumentRepository.list_docs_by_depth`, plus the
`akb_search`/`akb_browse` tool schemas and REST routes. The `akb-mcp`
proxy forwards the new param transparently (no proxy release).

Verified: `superseded` rejected on put + update (422); migration 032
drops the column on boot; browse + (embedding-backed) search hide
archived by default and include it with `include_archived=true`;
`test_mcp_e2e` 76/76, browse/search e2e green, status unit tests.

## 0.5.2 — 2026-06-02

Continuation of the v0.5.0/v0.5.1 cleanup. One more leftover surfaced
during a post-deploy MCP probe: the `akb_help` tool schema description
still listed `memory` and `sessions` as valid categories, and named
`link-documents` (the pre-rename workflow) instead of `link-resources`.

Tool descriptions are part of the `tools/list` MCP response that every
agent client receives at handshake — they show up verbatim in the agent's
own prompt. Listing dead categories there was the exact "leftover trace
that confuses people" failure mode the v0.5.1 audit set out to avoid; it
slipped because the audit grepped for service names + endpoints, not for
literal references inside tool schema descriptions.

Fix: rewrite the `topic` description against the actual `_HELP` keys —
`(quickstart, documents, search, tables, files, access, history,
publishing, relations)` for categories, `(link-resources, research,
onboarding, data-tracking, vault-skill)` for workflows. Also dropped
the stray `todos` entry (which was never an `_HELP` key in the first
place; the original description had it for navigation only).

No behavioural change. Verified by MCP probe against the deployed
instance after 0.5.1: `akb_help(topic="memory")` already returned
`No help found`; only the discovery hint was misleading.

## 0.5.1 — 2026-06-02

Cleanup of v0.5.0 leftovers. The agent-memory feature removal in 0.5.0
landed cleanly on the backend but left a handful of stale references
that would confuse anyone reading the code or the UI:

- **Frontend Settings page**: the "Memory" tab still rendered and
  called `/api/v1/memory` (404 after 0.5.0). The tab + its component
  (`memory-tab.tsx`) + its test (`memory-tab-chips.test.tsx`) + the
  `Memory` / `recallMemories` / `forgetMemory` / `forgetCategory`
  exports in `lib/api.ts` are all removed; settings.tsx no longer
  declares a `memory` `TabId`.
- **README**: the MCP tool table still listed
  `akb_remember/akb_recall/akb_forget` and `akb_session_start/end`. The
  row is replaced with a short paragraph pointing at the
  `/api/v1/agent-sessions` REST surface and the auto-provisioned memory
  vault, so a reader sees the correct mental model on first scan.
- **tools.py docstring** referenced `memory_id` as one of the
  non-URI-addressable opaque handles; removed.
- **Deprecation-note version labels** in the e2e shells + concurrency
  unit test said "retired in v0.4.0" — these were actually retired in
  v0.5.0 (the 0.4.x stream was license + concurrency fixes). Fixed
  inline so anyone tracing the retirement back to a release lands on
  the right one.
- **`settings-tokens-setup.test.tsx`** still mocked `recallMemories` —
  removed so the test stays accurate against the trimmed client API
  surface.

No backend runtime contract change. No new MCP tools, no schema
change. Just trace cleanup.

## 0.5.0 — 2026-06-02

### Agent memory — vault-shaped, REST-only (breaking)

The `akb_remember` / `akb_recall` / `akb_forget` MCP tools and the
`memories` / `sessions` PG tables are **removed**. Agent dedicated
memory is now expressed as a per-user vault (`agent-memory-{username}`)
with per-session collections at `sessions/{date}/{agent_id}/{session_id}`
and a `recap.md` document at end of session.

The new surface lives at `/api/v1/agent-sessions` (Bearer auth, REST,
**not MCP**) and is intended to be driven by lifecycle plugins
(`akb-claude-code`, `akb-cursor`, `akb-codex`) hooked into agent
SessionStart / PreCompact / SessionEnd / UserPromptSubmit events. The
agent itself never calls these endpoints — they sit outside the
tool-use loop, so the agent's tool list is unpolluted by lifecycle
plumbing.

#### Removed

- MCP tools: `akb_remember`, `akb_recall`, `akb_forget`
- Services: `app.services.memory_service`, `app.services.session_service`
- REST routes: `/api/v1/memory*`, `/api/v1/sessions/start`, `/api/v1/sessions/{id}/end`
- PG tables: `memories`, `sessions` (dropped by migration 031 —
  unconditional, no FK references existed)
- Concurrency test `INV-3` (covered the removed SessionService)
- Help topic: `memory`, `sessions`
- Activity / recent / diff endpoints relocated from
  `app.api.routes.sessions` to `app.api.routes.activity` (the original
  file was 60% session-management, 40% activity-history; once the
  session bits left, the rename made the remaining contents honest)

#### Added

- `app.services.agent_memory_service.AgentMemoryService` — vault
  auto-provisioning + session lifecycle + recall.
- REST endpoints under `/api/v1/agent-sessions`:
  - `POST /agent-sessions/{session_id}` — start (idempotent on
    `session_id`; SessionStart with `source=resume|clear|compact`
    returns the existing collection rather than 409).
  - `POST /agent-sessions/{session_id}/end` — write `recap.md` with
    `type: session` frontmatter; accepts the Cursor-style `reason`
    enum (`completed | aborted | error | window_close | user_close |
    stop`) and an independent `outcome` enum
    (`success | partial | abandoned`).
  - `POST /agent-sessions/{session_id}/snapshot` — durable partial
    summary for PreCompact-class events. Each call writes a sequential
    `snapshot-NNN.md` rather than mutating collection metadata, so
    every snapshot is git-versioned.
  - `GET /agent-sessions/{session_id}/context` — preferences /
    learnings / parent-recap injection for UserPromptSubmit-class
    hooks (synchronous by contract).
  - `GET /agent-sessions/{session_id}` — status (`ended` flag,
    `recap` pointer).
  - `GET /agent-sessions` — list the caller's sessions, optional
    `agent_id` filter.
- Migration `031_drop_memories_sessions.py`.
- E2E suite `backend/tests/test_agent_sessions_e2e.sh` (28 cases,
  covers auto-provision, idempotency on resume/compact, snapshot
  sequence, parent-recap injection across agents, ungraceful end with
  `reason=window_close`, list/filter, validation rejects, auth).

#### Convergent design — sourcing

The REST contract was synthesised from a cross-harness audit of agent
lifecycle hooks (run 2026-06-02 — see
`product/akb/design-proposals/akb-agent-memory-rest-final-design-2026-06-02-v050.md`).
Headline findings the API honours:

- Claude Code (1.0.85+), Cursor (1.7), and OpenAI Codex CLI (April
  2026) all expose a SessionStart hook that fires on
  resume / clear / compact / startup with the source as a discriminator
  field — the API is idempotent on `session_id`-in-path so the plugin
  does not need to dedupe client-side.
- All three agents pass at least `session_id`, `transcript_path`,
  `cwd`, and `hook_event_name` on stdin; the start body accepts the
  superset of these so a single plugin contract drives all three.
- Cursor's `sessionEnd` carries an explicit `reason` enum — the API
  adopts it verbatim plus `stop` to cover Claude Code's `Stop` hook.
- Claude Code natively supports `{type: "http", url, headers:
  {"Authorization": "Bearer $AKB_PAT"}, allowedEnvVars: ["AKB_PAT"]}`,
  so the plugin can call AKB directly from the hook script with no
  wrapper — Bearer auth on the REST endpoints is sufficient.

#### Migration

`memories` and `sessions` are dropped without backfill. The data was
never user-visible outside of the `akb_remember` tool; operators who
need to retain it must snapshot before upgrade. The recommended
replacement workflow is to write any persistent agent state into the
auto-provisioned memory vault through the lifecycle plugin (or
directly via `akb_put` to the vault — it is an ordinary vault).

The plugin (`packages/akb-claude-code/`) is a separate work-item; this
release is the backend it will call.

## 0.4.3 — 2026-06-02

`akb_put` can now set the document `status` on create.

### Optional `status` on document create

`akb_put` previously hard-coded `status: draft` into both the git
frontmatter and the `documents` row — the only way to land an `active`
document was to follow up with `akb_update`. `DocumentPutRequest` now
takes an optional `status` (default `"draft"`, so existing behaviour is
unchanged) that is stamped through to the frontmatter + DB row. Pass
`status: "active"` (or `archived`/`superseded`) to publish on create.

The value is validated against the known set
(`draft`/`active`/`archived`/`superseded`) in `DocumentService.put`
before any git/DB work — an unknown status returns a clean 422 instead
of silently landing a typo. Status remains **descriptive metadata** — it
does not gate search, browse, or access; this just removes the
put-then-update dance.

The MCP `akb_put` tool schema exposes the new `status` enum; the
`akb-mcp` proxy forwards it transparently (no proxy release needed). No
schema change.

Verified: REST + MCP round-trip (`status:"active"` → frontmatter + DB
`active`; default → `draft`; bad value → 422); `test_mcp_e2e` 76/76,
`test_edit_e2e` 37/37; unit tests
`test_put_request_status_defaults_to_draft_and_accepts_active`,
`test_put_rejects_unknown_status`.

## 0.4.2 — 2026-06-02

Collection-retirement vs document-PUT race: an unhandled foreign-key
violation surfaced as HTTP 500 instead of a clean conflict.

### `akb_put` into a collection being deleted returned 500

When a recursive collection delete commits in the exact window between a
concurrent PUT's `get_or_create` (which observed the collection) and that
PUT's `INSERT INTO documents` (which still references the now-gone
`collection_id`), the insert trips `documents_collection_id_fkey`. The
delete side is fine — `collection_id` is `ON DELETE SET NULL`, so existing
docs are re-homed — but a *new* insert against the vanished id is an FK
violation that `document_repo.create` did not catch, so it bubbled up as
an unhandled `asyncpg.ForeignKeyViolationError` → HTTP 500.

`create` already maps a `UniqueViolationError` (duplicate path) to a 409;
it now maps a `ForeignKeyViolationError` the same way, with a clear
"Target collection or vault was concurrently deleted" message. The racing
writer gets a clean, retryable 409 instead of a 500. No schema change.

Found via an external "E06 collection retirement race" report (40 PUTs
racing a recursive collection delete). Note: the report's primary symptom
— all 40 PUTs as transport-level "status 0" — was the 0.4.1 pool-deadlock
(the deployment under test had regressed to 0.4.0); with the pool fix in
place the deadlock is gone and only this residual FK 500 remained.

Verified: E06 repro (40 PUTs, seeded to widen the race window) returned
`{200, 500}` before and `{200, 409}` after (5xx → 0); `test_mcp_e2e`
76/76, `test_collection_lifecycle_e2e` 36/36; deterministic unit test
(`test_create_with_deleted_collection_raises_conflict`). Repro harness:
`backend/tests/concurrency/repro_e05_e06_delete_race.py`.

## 0.4.1 — 2026-06-02

Connection-pool deadlock on the document write paths under a concurrent
write burst. Reproduced from an external "E01 multi-vault knowledge
burst" report (100 PUT + 300 GET returned transport-level "status 0").

### Document writes deadlocked the pool at ≥ `pool_size` concurrent writers

`put`/`update`/`edit`/`delete` each acquired **two** pool connections at
once: `_path_lock()` held one connection (`lock_conn`, inside a
transaction holding the `pg_advisory_xact_lock`) for the whole critical
section, and the body then did a **second** `pool.acquire()` for the
chunks/relations/events transaction. With `max_size=20`, once 20
concurrent writers each held a lock connection and then all waited for a
second connection, none could free one — a textbook hold-and-wait
deadlock. It only broke when PG's `idle_in_transaction_session_timeout`
(60s) killed the idle lock transactions, so clients saw 60s hangs →
`ReadTimeout`. `/livez` stayed green the whole time (it touches neither
the pool nor the event loop), which is why the failure hid from health
checks. Reads were collateral: every DB-touching request starved while
the 20 connections sat frozen.

The trigger is just **20 simultaneous writes to one pod** — a realistic
bar (a bulk import, or ~20 agents), not only a synthetic stress test.

Fix: each writer now holds **exactly one** pool connection. `_path_lock`
yields its connection and every DB statement of the critical section
(conflict pre-check, `get_or_create`, `create`/`update`, chunks,
relations, events, publication cascade, doc-row delete, collection
count) runs on that one connection's transaction. `document_repo`'s
`create`/`update`/`update_hash` gained a `conn=` parameter
(backward-compatible) so they reuse the caller's connection instead of
acquiring their own. The pool is now a clean backpressure queue: the
21st concurrent writer waits for a free connection instead of
deadlocking. As a bonus, each write is now fully atomic in one
transaction (doc row + chunks + edges + events commit or roll back
together). No schema change, no migration.

Verified: a 100-PUT + 300-GET multi-vault burst that returned 0/100 PUT
and 2/300 GET before now returns 100/100 and 300/300; `test_mcp_e2e`
76/76, `test_edit_e2e` 37/37, `test_concurrency_repro_e2e` 22/22. Repro
harness lives at `backend/tests/concurrency/repro_e01_multivault.py`.

## 0.4.0 — 2026-06-02

License change: **PolyForm Noncommercial 1.0 → Business Source License
1.1**. No runtime contract change; this release exists to mark the
license transition cleanly.

The BSL 1.1 ships with a 100 Named Seats Additional Use Grant — small
commercial deployments that were previously forbidden are now
explicitly permitted, while large-scale or third-party-hosting use
still requires a commercial license. Each release converts
automatically to Apache License 2.0 four years after its first public
distribution.

See [LICENSE](../LICENSE) for the load-bearing text and
[LICENSE-CHANGE.md](../LICENSE-CHANGE.md) for the rationale and FAQ.

Releases ≤ 0.3.6 remain under PolyForm NC 1.0 as originally distributed.

## 0.3.6 — 2026-05-28

The "P2" cut of the functional/logic review — four data-integrity /
contract bugs plus one latent publication-cascade bug. Each was
designed from a current-code blueprint (adversarially checked) and
verified with a unit test. No schema change, no migration.

### Archived vaults are now genuinely read-only (both directions fixed)

The archive contract was broken in two opposing ways:
- **Writes weren't blocked.** `check_vault_access` only enforced the
  archived guard for `required_role == "writer"`, and the akb_sql write
  surface gated at `reader` then relied on PG ACL — which has no archive
  concept. A writer/admin/owner could still `INSERT/UPDATE/DELETE` (and
  `drop_table`/`alter_table`) on an archived vault.
- **Reads broke after reconcile.** `_reconcile_vault_roles` fetched only
  non-archived vaults and then *dropped* every group role not in that
  set, so on the next reconcile (startup + periodic) an archived vault's
  reader role + table GRANTs were dropped and `akb_sql` SELECT returned
  42501 for everyone incl. the owner. `_diff_vaults` used a different
  vault set, so diff/reconcile never converged (`is_clean()` never True).

One coherent model now: archived = READ-ONLY. The write block lives in
the app layer — `execute_sql` rejects any non-SELECT against an archived
referenced vault, and `check_vault_access`'s guard fires for `writer`
AND `admin` (so create/alter/drop table are refused), positioned before
the admin/owner short-circuits so even a system admin is blocked. PG
write grants are intentionally preserved (so unarchive is instant), and
the reconciler now keeps archived vaults' roles by fetching ALL vaults —
matching `_diff_vaults`. `delete_vault` passes a new `allow_archived=True`
so you can still delete an archived vault.

### `alter_table` reserved-column guard

`create_table` rejected `id`/`created_at`/`updated_at`/`created_by`, but
`alter_table` didn't — so `drop_columns=["id"]` dropped the table's
primary key, and `add_columns=[{name:"created_at",type:"text"}]` made
the registry lie about a bookkeeping column's type. A shared
`_validate_column_name` now guards add/drop/rename in both paths
(reserved names + `^[a-z][a-z0-9_]*$` shape so the registry name can't
diverge from its `safe_ident` PG identity), with the MCP handler
surfacing the `ValueError` as a friendly error.

### Collection delete handles tables

`CollectionService.delete` enumerated only documents and files, never
`vault_tables` — so a collection containing only a table passed the
empty-mode check and was silently destroyed, and even recursive delete
left the table (re-homed to vault root via `collection_id`
ON DELETE SET NULL). It now lists tables under the prefix (`FOR UPDATE`),
counts them in the empty-mode 409, and in recursive mode tears each one
down (dynamic PG table + chunk outbox + registry row + edges) inside the
same transaction, returning a `deleted_tables` count.

### Embedding response paired by `index`, not array order

`_embed_call` zipped the embeddings response to its inputs by position.
The OpenAI embeddings contract pairs each output to its input via the
item's `index` field; an OpenAI-compatible gateway that reassembles a
batched response out of order would silently attach vectors to the wrong
chunks. The response is now reordered by `index` with a completeness
assertion (`{0..n-1}`); a malformed/gapped index set is treated as a
transient error so the worker retries.

### Fix: `delete_publications_for_document` UUID branch built a legacy URI

When called with a doc UUID (vs a canonical URI), it materialized
`akb://V/doc/{path}` (pre-0.3.0 legacy shape) which never matched the
canonical `akb://V/coll/{coll}/doc/{name}` stored in
`publications.resource_uri` — so the cascade silently left orphan
publications. Now built via `doc_uri`. (Dormant — the only live caller
passes a canonical URI and the real doc-delete path already uses
`doc_uri` — but a latent landmine, now closed.)

### Tests

- `test_invariants_unit.py`: archived read-only (write blocked / read
  works), alter reserved-column guard (PK survives), collection delete
  table teardown + empty-mode reject, embed index reorder, and the
  publication-cascade canonical match.
- Modernized two stale `test_collection_*` cases that still inserted into
  the `vault_files.collection` column dropped in migration 020.

Regression: `test_mcp_e2e` 76/76, `test_pg_rbac_e2e` 50/50,
`repro_pub_security` 4/4 SAFE, unit suite green. Archived-vault model
verified end-to-end (read 200, write/DDL refused).

## 0.3.5 — 2026-05-28

Permanently fixes the recurring migration-026 boot crash and stops new
legacy-shape edges from being created. The 0.3.3 guard only skipped 026
when no legacy doc URIs remained; it did not make the rewrite itself
safe. Legacy edges kept being (re)created by an external caller, so
every cold restart re-tripped the
`edges_source_uri_target_uri_relation_type_key` UNIQUE violation and the
backend crash-looped (twice now in production, each needing a manual DB
cleanup).

### Fix — migration 026 is now conflict-safe (F2)

Before each rewrite of an `edges` URI column, 026 now DELETEs legacy
rows whose canonical-rewritten form would collide with an existing row,
preferring to keep the canonical row (or the smaller id among legacy
twins). Applied to the doc-shape regex rewrite and the table/file
temp-table rewrites, for both `source_uri` and `target_uri`. The
migration is now idempotent AND conflict-safe regardless of the data
state — verified by seeding a legacy↔canonical twin and running 026
twice with no error. (This is the exact cleanup that previously had to
be done by hand on every crash.)

### Fix — `akb_link` stores canonical URIs (F1, root cause)

`kg_service.link_resources` inserted the caller-supplied `source_uri` /
`target_uri` verbatim. An external tool calling `akb_link` with a
legacy-shaped but parseable URI (`akb://V/doc/{coll}/{name}`) therefore
persisted a legacy edge, which 026 then collided on. Both endpoints are
now canonicalized from their parsed parts (new
`kg_service.canonicalize_resource_uri` helper) before insert, so the
explicit-link path can no longer introduce legacy edges. (Edge
extraction via `_store_edge` already canonicalized; this closes the
remaining writer.)

### Operator notes

- No schema change.
- After this release, a cold restart no longer crashes even if legacy
  edges exist — 026 self-heals. Existing prod legacy edges are
  rewritten/deduped on the next boot.
- The external tool that was emitting legacy `akb://V/doc/{coll}/{name}`
  link URIs (observed in the `pdf-parser-test` vault) should still be
  updated to send canonical URIs; AKB now tolerates either.

## 0.3.4 — 2026-05-28

Security + data-integrity patch. Findings came out of a full
functional/logic review of the backend (20-subsystem multi-agent pass,
55 confirmed findings); this release lands the highest-priority cut.
Each fix was reproduced in the audit stack BEFORE the change and
re-verified SAFE after. No schema change, no migration.

### Security — publication public surface (unauthenticated)

Publications are served at `/api/v1/public/{slug}` with no auth, so
these were directly exploitable by anyone with the URL.

- **Public `table_query` ran as the privileged pool role.**
  `resolve_table_query_publication` executed the canned SQL on the
  default service-role connection with only `SET TRANSACTION READ ONLY`
  — no `SET LOCAL ROLE`, no table ACL. A publication such as
  `SELECT password_hash FROM users` / `SELECT token_hash FROM tokens` /
  `SELECT * FROM vt_othervault__secret` returned rows to unauthenticated
  visitors (verified: `SELECT count(*) FROM users` → 59 rows;
  `SELECT current_user` → `akb`). Now the query runs under the
  publication CREATOR's PG role (`SET LOCAL ROLE akb_user_<created_by>`),
  so PG returns 42501 for anything the creator could not read via
  `akb_sql`. Publications without a recorded creator fail closed (403).
  Adds `s.created_by` to the resolve SELECT.
- **`akb_publish` / `create_publication_route` now authorize every vault
  in `query_vault_names`,** not just the route vault — a writer on one
  vault could otherwise publish a query reading another vault's tables.
- **`delete_publication` IDOR fixed.** The REST delete route bound the
  delete only to the publication id, ignoring the route vault, so a
  writer on any vault could delete any publication by id. Now scoped to
  `WHERE id=$1 AND vault_id=$2`; returns 404 otherwise.
- **`create_snapshot` cross-vault fixed.** Loaded the publication by id
  with no vault filter (REST and MCP), letting a writer on vault A
  force-execute and snapshot vault B's publication. Now binds to the
  authorized vault and rejects with 404 before running the query / S3
  write.

### Data integrity / correctness

- **`delete_vault` orphaned file-chunk vectors when S3 is configured.**
  The 0.3.2 file-chunk outbox enqueue ran AFTER the early
  `DELETE FROM vault_files` (which only fires when S3 is configured), so
  it read zero rows and every file chunk CASCADE-dropped from PG without
  a `vector_delete_outbox` entry — permanent orphan points. The 0.3.2
  unit test missed it because the audit stack has no S3. File ids are now
  captured (`SELECT id, s3_key`) and enqueued BEFORE the delete.
- **`FileService.delete` swallowed chunk-delete failures.** The
  `delete_file_chunks` call was wrapped in a `try/except` that logged and
  continued, defeating the 03-F1 contract (the enqueue is meant to RAISE
  so a failure rolls back the file delete). Removed; failures now roll
  back the whole delete like the document/table/vault paths.
- **Collection-scoped search 500'd.** The search ACL prefilter's
  file-candidate branch filtered on `vault_files.collection`, a column
  dropped in migration 020 (replaced by `collection_id`). Any
  `search?...&collection=X` with the default `doc_type` raised
  UndefinedColumn and 500'd the whole request. Now joins `collections`
  on `collection_id` and prefix-matches `c.path`, same as the documents
  branch.

### Tests

- `backend/tests/concurrency/repro_pub_security.sh` — before/after
  VULNERABLE↔SAFE probes for the publication authz holes + the search
  crash.
- `test_invariants_unit.py`: `test_inv7b` (delete_vault file outbox with
  S3 configured) and `test_p1_2` (FileService.delete rollback).

Regression: `test_mcp_e2e` 76/76, `test_invariants.sh` 9/9, unit 6/6.
Legit flows reverified (own-table publication view, authorized
multi-vault publish, owner delete/snapshot).

## 0.3.3 — 2026-05-28

Hotfix on top of 0.3.2. The 0.3.2 image failed to start on existing
prod-shaped state: migration 026 (`uri_collection_prefix`, original
0.3.0 work) re-ran on every backend boot and tripped a
`UniqueViolationError` on the second pass.

### Cause

`026_uri_collection_prefix._run` rewrites legacy `akb://V/doc/{coll}/{name}`
URIs to the canonical `akb://V/coll/{coll}/doc/{name}` shape across
`edges`, `publications`, `events`. After the first deploy on a vault
the rewrite is complete, but the migration is still in the registry
that runs on every startup. New edges written between 0.3.0 and 0.3.2
ended up with the canonical shape directly; the second pass of the
rewrite would map those onto each other, colliding on
`UNIQUE (source_uri, target_uri, relation_type)`.

This is an idempotency bug in the original 0.3.0 migration, not in
the 0.3.2 fix. It surfaced now because nothing had been forcing a
backend rolling restart on these databases since 0.3.0 went out.

### Fix

`026._run` now starts with a cheap probe: a single SQL that checks
whether any URI column still matches the legacy shape. If none does,
the migration logs "no legacy URI shapes remain; skipping" and
returns. The probe is `LIMIT 1` so it's O(1) once the indexes
warm up.

### Notes for operators

- 0.3.2 was tagged + released but the image never made it past
  startup on any cluster that had previously processed migration
  026. Use 0.3.3.
- No schema change. The probe is read-only.
- 0.3.2 changelog body is preserved below for reference.

## 0.3.2 — 2026-05-28

Follow-up patch to 0.3.1. One new finding surfaced while writing the
invariant test suite added in 0.3.1, plus the suite itself.

### Fix: `delete_vault` enqueues file chunks for vector-store deletion

`access_service.delete_vault` already iterated `vault_tables` and ran
`_drop_source_chunks_with_outbox(conn, "table", id)` so the
vector-store points for table-metadata chunks got enqueued into
`vector_delete_outbox` before the cascade fired. The matching loop
over `vault_files` was missing.

Effect of the gap: `chunks.vault_id ON DELETE CASCADE` still removed
the file chunks from PG when the vaults row went, but
`vector_delete_outbox` doesn't ride the cascade, so the vector store
kept the points. Production vector stores accumulated one orphan
point per chunk per file in every deleted vault.

Why the audit-v2 pass missed it: the `delete_vault_chunks` docstring
claimed "tables/files CASCADE through their own vault_tables /
vault_files FKs at vault-drop time — their chunk cleanup is handled
in the service delete hooks." Half-true: vault_tables/vault_files
rows do cascade, but `chunks.source_id` has no FK (polymorphic
source), and the "service delete hooks" only existed for tables.

Fix mirrors the table loop inside the same outer transaction so the
outbox INSERT commits atomically with the chunks DELETE.

Surfaced by a multi-assertion invariant test (`post == 0 AND outbox
== 3`) — the single-condition "no orphan chunks" assertion would have
passed because the cascade does its job in PG; only the second
assertion noticed the missing outbox row.

### Tests: concurrency invariant suite

New `backend/tests/concurrency/` with two complementary tracks:

- `test_invariants.sh` — bombardment + PG ground-truth shell suite.
  Hits the audit Docker stack with N concurrent curl clients per
  invariant, then asserts the post-condition by querying PG via
  `docker exec`. Covers INV-1, INV-2, INV-4, INV-8, INV-9, INV-10
  (cross- + same-vault), INV-11, INV-12. 9/9 pass.
- `test_invariants_unit.py` — pytest for the four invariants that
  don't fit a curl-bombardment shape (INV-3 `end_session` dedup,
  INV-5 BM25 `try_advisory_lock`, INV-6 metadata stale guard, INV-7
  `delete_vault` orphan chunks). 4/4 pass.

Together the suite verifies every Tier 0 / Tier 1 fix from 0.3.1
plus the new 0.3.2 fix above (13/13 invariants).

### Notes for operators

- No schema change. No migration.
- Existing vaults with file-heavy history have already lost
  outbox rows for any file chunks deleted before this patch — those
  orphan vector-store points are not recoverable from PG state and
  would need a separate sweep job to reconcile (out of scope here).
- Running the new invariant suite locally:
  ```
  AKB_URL=http://localhost:8001 bash backend/tests/concurrency/test_invariants.sh
  AKB_TEST_DSN=postgresql://akb:akb@localhost:5433/akb \
    uv run pytest backend/tests/concurrency/test_invariants_unit.py -v
  ```

## 0.3.1 — 2026-05-27

Second-round concurrency/atomicity audit. 54 findings across six
domains were narrowed by a meta-review pass to a Tier 0 (HIGH,
deterministic data loss / surface bypass) and a Tier 1 (TX + advisory
lock + canonical URI hardening) cut, then reproduced in a Docker
desktop isolation environment before fix. The shape of every fix is
"narrow the surface or hold the right lock", not new feature.

### Tier 0 — HIGH severity

- **`edges.kind` discriminator (migration 028).** Before, every
  `akb_update` ran `DELETE FROM edges WHERE source_uri = X` and
  re-extracted from frontmatter + body. That wiped explicit edges
  created via `akb_link` — deterministic, not a race. New
  `kind ∈ {implicit, explicit}` column; rewrite DELETE is scoped to
  `kind = 'implicit'`, `akb_link` writes `kind = 'explicit'`. Existing
  rows default to implicit so current behaviour is preserved.
- **Token-aware SQL rewriter for `table_query`.** Pre-fix
  `table_data_repo.rewrite_table_names` used a regex substring
  substitution, so a publication that mapped table name `name` would
  silently corrupt a `WHERE label = 'foo_name_bar'` literal. New scan
  tokenises (single/double quoted strings, line + block comments,
  dollar-quoted strings, identifiers) and rewrites only `ident` tokens.
- **`file_uri` collection prefix.** `document_service.delete` was
  emitting the root-form URI for collection-resident files, so the
  same file appeared under two URIs across event payloads / responses.
- **MCP `version` parameter hex validation.** REST already rejected
  non-hex refs (`HEAD~1`, `refs/...`, `@~5`) for `?version=`, but the
  MCP `akb_get` / `akb_diff` handlers passed the string straight to
  GitPython — letting a caller bypass the lifecycle to read historical
  content the REST trust boundary refused. Now both handlers validate
  with the same `^[0-9a-f]{7,64}$` regex (shared via
  `app/util/git_refs.py`).

### Tier 1 — TX / advisory-lock / canonical URI hardening

- **`link_resources` runs in one TX with `acquire_path_lock`.**
  Doc-endpoint locks are taken in `(vault_id, identifier)`-sorted
  order so two `akb_link` calls touching overlapping endpoints never
  deadlock. Both vault and endpoint reads are inside the snapshot.
- **`unlink_resources(*, vault_id=...)`.** Without a vault scope the
  delete matched purely on `(source_uri, target_uri)`, so a caller
  could erase another vault's edge by spelling its URIs. MCP now
  threads `access["vault_id"]` through.
- **BFS edge dedup in `_bfs_collect`.** An `emitted: set[tuple[str,
  str, str]]` removes duplicate edges that surfaced when both
  endpoints sat in the same wave.
- **`create_table` / `drop_table` / `alter_table` fully transactional.**
  Includes new `RoleSync.grant_table_in_conn(...)` that propagates
  errors (vs `on_table_create` which swallows) — the grant commits
  atomically with the CREATE TABLE, eliminating the "exists but 42501"
  window callers used to see. `alter_table` holds `FOR UPDATE` on the
  registry row so two concurrent alters can't last-write-wins the
  column list. Table-name validator is now `[a-z][a-z0-9_]*` —
  rejecting hyphens because `pg_table_name`'s `[^a-z0-9] → _`
  sanitiser is otherwise non-injective.
- **`delete_vault` per-table chunk cleanup.** `chunks.source_id` has
  no FK to `vault_tables` (polymorphic source), so the prior
  vault-scoped chunk DELETE missed `source_type = 'table'` rows and
  orphaned their vector-store entries. Now iterates `vault_tables`,
  routes each through `_drop_source_chunks_with_outbox`, then drops
  the dynamic PG table — all inside the outer TX.
- **`external_git` reconciler hardening.**
  - `last_commit_for_path` takes the synced tip sha so attribution
    can't drift past the tree we're writing.
  - `mark_llm_metadata_filled(..., expected_blob=...)` gates the
    UPDATE on `external_blob = expected_blob` — a reconciler that
    superseded the row mid-LLM-call no longer overwrites with stale
    output. The worker clears `llm_next_attempt_at` on the dropped
    result so the next tick reprocesses immediately.
  - Emits `document.put` / `document.update` / `document.delete`
    events so mirror writes have the same subscriber surface as
    user PUTs. `_delete_external_path` also calls
    `delete_document_relations` to drop implicit edges with the doc.
- **Session / publication concurrency.**
  - `SessionService.end_session` wraps the row read in a transaction
    with `SELECT ... FOR UPDATE`; concurrent ends no longer both run
    the `auto_summarize_session` side effect.
  - `publication_service.create_snapshot` holds a session-scoped
    advisory lock keyed on `publication_id` so two concurrent
    snapshot calls don't both execute the SQL, both upload to S3,
    and race on the final UPDATE.
  - `resolve_publication` folds `expires_at` into the atomic view
    UPDATE — pre-fix a publication that expired between the SELECT
    and the UPDATE would still record a view.
- **BM25 `recompute_stats` holds `pg_try_advisory_lock` for the full
  scan + write.** A second replica that arrives mid-rebuild bails
  out cheaply with a log line instead of redoing the whole tokenise
  pass on top of the leader.
- **Abandoned-chunk reaper outbox dedup.** Reaper INSERT into
  `vector_delete_outbox` now guards with `NOT EXISTS (... WHERE
  chunk_id = a.id AND processed_at IS NULL)` so concurrent
  `_drop_source_chunks_with_outbox` calls don't enqueue the same
  chunk twice. New `idx_vector_outbox_chunk_pending` partial index
  (migration 029) so the guard is O(1) instead of a filtered seq scan.
- **Edge URI canonicalisation in `_store_edge`.** Parsed URIs are
  rebuilt through `doc_uri` / `table_uri` / `file_uri` before INSERT
  so two surface variants (trailing slash, coll-prefix shape) of the
  same target collapse onto one row — the `ON CONFLICT DO NOTHING`
  dedup is no longer defeated by string variance.
- **`external_git` paths run through `normalize_collection_path`.**
  Mirror docs whose upstream directory contained reserved segments
  (`coll`/`doc`/`table`/`file`) now fail at reindex time instead of
  smuggling unparseable URIs into the edges table.

### Schema

- Migration **028**: `edges.kind TEXT NOT NULL DEFAULT 'implicit'
  CHECK(kind IN ('implicit', 'explicit'))` + partial index
  `idx_edges_source_kind ON edges(source_uri, kind)`.
- Migration **029**: partial index
  `idx_vector_outbox_chunk_pending ON vector_delete_outbox(chunk_id)
  WHERE processed_at IS NULL`.

Both migrations are idempotent ADD COLUMN / CREATE INDEX IF NOT
EXISTS; PG 11+ runs the column add as a metadata-only ALTER (no
table rewrite).

### Notes for operators

- Existing edges are preserved as `kind = 'implicit'`. Explicit
  edges that were destroyed by prior `akb_update` rewrites are
  recovered by re-running `akb_link` — the new row lands as
  `kind = 'explicit'` and now survives.
- MCP clients that previously passed `version="HEAD~1"` (or any
  symbolic ref) to `akb_get` / `akb_diff` will start receiving a
  clear error. Switch to a hex commit hash from `akb_history`.
- `external_git` mirror vaults now emit `document.*` events on each
  reindex pass. Existing subscribers will see throughput proportional
  to the upstream change rate.

## 0.3.0 — 2026-05-27

### Follow-up patch: edge-extraction safety (PR #85, 2026-05-26)

Found during the on-prem verification of 0.3.0. Two paired contract
gaps the URI scheme refactor exposed, both surfacing as a
`edges_target_type_check` violation when something `parse_uri`
considers valid makes it past `kg_service` into the INSERT:

- **Markdown body containing URI template placeholders** — a doc
  whose body documents the URI scheme as
  `akb://{vault}/coll/{coll_path}/{type}/{id}` (curly braces literal
  in the text) tripped `extract_markdown_links` into treating the
  template string as a real edge target. `parse_uri` happily
  matched `{vault}` as the vault segment, `{coll_path}` as the coll
  path, etc.
- **Doc with `depends_on: [coll-URI]` or `akb_link(target=coll-URI)`** —
  collections are URI-citizens as of 0.3.0, but they are *navigation
  aids*, not link endpoints. The `edges.target_type` CHECK constraint
  enforces this at the DB layer (`doc | table | file` only); without
  a surface filter the constraint was reachable as a Postgres failure.

Fix is three lines in three files:

- `uri_service.parse_uri`: reject any URI containing `{` or `}`.
  Real AKB URIs never carry braces; this stops the placeholder
  hijack before regex runs.
- `kg_service._store_edge`: explicit
  `parsed.kind in ('doc','table','file')` gate. coll/vault URIs
  silent-skip with a DEBUG log.
- `kg_service.link_resources`: same gate, but with a friendly
  user-facing error so explicit `akb_link` callers get a clear
  4xx-style message instead of a Postgres failure.

E2E `§28` of `test_unified_browse_edges_e2e.sh` locks down all
three failure modes (placeholder body, coll-URI depends_on,
akb_link rejection). 401/401 across the full sweep.

### 0.3.0 main release

**BREAKING** — a coordinated contract pass that takes the AKB API
from "mostly consistent with quiet gaps" to "every surface tells the
same story":

1. `akb_browse.depth` redesigned as true tree-depth (from misnomer).
2. **URI scheme made location-aware** — every URI carries an
   optional `/coll/<path>` segment that names its containing
   collection, so siblings/parents are discoverable by walking up
   the URI without an extra lookup.
3. `akb_graph.depth` renamed to `hops` so it does not collide with
   the new browse `depth`.
4. List-style tools (`akb_recall`, `akb_activity`) report what they
   returned, the corpus total, and whether more exists, instead of
   leaving callers to guess.
5. `akb_search` / `akb_grep` hits now carry an explicit
   `collection` field so clients group/filter without URI parsing.
6. `akb_drill_down` surfaces sub-section hints on a successful
   match so a drilling agent has its next step in hand.

### Location-aware URI scheme — every resource self-describes its place

Pre-0.3.0 the URI scheme placed the collection inside the doc path
(`akb://V/doc/specs/api.md`) but emitted table and file URIs with
no collection prefix at all (`akb://V/table/expenses`,
`akb://V/file/<uuid>`). 0.3.0 unifies the scheme:

    akb://{vault}                                       vault root
    akb://{vault}/coll/{coll_path}                      collection
    akb://{vault}/doc/{filename}                        root doc
    akb://{vault}/coll/{coll_path}/doc/{filename}       doc in coll
    akb://{vault}/table/{name}                          root table
    akb://{vault}/coll/{coll_path}/table/{name}         table in coll
    akb://{vault}/file/{uuid}                           root file
    akb://{vault}/coll/{coll_path}/file/{uuid}          file in coll

Two new helpers exist alongside the typed ones:

  * `vault_uri(vault)` — addresses the vault root, useful as the
    starting point of a drill-down chain.
  * `coll_uri(vault, path)` — collections are first-class URI
    citizens now (previously they were the only navigation type
    without a canonical handle).

`table_uri(vault, name, collection=None)` and
`file_uri(vault, file_id, collection=None)` gained an optional
`collection` parameter — pass it when building from a row that has
the collection FK already JOINed. The `doc_uri(vault, path)`
helper splits the doc's full path at the LAST slash and emits the
new canonical form automatically — call sites that already pass
`documents.path` need no change.

`akb_browse` accepts a `uri` argument that takes precedence over
the legacy `vault` + `collection` pair. Drill-down chains are now
a paste-back loop: every browse item carries `uri`, paste it back
into `akb_browse(uri=...)` to drill in. Doc / table / file URIs
passed to browse are rejected with a hint pointing at the
appropriate leaf tool.

**Migration 026** rewrites every persisted URI in `edges`,
`publications`, and `events` to the new canonical form. Doc
rewrites run as a pure SQL `regexp_replace`; table/file rewrites
JOIN through `vault_tables` / `vault_files` to recover the
collection. Frontmatter URIs inside markdown bodies are **not**
rewritten — old URIs there will not parse against the new scheme
and edge extraction logs a warning. An optional batch-rewrite
tool can be run later if needed.

`make_uri(vault, type, identifier)` (the bottom-level builder that
produced the legacy shape) is gone. Every emit site goes through
the type-specific helpers so the location prefix is built the
same way everywhere — a hand-built `f"akb://..."` string outside
`uri_service.py` is now an audit finding, not a routine pattern.

### `akb_browse` — true tree-depth

### `akb_browse` — true tree-depth

`depth` was historically a misnomer: `1 = collections only`,
`2 = + documents` (always-all tables and files regardless). Issues
#81 / #82 fixed in 0.2.5 patched the document asymmetry, but the
underlying mental model stayed broken — depth wasn't depth, just
an "include docs" toggle, and tables/files leaked from sub-collections
into every top-level browse.

0.3.0 redefines `depth` as **tree-depth from the browse root** —
the `tree -L N` convention:

- `depth=0` — direct children of the browse root only, no descent
  into any collection
- `depth=N` (N ≥ 1) — descend N collection levels
- `depth=-1` — unbounded; the entire subtree of the browse root

Collection rows are always emitted as navigation aids regardless of
depth (the response would be useless without them). `doc` / `table` /
`file` rows are the ones gated. When `collection` is supplied, the
browse root is that collection, and depth is counted from inside it.

Schema bounds: `minimum: -1`, no maximum. Existing default
`depth=1` is preserved but its meaning shifts (root + 1 level
of collection contents, rather than the old misnomer).

Migration for the AKB frontend: `use-vault-tree.ts` now calls
`browseVault(vault, undefined, -1)` (unbounded) because the
client-side tree builder always wants the entire vault. External
MCP clients passing `depth=2` and expecting "everything" must
switch to `depth=-1`.

No data migration is required — depth is computed at query time
via PostgreSQL slash-counting (`length - length(replace(path, '/', ''))`),
the existing `collection_id` FK + path conventions cover every
case.

### `akb_recall` — corpus total + truncated flag

Pre-0.3.0 returned `list[dict]` straight out, with the MCP handler
synthesising `{memories, total}` where `total = len(memories)` —
i.e. it lied when `LIMIT` cut anything off. Callers had no way to
know more memories existed.

0.3.0 returns `{memories, returned, total, truncated}`:

- `total` is the **corpus count** matching the filter (one extra
  `COUNT(*)` query, cheap)
- `returned` is `len(memories)`
- `truncated` is `True` when `total > returned`

Callers that previously consumed the bare list now read `.memories`.
The REST `/api/v1/memory` mirror was updated symmetrically.

### `akb_activity` — truncated flag, drop the misleading `total`

Pre-0.3.0 returned `{vault, total: len(entries), activity: entries}`
where `entries` was already capped by `git log --max-count=limit` —
so `total` was the post-limit slice, never the corpus.

0.3.0 returns `{vault, activity, returned, truncated}`:

- `total` removed (it was wrong)
- `truncated` computed via peek-ahead (`git log --max-count=limit+1`)
- `returned` is `len(activity)` after any author post-filter

The `total` removal is the visible BREAKING change. `test_mcp_e2e.sh`
already reads the new `returned` field.

### `akb_graph` — `depth` → `hops`

Graph traversal radius is now spelled `hops` instead of `depth` to
disambiguate from `akb_browse.depth` (collection-tree depth). The
two parameters meant different things — one counts edges
followed, the other counts folder levels — and sharing a name
forced callers to memorize the difference. REST `?depth=` is
renamed to `?hops=` on `/graph` as well. No alias; explicit
rename so half-migrated callers fail loudly instead of using the
wrong radius.

### `akb_search` / `akb_grep` — `collection` field on hits

Hit envelopes now include `collection` (the containing-collection
path, null at vault root). Sourced from a `LEFT JOIN collections`
in the hydrate query — same row already gives us the URI, so the
cost is one extra column per type. Clients that grouped or
filtered hits by collection used to parse the URI themselves;
now they read the field directly.

### `akb_drill_down` — sub-section navigation hints

A successful match returns `sub_sections` — the immediate
children of the matched heading that actually appear in the
document. A `hint` field points at the next concrete drill step
(`section='Setup/Install'` or `mode='outline'`), so an agent that
just matched "Setup" gets one-step suggestions instead of having
to re-fetch the outline.

### Defense-in-depth — vault-template seed

`document_service.create_vault` (template seed path) now skips
`coll_repo.get_or_create` when the template's `collections[i].path`
is empty, with a warning log. Every shipped template already has
non-empty paths; the guard exists so a future template typo cannot
quietly resurrect the `path=''` phantom row that issues #81/#82
were about.

### Migration notes

- AKB frontend: included in this PR (`depth=-1`, memory type widened).
- `seahorse-mcp-agent-server` and any external MCP consumer: needs a
  coordinated update before deployment to environments where it runs
  (e.g. KISA prod). Hold off on AWS-prod rollout until the demo-agent
  team has aligned. On-prem rollout is safe in isolation.

## 0.2.5 — 2026-05-24

`akb_search` response now carries a `truncated` boolean and an
optional `hint`, mirroring the contract that `akb_grep` got in 0.2.4.
The motivation: `total_matches` in hybrid search was always the
size of the source-deduped *prefetch pool*, not a corpus-wide hit
count — vector ANN is fundamentally top-K. When the pool fills to
the `rerank_prefetch` ceiling (default 30), the corpus may hold
many more hits than ever reach the response, but the existing
contract (`total_matches >= returned`) made it look like the
caller had seen the truth.

Now:

- `truncated=true` iff `total_matches >= target_unique` (the
  prefetch ceiling computed from `rerank_prefetch` /
  `search_prefetch` / `limit`). Treats "pool filled" as "there may
  be more in the corpus."
- `hint` set when `truncated=true`, recommending `akb_grep` with
  `count_only=true` for an exact literal-substring count and noting
  that semantic queries can't be exhaustively enumerated.
- Model docstring on `SearchResponse` rewritten to call out the
  pool-depth semantics explicitly.

The `total_matches` value itself is unchanged — same number,
honest framing. `total` (deprecated alias of `returned`) stays.

## 0.2.4 — 2026-05-24

`akb_grep` default response now reports the true corpus-wide totals
even when the line snippets get truncated under `limit`. The old
shape aggregated `total_docs` / `total_matches` over the post-limit
slice — agents that read those as "how many hits exist in the corpus"
got false-low counts and made early-termination mistakes. Symptom:
"the pattern only appears in N docs" when the actual count was much
higher.

New default-mode fields:

- `returned_docs` / `returned_matches` — what fit under `limit`
- `total_docs` / `total_matches` — full ILIKE scan (no cap)
- `truncated` (bool) + `hint` — set when there's more than `limit`
  could hold, recommending `count_only=true` or
  `files_with_matches=true` instead of bumping `limit`

This aligns `akb_grep` with the `returned` vs `total_matches`
contract that `akb_search` already follows (issue #35). The
`count_only=true` and `files_with_matches=true` response shapes are
unchanged — they always reported full-scan counts and are now also
the official escape hatch when the default shape reports
`truncated=true`.

One small correctness side-effect: chunk hits that produced no
line-level matches after `strip_chunk_metadata_header` (header /
summary metadata artifacts riding along with every chunk) no longer
appear as zero-match docs in the response. They were never real
grep hits.

## 0.2.3 — 2026-05-23

Agent-facing polish on the search tools introduced in 0.2.1 / 0.2.2.
Three small changes driven by the agentic-bench v7 review of the tool
surface; all backward-compatible.

`akb_drill_down` gets a `mode` argument. Previously the only way to
get a document's outline was to trigger the empty-match fallback —
agents that just wanted the structure had to ask for sections,
discard the bodies, and parse heading paths out. `mode='outline'`
makes that a first-class call (no body fetch, cheap), with the same
`truncated` / `hint` metadata as the empty-match path. `mode='sections'`
is the default and preserves the old behaviour.

`akb_list_vaults` and `akb_browse` rename their substring-filter
argument from `query` to `filter`. The old `query` name collided with
`akb_search.query` (a natural-language retrieval string), and an
agent picking between the tools shouldn't have to remember that the
same parameter name means two different things. The old `query`
parameter remains accepted as an alias and is marked DEPRECATED in
the schema; it will be removed in 0.3.0.

Response shape standardisation. List-style responses now share four
common fields where applicable: `total` (matches before pagination),
`returned` (items in this response), optional `truncated` (true when
output was capped beyond the agent's control), optional `hint` (a
one-line guidance string for retry / pagination). Existing fields
remain in place for backward compatibility.

## 0.2.2 — 2026-05-23

Two more MCP tool overhauls along the same axis as 0.2.1's
`akb_list_vaults` slim-down. After fixing vault discovery in v7 of
the agentic-bench, the next failure modes the bench surfaced were
the *next* steps in the routing chain — `akb_browse` payloads
truncating before the agent could see the target collection, and
`akb_drill_down` returning nothing when the heading guess was off
by a word.

`akb_browse` — slim by default. The per-item `summary` field is
multi-paragraph English text and was 80-90 % of the response bytes
in vaults with 70+ collections (`legalize-kr-external-ro` at the
6 KB cap). Now dropped unless `include_summary=true`. Adds the
same `query` / `limit` / `offset` filters as list_vaults so an
agent searching for a specific collection (e.g. `query='민법'`)
gets one row, not a truncated list. Response gains `total` /
`returned`.

`akb_drill_down` — substring grep inside sections + outline
fallback. The old behaviour matched `section` against heading
paths only, so queries like `section='부칙'` either fetched the
entire 부칙 (often > 6 KB and truncated) or returned nothing when
the agent guessed the wrong heading. Two additions:

- `pattern` arg: case-insensitive substring filter on section
  bodies. Lets the agent grep *inside* a large section without
  refetching the whole document — useful for `'부칙'` + a
  specific 호수, or for finding a cross-reference like
  `'「개인정보 보호법」 제23조'` without scanning every section.
- When the (section, pattern) query returns no sections, the
  response now carries an `outline` field listing the document's
  available headings (capped at 200) plus a `hint` to retry or
  call `akb_get`. Replaces the silent empty-result trial-and-
  error loop observed in agentic-bench v7.

Schemas extended additively; existing callers keep working.

## 0.2.1 — 2026-05-23

`akb_list_vaults` MCP tool overhaul. The previous handler returned
every accessible vault with full metadata (id, role, created_at,
status, public_access), which inflated to ~80 bytes per vault. In
tenants with 70+ vaults the payload hit ~6 KB — exactly the size at
which the stdio proxy / agent client truncated. Vaults whose name
sorted late in the alphabet were silently invisible to the agent,
which then either hallucinated answers or claimed the data wasn't
in AKB at all. (Observed under
[agentic-bench v5–v6](../eval/agentic-bench/): the `A3_tree` arm
hovered around 2-4% PASS purely because `legalize-kr-external-ro`
was being trimmed off the end of the list.)

The new handler returns `{name, description}` only by default, plus
optional `query` / `limit` / `offset` / `include_archived` filters
and a `total` / `returned` count. Same MCP tool name, additive
schema, so existing callers continue to work; the new args are
opt-in. REST callers that need the full rows still use
`GET /api/v1/vaults`.

## 0.2.0 — 2026-05-21

First public minor release. The headline is the **security model
switch**: vault isolation in `akb_sql` is now enforced by
PostgreSQL ACL, not by application-side identifier filters. The
boundary lives in the platform's trusted base, not in a regex the
maintainer has to keep cat-and-mouse with new PG catalog names.

This is an additive change. Existing deployments upgrade in place;
no data migration, no breaking contract changes.

### Highlights

- **PG-native vault isolation for `akb_sql`.** Each user gets an
  `akb_user_<uid>` PG role, each vault gets three group roles
  (`akb_vault_<vid>_{reader,writer,admin}`), and `vault_access` rows
  map 1:1 to role memberships. The akb_sql executor opens a tx,
  `SET LOCAL ROLE` to the caller's role, and runs the query. PG
  returns `42501` directly for any cross-vault reference — the
  application no longer inspects user SQL for forbidden identifiers.
  Public vaults reach all authenticated users via a wildcard
  `akb_authenticated` role. Design in
  `docs/designs/pg-native-rbac/`.

- **ACL hardening at the MCP boundary.** `akb_search` now forwards
  the caller's `user_id` into the service layer so the ACL prefilter
  actually fires, and falls closed with `check_vault_access` when a
  vault arg is supplied (#66, #67). `SearchService.search` itself
  now raises `ValidationError` if both `vault` and `user_id` are
  None — mirroring the existing guard in `grep` (#70, #71).

- **Concurrency & atomicity audit follow-through.** Eight weeks of
  audit (`docs/reviews/2026-05-20-concurrency-audit/`) landed as
  hardening across the write path: deterministic lock order in
  `bm25_vocab` UPSERT, race-safe `pgvector.ensure_collection`,
  serialized GitPython IndexFile ops, vault-level advisory locking
  on put/update/edit/delete.

- **Server-side JWT revocation.** `users.tokens_revoked_before` is
  checked on every request so admin / user / self-initiated session
  invalidation reflects immediately, without rotating the global
  `jwt_secret`.

### Operational endpoints (admin-only)

- `GET  /api/v1/admin/role-state` — read-only diff of PG role state
  vs catalog (drift inspection). Returns missing/orphan user roles,
  missing memberships, missing or stale public-access grants, and
  per-table GRANT drift in one call.
- `POST /api/v1/admin/reconcile-roles` — on-demand reconciler if
  diff reveals drift. Same idempotent pass that runs at startup.
- `GET  /api/v1/admin/users` — list every user with stats.
- `DELETE /api/v1/admin/users/{user_id}` — admin-driven user
  deletion (owned vaults cascade).
- `POST /api/v1/admin/users/{user_id}/revoke-sessions` — invalidate
  every JWT for a user (incident response / offboarding).
- `POST /api/v1/admin/users/{user_id}/reset-password` — generate a
  one-time temporary password.

### Observability

- `/health.rbac` — `RoleSync` hook-failure counters + last reconcile
  outcome + timestamp. Surfaces silent drift to dashboards without
  log grep.
- `lifecycle.start_workers` now joins a periodic `RoleSync`
  reconcile loop (configurable via
  `role_sync_reconcile_interval_secs`, default 3600 s, 0 to
  disable).

### Versioned reads

- `akb_get` / REST `GET /documents/{id}?version=<commit>` —
  retrieve any historical commit of a document. Frontmatter is
  parsed against the version's metadata when possible, with a
  `metadata_is_current` flag when the version predates a
  frontmatter shape change. Git is canonical for body; PG metadata
  is best-effort for older commits.

### Search & indexing

- Parallel `embed_worker` via `indexing_concurrency` config — N
  workers drain the chunks queue in parallel, with per-row
  transactions so a crash doesn't double-index.
- BM25 corpus stats (`avgdl`, per-term `df`) refreshed on a cadence
  (`bm25_recompute_interval_secs`, default 6 h) so the sparse leg
  doesn't stay degenerate after fresh installs.
- Rerank fusion scoring (Reciprocal Rank Fusion) improved to
  preserve first-stage signal when rerank reorders.
- LongMemEval hybrid retrieval tuning — see
  `eval/longmemeval/results/2026-05-20-longmemeval-s.md`.
- Embed worker always sends the configured `embed_dimensions`
  param so MRL models match the schema dim.

### Skill-flow polish

- `akb_help(topic="vault-skill", vault=<name>)` returns the vault's
  skill doc + a default fallback when the vault hasn't customised
  it. `text/markdown` content-type for direct rendering.
- New `GET /api/v1/help/skill-template` exposes the default
  vault-skill body for previewing in the UI before vault creation.
- Vault create seeds `overview/vault-skill.md` with a "Guide" title
  + 6-relation enum + Document Template section.

### Internal / removed

- The application-side `akb_sql` sandbox (`_validate_sql_surface`
  identifier blocklist, `_FORBIDDEN_TOKEN` regex, `_VT_IDENTIFIER`
  match, `allowed_pg_tables` enumeration, read-only-tx forcing) is
  removed. The boundary moved to PG; these become redundant.
- `POST /api/v1/auth/tokens` no longer accepts a `scopes` array.
  The DB column stays, but the input was a user-visible lie — no
  request handler ever enforced it. Re-introduce with the matching
  check when scope enforcement is wired in.

### MCP tool surface

No tool was removed. Tools whose internal flow changed:

- `akb_sql` — runs via PG-RBAC executor; cross-vault references
  return PG `42501`; admin users bypass the per-user role and run
  as the backend service role (matching existing trust model).
- `akb_search` — forwards `user_id`; fails closed on
  unauthorized-vault arg.
- `akb_set_public` — moved out of the MCP handler into
  `access_service.set_public_access`; the handler is now a thin
  adapter. Same semantics, cleaner separation.

### Test plan

- 50 cases in the new `test_pg_rbac_e2e.sh` covering positive
  paths, 15 cross-vault SQL surface variations, app pre-flight
  reject, reader scope, public-vault access, and lifecycle drift
  recovery.
- 14 unit cases in `test_role_sync.py` covering helpers, lifecycle
  hook idempotency, public-access transitions, drift detection
  including per-table GRANT drift, hook metrics.
- New `scripts/bench_pg_rbac.py` microbenchmark. Measured locally:
  `SET LOCAL ROLE` adds ~63 µs per `akb_sql` tx; reconcile of
  50 users × 25 vaults + 251 grants finishes in 108 ms.
- All 8 existing e2e suites pass unchanged (`test_mcp_e2e` 75/75,
  `test_security_edge` 65/65 after #69, etc.).

### Acknowledgments

- **@MackDing** for #65 (proxy keep-alive perf) and #66/#67 (MCP
  search ACL enforcement) — first external contributor; the
  security PR pushed maintainer audit into the surrounding handlers
  and surfaced #70 / #71 follow-ups.

## 0.1.0

Initial OSS release. See git history for the pre-OSS development
series.
