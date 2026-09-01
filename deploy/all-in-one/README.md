# AKB single-container demo (`all-in-one` compatibility path)

The historical directory name is retained for image and documentation links,
but this is a single-container demo bundle rather than the production
"everything included" deployment. It contains Postgres + pgvector + Redis +
MinIO + backend + frontend behind nginx. Use cases:

- [Glama](https://glama.ai/) MCP introspection / listing
- Quick demos (one `docker run` brings up the full stack)
- Self-hosted single-box deployments

It deliberately does not run OpenBao/Vault or Keycloak inside the application
container. Doing so would place the Secret Manager, its storage, and the
secret-consuming application under the same root process and filesystem trust
boundary. Use `deploy/helm/akb` for an all-in-one installation whose components
remain isolated as Kubernetes workloads.

## Quick start (pre-built image)

```bash
docker run --rm -p 8080:8080 dnseahorse/akb
```

| What                   | URL                                                 |
|------------------------|-----------------------------------------------------|
| Frontend               | http://localhost:8080/                              |
| MCP Streamable HTTP    | http://localhost:8080/mcp/                          |
| REST liveness          | http://localhost:8080/livez                         |
| REST readiness         | http://localhost:8080/readyz                        |

The container prints the demo `DEMO_PAT` on first boot — grep
`docker logs <name> | grep DEMO_PAT` to find it. Use that token as
`Authorization: Bearer <PAT>` when configuring an MCP client.

## Build locally

```bash
docker build -f deploy/all-in-one/Dockerfile -t dnseahorse/akb .
docker run --rm -p 8080:8080 dnseahorse/akb
```

## With embeddings + search enabled

```bash
docker run --rm -p 8080:8080 \
    -e EMBED_BASE_URL=https://api.openai.com/v1 \
    -e EMBED_MODEL=text-embedding-3-small \
    -e EMBED_DIMENSIONS=1536 \
    -e EMBED_API_KEY=sk-... \
    -v akb-data:/data \
    -v akb-state:/var/lib/akb \
    dnseahorse/akb
```

Mounting `/data` and `/var/lib/akb` makes the demo state (PG cluster,
MinIO bucket, generated secrets + PAT) persist across restarts.

## Override the demo credentials

Pin the PAT (handy when registering the container as a Glama Connector):

```bash
docker run --rm -p 8080:8080 \
    -e DEMO_PAT=akb_my-fixed-token-for-glama \
    dnseahorse/akb
```

Any of `DEMO_USERNAME`, `DEMO_EMAIL`, `DEMO_PASSWORD`, `DEMO_VAULT`,
`DEMO_PAT` can be supplied; missing values are auto-generated on first
boot and persisted in `/var/lib/akb/state.env`.

## What runs inside

| Process    | Port      | Started by  |
|------------|-----------|-------------|
| nginx      | 8080      | supervisord |
| backend    | 8000      | supervisord |
| postgres   | 5432      | supervisord |
| redis      | 6379      | supervisord |
| minio      | 9000/9001 | supervisord |
| seed (one-shot — admin-owned demo vault + PAT) | – | supervisord |

`entrypoint.sh` runs idempotent bootstrap on every boot:

1. Generates and persists random `DB_PASSWORD`, `SYSTEM_HMAC_SECRET`,
   `S3_SECRET_KEY`, `DEMO_PASSWORD`, `DEMO_PAT` under
   `/var/lib/akb/state.env` on first run.
2. Generates and persists the RSA-3072 `local-session-rs256-v2` keyset under
   `/var/lib/akb/local-session` and renders `/etc/akb/secret.yaml`.
3. `initdb`s the PG cluster, creates the `akb` role + database, explicitly
   provisions the demo identity as the designated product/recovery admin, and
   installs the `vector` extension.
4. Creates the MinIO `akb-files` bucket once MinIO is up (background).
5. Hands off to supervisord — which starts everything and runs `seed.py`
   once `/readyz` returns ready.

## Not intended for

- Production deployments — use `deploy/k8s/` instead.
- Multi-tenant load — single container, no horizontal scaling.
