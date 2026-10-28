# LongMemEval benchmark — AKB runner

Measures `akb_search` retrieval recall on the public
[`xiaowu0162/longmemeval`](https://huggingface.co/datasets/xiaowu0162/longmemeval)
`_s` split (500 questions). The goal is to put one honest line for AKB next
to [gbrain-evals](https://github.com/garrytan/gbrain-evals) and
[MemPalace](https://github.com/mempalace/mempalace) on the same dataset.

Results (AKB measured 2026-05-20, backend `b54184a`; references as of 2026-05-07):

| System                          | R@5    | k | n   | LLM in retrieval | Source |
|---------------------------------|--------|---|-----|-------------------|--------|
| **AKB hybrid (rerank off)**     | **98.40%** | 5 | 500 | no  | this repo |
| MemPal hybrid+rerank (held-out) | 98.4%  | 5 | 450 | yes | [MemPalace](https://github.com/mempalace/mempalace) |
| **AKB hybrid + rerank (RRF fusion)** | **97.80%** | 5 | 500 | no | this repo |
| gbrain-hybrid                   | 97.60% | 5 | 500 | no  | [gbrain-evals](https://github.com/garrytan/gbrain-evals/blob/main/docs/benchmarks/2026-05-07-longmemeval-s.md) |
| gbrain-vector                   | 97.40% | 5 | 500 | no  | same |
| MemPal raw (ChromaDB)           | 96.6%  | 5 | 500 | no  | [MemPalace](https://github.com/mempalace/mempalace) headline |
| gbrain-keyword (BM25)           | 19.80% | 5 | 500 | no  | gbrain-evals |

AKB's hybrid retrieval (dense + BM25, no reranker) lands at **R@5 = 98.40%**
(492/500) — ahead of gbrain's published hybrid line and level with
MemPalace's reranked number. The embedding model differs (AKB: `bge-m3@1024`;
see [Comparability notes](#comparability-notes)), so treat this as a
**stack-level** comparison rather than apples-to-apples.

A cross-encoder reranker on top did **not** help: fusing the cross-encoder
rank with the first-stage hybrid rank (RRF) scored **97.80%** (489/500),
−0.6pp vs rerank-off.  With the first stage already placing the answer in
the top 5, the reranker mostly reorders correct hits — it costs more in
`single-session-user` (literal-match questions, −4.3pp) than it recovers in
`single-session-preference` (+3.4pp).  See per-category detail below.

### Per-category Recall@5 (500Q, backend `b54184a`)

| Question type | rerank off | rerank on (fusion) | Δ (off − on) |
|---|---:|---:|---:|
| knowledge-update          | 100.0% | 100.0% | 0 |
| multi-session             | 99.2%  | 98.5%  | +0.7 |
| single-session-assistant  | 100.0% | 100.0% | 0 |
| single-session-preference | 93.3%  | 96.7%  | −3.4 |
| single-session-user       | 100.0% | 95.7%  | +4.3 |
| temporal-reasoning        | 96.2%  | 96.2%  | 0 |
| **TOTAL**                 | **98.4%** | **97.8%** | **+0.6** |

Full report: [`results/2026-05-20-longmemeval-s.md`](results/2026-05-20-longmemeval-s.md)
— headline, per-category breakdown, the reranker ablation, and a reproduce
historical recipe (not executable against the current backend). Both runs use `bge-m3@1024` + pgvector, `search_prefetch: 30`; the
rerank-on run adds `rerank_enabled: true` with `cohere/rerank-v3.5` via
OpenRouter and `rerank_fusion_k: 60`.

The raw per-question NDJSON (`eval/reports/longmemeval-akb-rerank-{off,on}-final.ndjson`,
500 records each, `run_meta_start` header on line 1) was a local experiment
artifact — `eval/reports/` is gitignored, like the dataset. New runs produce
new results; they do not reproduce the historical numbers by assumption.

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

## Historical benchmark environment

The results above describe the historical benchmark configuration, not a
validated result for the current branch. The files in this directory retain
that configuration for comparison: bge-m3/1024 embeddings, pgvector posting
sparse vectors, reranking disabled, and a first-stage prefetch of 30.

**The bundled Compose recipe is archival and unsupported with the current
backend.** It builds the current checkout but retains HS256-era configuration;
it lacks the current RS256 signing key setup and explicit storage initialization.
No immutable backend image or complete historical revision is pinned here, so
these files alone cannot reproduce the published scores. Do not use this
recipe as an installation guide or infer current performance from those scores.
The local-only example credentials are not suitable for a shared deployment.

For a current installation, use the [Native quickstart](../../README.md#quick-start)
and [installation guide](../../docs/operations/native-installation.md), including
key generation and installation identity preparation. Verify that deployment:

```bash
curl --fail http://localhost:8000/livez
curl --fail http://localhost:8000/readyz
```

These checks verify service availability, not benchmark correctness. Before
porting `run.py` to a current installation, validate its auth, ingestion, indexing
wait, and cleanup contracts against the selected backend. Run only in a disposable
instance: it creates and deletes accounts and per-question vaults. Record the
backend revision, effective configuration, dataset revision, embedding model, and
provider settings alongside any new results. Never commit provider credentials.

The historical runner options and pseudocode below document the original
experiment; they are not a claim of compatibility with today's API.

---

## Adapter mapping

| gbrain-evals abstraction | AKB mapping |
|---|---|
| `PGLiteEngine` instance | Per-question temp vault `lme-{normalize(qid)}-{wid}` |
| `TRUNCATE` between questions | `DELETE /api/v1/vaults/{vault}` → re-create |
| `importFromContent(slug, body)` | `POST /api/v1/documents` body=`{vault, collection:"chat", title:session_id, content:rendered}` |
| Rendered session body | `[Session date: ...]\n\nUSER: ...\n\nASSISTANT: ...` (date prefix matters for `temporal-reasoning`/`knowledge-update`) |
| `slug = "chat/{session_id}"` | `path = "chat/{slugify(session_id)}.md"` — backend `_slugify` lowercases and appends `.md` |
| `hybridSearch(q, limit)` | `GET /api/v1/search?q=...&vault=...&limit=K` (rerank OFF via config) |
| `uniqSessionIds(results)` | `[r.path.removeprefix("chat/").removesuffix(".md") for r in results]` (path is stabler than title under backend normalization) |
| Wait for indexing | `GET /health/vault/{vault}` poll until `vector_store.backfill.upsert.pending == 0` (vault-scoped, not global) |

### Adapter scope

The retained adapter is `akb-hybrid`: dense + BM25 with rerank off,
embedding fixed, and `search_prefetch` widened so source-level dedup sees
the same kind of candidate headroom that gbrain's no-rerank path uses.

---

## Runner

### `run.py`

Stdlib-only single file.  No external dependencies — `python3` is
enough.

```bash
python3 eval/longmemeval/run.py \
  --dataset ~/datasets/longmemeval/longmemeval_s.json \
  --ndjson eval/reports/longmemeval-akb.ndjson \
  --adapter akb-hybrid \
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

Merge shards for analysis:

```bash
cat eval/reports/longmemeval-akb.shard-*.ndjson > eval/reports/longmemeval-akb.ndjson
```

---

## Performance and cost

Measured on a 5Q smoke (one worker, M-series Mac talking to OpenRouter).
The indexing-wait column shows two numbers: the original single-task
`embed_worker` (`indexing_concurrency: 1`) and the bench default
(`indexing_concurrency: 8`, parallel runners draining the queue via
`FOR UPDATE SKIP LOCKED`).

| Stage | Per question | Per 500 (1 worker) |
|---|---|---|
| Vault create/delete | ~50ms × 2 | ~50s |
| Session PUTs (~50/Q) | ~600ms total (backend chunks asynchronously) | ~5 min |
| Indexing wait | **~45s** at concurrency 1 → **~10–14s** at concurrency 8 (~486 chunks/Q ÷ ~16-batch × ~1.7s embedding + per-chunk upsert) | **~6.25 h → ~1.7 h** |
| Search (rerank ON) | ~1000ms | ~8 min |
| **Total wall-clock (1 worker, concurrency 8)** | **~13s** | **~1.8 h** |

**Cost estimate** (`bge-m3` + `cohere/rerank-v3.5` via OpenRouter):

| Item | Unit | Per full run |
|---|---|---|
| Embedding (ingest) | $0.01–0.04 / M tokens | ~$0.40 |
| Rerank | ~$2 / 1000 queries | ~$1.00 |
| **Total** | | **~$1.40** |

OpenRouter is the rate-limit boundary (account-balance based), not
Cohere's trial 10 req/min cap.  The backend follow-ups that once blocked
parallelism are fixed, so `WORKERS=3` on top of `indexing_concurrency: 8`
is the standard full-run setup — wall time lands around **~40–50 min**
for all 500 questions.

---

## Comparability notes

- **Embedding model differs.** gbrain uses OpenAI
  `text-embedding-3-large@1536`; AKB uses `bge-m3@1024`.  Score
  differences mix model effect with system effect.  AKB posts as its
  own line, framed as stack-level comparison.
- **Two reranker configs are evaluated.** gbrain's 97.6% is
  hybrid-only (no reranker), so a fair head-to-head needs AKB's
  rerank-off number.  Both have been run: a **rerank-off** line
  (committed `config/app.yaml` default — dense + BM25 with conservative
  English sparse-token variants and `search_prefetch` headroom) and a
  **rerank-on** line (RRF fusion of the first-stage hybrid rank with the
  cross-encoder rank, rather than a hard reorder — toggle
  `rerank_enabled: true`).  Flip `rerank_enabled` and restart the
  backend to switch; no re-index is needed since reranking is a
  search-time stage.
- **Abstention handling unverified across systems.** §1 includes `_abs`
  in the denominator (the only consistent choice when ground truth
  exists).  If gbrain excluded them, the comparison drifts slightly;
  the `is_abstention` flag lets readers re-aggregate either way.

---

## Historical runner limitations

1. **`SearchResult.path` round-trip stability.** Backend `_slugify`
   lowercased and appended `.md` in the original run.  If that normalization
   changes, ground-truth matching breaks silently.  Worth a single
   PUT→GET sanity check before each round.
2. **HF deprecated label on the dataset.** Raw file is still served,
   but if HF removes it, the `curl` command breaks.  Save a local
   SHA256 of the downloaded JSON for reproducibility.

The historical stack installed pgvector through `postgres-init/01-pgvector.sql`
and configured an explicit reranker URL. These are experiment-era choices,
not a current backend defect list. Check the selected revision's bootstrap
and validation behavior before reusing them.

---

## Success criteria

- **Smoke (`--limit 5`)**: 5/5 OK, `hit_at_k` neither 0/5 nor 5/5 — a
  plausible non-degenerate number.  Current: 4/5 at R@5=80%.
- **Full report** (done): 500Q R@5 + per-type table + comparison table
  including gbrain/MemPal lines, for both rerank-off and rerank-on
  configs.  See the results tables at the top of this file.
- **Honesty bar**: publish the number whatever it is.  Footnote the
  embedding-model and rerank-default differences explicitly so readers
  can interpret.

---

## References

- gbrain-evals repo: https://github.com/garrytan/gbrain-evals
- gbrain LongMemEval report: `docs/benchmarks/2026-05-07-longmemeval-s.md` in that repo
- MemPalace repo: https://github.com/mempalace/mempalace
- LongMemEval original (used here): https://huggingface.co/datasets/xiaowu0162/longmemeval
- LongMemEval cleaned (v2 option): https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned
- LongMemEval paper + eval code: https://github.com/xiaowu0162/LongMemEval
- AKB internal regression harness (small, different purpose): `backend/tests/eval/`
- Cluster effective config: `kubectl -n akb get configmap akb-app-config -o yaml`
- AKB config loader (application YAML settings; deployment overrides are documented separately): [backend/app/config.py](../../backend/app/config.py)
- SearchResult schema (path-based mapping rationale): [backend/app/models/document.py](../../backend/app/models/document.py)
- Vault-scoped health endpoint: [backend/app/main.py](../../backend/app/main.py)
- Vault name regex: [backend/app/services/document_service.py](../../backend/app/services/document_service.py)
- rerank dispatch: [backend/app/services/rerank_service.py](../../backend/app/services/rerank_service.py)
