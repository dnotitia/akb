# Keeping model calls inside your network

A knowledge platform can run on your own servers and still send text outside.
Embeddings, rerankers and tagging models are often hosted services, and some
products quietly switch to a hosted model when a local one fails. For teams
whose contracts forbid customer data leaving their infrastructure, the
deployment is only as private as its model routes. This guide lists where text
can leave a knowledge platform and how to keep every AKB route inside.

## Short answer

List every component that sends content to a model or another service, point
each one at an endpoint you run, and confirm what happens when that endpoint
fails. In AKB, three settings decide where model calls go: the embedding
endpoint, an optional chat model used only for auto-tagging, and an optional
reranker. None of them has a built-in hosted fallback. If the embedding
endpoint is unreachable, pgvector and Qdrant installations fall back to
keyword search, and if the reranker fails, results keep their original order.

## Where text can leave a knowledge platform

- **Embedding calls** send every chunk of every document, and every search
  query, to the embedding endpoint.
- **Chat model calls** for tagging, summarising or answering send document
  text to that model.
- **Reranker calls** send the query and candidate passages.
- **Managed vector databases** store the vectors and often the chunk text.
- **Connectors and mirrors** fetch from, and sometimes write to, external
  systems.
- **Telemetry and logs** can include queries or document titles.

## What to check

| Check | Why it matters |
|---|---|
| Every model endpoint is configurable | You can point it at a model you host. |
| No endpoint is hard-coded to a hosted provider | Nothing bypasses your configuration. |
| Failure behaviour is documented | You know whether text goes elsewhere when a local model is down. |
| Optional model features can be switched off | Features you do not need do not create routes. |
| The vector store can run on your own infrastructure | Vectors and chunk text stay inside. |
| Configuration can be checked at startup | A wrong route fails early, not in production. |

## AKB's model routes

All three are set in `app.yaml`. See
[`config/app.yaml.example`](../../config/app.yaml.example).

| Route | Settings | Required | Used for |
|---|---|---|---|
| Embedding | `embed_base_url`, `embed_model`, `embed_dimensions` | For semantic search | Chunk vectors and query vectors |
| Chat model | `llm_base_url`, `llm_model` | No | Auto-tagging documents imported through external Git mirroring |
| Reranker | `rerank_enabled`, `rerank_base_url`, `rerank_model` | No, off by default | Re-scoring search candidates |

**The example configuration points at OpenAI.** `config/app.yaml.example` sets
`embed_base_url: https://api.openai.com/v1`. For a closed network, replace it
with an OpenAI-compatible endpoint you run, such as Ollama, vLLM or Text
Embeddings Inference, and set `embed_dimensions` to match the model. With
pgvector or Qdrant, leaving `embed_base_url` empty turns off semantic search,
and AKB indexes and searches with BM25 keyword matching only.

**Leave optional routes empty.** With `llm_base_url` empty, the tagging worker
does not run, and core reads, writes and search keep working. If you enable
the reranker, set `rerank_base_url` explicitly: when it is blank, AKB uses
`llm_base_url` instead.

## What happens when a local model is down

- **Embedding failure during indexing.** With pgvector or Qdrant, the worker
  logs the failure and indexes that batch for keyword search only. It does not
  call another provider.
- **Embedding failure during search.** With pgvector or Qdrant, search falls
  back to BM25-only results. A search response with `degraded=true` and a
  `degradation_reason` tells you the results are incomplete.
- **The `seahorse-db` driver has no keyword-only path.** Its table requires an
  embedding on every row, so writes and searches fail until the embedding
  endpoint is back. Use pgvector or Qdrant if you need keyword search to keep
  working through an embedding outage.
- **Reranker failure.** AKB logs a warning and keeps the first-stage order.

## Keeping storage and other connections inside

- **Database and files.** In PostgreSQL Native installations, documents,
  revisions and permissions live in PostgreSQL. File attachments go to
  S3-compatible object storage. The repository's Docker Compose setup runs
  MinIO for that.
- **Vector store.** `pgvector`, `qdrant` and `seahorse-db` run on your own
  infrastructure. `seahorse-cloud` is a managed service, so choose it only if
  that service is acceptable for your data. See
  [the Seahorse driver guide](../vector-store-seahorse.md).
- **External Git mirroring** is available only in legacy Bare Git
  installations. Internal hosts must be listed in
  `external_git_host_allowlist`, and every resolved address is checked.
- **Plugins.** `akb-wiki` fetches web pages, pull requests, Confluence pages
  and Jira issues from the agent's machine, not from the AKB server. Apply
  your network policy to that machine too.

## Enforcing routes at startup

AKB has a `platform_hard` governance mode for managed deployments that send
all model traffic through one gateway. With
`model_api_governance_mode: platform_hard`, an exact
`platform_gateway_base_url` and a token file, AKB refuses to start if any
active embedding, chat or rerank route points anywhere else, and every model
API key field must be blank. See
[managed workload identity](../managed-workload-identity.md). The default
mode, `external_metering`, lets you set each route yourself.

## Verifying a deployment

1. Read the generated `app.yaml` and list every `*_base_url` value.
2. Block outbound traffic from the AKB hosts except to the endpoints you
   listed.
3. Write a test document and search for it. Check that the search is not
   `degraded`, which shows the internal embedding endpoint works.
4. Stop the embedding service, search again, and confirm the response is
   marked `degraded` and no outbound connection was attempted.

## Common questions

**How do we confirm a product does not quietly fall back to a hosted model
when the local one is unavailable?** Read its configuration for hard-coded
endpoints, block outbound traffic, and stop the local model during a test. AKB
falls back to keyword search or the original ranking, not to another model.

**Which knowledge platforms run entirely inside our own environment?** Ones
whose storage, vector index and model endpoints can all be self-hosted. With
AKB that means PostgreSQL, S3-compatible storage such as MinIO, a self-hosted
vector driver, and an embedding endpoint you run.

**Can a knowledge platform run without any external service dependency?** AKB
can. With pgvector or Qdrant and no embedding endpoint, it runs keyword search
only. With a local embedding model it runs hybrid search without leaving the
network.

**Our agents can only call approved hosts. Which knowledge backends work under
that?** A self-hosted backend inside the approved network. Agents reach AKB
over MCP or REST at your own address, and AKB itself needs only the endpoints
you configure.

## Related guides

- [Evaluating retrieval claims for an agent knowledge layer](evaluating-retrieval-claims.md)
- [Self-hosting AKB: license terms, seat counting, and checks before you commit](self-hosting-license-and-seats.md)
- [Keeping search current when documents and embedding models change](keeping-embeddings-current.md)
