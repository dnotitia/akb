# AKB Backend — Changelog

The AKB backend ships as a Docker image and as the HTTP layer behind
the `akb-mcp` stdio proxy. This changelog tracks the backend
specifically; the proxy has its own log in
`packages/akb-mcp-client/CHANGELOG.md` and a separate version stream.

## 0.8.10 — 2026-06-11  *(patch — table create: reserved/duplicate column → clean 422, never 500)*

`POST /api/v1/tables/{vault}` returned a bare **500** when a create payload included a reserved column name (`id`/`created_at`/`updated_at`/`created_by`) or two same-named columns — a client contract violation surfaced as an opaque internal error. Two paths produced it: `_validate_column_name` raised a bare `ValueError` (not an `AKBError`, so the global handler missed it → FastAPI 500), and a duplicate column reached the DDL as an uncaught `asyncpg.DuplicateColumnError`. The reserved-reject gap dates to 0.3.6; it only began firing when a caller (reef) started sending an `id` column.

Now every reserved / duplicate / malformed / missing-`name` column on create (and the symmetric `alter_table` paths) returns a clean **422** with an actionable message, on both REST and MCP. Keystone: `ValidationError` now **IS-A** `ValueError`, so the existing `except ValueError` MCP handlers and `pytest.raises(ValueError)` guards keep classifying validation rejects correctly (`AKBError` stays first in the MRO → status stays 422). Adds `tests/concurrency/test_invariants_unit.py::test_p2_create_table_bad_columns_are_validation_errors` covering every create-422 vector plus the corrected-create-succeeds path. reef must drop the reserved `id` (it already has `reef_id`); AKB does not alias reserved columns by design.

Companion **frontend** fix (separate image): the table viewer referenced a table with a double-quoted identifier (`SELECT * FROM "reef_issues"`), which the akb_sql rewriter intentionally skips, so it was never resolved to the physical `vt_<vault>__<table>` → "relation … does not exist" even though the table + rows existed. The viewer now sends the bare short name (table names are `^[a-z][a-z0-9_]*$`, so a bare reference is always well-formed and gets rewritten).

## 0.8.9 — 2026-06-10  *(patch — register migration 035 in the runtime applier + guard test)*

Migrations run from an **explicit hardcoded list** in `_apply_migrations` (`app/db/postgres.py`), not by globbing the directory — a deliberate design so a steady-state boot runs zero DDL. 0.8.8 added `035_fix_wikilink_alias_edges.py` but **not** its entry in that list, so the migration shipped as a **no-op**: the file was in the image but never invoked, and the already-corrupted edges stayed corrupted on deploy.

This registers `035_fix_wikilink_alias_edges.py` so it actually runs, and adds `tests/test_migrations_registered_unit.py` — a guard that fails if any `0NN_*.py` migration file on disk is missing from the applier list (with a tiny documented allowlist for the two applied via init.sql). That guard would have caught the 0.8.8 omission in CI.

## 0.8.8 — 2026-06-10  *(patch — knowledge graph: wikilink-alias edge corruption + implicit-edge existence validation)*

Body-link extraction had **no handling for Obsidian wikilinks** `[[target|alias]]`. The greedy bare-`akb://` scan (`_AKB_URI_RE`) then swallowed the alias's first word onto the target — a References line like `[[akb://v/coll/decisions/doc/x.md|PWC Query Performance Optimization]]` produced the edge target `akb://v/coll/decisions/doc/x.md|PWC` (matching stopped at the first space). One bug, two symptoms:

- **Relations panel / `akb_relations`** returned the malformed URI, so "related to" navigation built a broken `…/x.md%7CPWC` link to a non-existent doc.
- **Graph drew no edges** — the corrupted target matched no document node, so every body-derived edge was silently un-drawable. A vault whose only relations were wikilinks showed all nodes, zero edges.

Three-part fix:

1. **Parser** (`extract_markdown_links`): added a wikilink matcher `[[target|alias]]` that keeps only the target (alias is display text), and tightened `_AKB_URI_RE` to stop at `|`, `[`, `]` so the bare-URI fallback can no longer absorb an alias or a closing `]]`.
2. **Existence validation** (`_store_edge`): the implicit extraction path now validates the target **exists** before storing — via the *same* `_resource_exists` primitive the explicit `akb_link` path uses (the two paths differ only in policy: `akb_link` returns `NOT_FOUND`, extraction silently skips). An implicit edge to a non-existent resource can never be drawn and only pollutes the graph; previously the URI branch skipped this check, which is how the malformed targets persisted. *Note:* a frontmatter `depends_on` / `related_to` (or body link) whose target does not yet exist is now dropped rather than stored as a dangling edge — a deliberate forward-reference is re-materialized on the next index after the target is created.
3. **Migration 035** (`035_fix_wikilink_alias_edges.py`): repairs rows already persisted — for each edge whose `target_uri` contains `|`, strips to the real target and, if that target resolves, re-inserts the corrected edge (ON CONFLICT DO NOTHING) and drops the corrupted row; orphans whose cleaned target doesn't resolve are dropped. Idempotent.

### Tests
`tests/test_kg_extract_links_unit.py` gains wikilink coverage: akb:// + alias strips to the clean URI (and asserts no alias fragment leaks), path + alias, alias-less `[[…]]`, the exact historical "stopped at the space" failure shape, bare-scan dedup, and a wikilink inside a code span staying ignored.

## 0.8.7 — 2026-06-10  *(minor — Keycloak SSO: cross-origin companion-app post-login redirect allowlist)*

