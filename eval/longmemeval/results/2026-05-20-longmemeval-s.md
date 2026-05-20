# LongMemEval-S — AKB hybrid retrieval (2026-05-20)

Recall@K of `akb_search` on the public
[`xiaowu0162/longmemeval`](https://huggingface.co/datasets/xiaowu0162/longmemeval)
`_s` split (500 questions, ~50 chat sessions per question). This is the
committed report behind the numbers quoted in the repo README; the raw
per-question NDJSON is a reproducible local artifact (gitignored — see
[the runner](../README.md) to regenerate).

- **Backend**: `b54184a` (image rebuilt from this commit before scoring)
- **Embedding**: `baai/bge-m3@1024` via OpenRouter
- **Vector store**: pgvector (`posting` sparse shape), same PG instance
- **First stage**: dense + BM25 hybrid, `search_prefetch: 30`, `bm25 k1=1.5/b=0.75`
- **Workers**: `WORKERS=3`, `indexing_concurrency=8`
- **Metric**: Recall@5 — top-5 results contain ≥1 `answer_session_ids`. No LLM judge.

## Headline

| System | R@5 | n | Reranker | Embedding | Source |
|---|---:|:---:|:---:|---|---|
| **AKB hybrid (rerank off)** | **98.40%** | 500 | no | bge-m3@1024 | this run |
| MemPalace hybrid + rerank | 98.4% | 450 | yes | — | [MemPalace](https://github.com/mempalace/mempalace) |
| **AKB hybrid + rerank (RRF fusion)** | **97.80%** | 500 | yes | bge-m3@1024 | this run |
| gbrain hybrid | 97.60% | 500 | no | text-embedding-3-large@1536 | [gbrain-evals](https://github.com/garrytan/gbrain-evals/blob/main/docs/benchmarks/2026-05-07-longmemeval-s.md) |
| gbrain vector | 97.40% | 500 | no | text-embedding-3-large@1536 | gbrain-evals |
| MemPalace raw (ChromaDB) | 96.6% | 500 | no | — | MemPalace headline |
| gbrain keyword (BM25) | 19.80% | 500 | no | — | gbrain-evals |

AKB's hybrid retrieval, **with no reranker**, scores **R@5 = 98.40%**
(492/500) — ahead of gbrain's published hybrid line (+0.8pp) and level with
MemPalace's reranked number. The embedding model differs across systems, so
read this as a stack-level comparison rather than apples-to-apples.

## Per-category Recall@5

| Question type | n | rerank off | rerank on (fusion) | Δ (off − on) |
|---|---:|---:|---:|---:|
| knowledge-update          | 78  | 100.0% | 100.0% | 0 |
| multi-session             | 133 | 99.2%  | 98.5%  | +0.7 |
| single-session-assistant  | 56  | 100.0% | 100.0% | 0 |
| single-session-preference | 30  | 93.3%  | 96.7%  | −3.4 |
| single-session-user       | 70  | 100.0% | 95.7%  | +4.3 |
| temporal-reasoning        | 133 | 96.2%  | 96.2%  | 0 |
| **TOTAL**                 | 500 | **98.40%** | **97.80%** | **+0.6** |

## Reranker ablation: a cross-encoder did not help

Both runs use the **same code, sparse encoder, and `search_prefetch`** —
only `rerank_enabled` is toggled. On top of the strong hybrid first stage,
fusing a cross-encoder rerank rank with the first-stage rank (RRF, `k=60`)
scored **−0.6pp** vs rerank-off.

Per-question over all 500:

| | count | breakdown |
|---|---:|---|
| rerank **hurt** (off hit → on miss) | 5 | single-session-user 3, multi-session 1, temporal 1 |
| rerank **helped** (off miss → on hit) | 2 | single-session-preference 1, temporal 1 |
| **net** | **−3** | 492 → 489 |

Every "hurt" case is one where the first stage already placed the correct
session in the top 5 (often rank 1–2) and the cross-encoder demoted it out.
Three of the five are `single-session-user`, where the question and the
answer share vocabulary and a literal lexical match is the right signal —
exactly where a relevance-tuned cross-encoder adds noise. RRF fusion
preserves the first-stage vote but cannot always rescue a hit the reranker
pushes deep into the prefetch pool. (The reranker did fire: overlap query
latency is ~1.0s with rerank on vs ~0.5s off — the second hop.)

Takeaway: **when the first-stage retriever is already near-ceiling, a
cross-encoder reranker has little upside and real downside.**

## Reproduce

```bash
cd eval/longmemeval
cp config/secret.yaml.example config/secret.yaml   # fill in OpenRouter key
docker compose up -d --build
cd ../..
# rerank-off (committed config default):
WORKERS=3 NDJSON_PREFIX=longmemeval-akb-rerank-off bash eval/longmemeval/batch.sh
# rerank-on: set rerank_enabled: true in eval/longmemeval/config/app.yaml,
#   `docker compose restart backend`, then rerun with a fresh NDJSON_PREFIX.
```

Each NDJSON's first line is a `run_meta_start` header pinning `backend_sha`
and the embed/rerank config. Both runs here recorded `backend_sha=b54184a`,
`embed_model=baai/bge-m3@1024`, `search_prefetch=30`; the rerank-on run adds
`rerank_enabled=true`, `rerank_model=cohere/rerank-v3.5` (via OpenRouter),
`rerank_fusion_k=60`.

## Notes

- **Embedding model differs.** gbrain uses `text-embedding-3-large@1536`;
  AKB uses `bge-m3@1024`. Treat cross-system numbers as stack-level.
- **Abstention** (`_abs`, 30/500) is included in the denominator; the
  `is_abstention` NDJSON flag lets you re-aggregate without them.
- **Bottleneck is the embedding API**, not the AKB pipeline: per-question
  index-wait averages ~15s (p90 ~33s) on OpenAI-compatible remote embedding
  under concurrent load; local backend CPU stays under 1%.
