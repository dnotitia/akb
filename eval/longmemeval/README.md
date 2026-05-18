# LongMemEval benchmark — AKB runner

Measures `akb_search` retrieval recall on the public
[`xiaowu0162/longmemeval`](https://huggingface.co/datasets/xiaowu0162/longmemeval)
`_s` split (500 questions). The goal is to put one honest line —
`akb-hybrid+rerank` — next to [gbrain-evals](https://github.com/garrytan/gbrain-evals)
and MemPalace on the same dataset.

Reference lines as of 2026-05-07:

| System                          | R@5    | k | n   | LLM in retrieval | Source |
|---------------------------------|--------|---|-----|-------------------|--------|
| gbrain-hybrid                   | 97.60% | 5 | 500 | no  | [gbrain-evals](https://github.com/garrytan/gbrain-evals/blob/main/docs/benchmarks/2026-05-07-longmemeval-s.md) |
| gbrain-vector                   | 97.40% | 5 | 500 | no  | same |
| MemPal hybrid+rerank (held-out) | 98.4%  | 5 | 450 | yes | MemPalace |
| MemPal raw (ChromaDB)           | 96.6%  | 5 | 500 | no  | MemPalace headline |
| gbrain-keyword (BM25)           | 19.80% | 5 | 500 | no  | gbrain-evals |

AKB posts its own line. The embedding model differs (see
[Comparability notes](#comparability-notes)), so this is a **stack-level**
comparison rather than apples-to-apples; the result still pins where AKB's
production default lands on the same dataset.

---

## Dataset

- **Source**: HuggingFace [`xiaowu0162/longmemeval`](https://huggingface.co/datasets/xiaowu0162/longmemeval),
  `_s` split — same as gbrain-evals.  The dataset is HF-deprecated but the
  raw file at `resolve/main/longmemeval_s` is still served and is what
  gbrain's published numbers were computed against.
- **Splits**: `_s` (500 questions, ~50 sessions per haystack). `_oracle`
  (3 sessions/Q) and `_m` (200 sessions/Q) are out of scope here.
- **Format**: single JSON file (~278MB). Each question has
  `{question_id, question, question_type, question_date, haystack_dates,
   haystack_session_ids, haystack_sessions, answer_session_ids, answer}`.
- **Download**:
  ```bash
  mkdir -p ~/datasets/longmemeval
  curl -Lo ~/datasets/longmemeval/longmemeval_s.json \
    https://huggingface.co/datasets/xiaowu0162/longmemeval/resolve/main/longmemeval_s
  ```

The cleaned variant ([`xiaowu0162/longmemeval-cleaned`](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned))
exists but is reserved for a v2 round so first-round numbers stay
comparable with gbrain.

### Question types (per-type recall reported)

| Type | What it stresses |
|---|---|
| `single-session-user` | Answer lives in user turns of one session |
| `single-session-assistant` | Answer is in assistant turns — user vocabulary ≠ answer vocabulary (BM25 stress) |
| `single-session-preference` | Indirect statements ("I usually prefer X") |
| `multi-session` | Evidence spread across multiple sessions |
| `temporal-reasoning` | Requires "first time", "last time", date comparison |
| `knowledge-update` | A fact changes over time |

`temporal-reasoning` + `knowledge-update` = 211/500 (42%) need date
signals; the runner therefore prefixes each rendered session with
`[Session date: ...]` (see [Adapter mapping](#adapter-mapping)).

### Metrics

- **Primary**: `Recall@K` — top-K results contain at least one of
  `answer_session_ids`. No LLM judge; cleanly comparable.
- **Secondary**: per-type Recall@K, p50/p99 client-side query latency,
  per-question ingest wall-clock.
- **Abstention (`_abs` suffix, 30/500)**: included in the denominator.
  All `_abs` questions still have ground truth (`answer_xxx_abs`
  patterns present in the haystack), so retrieval-side measurement is
  the same shape as any other question.  The NDJSON `is_abstention`
  flag lets you re-aggregate excluding `_abs` after the fact.
- **Non-goal**: QA accuracy. LongMemEval's `evaluate_qa.py` (generation
  judge) is out of scope; a separate `akb-rag-qa` adapter would address
  it later.

---

## Stack

Self-contained `docker-compose.yaml`. Coexists with the main `akb` dev
stack — ports are shifted (`18000`/`19000`/`19001`) and the compose
project name (`longmemeval`, taken from this directory) keeps volumes
isolated. `docker compose down -v` resets only `longmemeval_*` volumes;
your main dev data is untouched.

### Effective config (pinned from the cluster's `akb-app-config`)

| Setting | Value | Note |
|---|---|---|
| `vector_store_driver` | `pgvector` | same PG instance, separate `vector_index` schema; visibility lag = 0 |
| `vector_store_sparse_shape` | `posting` | production-recommended |
| `embed_model` / `embed_dimensions` | `baai/bge-m3` / `1024` | multilingual, within pgvector HNSW 2000-dim limit |
| `embed_base_url` | `https://openrouter.ai/api/v1` | |
| `rerank_enabled` / `rerank_model` | `true` / `cohere/rerank-v3.5` | production default ON |
| `rerank_base_url` | `https://openrouter.ai/api/v1` | explicit — see [run #3](#known-issues) |
| `rerank_prefetch` | `30` | RRF top-30 → rerank → top-K |
| `bm25_k1` / `bm25_b` | `1.5` / `0.75` | standard Lucene values |

`config/app.yaml` is committed because it's part of the benchmark
definition.  Anything sensitive lives in `config/secret.yaml`
(gitignored).

### Secrets

Copy `config/secret.yaml.example` → `config/secret.yaml` and fill in:

```yaml
db_password: akb                            # matches docker-compose hardcoded value
jwt_secret: <local-only dev string>
embed_api_key: ${OPENROUTER_API_KEY}        # bge-m3 embedding
llm_api_key:   ${OPENROUTER_API_KEY}        # SAME key — rerank fallback target
rerank_api_key: ""                          # leave blank; rerank_service falls back to llm_api_key
```

**One OpenRouter key is enough.** [rerank_service.py:56](../../backend/app/services/rerank_service.py:56)
resolves the rerank URL from `rerank_base_url or llm_base_url` and the key
from `rerank_api_key or llm_api_key`.  OpenRouter routes `/v1/rerank`
calls to `cohere/rerank-v3.5` — verified live against the cluster's
backend pod.  Putting a Cohere key under `rerank_api_key` would send a
Cohere credential to the OpenRouter URL → 401 → backend silently falls
back to RRF-only output, making the `akb-hybrid+rerank` label a lie.

---

## Setup and smoke test

```bash
cd eval/longmemeval
cp config/secret.yaml.example config/secret.yaml
# edit config/secret.yaml — fill embed_api_key and llm_api_key with the same OpenRouter key
docker compose up -d --build                  # first run takes a few minutes for backend build
curl http://localhost:18000/livez             # {"status":"alive"}
curl http://localhost:18000/readyz            # {"status":"ready", ...}

cd ../..
python3 eval/longmemeval/run.py \
  --dataset ~/datasets/longmemeval/longmemeval_s.json \
  --ndjson eval/reports/smoke.ndjson \
  --limit 5
```

Expected smoke output (current baseline): 5/5 OK, ~4 hits, R@5 ≈ 80%,
~45s per question of indexing wait.

### Reset between full runs

```bash
cd eval/longmemeval
docker compose down -v
docker compose up -d
```

---

## Adapter mapping

| gbrain-evals abstraction | AKB mapping |
|---|---|
| `PGLiteEngine` instance | Per-question temp vault `lme-{normalize(qid)}-{wid}` |
| `TRUNCATE` between questions | `DELETE /api/v1/vaults/{vault}` → re-create |
| `importFromContent(slug, body)` | `POST /api/v1/documents` body=`{vault, collection:"chat", title:session_id, content:rendered}` |
| Rendered session body | `[Session date: ...]\n\nUSER: ...\n\nASSISTANT: ...` (date prefix matters for `temporal-reasoning`/`knowledge-update`) |
| `slug = "chat/{session_id}"` | `path = "chat/{slugify(session_id)}.md"` — backend `_slugify` lowercases and appends `.md` |
| `hybridSearch(q, limit)` | `GET /api/v1/search?q=...&vault=...&limit=K` (rerank ON via config) |
| `uniqSessionIds(results)` | `[r.path.removeprefix("chat/").removesuffix(".md") for r in results]` (path is stabler than title under backend normalization) |
| Wait for indexing | `GET /health/vault/{vault}` poll until `vector_store.backfill.upsert.pending == 0` (vault-scoped, not global) |

### Adapter scope

Only `akb-hybrid+rerank` (production default) is wired up.  Ablations
(`akb-hybrid` rerank-off, `akb-keyword` BM25-only, `akb-vector`
vector-only) are deferred — turning rerank off requires editing
`app.yaml` and restarting the backend, which we'd only pay for once the
first numbers say it's worth it.

---

## Runner

### `run.py`

Stdlib-only single file.  No external dependencies — `python3` is
enough.

```bash
python3 eval/longmemeval/run.py \
  --dataset ~/datasets/longmemeval/longmemeval_s.json \
  --ndjson eval/reports/longmemeval-akb.ndjson \
  --adapter akb-hybrid+rerank \
  --top-k 5 \
  --worker-id 0 --total-workers 1 \
  [--limit N | --stratify N] [--max-wall-seconds N]
```

Environment variables:

| Name | Default | Purpose |
|---|---|---|
| `AKB_URL` | `http://localhost:18000` | benchmark stack backend (main dev is on `:8000`) |
| `LONGMEMEVAL_PATH` | — | fallback when `--dataset` is omitted |

### Per-question lifecycle

```
for q in questions:
    if (adapter, q.question_id) in resume_set: continue

    vault = f"lme-{normalize(q.question_id)}-{worker_id}"
    DELETE /vaults/{vault}                  # 404 OK — clear stale state
    POST /vaults?name={vault}&public_access=none

    try:
        for s in dedup(q.sessions):         # ~50 sessions, render with date header
            POST /documents body={vault, collection:"chat", title:s.id, content:s.body, ...}
    except HTTPError:                       # skip on partial fail
        DELETE /vaults/{vault}; append_ndjson(status="ingest_error"); continue

    wait_for_indexing(vault)                # poll GET /health/vault/{vault}, pending==0
    res = GET /search?q=q.question&vault=vault&limit=K
    retrieved = [strip(r.path) for r in res.results]
    hit = bool(set(retrieved) & set(q.answer_session_ids))
    append_ndjson(status="ok", hit_at_k=hit, ...)
    DELETE /vaults/{vault}
```

Per-question vault isolation is what lets the runner write to the same
backend without cross-question interference.

### Auth and cleanup

On startup: `POST /auth/register` → `POST /auth/login` → JWT (no PAT —
JWT lifetime of 24h covers even the slow full run).

On exit (normal, exception, SIGINT, SIGTERM): `DELETE /my/account`
cascades through all per-question vaults.  `finally` block plus signal
handlers — no orphan vaults on Ctrl-C.

### NDJSON output

First line is a `run_meta_start` header (backend git sha, app.yaml sha,
embed/rerank config) for post-hoc reproducibility.  Each subsequent
line is one question:

```json
{"adapter":"akb-hybrid+rerank","question_id":"e47becba",
 "question_type":"single-session-user","status":"ok",
 "is_abstention":false,"num_haystack":54,
 "ground_truth":["answer_280352e9"],
 "retrieved":["answer_280352e9","sharegpt_xxx_0","ultrachat_yyy", ...],
 "hit_at_k":true,
 "ingest_ms":601,"index_wait_ms":43549,"query_ms":964}
```

Resume key is `(adapter, question_id)` — re-running with the same
`--ndjson` skips completed questions.

---

## Multi-worker (`batch.sh`)

```bash
WORKERS=3 LIMIT=10 ./eval/longmemeval/batch.sh         # 3 shards, 10 Q each
WORKERS=1 WALL=600 ./eval/longmemeval/batch.sh         # 1 worker, 10-min cap
STRATIFY=5 ./eval/longmemeval/batch.sh                 # 5 per question_type
```

Each worker writes its own NDJSON shard
(`reports/longmemeval-akb.shard-{i}of{N}.ndjson`); questions are
partitioned by `hash(qid) mod N` so a worker always sees the same
subset across reruns (resume works per-shard).  After workers finish,
the script aggregates totals + per-type breakdown.  Any non-zero shard
exit code propagates as `exit 1` so CI sees the failure.

> **Multi-worker (WORKERS > 1) is currently blocked** by a cross-vault
> race condition in the backend's GitService — concurrent PUTs to
> different vaults trigger `FileNotFoundError` from a process-global
> `os.chdir` inside GitPython.  See [Backend follow-ups](#backend-follow-ups).
> Until that's fixed, run with `WORKERS=1`.

Merge shards for analysis:

```bash
cat eval/reports/longmemeval-akb.shard-*.ndjson > eval/reports/longmemeval-akb.ndjson
```

---

## Performance and cost

Measured on a 5Q smoke (one worker, M-series Mac talking to OpenRouter):

| Stage | Per question | Per 500 (1 worker) |
|---|---|---|
| Vault create/delete | ~50ms × 2 | ~50s |
| Session PUTs (~50/Q) | ~600ms total (backend chunks asynchronously) | ~5 min |
| Indexing wait | **~45s** (~486 chunks/Q ÷ ~32-batch × ~1.7s embedding + per-chunk upsert) | **~6.25 h** |
| Search (rerank ON) | ~1000ms | ~8 min |
| **Total wall-clock** | **~48s** | **~6.5 h** |

**Cost estimate** (`bge-m3` + `cohere/rerank-v3.5` via OpenRouter):

| Item | Unit | Per full run |
|---|---|---|
| Embedding (ingest) | $0.01–0.04 / M tokens | ~$0.40 |
| Rerank | ~$2 / 1000 queries | ~$1.00 |
| **Total** | | **~$1.40** |

OpenRouter is the rate-limit boundary (account-balance based), not
Cohere's trial 10 req/min cap.  3-worker concurrency would cut wall
time to ~2.5h, but requires the backend follow-ups below first.

---

## Comparability notes

- **Embedding model differs.** gbrain uses OpenAI
  `text-embedding-3-large@1536`; AKB uses `bge-m3@1024`.  Score
  differences mix model effect with system effect.  AKB posts as its
  own line, framed as stack-level comparison.
- **Reranker default ON.** gbrain's 97.6% is hybrid-only.  AKB's
  default has rerank on; posting them side by side isn't fair until
  the rerank-off ablation runs.  First-round number is the production
  default; ablation is a follow-up if the gap warrants it.
- **Abstention handling unverified across systems.** §1 includes `_abs`
  in the denominator (the only consistent choice when ground truth
  exists).  If gbrain excluded them, the comparison drifts slightly;
  the `is_abstention` flag lets readers re-aggregate either way.

---

## Known issues

1. **bge-m3 absolute performance on English-only data is uncertain.**
   Multilingual models typically trail English-only models slightly.
   First-run number is the answer.
2. **Indexing worker throughput is the bottleneck** (~45s/Q for 486
   chunks).  See [Backend follow-ups](#backend-follow-ups).
3. **`SearchResult.path` round-trip stability.** Backend `_slugify`
   currently lowercases and appends `.md`.  If that normalization
   changes, ground-truth matching breaks silently.  Worth a single
   PUT→GET sanity check before each round.
4. **Source-aware boost may interact poorly.** Everything is in the
   `chat/` collection, so any collection-weighting becomes noise.
   First-run numbers will tell whether it matters.
5. **OpenRouter → Cohere rerank latency is two hops, not one.** p99
   query latency could exceed 500ms.  Time estimate uses ~150ms; update
   after first measurement.
6. **HF deprecated label on the dataset.** Raw file is still served,
   but if HF removes it, the `curl` command breaks.  Save a local
   SHA256 of the downloaded JSON for reproducibility.

---

## Backend follow-ups

Four backend issues were discovered while building the runner.  None
are fixed in this directory; they live as separate tasks because each
needs an independent backend PR.

1. **`rerank_service` fallback chain produces a cryptic httpx error**
   when both `rerank_base_url` and `llm_base_url` are blank.  Should
   fail fast at startup with a clear config error.
2. **`backend/app/db/init.sql` is missing `CREATE EXTENSION IF NOT EXISTS
   vector`.**  The cluster's `akb-init-sql` ConfigMap papers over this;
   a fresh `docker compose up` against repo root would otherwise leave
   every chunk in a `vector_last_error = 'unknown type: public.vector'`
   loop.  Worked around here via `postgres-init/01-pgvector.sql`.
3. **`embed_worker` runs as a single asyncio task.**  Per-chunk upsert
   in a separate transaction dominates after the embed call (~50ms ×
   N).  Adding N concurrent runners + a batched upsert would drop
   45s/Q to <5s/Q.
4. **`GitService` has a cross-vault `os.chdir` race condition.**
   GitPython mutates process-global cwd inside `index.add`, so
   concurrent PUTs to different vaults race.  Per-vault `threading.Lock`
   doesn't help.  Blocks `WORKERS > 1` in `batch.sh`.

---

## Success criteria

- **Smoke (`--limit 5`)**: 5/5 OK, `hit_at_k` neither 0/5 nor 5/5 — a
  plausible non-degenerate number.  Current: 4/5 at R@5=80%.
- **First report**: 500Q full-run R@5 + per-type table + latency
  distribution + comparison table including gbrain/MemPal lines.  One
  `akb-hybrid+rerank` row.
- **Honesty bar**: publish the number whatever it is.  Footnote the
  embedding-model and rerank-default differences explicitly so readers
  can interpret.

---

## References

- gbrain-evals repo: https://github.com/garrytan/gbrain-evals
- gbrain LongMemEval report: `docs/benchmarks/2026-05-07-longmemeval-s.md` in that repo
- LongMemEval original (used here): https://huggingface.co/datasets/xiaowu0162/longmemeval
- LongMemEval cleaned (v2 option): https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned
- LongMemEval paper + eval code: https://github.com/xiaowu0162/LongMemEval
- AKB internal regression harness (small, different purpose): `backend/tests/eval/`
- Cluster effective config: `kubectl -n akb get configmap akb-app-config -o yaml`
- AKB config loader (justifies "yaml only, no env vars"): [backend/app/config.py:12](../../backend/app/config.py:12)
- SearchResult schema (path-based mapping rationale): [backend/app/models/document.py:137](../../backend/app/models/document.py:137)
- Vault-scoped health endpoint: [backend/app/main.py:207](../../backend/app/main.py:207)
- Vault name regex: [backend/app/services/document_service.py:845](../../backend/app/services/document_service.py:845)
- rerank dispatch: [backend/app/services/rerank_service.py:56](../../backend/app/services/rerank_service.py:56)