A single AKB backend can act as the identity owner for a small family of first-party apps on **different origins** (e.g. reef at `reef-<slug>.<domain>` alongside akb's own `akb-<slug>.<domain>`), each delegating to the *same* tenant Keycloak client instead of registering its own. The blocker was that the SSO post-login redirect was a **single per-instance** `keycloak_post_login_path`: the callback always bounced the one-time code to that one same-site path, and `_safe_redirect_path()` collapsed any cross-origin redirect (open-redirect guard). So akb's own SPA and a companion app could not both complete SSO — they fought over one global path, and pointing `keycloak_post_login_path` at the companion's absolute URL (the earlier workaround) just broke akb's own SPA instead.

New optional setting **`keycloak_post_login_allowed_origins`** (list, default empty) lifts this **per request** without weakening the open-redirect guard:

- A companion app starts SSO via `GET /auth/keycloak/login?redirect=<absolute-URL-on-an-allowlisted-origin>`; the callback delivers the one-time `code` straight to that URL (origin + path + its own query preserved). It then exchanges the code server-side via the existing `POST /auth/keycloak/exchange` — same single-use, ≤60s-TTL guarantees as the akb-SPA path, now delivered to a vetted origin.
- akb's own SPA (a same-site `redirect` path) is unchanged: code goes to `keycloak_post_login_path?code=&redirect=<safe path>`.
- **Empty list ⇒ identical to before.** No origin can ever receive the code; behaviour is 100% the pre-existing same-site flow.
- **Open-redirect protection preserved.** Any redirect whose origin is not explicitly listed — absolute URLs, scheme-relative `//host`, and `https://trusted@evil.com` userinfo spoofs — collapses to the safe same-site path. Origins match as `scheme://host[:port]`.
- **Re-validated at delivery.** The origin is checked at `begin_login` *and* again in the callback, so a config change during the ≤10-min flow window can only ever tighten where the code goes; flow-state values are never trusted blindly.
- No new trust in the companion: it still stores no Keycloak client/secret/realm — identity stays owned by AKB.

This is the AKB-side counterpart to the reef SSO-delegation work (reef-test REEF-102 / REEF-112) and supersedes the "point `keycloak_post_login_path` at the companion's absolute URL" workaround, which could satisfy only one origin per instance.

`backend/app/api/routes/auth.py` gains `_normalize_origin` / `_allowed_companion_origin` / `_with_query_param` / `_post_login_target`; `keycloak_login` and `keycloak_callback` route through them. `backend/app/config.py` adds the setting (default empty). `config/app.yaml.example` and `docs/designs/keycloak-oidc/00-overview.md` document it.

### Tests
`tests/test_keycloak_redirect_unit.py` (new, 21 cases, no DB) pins the allowlist gate and the open-redirect guard: origin normalization (host lowercasing, port retention, non-http rejection), the userinfo-spoof rejection even when the trusted origin is listed, empty-list ⇒ same-site for every input, listed-origin code delivery with existing query preserved, and unlisted/absolute/scheme-relative collapse to the safe path.

## 0.8.6 — 2026-06-08  *(patch — GET document: read body at the row's current_commit, not floating HEAD (E03 read-side race))*

`GET /documents/{vault}/{id}` (`akb_get`) assembled its response from **two unsynchronized reads**: the body via `git.read_file(vault, path)` — which reads the *floating vault HEAD* — and `current_commit` from the DB row. Under concurrent updates to the **same** document, a writer could advance git HEAD and the DB row between those two reads, so a single response carried a `content` and a `current_commit` belonging to **different writers** (the "E03" symptom: body ↔ current_commit disagree).

Reproduced against the live cluster: at quiescence the two always agree (writes are correctly serialized by the PG path-lock + git vault-lock, so there is no persistent divergence and no data loss), but **during** a 20-writer / 6-reader hammer ~44% of GET responses were internally inconsistent.

Fix: `document_service.get` now reads the body **at `row["current_commit"]`** instead of HEAD, so the `(content, current_commit)` pair is consistent by construction (and semantically correct — `current_commit` is the document's version pointer, whereas HEAD is vault-wide and can even point at another document's commit). A NULL `current_commit` on legacy rows falls back to HEAD inside `read_file`, so there is no behavior change for un-pinned documents.

This is a *read-path* fix; it does not touch the (correct) write serialization. The earlier read-path hypotheses (repo caching, bare-vs-worktree ref divergence) were ruled out — the real cause was the non-atomic two-source assembly in `get()`.

### Tests
`tests/test_document_hash_contract_unit.py` gains `test_document_get_reads_body_at_current_commit_not_head`, which asserts `read_file` is invoked with the row's `current_commit` (a revert to the HEAD read fails it). Verified end-to-end with a concurrent read/write harness against the deployed cluster: inconsistent GETs dropped from ~44% to 0.

## 0.8.5 — 2026-06-08  *(patch — table create: clean 422 for over-long PG identifiers instead of an opaque 500)*

Creating a table whose PostgreSQL identifier `vt_<vault>__<table>` would exceed PostgreSQL's 63-byte `NAMEDATALEN` limit failed with an **opaque HTTP 500**. The over-long name passed table creation but tripped `role_sync._is_safe_pg_table_name` during the in-transaction `GRANT`, which `raise`d `ValueError("unsafe pg_table_name … refusing grant")` deep in the stack. Found by a concurrency test (E08) whose long ephemeral vault name (`prod-conc-…`, 27 chars) + long table name (32 chars) produced a 64-char identifier — exactly one byte over.

The safety check is **correct and stays** — PG *silently truncates* identifiers past 63 bytes, so granting on a truncated `vt_*` name risks a truncation-collision where a privilege lands on the wrong table. We must refuse, not truncate.

What changed is *where* and *how* it's reported:

- `table_service.create_table` now pre-validates the identifier length immediately after deriving `pg_table_name(...)` and **before any DDL**, raising `ValidationError` (HTTP **422**) with a message that names the offending identifier, its length, the limit, and which name (vault or table) to shorten.
- The sibling name-shape check (the `_TABLE_NAME_RE` guard) was likewise raising a bare `ValueError` → 500; it now raises `ValidationError` (422) too, so every malformed-name rejection is a clean 4xx.
- The 63-byte bound is now a single named constant `table_data_repo.PG_IDENT_MAX_LEN`, consumed by both the new pre-check and `role_sync`'s defense-in-depth guard (no more magic `63` in two places).
- **MCP transport:** `ValidationError` is an `AKBError`, not a `ValueError`, so the `akb_create_table` handler's `except ValueError` would have let these rejections fall to the dispatch catch-all as `code="internal"` instead of `invalid_argument`. The handler now catches `ValidationError` (→ `invalid_argument`) and `ConflictError` (→ `conflict`, previously also mis-coded `internal`), keeping the error caller-actionable for agents.

### Tests
`tests/test_table_identifier_unit.py` pins the constant and the 63/64-byte boundary math. `tests/test_table_name_length_unit.py` exercises `create_table` with a fake pool: the exact E08 trigger returns 422 **and** never reaches `create_dynamic_table`, a 63-byte identifier passes the guard (off-by-one fence), and a malformed name returns 422 (not 500) — all CI (no DB). `tests/test_mcp_e2e.sh` §11 asserts the over-long name comes back as `invalid_argument` over the real JSON-RPC transport (post-deploy).

## 0.8.4 — 2026-06-07  *(patch — agent-memory: user_id-keyed vaults + Claude Code SessionEnd reasons)*

Two fixes to the agent-session lifecycle surface (`/api/v1/agent-sessions/*`) that together made the `akb-claude-code` lifecycle plugin unusable for a large class of users. Both were confirmed against a live deployment before the fix.

### 1. Non-ASCII usernames could not provision a memory vault
`sanitise_username()` NFKD-folds to ASCII and rejects an empty result, so an all-CJK username (e.g. `한병전`) raised `username cannot be safely slugified` and **every** `SessionStart` returned `422` — the user never got a memory vault at all.

The memory vault name is now keyed on the immutable `user_id` (a UUID, always a valid slug) via `memory_vault_name(user_id)` instead of the mutable, possibly-non-ASCII username. Consequences:

- Works for any username, including all-CJK.
- The name never drifts when a user later changes username / display_name / email. The human-readable identity moved to the vault **`description`** (`_memory_vault_description`), which is refreshed from the current profile on every `SessionStart`.
- **Back-compat:** `ensure_memory_vault` / `_resolve_memory_vault` probe the pre-migration `agent-memory-{slug(username)}` name and **adopt** an existing vault rather than orphaning it — so vaults provisioned under the old scheme keep working with no data migration. Adoption is **owner-scoped** (`owner_id = user_id`): distinct usernames can slugify to the same legacy name, so the name alone is not proof of ownership — a legacy vault owned by someone else is ignored and the user gets their own canonical vault.

### 2. Claude Code SessionEnd `reason` values were all rejected
`EndRequest.reason` only accepted the neutral cross-harness set (`completed`/`aborted`/`error`/`window_close`/`user_close`/`stop`), but Claude Code emits `clear`/`logout`/`prompt_input_exit`/`bypass_permissions_disabled`/`other`/`resume`. The plugin forwards the hook reason verbatim, so **every** `SessionEnd` returned `422` and no `recap.md` was ever written.

`reason` now accepts the Claude Code values verbatim too — mirroring how `source` already accepts Claude Code's raw `SessionStart` values — so a plugin needs no client-side mapping table. Unknown reasons are still rejected (`422`).

### Tests
`backend/tests/test_agent_sessions_e2e.sh` gains coverage for both: Claude Code reasons accepted (§12) and CJK-username provisioning + user_id-keyed vault + description label (§13). Full suite: 37 passed.

## 0.8.3 — 2026-06-06  *(patch — pgvector bootstrap: commit `CREATE EXTENSION` before codec registration on the shared-main-pool path)*

Semantic search silently returned empty on a **fresh shared-PG deployment** (`vector_store_driver: pgvector` with a blank `vector_url`, i.e. the pgvector index lives in the main application DB). Every search logged `vector hybrid_search failed (unknown type: public.vector); returning empty` and the indexing worker could never drain — chunks stayed `vector_indexed_at IS NULL` forever.

### Root cause

`PgvectorStore.ensure_collection()` ran `CREATE EXTENSION IF NOT EXISTS vector` and then registered the pgvector binary codec (`register_vector` → asyncpg `set_type_codec('vector', …)`) **inside the same uncommitted transaction**. asyncpg's codec introspection cannot resolve a type created by an as-yet-uncommitted `CREATE EXTENSION`, so it raised `ValueError: unknown type: public.vector`. That `ValueError` is not an `asyncpg.PostgresError`, so it escaped `ensure_collection`'s except clause and surfaced at every `hybrid_search`.

#117 (0.7.4) fixed this for the **separate-DSN** pool mode by bootstrapping the extension on its own committed connection in `_pool()` before building the codec-registering pool — but the **shared-main-pool** path (`dsn is None`) never gets that pre-commit, and #117 assumed the same-transaction create was visible to the codec. It is not. The bug only triggers on a DB where the extension has never been created, so existing installs (extension already present) never saw it; the first real exercise of the fresh-shared-pool path was a demo PVC-wipe reset.

### Fix

`ensure_collection()` now issues `CREATE EXTENSION IF NOT EXISTS vector` as a **committed (autocommit) statement before** opening the schema-build transaction, so the `vector` type is resolvable when the codec registers. Idempotent and a no-op once the extension exists; covers both pool modes.

### Verification (and a caveat on the test)

Validated against the actual failing environment — a full demo PVC-wipe **reset → seed → search** cycle on the deployed backend: after the fix the lifecycle logs `Vector store schema ensured (eager init)` (previously `eager init failed: unknown type: public.vector`), the `vector` extension is created with no manual step, and all chunks index (`vector_indexed_at` drains to 0) so search returns hits.

Note this manifestation **does not reproduce in isolation**: running `test_pgvector_ext_bootstrap_e2e.py` against a fresh `pgvector/pgvector:pg16` DB — and even a faithful replica of the backend's pool lifecycle (`min_size=2`, `init.sql` warm-up, real queries, identical asyncpg/pgvector pins) — passes on the *pre-fix* code too. The deployed trigger is almost certainly the lifespan eager-init racing the embed workers to be the first `ensure_collection` caller, which a single-threaded test can't recreate. The pgvector e2e tests are now wired into CI (`pgvector-e2e` job in `backend-pytest.yml`) as general bootstrap hardening for the #117-class scenarios, but they are **not** a gate for this concurrency-specific manifestation — that is covered by the deploy-time reset-cycle check above.

## 0.8.2 — 2026-06-06  *(patch — bulk migration: `VectorStore.upsert_batch`, REST seahorse-db multi-line JSONL, resume + per-row fallback)*

`scripts/migrate_pgvector_to_seahorsedb.py` was the bottleneck for any operator moving a real-sized vault from pgvector to a self-hosted Coral. 0.7.9's script issued one `upsert_one` per chunk — fine for the first 65 rows we tested with, painful at 350 k. This release routes the script through a new Protocol-level batch path and adds the operational surface every bulk migration actually needs: a checkpoint file so a connection drop doesn't restart from zero, and a per-row fallback so a single transient batch failure doesn't cost a few hundred chunks.

### What ships

**`VectorStore.upsert_batch(chunks, *, conn=None)`** — new Protocol method (`backend/app/services/vector_store/base.py`). Drivers with a native batch shape override; drivers without it delegate to `loop_upsert_batch(self, chunks)`, which is the obvious N-calls-of-`upsert_one` fallback factored into a helper so each driver doesn't paste the same six-line loop.

**`ChunkUpsert` dataclass** — frozen-shape input to `upsert_batch`, mirroring `upsert_one`'s kwargs one-for-one. Adding a column to `upsert_one` means adding it here too; the two methods don't get to drift on what a chunk *is*.

**REST seahorse-db driver (`seahorse_db.py`) — specialised `upsert_batch`**. Builds a single `\n`-joined JSONL body and ships it in one `POST /v2/tables/{name}/data`. Coral's `insert_jsonl_bytes` runs the body through `arrow_json::ReaderBuilder.with_batch_size(reader_batch_size)` (64 by default) and dispatches the resulting record batches segment-by-segment with the eight-way concurrency in `coral/src/ingest/application/insert/dispatch.rs`. Net result: ~`len(chunks)/64` Kafka WAL appends per call instead of one per chunk, and a single HTTP round trip instead of one per chunk. A shared `_record_dict` helper is the single source of truth for the JSON record shape, so `upsert_one` and `upsert_batch` can't drift on column names, sparse encoding, the signed-i64 PK label, or the `dense=None` refusal.

**Fallback `upsert_batch` on the other four drivers** — `pgvector`, `qdrant`, `seahorse-cloud`, `seahorse-db-grpc`. Each does the trivial `await loop_upsert_batch(self, chunks)`. No behaviour change versus 0.8.1; the point is that the Protocol now declares the method everywhere and callers can use it without an `isinstance` narrowing.

**Migration script — `scripts/migrate_pgvector_to_seahorsedb.py`**.
- Switches from per-row `upsert_one` to `upsert_batch` with `ChunkUpsert` payloads.
- New `--progress-file <path>` (default `/tmp/akb-migrate-progress.txt`). After every batch the script writes the last shipped `chunk_id` (UUID) via atomic tmp-rename. On startup it reads the file and resumes with a `chunk.id > <stored UUID>` filter on the same `ORDER BY c.id ASC` cursor. The cursor ordering changes from `c.created_at, c.id` to `c.id` only so the resume gate is unambiguous on chunks that share a timestamp. Pass `--progress-file ''` to disable.
- New `--checkpoint-every <N>` (default 2 048) to tune the checkpoint cadence.
- When `upsert_batch` raises (the Writer's `batch_insert_via_catalog` returns 503 under sustained load with non-trivial frequency — we measured ~0.15 % of batches on the dogfood stack), the script now falls back to per-row `upsert_one` for the failed batch instead of abandoning the entire 256 chunks. Each row gets its own attempt; failures are reported per `chunk_id`. Slow on the failure path, correct on every row.

### Where this matters

Pgvector users will not see a behaviour change in normal API traffic — `embed_worker` and the search path still use `upsert_one` / `hybrid_search`. The migration script is the only caller of `upsert_batch` in this release; other call sites (an internal reindex task, a future bulk-import endpoint) can adopt the same path without further driver changes.

### What's deliberately out of scope

Native `upsert_batch` for `pgvector`, `qdrant`, and `seahorse-cloud` (a single INSERT VALUES, a single `points_batch`, a single bulk POST respectively). The fallback is correct; native batch is a measurable throughput win on each backend and is queued as a follow-up, but landing five native batch shapes alongside the contract change would have made this release substantially riskier than the actual win it delivers.

The "SeahorseDB has no PK-aware upsert" caveat is real and now appears in the per-row fallback's docstring: if the failing batch had already landed partially on the Writer before raising, the per-row retry can produce duplicates (Coral's `drop_duplicate_primary_key_rows` only dedups *within* a single record batch). The operational pattern of "new target table per dogfood rerun" — which we used throughout the 35 k-chunk validation — sidesteps this; the alternative ("silently lose 256 chunks per transient 503") is worse than the duplicate risk.

### Verification

- `scripts/check.sh` — green.
- The existing 25-scenario `tests/test_hybrid_search_e2e.sh` covers the REST `seahorse-db` driver including `upsert_one`; the new batch path uses the same `_record_dict` and the same wire format, so the end-to-end behaviour is unchanged. A dedicated batch e2e is queued with the native-batch follow-up so they ship together.
- Existing unit tests stay green: `test_seahorse_db_grpc_unit.py` (31 tests), `test_sparse_weight_convention.py` (6 tests).

### Files

- `backend/app/services/vector_store/base.py` — `ChunkUpsert` dataclass, `loop_upsert_batch` helper, Protocol `upsert_batch` declaration.
- `backend/app/services/vector_store/seahorse_db.py` — specialised `upsert_batch`, shared `_record_dict`.
- `backend/app/services/vector_store/pgvector.py`, `qdrant.py`, `seahorse_cloud.py`, `seahorse_db_grpc.py` — fallback `upsert_batch` delegating to `loop_upsert_batch`.
- `backend/scripts/migrate_pgvector_to_seahorsedb.py` — batch path, checkpoint resume, per-row fallback, `--progress-file` / `--checkpoint-every` CLI.

### Not deployed

prod + demo remain on pgvector. The dogfood stack we built to validate this path (gitignored manifests under `deploy/k8s/internal/seahorse-db/`) still runs against an out-of-band Coral; throughput observations from that run will be appended to the AKB product vault rather than to this release.

---

## 0.8.1 — 2026-06-06  *(patch — compliance-grade audit log, producer-only, off by default)*

AKB now emits a structured, append-only, hash-chained **audit log** and
(optionally) hands the daily rolled file off to a WORM object-storage
bucket. AKB is a *producer*: it does not store, query, or retain audit
data. Off by default; enable with the new `audit:` config section.

### Why producer-only (the design, recorded here deliberately)

AKB is delivered to customer sites. In that setting the customer's
security org already runs a SIEM (Splunk / QRadar / ArcSight / Elastic /
Chronicle) and owns retention, WORM, query, and correlation under *its
own* compliance regime. The dominant and correct pattern for delivered
software is **"produce, don't own"**: the vendor emits a faithful,
complete, tamper-evident audit stream in a standard place/format and the
customer's audit system scrapes it. An AKB-side audit store/query/
retention tier would be redundant infrastructure the customer doesn't
want (it fragments their single pane of glass) and would wrongly impose
our retention policy over theirs.

What a producer still owns — because the customer cannot retrofit it:

- **Completeness** — no lost events, no events for actions that didn't
  happen. Captured at the source.
- **A durable handoff buffer** so a collector/bucket outage doesn't lose
  events.
- **A standard format** the SIEM can parse.
- **Tamper-evidence at the source** so the customer can *prove* to its
  auditor that what it scraped is complete and unaltered.

### Alternatives considered and rejected

- **Transactional outbox (in-tx with the domain write).** The existing
  `events` outbox binds an event to the PG transaction. But an AKB write
  already spans PG + git + vector store + S3 and is *not* globally
  atomic — PG is the authority and the rest reconcile around it. A strict
  in-tx audit outbox would hold the audit line to a *stronger* consistency
  standard than the domain operation itself has, for the sake of one rare
  edge (commit-then-crash-before-log). Not worth the machinery, so the
  model is uniform **best-effort post-operation append** for reads and
  writes alike.
- **Kafka backbone / Debezium → Kafka.** A good *transport* upgrade for
  multi-sink fanout, but it's a bus, not a system of record: it solves
  neither atomicity (front) nor immutability (back), and mandating a Kafka
  cluster raises the floor for self-host OSS users. Left as a possible
  future sink driver, not a dependency.
- **A separate audit PG instance.** Only meaningful if AKB owned the
  query/system-of-record tier — which the producer model gives to the
  customer's SIEM. A second PG that isn't in the domain transaction buys
  nothing over a file in terms of atomicity, so it's dropped.
- **A vendor-side 3-tier WORM/query stack.** Same reason — that's the
  customer's job.

The capture point is the **Kubernetes audit-backend pattern**: log at the
API layer (the MCP `_dispatch` chokepoint), not per-service, so one
instrumentation point covers every read and write uniformly. Per-action
verbosity follows K8s "levels" — reads are logged at *Metadata* level
(who/verb/target, no bodies) and can be turned off (`audit.log_reads`);
state-changing calls are always logged.

### What ships

- `backend/app/services/audit_log.py` — the producer. `record()` /
  `record_tool()` (best-effort, never raise into the caller),
  `verify_chain()` (operators/SIEM prove integrity), and a background
  uploader.
  - **Hash chain + per-file seq.** Each line carries a monotonic `seq` and
    `h = sha256(prev_h ‖ canonical(line))`; a dropped/altered/re-ordered
    line breaks the chain. The chain re-seeds from the on-disk file on
    restart so it survives a pod bounce.
  - **Manifest on handoff.** Each uploaded day object gets a sibling
    `*.manifest.json` (line count, first/last seq, file digest, chain
    head) so completeness and integrity are checkable from the bucket
    alone.
  - **Local file lifecycle.** Day 0 append → day ≥1 upload to bucket →
    day ≥`local_retention_days` (default 2) prune the local copy, **but
    only after a confirmed upload**. A bucket outage accumulates files
    locally and warns; it never deletes un-uploaded audit.
- `backend/app/config.py` — new nested `audit:` section (`AuditSettings`):
  `enabled`, `log_dir`, `log_reads`, `bucket`, dedicated S3 credentials
  (`endpoint_url` / `access_key` / `secret_key` / `region`),
  `upload_interval_secs`, `local_retention_days`. Nested (not flat
  `audit_*`) so the surface can grow — redaction, per-action levels,
  signing keys, syslog/webhook sinks — without littering the top level.
- **Credential isolation.** The handoff uses a *dedicated* audit-storage
  credential when set, falling back to the system S3 connection only for
  convenience. The recommended posture is a **write-only** key
  (PutObject, no Delete) on a separate Object-Lock account: AKB never
  deletes bucket objects (only the local buffer is pruned), so a
  compromise of the app's primary S3 key cannot rewrite or erase the
  trail. boto3 client construction is centralised in
  `s3_adapter.make_client()` so the file store and the audit store build
  clients identically.
- `backend/mcp_server/server.py` — `call_tool` resolves the actor once,
  passes it to `_dispatch`, and audits both the success and the
  last-resort error envelope. `_get_user` audits `auth.denied` when a
  presented credential is rejected (no token material recorded).
- `backend/app/services/lifecycle.py` — `audit_log.init()` seeds the
  chain at startup; the uploader starts only when `audit.bucket` is set
  (file-only mode still writes the stream for a co-located shipper to
  tail). Stopped in `stop_workers`.
- `backend/app/main.py` — `/health` gains an `audit` block (enabled, dir,
  today's file, seq, bucket, pending-upload count).
- `backend/tests/test_audit_log.py` — unit tests: append + schema, seq
  monotonicity, hash-chain verify + tamper detection, restart re-seed,
  read-skip when `log_reads=false`, write-always-logged, never-raises on
  an unwritable dir, and the upload/prune lifecycle (with a fake S3).
- `config/app.yaml.example` — documented (commented) `audit:` section.

### Not yet covered (follow-ups)

- REST `/auth/login` success/failure is not yet audited — auth events that
  flow through MCP tools are captured at dispatch, but the REST login
  route is a separate entry point. Tracked for a later pass.
- Sink drivers beyond `file` + S3 handoff (syslog/CEF, webhook, OTLP) are
  designed for but not implemented; the `audit:` section is shaped to
  grow into them.

## 0.8.0 — 2026-06-06  *(minor — `seahorse-db-grpc` driver, opt-in gRPC sibling of `seahorse-db`)*

A second SeahorseDB driver that talks gRPC to the same Coral coordinator (port and config are shared with `seahorse-db`; only the wire format differs). Opt-in via `vector_store_driver: seahorse-db-grpc`. The REST `seahorse-db` driver remains the documented production path; the gRPC variant is shipped as `experimental` until it clears its own QPS / recall benchmark.

### Why

0.7.7's release notes called out two real bugs we caught only because the wire happened to be JSON — i64 overflow on the PK column (`arrow_json::Decoder` rejects unsigned > 2^63 - 1 with a generic 500) and double-IDF/double-saturation on BM25 sparse weights. Both were client bugs, but the i64 surface is a JSON-parsing artifact. Typed gRPC (protobuf int64 for the PK, Arrow IPC for streamed search results, explicit message types for the dense and sparse legs) makes the type contract obvious at the wire boundary instead of letting "any uint64 value" reach Coral as untyped JSON. The intent is not to retire the REST driver — it works — but to give operators a transport with stricter typing on the same backend.

### What ships

- `backend/app/services/vector_store/seahorse_db_grpc.py` — new driver, ~470 lines.
- `backend/app/services/vector_store/_seahorse_common.py` — `chunk_id_to_label`, `encode_sparse_string`, `validate_uuid_for_sql`. Shared by both `seahorse_db.py` (REST) and `seahorse_db_grpc.py` (gRPC); the REST driver re-exports them under their old underscore names for backwards compatibility within this package.
- `backend/tests/test_seahorse_db_grpc_unit.py` — 31 mock-based unit tests covering protobuf wire shapes for every Protocol method, Arrow IPC decode round-trip, SQL-injection guards on `delete_point` and `hybrid_search`'s `source_id` filter, and CRUD parity with the REST driver.
- `backend/tests/test_sparse_weight_convention.py` — cross-driver regression. Parametrised over every value in `vector_store_driver`'s Literal; fails if the encoder's `_use_raw_weights()` flag doesn't match a deliberately-declared `_EXPECTED` table. Caught the gRPC driver's silent inheritance of 0.7.7's bug before merge.
  - Five Protocol methods:
    - `health` → `HealthService.Check`
    - `ensure_collection` → `TableService.GetTable` + `CreateTable` (typed `CreateTableSpec`; no Arrow IPC schema bytes needed)
    - `upsert_one` → `IngestService.InsertJsonl` (same JSONL bytes the REST driver ships — keeps the 0.7.7 signed-i64 label fix and the raw-mode BM25 fix shared at the encoder level)
    - `delete_point` → `IngestService.DeleteTableData`
    - `hybrid_search` → `QueryService.HybridSearch` (server streaming; chunks are Arrow IPC stream bytes decoded with pyarrow)
  - Sparse + label helpers (`_chunk_id_to_label`, `_encode_sparse_string`, `_validate_uuid_for_sql`) are imported from `seahorse_db.py` so encoder bugs fixed in one place don't drift between drivers.
- `backend/app/services/vector_store/_grpc/proto/coral/**` — vendored Coral `.proto` files and `grpc_tools.protoc`-generated stubs. Source pinned at SeahorseDB monorepo commit `e1364f27` (`SDDEV-244/monorepo-coral-sparse`). Regeneration procedure documented in `_grpc/README.md`.
- `backend/app/services/vector_store/factory.py` — new `elif driver == "seahorse-db-grpc":` branch that reuses the existing `seahorsedb_*` settings (same Coral, same port, same auto-create flag).
- `backend/app/config.py` — `vector_store_driver` Literal extended with `"seahorse-db-grpc"`.
- `backend/pyproject.toml` — three new runtime deps:
  - `grpcio==1.81.0` (the floor `grpc_tools.protoc` 1.81 emits in generated `_pb2_grpc` files)
  - `protobuf==6.33.6` (inside grpcio 1.81's `>=6.33.5,<7.0.0` range)
  - `pyarrow==22.0.0` (decode `ResultStreamChunk` Arrow IPC bytes)

### Wire details worth remembering

(All discovered during local validation; recorded here so the next reader doesn't have to rediscover them.)

- `CreateTableRequest.table` is `CreateTableSpec`, **not** `TableInfo` — so the schema can be described column-by-column in proto-native form and an Arrow IPC schema-bytes payload is not required.
- `DenseVectorSearchConfig.vectors` is `repeated FloatVector` (not wrapped in `DenseQueryVectors`); `SparseVectorSearchConfig.vectors` is `repeated string`. The wrapper messages exist in the proto but only inside `VectorQuery`, not inside the SearchConfig path.
- `FusionConfig.parameters` carries the RRF `k`. The REST driver pins `k=60`; the gRPC driver does the same via `FusionParameters(k=60)`. Leaving it as the server default silently shifts fused ordering vs the REST stream.
- BM25 `SparseSearchParameters` and `SparseMetadata` are sent together. Coral returns `INVALID_ARGUMENT "BM25 parameters (k/b) require N, avgdl, and df metadata"` if you send parameters without metadata. The driver mirrors the REST driver's "send metadata only when stats + query tokens exist" gate, which is an asymmetry that affects both drivers when the corpus stats haven't loaded — flagged as follow-up.

### Verification

- Local Coral (port 53286) — each Protocol method individually exercised; round-trip with create / health / upsert (one row, then five rows) / hybrid search returning Arrow-decoded hits / delete.
- `test_hybrid_search_e2e.sh` — full 25 scenarios against a backend whose `vector_store_driver` was flipped to `seahorse-db-grpc`. Pass: 25 / Fail: 0. Same result the REST driver gets on the same harness.
- `bash scripts/check.sh` — green. The generated stubs are excluded from ruff via `pyproject.toml`'s `extend-exclude` since they're vendored, not hand-written.

### Caveats

- **Experimental**: not yet a recommended production driver. Use `seahorse-db` (REST) unless you have a measurement that says otherwise. QPS / recall benchmark against the REST driver is queued as the next follow-up.
- **CI does not exercise the gRPC path** — no live Coral in CI. The driver's import-time correctness and the unit tests under `backend/tests/test_seahorse_db_grpc_unit.py` (also in this release) are what CI catches; end-to-end behaviour relies on the local 25-scenario run an operator does before flipping the production config.
- **BM25 metadata asymmetry** (pre-existing, also in REST): both drivers send `parameters` whenever a search runs, but `metadata` only when `bm25_stats` has populated values. On a fresh corpus where `total_docs == 0` Coral will reject the hybrid search until the stats catch up. Filed as a follow-up to fix in both drivers.
- **Three new runtime deps** raise the wheel size for everyone, not just gRPC users. We considered an optional dep group (`pip install akb[seahorse-grpc]`) and decided against it: AKB ships and is run as a container image, and ~50 MB inside Docker is not a meaningful constraint. The "extra failure mode if deps are missing" argument we initially gave for landing them as required is weaker — a `try: import grpc` in the factory branch would handle that in three lines. Revisit if a non-container distribution emerges; until then the call is "we don't gate wheel size for non-container users", said out loud.

### Mid-merge design review — what it caught

Before merging, an independent reviewer walked the week's changes (0.5.x → 0.8.0 working tree) and flagged two real issues that this section now reflects.

**Blocker — the gRPC driver inherited 0.7.7's bug.** `sparse_encoder._use_raw_weights()` was a literal equality check against the string `"seahorse-db"`; the new `"seahorse-db-grpc"` driver fell through to the pre-baked branch (saturated TF × IDF), which is exactly the double-IDF/double-saturation shape 0.7.7 fixed for the REST driver. The 25-scenario hybrid e2e still passed because RRF fusion with a healthy dense leg masks BM25 weight drift unless the test set is specifically engineered to surface it (ours isn't, yet). Fixed by switching the gate to a frozenset (`_RAW_WEIGHT_DRIVERS = {"seahorse-db", "seahorse-db-grpc"}`), and added `backend/tests/test_sparse_weight_convention.py` — a `typing.get_args`-driven regression that fails the moment the `vector_store_driver` Literal grows a value not declared in the test's `_EXPECTED` convention table. Next time a Coral-family transport lands, the contributor either declares its BM25 convention out loud or the test goes red on PR.

**Hygiene — load-bearing cross-file private import.** The gRPC driver was reaching into `seahorse_db.py` for `_chunk_id_to_label`, `_encode_sparse_string`, and `_validate_uuid_for_sql`. Pragmatic, but it made the REST driver both "a driver" and "the home of shared helpers", which is the separation-of-concerns violation 0.7.0's seahorse-cloud / seahorse-db split was supposed to enforce. Promoted the three helpers to `backend/app/services/vector_store/_seahorse_common.py`; both drivers now import them from the same first-class module. No behaviour change, but the contract is now public-to-this-package instead of "please don't break me".

**Acknowledged but deferred** (filed as follow-ups, not blockers for this release):

- The `_use_raw_weights()` frozenset is still the wrong shape architecturally; the cleaner fix is to put `sparse_weight_convention` on the driver class (or the Protocol) and have the encoder ask the driver, not the settings. Doing it now would change every driver implementation; doing it next release lets us measure the gRPC variant first.
- `seahorse-db-grpc` could have been a `seahorsedb_transport: rest|grpc` flag on a single `seahorse-db` enum value instead of a separate enum entry; that would have moved the choice off the driver-factory axis. Worth considering before a third Coral transport (Arrow IPC streaming ingest, eventually).
- 0.7.0's split was completed at the wrong altitude — sparse weight ownership ended up on the wrong side of the seam. 0.7.7 plumbed driver-awareness *backwards* into `sparse_encoder` to compensate, and 0.8.0 now leans on that workaround. The next time we touch this area, the goal should be to move BM25 convention ownership onto the driver and stop the encoder from reading config.

### Not deployed

prod + demo are unchanged (pgvector). The dogfood stack we built to evaluate `seahorse-db-grpc` lives outside this repo (gitignored internal manifests); details in `deploy/k8s/internal/seahorse-db/README.md` and the AKB product vault `seahorsedb-rust-on-prem-dogfood-k8s-deployment-reference.md`.

---

## 0.7.9 — 2026-06-05  *(patch — `scripts/migrate_pgvector_to_seahorsedb.py`: bulk migrate without re-embedding)*

The default driver-switch path (flip `vector_store_driver`, restart, let `embed_worker` rebuild) re-calls the embedding API for every chunk. On a production-sized vault that's real OpenRouter cost and real wall-clock time. The dense vectors are already sitting in pgvector's `vector_index.chunks` table, so we don't have to.

`backend/scripts/migrate_pgvector_to_seahorsedb.py` reads the existing dense vectors directly out of pgvector and ships them to a fresh seahorse-db table over the driver's normal `upsert_one` path:

- **Embedding API calls: 0.** Dense vectors are deterministic in `(content, model)` per `init.sql:135-138`; bulk-copy doesn't change them.
- **Sparse is re-encoded from `chunks.content`.** The pgvector path stores pre-saturated TF weights for its posting table; the seahorse-db path needs raw TF + query weight 1.0 (see 0.7.7 CHANGELOG and `sparse_encoder` module docstring). Re-running `encode_document` with `settings.vector_store_driver = "seahorse-db"` produces the correct convention.
- **Idempotent.** Re-running against a partially-migrated table is safe; `upsert_one` is idempotent on `(table_name, id)`.

### Local verification

65-chunk vault: 65/65 succeeded, 0 failed, 2.7s wall clock (~24 chunks/sec), **0 embed API calls**. Coral's `/indexes/indexed-row-count` reports 65 dense and 65 sparse after Kafka catches up (~10s). Default reindex on the same chunks would take ~30-60s of wall clock + the OpenRouter charge.

### Caveats documented inline

- Operator must flip `vector_store_driver` in `app.yaml` to `seahorse-db` BEFORE running the script — otherwise `sparse_encoder` emits pgvector-shape weights and the migration ships incorrect sparse data. The script refuses to run with a mismatched driver setting.
- Embedding-disabled rows (`dense IS NULL` in pgvector) are skipped with a warning — seahorse-db's schema rejects nullable vector columns (0.7.6 CHANGELOG).
- The script does NOT delete from pgvector. After a clean migration the operator can drop the `vector_index` schema manually; source rows are left untouched for rollback.

### Driver-side fix while we were here

`SeahorseDbStore.upsert_one` received a `numpy.float32`-typed dense array on the migration path (asyncpg's pgvector codec yields numpy arrays). `json.dumps` doesn't know how to serialise those. The migration script casts to Python `float` at the boundary; cleaner there than plumbing numpy awareness into the driver.

### Files

- `backend/scripts/migrate_pgvector_to_seahorsedb.py` — new

### Not deployed

prod + demo unchanged (pgvector). Running this script against production is now an option an operator can reach for during a future seahorse-db evaluation — it is not yet a recommended operation.

---

## 0.7.7 — 2026-06-05  *(patch — `seahorse-db` two real bugs in our own driver, not Coral: unsigned i64 PK label + double-BM25 sparse weights)*

The Coral `error_code 500233` we filed as [SeahorseDB#433](https://github.com/dn-inc/SeahorseDB/issues/433) and the 4 retrieval-side failures from 0.7.3 (`dense`, `bm25-en`, `bm25-ko`, `isolation-B`) both turned out to be bugs in **this driver**, not in SeahorseDB. Reading Coral's source code with the actual log lines in hand isolated both in about an hour.

### Real cause #1 — unsigned i64 PK label

`_chunk_id_to_label` hashed the UUID's first 8 bytes as **unsigned**:

```python
return int.from_bytes(raw[:8], "big", signed=False)
```

Coral's JSONL ingest path parses INT64 columns through `arrow_json::Decoder`. Arrow's INT64 is signed; values > 2^63 - 1 fail the JSON-to-RecordBatch conversion with a generic `ComponentError::Arrow(_)` → HTTP 500 `error_code 500233 "Internal error"` (no row context, because the parser errors before the body is even traversed).

Random UUIDs have a high bit set in their first 8 bytes about 50% of the time. The "sustained insert load 4-7% reject rate" we observed in 0.7.3 was a sampling artifact — the **actual** rejection rate for in-flight batches was much higher; the worker just retries each chunk up to 8 times before abandoning, so most rows eventually landed once retried with the bit unset, and only the worst-luck ~5% remained unindexed past the e2e budget.

Direct check on the live PG vault that was reproducing the failure:

```
chunk_id                              label                  fits_in_i64
7471f432-c50f-47e5-9bff-7b169c24e3ec   8390756079659599845   OK
3e781cd3-49c4-4aa2-9973-f80492ba116c   4501379521358088866   OK
565c66b6-959c-4779-b09c-2205fa5c185c   6222961719499310969   OK
cf5c4205-7f7b-48ae-ab49-2fe10a5bd1f3   14941890255089518766  OVERFLOW
eb486b7b-c81c-4ebd-b74a-17998e6daa03   16953918976618679997  OVERFLOW
```

40% overflow on a sample of 5 — perfectly consistent with the ~50% prior.

**Fix**: `signed=True`. One character. The label space is still the full 64 bits; signedness has no effect on collision probability. The docstring now explains the constraint and points back to SeahorseDB#433 so the next reader doesn't reinvent the bug.

**Reported back upstream**: SeahorseDB#433's "no row context on 500233" surface complaint is mostly cosmetic now — the error type was always Arrow, but the response body's `"Internal error"` makes it hard to find. Upstream may want to enrich the error context, but the driver-side fix removes the user-facing symptom entirely.

### Real cause #2 — double-BM25 sparse weight encoding

`sparse_encoder` ships AKB's BM25 in a **pre-baked dot-product form** specifically tuned for pgvector's posting table: doc weight = saturated TF, query weight = IDF, dot = BM25 score. Dropping that into Coral's inverted index — which **also** applies BM25 saturation + computes IDF from the metadata we pass at search time — produces:

- Doc side: `BM25_saturate(saturated_TF)` → over-saturated TF, downstream relevance collapses for high-TF terms
- Query side: `IDF × IDF = IDF²` → over-weights rare terms, under-weights medium-rare ones

That's the 0.7.3 narrative's "4 retrieval failures unexplained by `vector_indexed_at` numbers" answered. The chunks were indexed; the rankings were just structurally wrong.

`seahorse-index/src/sparse/scoring.rs:13-37` shows Coral's exact BM25 component formula. `seahorse-index/src/sparse/parser.rs:158-176` confirms doc-side weight is the raw `term_frequency` parameter, expecting raw TF. No vocab management on the SeahorseDB side — caller (us) owns vocab + emits `(term_id, raw_tf)` per doc and `(term_id, 1.0)` per query term with per-term `df` in the query metadata.

**Fix**: `sparse_encoder` now branches on `settings.vector_store_driver`:

- `pgvector`, `qdrant`, `seahorse-cloud` → unchanged pre-baked encoding (saturated TF + IDF). Their stores either don't compute BM25 (pgvector/qdrant) or don't care (cloud).
- `seahorse-db` → new raw branch. Doc weight = raw TF, query weight = 1.0. Coral applies the BM25 math at search time from the (k, b, N, avgdl, df) we already ship.

Module docstring carries the convention table. `_use_raw_weights()` is the single source of truth — adding a future driver means picking which of the two columns it lives in.

### Reverses 0.7.6's "structurally unsupported" claim partially

0.7.6 closed BM25-only fallback as structurally impossible because of Coral's NOT NULL on vector columns. That part stays — `dense=None` ingest is still rejected. But the four hybrid-search failures it was lumped in with were unrelated to the NULL constraint; they were the two driver bugs above. 0.7.6 narrative now reads as overclaim — we attributed driver bugs to the catalog. Not retracting 0.7.6 in a follow-up version because 0.7.6's NOT NULL finding is still accurate; just noting here that the "4 e2e fails are recall-side" reasoning in 0.7.3 was wrong about *why* and the "BM25-only is the cause" reasoning in 0.7.6 was wrong about *which fails*.

### Verification

- `_chunk_id_to_label`: confirmed `signed=True` makes the same UUIDs that previously generated overflow labels fit in i64; backend log under `seahorse-db` mode now reads 0 × 500 across all POST /data calls (was previously 100% on a fresh table).
- `sparse_encoder._use_raw_weights()`: returns True only when `settings.vector_store_driver == "seahorse-db"`.
- Fresh local validation: 1 vault, 1 Kubernetes doc with mixed Korean/English content, raw-mode ingest:
  - `q=쿠버네티스` → Kubernetes Guide #1 (correct)
  - `q=Korean tokenization` → hello.md #1 (correct)
- 25-scenario `test_hybrid_search_e2e.sh` against the same Coral with both fixes active: **pending in background at release prep time; expected to clear the 4 prior failures.** Will append the result to this CHANGELOG entry on PR merge.

### Files

- `backend/app/services/vector_store/seahorse_db.py` — `_chunk_id_to_label` unsigned → signed, docstring expanded with the Arrow overflow explanation
- `backend/app/services/sparse_encoder.py` — new `_use_raw_weights()` selector; `encode_document` and `encode_query` each branch on it; module docstring carries the convention table

### prod / demo

Unchanged. Both still pgvector and unaffected by the seahorse-db driver work in this release.

---

## 0.7.6 — 2026-06-05  *(patch — `seahorse-db` BM25-only fallback documented as structurally unsupported)*

### What

0.7.3 left "BM25-only / dense-less path" in the gap list. After
trying to wire it in 0.7.6 we hit a structural blocker:

```bash
$ curl -X POST $CORAL/v2/tables -d '{
    "table_name": "...",
    "columns": [
        {"name": "embedding", "type": {"name": "DENSE_VECTOR", ...}, "nullable": true},
        ...
    ], ...
}'
HTTP 400
error_code 400101
"Invalid argument: Vector column 'embedding' must not be nullable"
```

`coral-models/src/api/schema.rs` says the field can carry
`nullable: true`, but the field's own docstring warns "even if this
is set to true for Vector type, the server may reject it at
validation time" — and the live Coral does exactly that on every
build we've tested. There's no honest way to insert a sparse-only
row, and therefore no honest way to retrieve one.

Three workarounds were considered and rejected:

1. **Zero-vector for dense=None**. The HNSW index treats every
   sparse-only row as equidistant from any dense query, so dense
   recall silently degrades to "random sparse subset" and fusion
   results become noise. Pretending the row had a vector when it
   doesn't is dishonest under the AKB sparse_encoder contract.
2. **`bm25_only` BOOL column + dense-leg filter**. Adds an always-on
   `WHERE bm25_only = false` to every hybrid_search and a separate
   sparse-only search path. Big driver change, but the catalog
   constraint above means we'd still have to write a synthetic
   embedding value — there's no "skip dense" Coral can express on
   insert. Same dishonest-on-disk problem as (1), with more
   moving parts.
3. **A second sparse-only table alongside the hybrid one**. Doubles
   the operational surface (two Coral tables, two segment streams,
   two indexing-state checkpoints) for a feature parity the other
   drivers expose for free. Not worth it.

### Changes

- `seahorse_db.py` module docstring now states BM25-only is
  structurally unsupported, with a citation to the catalog 400 and
  the schema.rs docstring.
- `upsert_one(dense=None)` and `hybrid_search(query_dense=None)` both
  raise `VectorStoreUnavailable` with the structural reason and the
  "use pgvector or qdrant" recommendation in the message. Same
  behavior as 0.7.3, just clearer about *why* it's permanent.
- `config.py`'s `vector_store_driver` docstring carries a new
  paragraph spelling out which drivers tolerate `embed_base_url`
  being unset/unreachable (pgvector / qdrant) and which don't
  (both seahorse drivers). Operators picking a driver now have
  the embed-API-availability dimension in front of them.

### What this means for operators

- If `embed_base_url` is reliable in your environment (OpenRouter,
  managed embed endpoint, OpenAI), `seahorse-db` is fine — every
  AKB chunk has a real embedding and the driver behaves like the
  others.
- If `embed_base_url` may be empty (self-hosted, no model yet) OR
  intermittent (degraded endpoint), `seahorse-db` will stall the
  indexing queue on `dense=None` chunks until the endpoint
  recovers. Use `pgvector` or `qdrant` if that's the failure mode
  you need to absorb.

### Not deployed

prod + demo still pgvector. The 4 e2e fails 0.7.3 reported stay open
— this release closes the BM25-only path as "won't fix in driver";
the other three (Coral 500 #SeahorseDB/433, Kafka eventual
consistency budget, retry policy) are still upstream-side.

### Verification

- `bash scripts/check.sh` — green.
- Coral live-rejection of `nullable: true` on the vector column
  confirmed by direct curl against `POST /v2/tables`. Documented
  inline.

---

## 0.7.5 — 2026-06-05  *(patch — `test_seahorse_db_e2e.sh` rewritten with content-type asserts + 0.7.3 wire formats; 13/13 against live Coral)*

### What

The 0.7.1-shipped smoke had been an early-exit skip since 0.7.2's
retraction (it had reported 6/6 PASS that were all Coral gRPC
fallbacks). 0.7.4 ships an actual smoke that exercises the wire
formats `seahorse_db.py` emits.

Every assertion now checks **HTTP status AND
`content-type: application/json`**. The same-port tonic gRPC
fallback that fooled 0.7.1 returns `HTTP 200 OK` +
`content-type: application/grpc` + `grpc-status: 12` on unmatched
REST paths; the content-type check makes that impossible to confuse
with a real PASS again.

### Scope

7 stages, 13 assertions, against the 0.7.3 corrected wire formats:

| # | Assertion | Why |
|---|---|---|
| 1 | `GET /health` 200 + json | reachability + no gRPC fallback |
| 2 | `POST /v2/tables` 200 + json | the exact `CreateTableRequest` shape `SeahorseDbStore._build_create_table_payload()` emits (flat `table_name`, SCREAMING_SNAKE column types, `segmentation` hash/single, `indexes` hnsw + inverted with `sparse_model=bm25`) |
| 3 | `GET /v2/tables/{name}` 200 + json, plus `sparse_model=bm25` and `segmentation`/`primary_key` round-trip in body | mirrors `ensure_collection`'s probe + verifies the create stuck |
| 3b | `GET /v2/tables/{missing}` 404 + json | negative — 0.7.1's bug was treating 200 + grpc-status as existence |
| 4 | `POST /v2/tables/{name}/data` with `application/x-ndjson` 200 + json | JSONL is the only accepted content-type |
| 4b | same `POST` with `application/json` 400 + `Unsupported Content-Type` | if a future Coral starts accepting JSON the driver's pinned content-type silently underspecifies — this assertion catches that |
| 5 | `POST /data/delete` with `{"delete_condition": "chunk_id = '...'"}` 200 + json, twice | SQL WHERE clause + idempotency |
| 6 | `POST /data/hybrid-search` with dense+sparse configs + BM25 `parameters`+`metadata` + `fusion` 200 + json; `body.data.data` parses as `list[list[hit]]` | the response envelope shape the driver actually reads |
| 7 | `DELETE /v2/tables/{name}` 200 + json | cleanup; `trap EXIT` guard against test aborts |

`SEAHORSEDB_CORAL_URL` unset → script exits 0 with a help blurb. CI
still skips cleanly; developers with a local Coral run it.

### Verified locally against a Coral built from `SDDEV-244/monorepo-coral-sparse`

**13/13 PASS.** Every response checked for `application/json`;
zero gRPC fallbacks observed.

### Not covered (by design)

- AKB-side end-to-end search relevance — that's `test_hybrid_search_e2e.sh`
  pointed at a backend running `vector_store_driver: seahorse-db`,
  which 0.7.3 reports as 21/25 against the same Coral. Driver wire
  correctness vs retrieval recall are different gates.
- POST → search visibility lag — Kafka eventual consistency is upstream
  of this driver; AKB's own e2e budget owns the wait window.

### Files

- `backend/tests/test_seahorse_db_e2e.sh` — rewritten from scratch
  with two reusable helpers (`coral_call`, `ndjson_post`) that bake
  the content-type assertion into every call. `trap EXIT cleanup_table`
  guards against mid-script aborts leaving the smoke's ephemeral
  table behind.

---

## 0.7.4 — 2026-06-05  *(patch — pgvector driver creates the `vector` extension before registering the asyncpg codec; fixes "unknown type: public.vector" on fresh self-hosted DBs, #117)*

### Semantic search silently returned empty on a fresh self-hosted install

A self-hosted reporter (#117) got zero semantic-search hits on a
freshly stood-up stack. The backend log showed the embedding endpoint
answering `200 OK` but every search degrading to empty:

```
WARNING akb.search: vector hybrid_search failed (unknown type: public.vector); returning empty
```

**Root cause — codec registered before the extension exists.** The
`pgvector` driver registers pgvector's asyncpg binary codec
(`register_vector` → `set_type_codec('vector', schema='public', …)`)
*before* it runs `CREATE EXTENSION IF NOT EXISTS vector`. asyncpg can't
build a codec for a type that isn't in the catalog yet, so it raises
`ValueError: unknown type: public.vector`. That's a `ValueError`, not an
`asyncpg.PostgresError`, so it slipped past `ensure_collection`'s except
clause and resurfaced at every `hybrid_search`, where `search_service`
catches it and returns empty.

The trap is sharpest on the default `pgvector/pgvector` image: the
extension is *available* but not *created* in a fresh app DB, and the
one place that would create it (`_do_ensure`) ran *after* the codec
registration that needed it — a chicken-and-egg that made the first
`ensure_collection` fail on itself.

Both codec-registration sites are now ordered after extension creation:

- **Own pool** (separate `vector_store_dsn`): a new
  `_bootstrap_extension()` runs `CREATE EXTENSION IF NOT EXISTS vector`
  over a one-off connection *before* the pool — whose `init` callback
  registers the codec — is built.
- **Shared main pool** (blank DSN): inside `ensure_collection`, the
  codec is registered *after* `_do_ensure` (whose first statement is
  the `CREATE EXTENSION`), within the same transaction/connection that
  already sees its own uncommitted DDL.

Regression test `tests/test_pgvector_ext_bootstrap_e2e.py` exercises the
real `PgvectorStore` against a throwaway DB with no `vector` extension
in both pool modes: it failed with the exact `unknown type:
public.vector` before this change and passes (extension installed +
dense round-trip + `hybrid_search` returns the chunk) after. Like the
other DB-backed e2e suites it's excluded from the no-DB CI unit job and
runs locally / against a deployment.

No schema or data migration. Operators who already worked around this
by hand (`CREATE EXTENSION vector` + restart) are unaffected; the
extension creation is idempotent.

## 0.7.3 — 2026-06-05  *(patch — `seahorse-db` BM25 metadata + `sparse_model=bm25` index param; AKB hybrid_search e2e 21/25 against live SeahorseDB)*

### `sparse_model: "bm25"` is required on the INVERTED index

0.7.2 emitted `{"type": "inverted", "column": "sparse"}` with no
`params` on the sparse index. Coral accepts the table without
complaint at create time, but `POST /v2/tables/{name}/data/hybrid-search`
later fails with:

```
HTTP 503
error_code 503101
message: "BM25 sparse scoring requires sparse_model=bm25 index"
```

0.7.3 pins the param explicitly:

```python
{
    "type": "inverted",
    "column": "sparse",
    "params": {"sparse_model": "bm25"},
}
```

This means **every `seahorse-db` table created by 0.7.0/0.7.1/0.7.2 is
unusable for hybrid_search** — there is no online migration; the
table has to be dropped and recreated by the driver on next startup.
At time of writing, no production or demo cluster runs this driver,
so the practical blast radius is zero. (If you've been experimenting
locally: drop the table on Coral and `UPDATE chunks SET
vector_indexed_at = NULL` to re-emit.)

### BM25 metadata is now sent per query

0.7.2 omitted `parameters` and `metadata` on the sparse leg of
`hybrid-search`, expecting Coral to fall back to reasonable defaults.
That worked for the basic-shape e2e but produced empty or
wrong-doc results on the AKB hybrid-search e2e's English-keyword and
Korean-keyword scenarios. 0.7.3 ships our corpus stats every query:

- `parameters: {k, b}` — pulled from `bm25_stats` (the values AKB
  uses for ingest-side encoding, kept consistent with retrieval).
- `metadata.N` and `metadata.avgdl` — same source.
- `metadata.df` — per-term document frequencies from `bm25_vocab`,
  one PG batch fetch per search (~ms even at our scale). Coral
  takes them as a `Vec<String>` of `"term_id:df term_id:df ..."`,
  one entry per query vector.

One extra `await sparse_encoder.load_df_for_terms(...)` per search
call. Cheap; the alternative is shipping the full df table to Coral
once and hoping it doesn't drift.

### AKB `test_hybrid_search_e2e.sh` against live `seahorse-db`

First real run of AKB's 25-scenario hybrid retrieval e2e with the
driver pointed at a live Coral (built from SeahorseDB monorepo's
`SDDEV-244/monorepo-coral-sparse` branch, infra-only scenario kept
up via `--no-cleanup`). **21/25 PASS** vs **25/25 PASS** for the
same backend running pgvector against the same chunks.

Failures (all four are recall-side, not driver-side):

- `dense: expected PostgreSQL doc, got: <empty>`
- `bm25-en: expected GraphQL doc, got: <empty>`
- `bm25-ko: expected Kubernetes doc, got: hybrid-a-… Guide`
- `isolation-B: expected vault B doc, got: <empty>`

Two interacting causes:

1. **Coral 500 `error_code 500233` under sustained single-row
   JSONL load** — ~4–7% of `POST /v2/tables/{name}/data` requests
   come back 500 with no row-level reason, only `tower_http`
   "response failed" in Coral's log. Reproduces from AKB's
   `embed_worker` pacing (~10/sec, serial-per-process); does NOT
   reproduce from direct curl loops (single, dup PK, 30-concurrent
   all 100% OK). The chunks that hit retry_count >= 8 stay
   `vector_indexed_at IS NULL` past the e2e's 180s
   `wait_for_indexing` budget, so the docs they belong to never
   become searchable.
   Filed upstream as [SeahorseDB#433](https://github.com/dn-inc/SeahorseDB/issues/433)
   with a standalone Python repro.

2. **Kafka eventual consistency** — `POST /data` returns on
   Kafka-accept, not on segment-visible. Even chunks that DO get
   through can be invisible to search for ~10-30s. AKB's
   `embed_worker` marks `vector_indexed_at` at the same point, so a
   doc you just put may not show up immediately. Same shape any
   async-indexing driver has, and not strictly a regression — but
   the 180s e2e budget assumed pgvector's "indexed = visible"
   semantics.

### Where this leaves the driver

`seahorse-db` driver now passes the bulk of AKB's retrieval workload
end-to-end, but is **not** at pgvector-parity yet. Honest gaps still
in:

- The four e2e scenarios above
- BM25-only / dense-less path raises `VectorStoreUnavailable`
- Cross-process race-safety on `ensure_collection` ⚠
- `test_seahorse_db_e2e.sh` is still an early-exit skip (will be
  rewritten with content-type asserts + 0.7.3 wire formats next)
- `embed_worker` retry semantics need a Coral 500 backoff /
  abandon policy distinct from "vector store unavailable"

### Verification

- `bash scripts/check.sh` — green.
- `vector_store_driver: seahorse-db` startup, `ensure_collection`
  with new sparse_model:bm25 schema.
- One doc put → embed_worker → Coral; one search → driver
  hybrid-search → 2 real hits with BM25 metadata in the payload.
- `bash backend/tests/test_hybrid_search_e2e.sh` — 21/25 (vs
  pgvector 25/25).

---

## 0.7.2 — 2026-06-05  *(patch — `seahorse-db` driver wire format actually verified end-to-end; retracts 0.7.0/0.7.1 false-positive claims)*

### Retraction of 0.7.0 + 0.7.1 status claims

0.7.0 shipped the `seahorse-db` driver with wire formats reverse-
engineered from `routes.rs` path strings alone. 0.7.1 added an opt-in
smoke E2E that claimed 6/6 PASS. Both releases overstated what was
verified. Running the driver against a live Coral coordinator in
this release uncovered that:

- The route prefix is `/v2/tables/...`, NOT `/v2/catalog/tables/...`
  (we had `catalog/` in there) and NOT `/catalog/tables/...` (0.7.0
  shape). Wrong prefixes fell through to the same-port gRPC
  fallback and returned `HTTP/1.1 200 OK` + `content-type:
  application/grpc` + `grpc-status: 12`. The 0.7.1 smoke checked
  status code only and counted every call as a pass.
- `POST /v2/tables` expects flat `table_name` + top-level `columns`
  (SCREAMING_SNAKE types: `INT64`, `STRING`, `{name: DENSE_VECTOR,
  element: FLOAT32, dim}`, `{name: SPARSE_VECTOR}`) + `segmentation`
  (`{strategy: hash, columns: [id], buckets: 1, composition:
  single}`) + `indexes` (lowercase `hnsw` / `inverted`). 0.7.0
  emitted nested `schema.columns` with `column_type` keys, no
  segmentation, PascalCase index types, and `distance_space: Cosine`
  — every field name and shape was off.
- HNSW only accepts `space: "ip" | "l2"`. `cosine` is rejected at
  segment build time (`Hnsw index does not support cosinespace`).
  AKB callers using cosine-equivalent retrieval must normalise to
  unit norm and use `ip`. The `seahorsedb_distance` Literal is now
  `"l2" | "ip"`.
- `POST /v2/tables/{name}/data` requires
  `Content-Type: application/x-ndjson` (JSONL). `application/json`
  fails with HTTP 400 `400102 Unsupported Content-Type for /data
  insert`. 0.7.0 sent `application/json` + a `{"records": [...]}`
  wrapper, both wrong.
- Sparse vectors are a **single string** per row
  (`"term_id:weight term_id:weight"`, space-separated), not a list
  of pairs.
- `POST /v2/tables/{name}/data/delete` payload is
  `{"delete_condition": "<SQL WHERE clause>"}`, not
  `{"labels": [u64, ...]}`.
- `POST /v2/tables/{name}/data/hybrid-search` payload uses separate
  `dense` and `sparse` config objects (each with `column`,
  `vectors`, optional `parameters`), a `fusion` block, an SQL
  `projection` string, and an SQL `filter` string. Response is
  enveloped: `body["data"]["data"][0]` is the hit list (one
  resultset per query vector).

### End-to-end verification in this release

- Backend started with `vector_store_driver: seahorse-db` pointed
  at a live Coral. Lifespan `ensure_collection` made the table:
  `GET /v2/tables/akb_validation → 404` then
  `POST /v2/tables → 200 application/json`.
- Document put via AKB REST API → `embed_worker` claimed the
  chunks → driver POSTed each row as JSONL to
  `/v2/tables/akb_validation/data` → Coral returned `200 OK` with
  `inserted_row_count: 1` per chunk.
- AKB search REST endpoint with `vault=sdb-test&q=Korean tokenization`
  → driver POSTed hybrid-search → Coral returned 2 hits with the
  expected `content`/`source_id`/`chunk_id`/`score` fields →
  AKB rendered the hits at `/api/v1/search`. **Every response
  checked for `content-type: application/json` to rule out gRPC
  fallback.**

### Driver gaps still present (honestly)

- **Dense-less (BM25-only) upsert + search not supported.** The
  embedding column is currently `nullable: false`; ingest raises
  `VectorStoreUnavailable` when `dense=None`. To match pgvector's
  BM25 fallback we'd need a NULL embedding column AND a
  sparse-only search path on Coral (single-leg vector search
  endpoint exists; not wired through this driver yet).
- **BM25 per-query metadata not sent.** Hybrid search omits
  `parameters` and `metadata` on the sparse leg; Coral falls back
  to defaults. AKB has `bm25_stats` (N, avgdl, df), so the
  retrieval quality could improve by shipping them per query.
- **Race-safety still ⚠.** No PG-advisory-lock-grade primitive
  on Coral; concurrent `ensure_collection` peers rely on Coral's
  server-side serialisation.
- **No `test_hybrid_search_e2e.sh` against seahorse-db yet.** AKB's
  25-scenario hybrid suite still runs only against pgvector. The
  driver-level smoke (`test_seahorse_db_e2e.sh`) needs its own
  rewrite to match the corrected wire formats — that's the next
  PR. Until then, "driver works on a single happy-path doc" is
  what we claim, not "production ready".
- **Kafka eventual consistency UX.** Coral's `POST /data` returns
  on Kafka-accept, not on visibility (~10-30s lag). `embed_worker`
  marks `vector_indexed_at` on insert-accept, so a doc you just
  put may not appear in search for that window. Same shape any
  async-indexing driver has, but worth knowing.

### Files changed

- `backend/app/services/vector_store/seahorse_db.py` — every wire
  format and the helpers (`_encode_sparse_string`,
  `_validate_uuid_for_sql`, `_is_grpc_fallback`). The
  `_is_grpc_fallback` content-type check is the safety net that
  prevented this miss from happening again.
- `backend/app/config.py` — `seahorsedb_distance` Literal
  `"cosine" | "l2" | "ip"` → `"l2" | "ip"` (cosine was never
  valid).

### What this PR does NOT do

- Does NOT publish a corrected `test_seahorse_db_e2e.sh` (that
  ships in the next PR with content-type assertions baked in).
- Does NOT change prod / demo — both still pgvector.
- Does NOT cover the BM25-only / sparse-only path.

---

## 0.7.1 — 2026-06-05  *(patch — `seahorse-db` driver wire-shape smoke E2E + `.gitignore` overlay slot)*

⚠ **RETRACTED by 0.7.2**: the "6/6 PASS" claim in this release is
a false positive. Every request fell through Coral's gRPC fallback
on the unmatched HTTP REST path, so the smoke validated nothing.
See 0.7.2's retraction section.

### `backend/tests/test_seahorse_db_e2e.sh`

Opt-in smoke E2E that confirms the `seahorse-db` driver's emitted wire
shapes (table create schema, dense+sparse insert payload, delete by
label) are accepted by a live Coral coordinator. Skips cleanly when
`SEAHORSEDB_CORAL_URL` is unset — CI passes without a Coral available;
developers with a local SeahorseDB stack pass the URL.

Covers:
- Coral `/health` reachability
- `POST /catalog/tables` with the exact schema `SeahorseDbStore._build_create_table_payload()` emits
- `GET /catalog/tables/{name}` (mirrors `ensure_collection`'s existence check)
- `POST /data` with one record carrying both `embedding` (dense) and `sparse` ([[term_id, weight], ...])
- `POST /data/delete` by `u64` label (matches `_chunk_id_to_label` output)
- Cleanup

Does NOT cover (intentionally):
- Hybrid-search retrieval. SeahorseDB ingest goes through Kafka before
  the row is searchable; wait windows are ~10-30s and harder to budget
  for a smoke. Retrieval flow lives in `test_hybrid_search_e2e.sh`
  against pgvector; this script asserts only "the bytes reach Coral
  in the shape the driver promised".
- Cross-process race-safety (no Coral primitive equivalent to PG
  advisory lock; ⚠ in `base.py` audit table).

Verified locally against a Coral built from the SeahorseDB monorepo's
`cloud-functional-test` scenario: **6/6 PASS**.

### `.gitignore` — `docker-compose.override.yml` slot

Added `docker-compose.override.yml` + `docker-compose.*.override.yml`
to `.gitignore`. `docker compose` auto-merges any `*.override.yml` in
CWD on top of the base — this lets developers wire AKB backend to a
local SeahorseDB stack (or any other private-image stack) without that
overlay leaking into the tracked compose, which has to stay OSS-friendly.
SeahorseDB images are dn-inc commercial and aren't pullable by external
users, so the overlay path is local-only by design.

### Verification

- `bash scripts/check.sh` — green.
- `SEAHORSEDB_CORAL_URL=http://localhost:NNNN bash backend/tests/test_seahorse_db_e2e.sh` — 6/6 against live Coral.
- Without env var — skips cleanly (exit 0).

---

## 0.7.0 — 2026-06-05  *(minor — split `seahorse` driver into `seahorse-cloud` + new self-hosted `seahorse-db`)*

### What

Two vector-store drivers under the Seahorse brand, separated:

- **`seahorse-cloud`** (renamed from `seahorse`) — talks to the
  managed Seahorse Cloud BFF + per-table data-plane host. Zero
  infrastructure to run; you supply tenant + token + table.
- **`seahorse-db`** (new) — talks to a self-hosted SeahorseDB cluster
  via its Coral coordinator HTTP API. You run Coral + Writer +
  Reader(s) + Redis + Kafka + sparse-embedding-server yourself
  (SeahorseDB monorepo's `deploy/docker-compose.yml` brings up a
  minimal stack on one box).

The pre-0.7.0 single `seahorse` driver value implicitly meant
cloud. Keeping the name ambiguous would have been a footgun the
moment a self-hosted user filled in `seahorsedb_coordinator_url`
expecting the same enum value to "just work".

### Driver matrix now

| `vector_store_driver` | required settings                                       |
| --------------------- | ------------------------------------------------------- |
| `qdrant`              | `vector_url` (+ optional `vector_api_key`)              |
| `pgvector`            | `vector_store_dsn` blank reuses main PG                 |
| `seahorse-cloud`      | `seahorse_cloud_token` + `seahorse_cloud_tenant_uuid` + |
|                       | one of `..._table_name` / `..._table_uuid`              |
| `seahorse-db`         | `seahorsedb_coordinator_url` (Coral HTTP API)           |

### Config migration (BREAKING)

If you were using the `seahorse` driver pre-0.7.0:

```diff
-vector_store_driver: seahorse
-seahorse_management_url: "https://console.seahorse.dnotitia.ai/bff"
-seahorse_token: "shsk_..."
-seahorse_tenant_uuid: "..."
-seahorse_table_name: "..."
-seahorse_auto_create: true
+vector_store_driver: seahorse-cloud
+seahorse_cloud_management_url: "https://console.seahorse.dnotitia.ai/bff"
+seahorse_cloud_token: "shsk_..."
+seahorse_cloud_tenant_uuid: "..."
+seahorse_cloud_table_name: "..."
+seahorse_cloud_auto_create: true
```

`pydantic` config has `extra="forbid"`, so an un-migrated `seahorse_*`
key fails the app launch loudly with the bad key name — no silent
fallback. That's intentional; a half-migrated config silently
sending writes to the wrong driver is the worst possible outcome.

Default driver is unchanged (`qdrant`). prod + demo run `pgvector`,
so neither is affected by this rename.

### New driver implementation notes

- Surface area: 5 methods (`ensure_collection`, `health`, `upsert_one`,
  `delete_point`, `hybrid_search`) — same Protocol as the other drivers,
  no service-layer changes needed.
- Coral endpoint mapping is documented at the top of
  `backend/app/services/vector_store/seahorse_db.py`.
- Sparse encoding: AKB's parallel `sparse_indices` + `sparse_values`
  arrays are zipped into Coral's `[[term_id, weight], ...]` shape at
  the driver boundary. The rest of AKB stays Coral-shape-free.
- Label mapping: SeahorseDB identifies records by `u64` labels; AKB
  uses UUID `chunk_id`. We take the first 8 bytes of the UUID
  (big-endian). Birthday-paradox safe to ~2^32 chunks per table.
- Eventual consistency: Coral's `POST /data` returns on Kafka-accept,
  not on visibility. `embed_worker` marks `vector_indexed_at` at the
  same point — the search-visibility gap (~10-30s) is the same
  asymmetry any async-indexing pipeline has.
- Race-safety: documented as ⚠ in `base.py`'s audit table; Coral
  serializes concurrent `POST /catalog/tables` server-side so peer
  startups mostly converge, but no advisory-lock-grade guarantee
  like pgvector.

### What this PR does NOT do

- No production migration. prod + demo stay on `pgvector`. A future
  PR may add an opt-in seahorse-db validation tier; that's a separate
  decision based on operational ergonomics of running the 7-container
  SeahorseDB stack.
- No integration test against a live Coral. The driver is type-checked
  and unit-friendly; an e2e is the next PR once we decide where the
  SeahorseDB stack lives (host machine vs CI runner vs neither).
- No proxy/frontend change.

### Verification

- `bash scripts/check.sh` — green (ruff + mypy + tsc + vitest + secrets).
- `mypy --python-version 3.14 app/services/vector_store/` —
  `Success: no issues found in 7 source files`.

---

## 0.6.6 — 2026-06-05  *(patch — clear the 18 mypy errors 0.6.5 exposed + exact-pin runtime deps)*

The 0.6.5 Python 3.11 → 3.14 bump closed the "CI runs a different
analyzer than dev" gap but left 18 mypy errors actually surfaced —
the older runtime had been hiding them via wider stub inference.
This release works through every one of them and pins runtime deps
so the image's behavior is also reproducible.

### Cleared 18 mypy errors (no behavior change)

- `app/services/git_service.py` (14): `commit.tree.traverse()` yields
  `Tree | Blob | Submodule | tuple` and we were narrowing with
  `item.type == "blob"`. Replaced with `isinstance(item, Blob)`, which
  both narrows for the type checker AND skips submodule pointers /
  nested trees explicitly. `repo.iter_commits(**kwargs)` — gitpython's
  stub forbids `**kwargs` splatting (each named param is individually
  typed); spelled out the argument shapes. `c.message` is typed as
  `str | bytes`; normalised via `str(...)`. `d.b_path`/`d.a_path` and
  `item.path` come back as `str | PathLike[str] | None`; made the str
  narrowing explicit with `str(...)` + None-skip.
- `app/services/events_publisher.py` (1): `redis-py`'s `xadd` stub
  takes the wider `dict[bytes|bytearray|memoryview|str|int|float, …]`;
  dict invariance means our narrower `dict[bytes, bytes]` was
  rejected. Widened the helper's return type and added a
  `cast(dict, fields)` at the call site.
- `app/services/vector_store/qdrant.py` (1): qdrant-client 1.13+
  narrowed `timeout` to `int | None` even though the runtime still
  accepts floats. Changed `30.0` → `30`.
- `mcp_server/server.py` (2): `_handle_get` reused the identifier
  `doc` for two different shapes — a `dict` in the version branch, a
  `DocumentResponse` on the un-versioned path. Renamed the latter to
  `response`.

`mypy --python-version 3.14` now reports `Success: no issues found
in 119 source files`.

### Runtime deps exact-pinned

The main `dependencies` block in `backend/pyproject.toml` was
floor-only (`>=`); a silent resolver upgrade could swap backend image
behavior on a re-deploy. Bumps are now an explicit code change. Pins
captured from the resolved set in the 0.6.5 image (post Python 3.14
base rebuild) and verified end-to-end:
- `fastapi==0.136.3`, `uvicorn[standard]==0.49.0`, `pydantic==2.13.4`
- `asyncpg==0.31.0`, `pgvector==0.4.2`, `gitpython==3.1.50`
- `python-frontmatter==1.3.0`, `httpx==0.28.1`, `mcp[cli]==1.27.2`
- `pyyaml==6.0.3`, `python-multipart==0.0.32`, `pyjwt==2.13.0`
- `bcrypt==5.0.0`, `boto3==1.43.23`, `qdrant-client==1.18.0`
- `kiwipiepy==0.23.1`, `redis==8.0.0`

### Verification

- `bash scripts/check.sh` — green.
- `bash backend/tests/test_publications_e2e.sh` — 97/97.
- `bash backend/tests/test_mcp_e2e.sh` — 76/76.
- `bash backend/tests/test_hybrid_search_e2e.sh` — 25/25.

---

## 0.6.5 — 2026-06-05  *(patch — pin static-analysis tooling + Python 3.11 → 3.14)*

### Python 3.11 → 3.14

`backend/Dockerfile`'s `FROM python:3.11-slim` was four years stale
(3.11 released 2022-10) and starting to gate us out of newer typing
features and faster MCP / pgvector library cuts. Bumped to
`python:3.14-slim` end-to-end:

- `backend/Dockerfile`: `python:3.11-slim` → `python:3.14-slim`.
- `backend/pyproject.toml`: `requires-python = ">=3.11"` → `>=3.14`,
  `[tool.mypy] python_version = "3.11"` → `"3.14"`,
  `[tool.ruff] target-version = "py311"` → `"py314"`.
- `.github/workflows/check.yml` + `backend-pytest.yml`:
  `python-version: '3.11'` → `'3.14'`.
- `scripts/check.sh` mypy `--python-version 3.11` → `3.14`.

Dependency-wheel check before pulling the trigger:
- `kiwipiepy 0.23.1` — Cython native binding; ships cp314 wheels for
  Linux x86_64/aarch64, macOS, Windows. ✓
- `asyncpg 0.31.0`, `bcrypt 5.0.0` — native; ship cp314. ✓
- `pgvector 0.4.2`, `pyjwt 2.13.0`, `mcp 1.27.2` — pure-python sdist,
  Python-version-agnostic. ✓

Local docker-compose build of `python:3.14-slim`-based backend image
succeeded; all three regression suites passed against the rebuilt
backend:
- `test_publications_e2e.sh` — 97/97
- `test_mcp_e2e.sh` — 76/76
- `test_hybrid_search_e2e.sh` — 25/25

### Static-analysis tooling pin

`backend/pyproject.toml`'s `dev` deps and `.github/workflows/check.yml`'s
tool install line both used floor-only version specifiers (`>=`) for
`mypy`, `ruff`, `bandit`, and `detect-secrets`. A silent upgrade of one
of those tools (or their transitive type-stub deps) could flip the same
commit's CI from pass→fail or fail→pass on a re-run, without anyone
noticing the gate had changed under them.

This is exactly the shape of the 0.6.4 surprise: local `mypy 2.1.0`
showed 18 errors on the exact ref CI marked green. Both ends ran
`mypy 2.1.0`, but the runtime Python (CI 3.11 vs local 3.13) and the
lack of any explicit stub pin made the analyzer reach different
conclusions. We caught it by accident because of an unrelated production
incident — the gate that's supposed to catch type regressions wasn't
itself reproducible.

### How

- `backend/pyproject.toml`'s `dev` deps are exact-pinned: `pytest==9.0.3`,
  `pytest-asyncio==1.3.0`, `ruff==0.15.16`, `mypy==2.1.0`,
  `bandit[toml]==1.9.4`.
- `.github/workflows/check.yml` mirrors the same exact pins on its
  direct install line (it pre-dates the pyproject `dev` group and
  installs the four tools directly, not via `pip install -e '.[dev]'`,
  so it has to keep its own list in sync — comment added).
- `backend-pytest.yml` already uses `pip install -e '.[dev]'`, so it
  picks up the pyproject pins automatically.

Bumps to any of these tools are now an explicit code change, not a
`pip install` side-effect.

### Honest scope note

This release does **not** fix the 18 local mypy errors in
`git_service.py` / `mcp_server/server.py` / `qdrant.py` /
`events_publisher.py` that the Python 3.11-vs-3.13 mismatch surfaced.
Those need a separate look — most appear to be real type ambiguities in
code we didn't touch in the 0.6.x line. Tracked as a follow-up.

Only the four lint/type/sec tools are pinned. Runtime deps in the main
`dependencies` block are still floor-only. The risk profile is asymmetric
— a runtime-dep silent upgrade tends to fail loud (import errors, API
mismatch), while an analyzer silent upgrade fails quiet (gate changes
meaning). Starting where the silent-failure risk is highest; runtime
pinning is a separate follow-up worth doing once we have a lock-file
workflow.

### Verification

- `pip install -e '.[dev]'` resolves to the exact versions above.
- CI install step uses the same pins.
- 0.6.4 prod incident's reproducibility gap is closed for this category
  of failure.

---

## 0.6.4 — 2026-06-05  *(patch — `ensure_collection` race-safety, post-mortem of a real prod incident)*

### What happened

The 0.6.2 BM25-fallback feature changed `vector_index.chunks.dense`
from `NOT NULL` to nullable and the HNSW index from full to partial
(`WHERE dense IS NOT NULL`). On the prod rollout the migration step
in `PgvectorStore.ensure_collection` ran on a 350k-chunks table and
needed ~533 MB of shared memory for the HNSW graph build — more
than the 512 Mi `/dev/shm` the postgres pod had been given. The
`CREATE INDEX` aborted mid-build, leaving the schema with **no
dense index at all**. Every search after that hit a seq scan and
timed out (504 nginx); the timeout cancelled the in-flight
`ensure_collection`, the next request found
`_ensured_collection=False` and started another build, and so on
ad infinitum.

Even at a single uvicorn worker the cancel-driven retry looked
exactly like multi-process contention: four concurrent CREATE
INDEX statements visible in `pg_stat_activity`, none ever
committing.

Manual recovery: scale backend to 0, terminate the in-flight
builds, build the index once from a psql session with
`maintenance_work_mem='2GB'`, scale backend back to 1. None of
this was the user's job.

### The fixes

1. **`PgvectorStore.ensure_collection` is now race-safe across
   processes.** Three layers:
   - instance flag (hot-path bool read, unchanged)
   - `asyncio.Lock` (same-process callers serialize, unchanged)
   - **new:** `pg_advisory_xact_lock(hash(schema))` (cross-process
     callers serialize on a transaction-scoped PG lock that
     auto-releases on commit/rollback/conn-close — a cancelled
     CREATE INDEX cannot strand it).
2. **Index swap is now atomic.** Pre-0.6.4 did `DROP legacy
   idx_vi_chunks_dense` then `CREATE partial idx_vi_chunks_dense`;
   a failure between the two left the schema dense-index-less.
   Now: `CREATE partial idx_vi_chunks_dense_new` → `DROP legacy
   idx_vi_chunks_dense` → `RENAME idx_vi_chunks_dense_new →
   idx_vi_chunks_dense`, all inside the advisory-lock tx. If the
   `CREATE` fails the legacy index stays in place and search
   keeps working with the full-form behavior.
3. **`maintenance_work_mem` is now set per build.** The new
   `_build_partial_hnsw` does `SET LOCAL maintenance_work_mem =
   '2GB'` before issuing the `CREATE INDEX`. At our scale
   (a few hundred K rows × 1024-dim vectors) HNSW peaks well
   above the PG default 64 MB.
4. **`deploy/k8s/postgres.yaml` `/dev/shm` default is now 4Gi**
   (was 512Mi). Doc-comment records the trade-off for operators
   on much larger corpora.
5. **`backend/tests/concurrency/test_ensure_collection_race_unit.py`**
   — three pytest regressions against a live Postgres
   (`AKB_TEST_DSN`): N concurrent callers serialize, legacy →
   partial swap is atomic, idempotent on already-partial schemas.
   Skips when no PG is reachable; docker-compose locally passes
   3/3.

### Process gaps that let this ship

The 0.6.2 PR's verification was `test_publications_e2e.sh` 97/97 +
`test_mcp_e2e.sh` 76/76. Both pass through small ephemeral vaults
with under a hundred chunks — the HNSW rebuild was sub-second on
that data, so neither the OOM nor the cancel race ever surfaced.
`test_hybrid_search_e2e.sh` exists but is not part of the default
PR gate; we didn't run it. And the prod smoke after each deploy
exercised publications only, not search.

This release's verification section explicitly lists
`test_hybrid_search_e2e.sh` and prod search-smoke alongside the
publications regressions; vector_store / embed_worker /
sparse_encoder changes should run all three. The new pytest
regressions catch the specific ensure_collection bug shape but
they are necessarily a narrow guard — a future indexing-side
change will only be caught by exercising search end-to-end.

### Honest scope note

This release does not introduce any new feature and does not
change any external contract. It changes the schema migration
inside `ensure_collection` to be forward-progress-safe under
adversarial conditions (cancel storms, undersized `/dev/shm`),
ships the corresponding `/dev/shm` deploy default, and adds a
regression that would have caught the original failure. The
0.6.2/0.6.3 BM25 fallback behavior is unchanged.

### Verification

- `bash scripts/check.sh` — green.
- `pytest tests/concurrency/test_ensure_collection_race_unit.py` —
  3/3 (AKB_TEST_DSN set to docker-compose PG).
- `bash backend/tests/test_publications_e2e.sh` — 97/97.
- `bash backend/tests/test_mcp_e2e.sh` — 76/76.
- `bash backend/tests/test_hybrid_search_e2e.sh` against
  docker-compose local — **operator should run this for any
  pgvector / embed_worker / sparse_encoder change**; counts as
  release gate from 0.6.4 on.

---

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
