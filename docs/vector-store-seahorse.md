# Vector store — Seahorse Cloud

This guide walks through wiring AKB to a managed
[Seahorse Cloud](https://console.seahorse.dnotitia.ai) table as the
hybrid (dense + BM25 sparse) vector index, in place of the default
in-cluster pgvector or an external Qdrant.

When this is the right pick:

- You don't want to operate a vector store yourself (no extra
  container, no scaling decisions, no upgrades).
- Your AKB deployment is on a network that can reach
  `console.seahorse.dnotitia.ai` (BFF) **and** the per-table data-plane
  host that Seahorse provisions per table.
- You're OK with the embedding model being constrained to dimensions
  that match a Seahorse table you've already created (or that the
  driver auto-creates on first boot).

If those don't apply, prefer `pgvector` (default) or `qdrant`.

## 1. Sign up & create a tenant

1. Open <https://console.seahorse.dnotitia.ai> and sign up.
2. After login, the console drops you into a tenant. The tenant UUID
   appears in the URL of the database/tables page, e.g.
   `…/main/database/tables?tenant=37cfb12f-7093-412f-8926-b5c4fba4356c`.
   Copy this — it's the `seahorse_tenant_uuid` you'll set later.

## 2. Issue an API token

1. In the console, open *API Keys* (or equivalent — the UI labels this
   as a personal access token). Generate a new token; the value starts
   with `shsk_`.
2. Save it somewhere safe — Seahorse only displays it once.

The driver authenticates with `Authorization: Bearer shsk_…`. Even
though the OpenAPI spec advertises an `api-key` header alternative,
only the bearer scheme is accepted by the live gateway.

## 3. Create the table

You have two options.

### Option A — Auto-create (recommended for first-time setup)

Set `seahorse_auto_create: true` in `app.yaml` and pick a
`seahorse_table_name`. On first boot AKB calls the BFF and
provisions a table with exactly the schema it needs (columns,
indexes, segmentation). No manual schema work.

Caveats:

- Auto-create runs **once** when the table doesn't exist. After that
  the driver only verifies the schema; if you change
  `embed_dimensions` later, recreate the table by hand (auto-create
  won't migrate an existing table).
- If your tenant is locked down to console-only DDL, this won't
  succeed — fall back to Option B.

### Option B — Create manually in the console

Provision a table whose schema matches what AKB expects. The
driver's `_validate_schema` enforces the required columns and the
`dense_vector` dimension; everything else is checked loosely.

Required columns (all `STRING` unless noted):

| Column | Type | Nullable | Notes |
|---|---|---|---|
| `id` | STRING | NO | Primary key. Seahorse-format `<source_id>\x1e<chunk_index>` (record separator + uint64). The driver builds it; you only need to declare the column as PK. |
| `external_chunk_id` | STRING | NO | AKB's own chunk UUID. The driver deletes by this column. |
| `source_type` | STRING | NO | `document` / `table` / `file`. |
| `source_id` | STRING | NO | AKB document/source identifier. Used for `WHERE source_id IN (…)` filters at search time. |
| `section_path` | STRING | YES | Heading path (e.g. `# Intro > ## Background`). |
| `content` | STRING | NO | Chunk text; returned in search projection. |
| `chunk_index` | INT64 | NO | 0-based position of the chunk inside its source. |
| `dense_vector` | VECTOR (FLOAT32, dim = `embed_dimensions`) | NO | Indexed `DISKBASED`, `space=ip`, `M=16`, `ef_construction=128`. |
| `sparse_vector` | SPARSE_VECTOR | NO | Indexed `INVERTED`, `sparse_model=bm25`. AKB sends `"i:v i:v …"` strings (term_id:weight, IDF baked in). |

Segmentation: hash on `id`, 1 bucket, single composition.
The exact JSON the auto-create path posts is in
`backend/app/services/vector_store/seahorse.py` (`_create_table`) if
you want to mirror it via the console or BFF directly.

The dense dim **must equal** your `embed_dimensions` in `app.yaml`.
Mismatches are caught at startup (`ensure_collection` fails with a
clear message) but only after the table has been used, so set this
correctly the first time.

## 4. Configure AKB

`config/app.yaml`:

```yaml
# Switch the driver
vector_store_driver: seahorse

# Embedding dim must match the table's dense_vector dim
embed_base_url: https://api.openai.com/v1   # or your provider
embed_model: text-embedding-3-small
embed_dimensions: 1536

# Seahorse driver settings
seahorse_management_url: "https://console.seahorse.dnotitia.ai/bff"
seahorse_tenant_uuid: "37cfb12f-…"            # from step 1
seahorse_table_name: "akb_chunks"             # one of (name, uuid)
# seahorse_table_uuid: ""                     # alternative to name
seahorse_auto_create: false                   # true on first boot if going Option A
```

`config/secret.yaml`:

```yaml
seahorse_token: "shsk_…"   # from step 2
```

You don't need any extra docker-compose container for Seahorse — the
default 3-container stack (`postgres` + `backend` + `frontend`) is
all that runs locally; the backend reaches out to Seahorse Cloud
over HTTPS.

## 5. Bring it up

```bash
docker compose up -d
docker compose logs -f backend | grep -i seahorse
```

On boot you should see (only when `auto_create: true` and the table
doesn't yet exist):

```
INFO  akb.vector_store.seahorse: Seahorse table auto-created: name=akb_chunks tenant=37cfb12f-…
```

Subsequent boots silently call `ensure_collection` and validate the
schema.

Smoke-test from the UI: create a vault, put a document, run a
search. If hybrid search returns results, the round-trip is working.

## 6. Reference

For the API reverse-engineering writeup (raw curl for each verb,
auth gotchas, BM25 parameter caveat, PK format derivation), see the
AKB product vault doc:

> `seahorse-cloud/engineering/seahorse-cloud-hybrid-vector-table-api-사용법` —
> doc id `d-f25fb2f7`.

Driver source: `backend/app/services/vector_store/seahorse.py`.

## Caveats / known gotchas

- **No `parameters` on the sparse leg.** AKB's BM25 encoder bakes IDF
  into the sparse weights, so the driver omits `parameters.k/b` on
  search. Sending them would force the server to demand corpus
  metadata (`N`, `avgdl`, `df`) per query, which AKB doesn't track in
  Seahorse's expected shape.
- **Insert is `Content-Type: text/plain` (JSONL).** `application/json`
  is rejected on `/v2/data` — this is a Seahorse contract, not an
  AKB choice.
- **Indexing latency.** A document indexed in AKB is searchable
  almost-instantly with pgvector/qdrant; Seahorse has its own
  build-time. Expect O(seconds–tens of seconds) before a freshly
  inserted chunk surfaces in `akb_search`. This is a server-side
  property, not the driver's behaviour.
- **Per-table data-plane host.** Each table gets its own subdomain
  (returned in the BFF table descriptor as `host_name`). The driver
  caches it on `ensure_collection`; if Seahorse rotates a host, a
  pod restart picks up the new value.
- **Bearer-only auth.** The spec mentions an `api-key` header — that
  scheme returns 401 on the live gateway. Use the bearer token only.
