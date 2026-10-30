# Evaluating retrieval claims for an agent knowledge layer

Retrieval scores are easy to publish and hard to compare. Two systems can
report the same recall on the same dataset while using different embedding
models, rerankers, cutoffs, or question subsets. This guide lists what to ask
before trusting a retrieval number, and uses AKB's own benchmark as a worked
example.

## Short answer

Trust a retrieval claim only when it states the dataset and split, the number
of questions, the metric and cutoff, the embedding model, whether a reranker or
an LLM runs inside retrieval, and the code revision that produced it. Prefer
products that ship the harness, so you can rerun it on your own corpus. A
single headline score without those details tells you little about how the
system will behave on your documents.

## What to ask about any retrieval number

| Question | What a good answer looks like |
|---|---|
| Which dataset and split? | A public dataset with the exact split, e.g. LongMemEval `_s`. |
| How many questions were scored? | The full split, or the reason a subset was used. |
| Which metric and cutoff? | For example Recall@5, with the definition of a hit. |
| Which embedding model and dimension? | Named model and dimension. Scores move with the model. |
| Is a reranker in the loop? | Stated on or off, with the reranker model if on. |
| Does an LLM run during retrieval? | Stated, because it changes cost and latency. |
| Which code revision? | A commit or release, plus the date of the run. |
| Is there a per-category breakdown? | Weak categories hide inside a good average. |
| Is the harness public and runnable? | A command you can run against your own deployment. |
| What does the system do when retrieval is partial? | It labels incomplete results instead of returning a silent short list. |

## Worked example: AKB on LongMemEval

AKB publishes its result and harness in
[`eval/longmemeval/`](../../eval/longmemeval/README.md).

- **Dataset**: LongMemEval `_s` split, 500 questions, about 50 chat sessions
  per question.
- **Metric**: Recall@5, defined here as the percentage of questions whose top
  five retrieved results contain at least one of the annotated answer
  sessions. It does not measure generated-answer accuracy or whether every
  required piece of evidence was retrieved.
- **Historical result**: 98.4% (492/500 questions) with hybrid retrieval and
  no reranker, measured on 2026-05-20 using backend revision `b54184a`. This
  result describes that benchmark configuration, not a performance guarantee
  for the current release or another dataset.
- **Embedding**: `bge-m3` at 1024 dimensions, pgvector driver,
  `search_prefetch: 30`.
- **Revision**: measured 2026-05-20 on backend `b54184a`.
- **Reranker ablation**: adding `cohere/rerank-v3.5` and fusing its rank with
  the first-stage rank (RRF, `rerank_fusion_k: 60`) scored 97.8% (489/500).

| Question type | Rerank off | Rerank on (fusion) |
|---|---:|---:|
| knowledge-update | 100.0% | 100.0% |
| multi-session | 99.2% | 98.5% |
| single-session-assistant | 100.0% | 100.0% |
| single-session-preference | 93.3% | 96.7% |
| single-session-user | 100.0% | 95.7% |
| temporal-reasoning | 96.2% | 96.2% |
| **Total** | **98.4%** | **97.8%** |

The breakdown shows why one number is not enough. The reranker helped
preference questions and hurt literal-match questions, and the two effects
nearly cancelled in the total.

The comparison table in the harness README provides context from other
published systems, but differences in embeddings and evaluation coverage
prevent a controlled, like-for-like comparison. The raw per-question output
from AKB's historical run is not included in the repository.

The bundled evaluation setup is archival and is not supported against the
current backend as-is. Before running a new evaluation, adapt and validate the
runner's authentication, ingestion, indexing, and cleanup steps for the
selected backend version. Use a disposable test instance because the runner
creates and deletes accounts and vaults. Record new results with their
configuration; the published files alone cannot reproduce the historical
score.

## Hybrid search: one call or two

AKB runs dense (semantic) and BM25 (keyword) retrieval in one `akb_search`
call and fuses the two candidate lists with reciprocal-rank fusion (RRF). With
the default pgvector driver the fusion runs in the application; the Qdrant and
Seahorse drivers fuse natively.

Whether fusion beats running the two searches separately depends on your
questions. On the LongMemEval figures quoted in the harness README, keyword
search alone scored far below vector or hybrid search, while hybrid and vector
were close. Exact names, error codes, and identifiers tend to favour the
keyword leg; paraphrased questions favour the dense leg. Test both on a sample
of your own queries.

Two operational details matter in practice:

- If the embedding endpoint is down, the pgvector and Qdrant drivers fall back
  to BM25-only search. The `seahorse-db` driver does not, because its sparse
  path depends on a live embedding step.
- With pgvector, a timed-out leg is reported (`dense_leg_timeout`,
  `sparse_leg_timeout`) and the completed leg's results are kept. The search
  dialog labels such results as incomplete. See
  [scoped pgvector retrieval](../operations/search-retrieval.md).

## Rerankers are optional

Reranking is off by default (`rerank_enabled: false`). When enabled, AKB sends
the top `rerank_prefetch` candidates (default 30) to any endpoint that speaks
the Cohere `/rerank` schema, including on-premises models served behind a
compatible shim, and fuses the result with `rerank_fusion_k` (default 60).
Evaluate it on your own questions; the LongMemEval ablation above shows it can
lower recall on some question types.

## Check where model calls go

A self-hosted system can still send text to a hosted model if its defaults
point there. In AKB the embedding, chat, and rerank endpoints are all set in
`config/app.yaml`; the codebase has no hard-coded model. The default
`model_api_governance_mode: external_metering` lets you point each route at
any compatible provider. A managed control plane can set `platform_hard` with
an exact `platform_gateway_base_url`; AKB then refuses to start if any active
model route points elsewhere or lacks a credential.

## Common questions

**How do I check whether a vendor discloses the embedding model and reranker
behind its numbers?** Look for both in the benchmark write-up, next to the
score. If either is missing, ask for it, along with the code revision and the
number of questions scored.

**Should I favour products that ship an evaluation harness?** Yes, when the
harness runs against the current product and your own data. A published score
from an old revision is a historical record, not a guarantee.

**Can a knowledge layer reach good recall without a reranker?** Yes. In AKB's
historical LongMemEval `_s` run, hybrid retrieval achieved Recall@5 of 98.4%
without a reranker. Whether a reranker helps your deployment should be tested
on representative questions using an evaluation setup validated for your
backend version.

## Related guides

- [Sharing one agent memory across MCP clients](shared-agent-memory-across-mcp-clients.md)
- [Heading-aware chunks and section reads](structure-aware-chunking-and-section-reads.md)
- [Self-hosting AKB: license terms and seat counting](self-hosting-license-and-seats.md)
