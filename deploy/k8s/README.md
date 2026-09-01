# Kubernetes deploy

Generic kustomize base for deploying AKB to a Kubernetes cluster. Pair
with an operator-specific overlay for real hostnames, registries, and
TLS issuers.

As in `akb-platform`, treat one AKB workspace as one namespace. Pass that
boundary explicitly with `NAMESPACE`; `deploy.sh` creates only that namespace
and the selected Secret profile binds its reader identity to it.

## Layout

```
deploy/k8s/
├── deploy.sh              # build → push → kubectl apply (kustomize base)
├── kustomization.yaml     # base resources (pgvector default; qdrant.yaml not listed)
├── namespace.yaml
├── postgres.yaml          # pgvector/pgvector:pg16 StatefulSet — hosts both
│                          # the main DB and the vector_index schema
├── qdrant.yaml            # optional Qdrant StatefulSet — add to
│                          # kustomization.yaml only if you flip the
│                          # backend's vector_store_driver to qdrant
├── redis.yaml             # optional event-stream Redis; add from an overlay
│                          # only when the OT Redis Operator is installed
├── backend.yaml           # Deployment + ConfigMap (vector_store_driver: pgvector)
├── frontend.yaml          # Deployment + Service
├── ingress.yaml           # placeholder host (akb.example.com)
├── secrets/               # stable Secret Contract v1 + manual/bundled/external producers
├── standalone-sso/        # AKB + owned Keycloak 26.7 + dedicated Keycloak DB;
│                          # temporary bootstrap service-account retirement
└── internal/              # gitignored — operator-private overlays
    ├── deploy-internal.sh
    ├── cluster-issuer.yaml
    ├── ingress-patch.yaml
    └── backend-config-patch.yaml
```

**Vector store**: the base ships with `vector_store_driver: pgvector`
inside the Postgres pod. Other options:

- **Qdrant** as a separate StatefulSet — add `qdrant.yaml` to
  `kustomization.yaml` and patch `akb-app-config` to set
  `vector_store_driver: qdrant` + `vector_url: http://qdrant:6333`.
- **Seahorse Cloud** (managed) — no extra StatefulSet; patch
  `akb-app-config` with `vector_store_driver: seahorse` +
  `seahorse_tenant_uuid` + `seahorse_table_name`/`seahorse_table_uuid`,
  and put `seahorse_token: shsk_<...>` in the secret. AKB calls the
  Seahorse BFF (`https://console.seahorse.dnotitia.ai/bff`) for table
  lifecycle and the per-table host for data CRUD/search.

The `internal/` overlay shows the Qdrant pattern for the production
cluster.

## Backend process topology

The base Deployment keeps one `Recreate` Pod and one RWO Git PVC, but runs two
containers from the same backend image:

- `backend` uses `AKB_PROCESS_ROLE=api` and serves FastAPI/MCP. It owns only
  serving-process sinks and one query-tokenizer child.
- `worker` runs `python -m app.worker_main` with
  `AKB_PROCESS_ROLE=worker`. It owns durable queue consumers, external-Git
  reconciliation, and periodic maintenance. Its exec probe checks an
  event-loop heartbeat.

This separation prevents a worker stall from blocking the serving loop. It is
not the final horizontally scalable topology: both containers still mount the
RWO PVC because synchronous Bare-Git reads/writes remain in the API path.
Keep `replicas: 1`, `strategy: Recreate`, and the API PVC mount until the
single-writer gitd, MCP session, audit/throttle, and drift-recovery gates in
[`docs/design/accepted/2026-08-18-worker-runtime-safety-foundation`](../../docs/design/accepted/2026-08-18-worker-runtime-safety-foundation/README.md)
are complete.

For a standalone installation whose canonical human-auth mode is `sso`, use
[`standalone-sso/README.md`](standalone-sso/README.md). That overlay owns its
Keycloak lifecycle and dedicated database. Do not apply it to a managed tenant
that reuses a platform-owned/shared Keycloak realm.

## Quickstart (generic)

```bash
# 1. Provide a registry to push images to.
export REGISTRY=ghcr.io/myorg          # or my-registry.local:5000
export PUBLIC_URL=https://akb.example.com    # printed at the end; optional
# Required only when the cluster has no default StorageClass. The same value
# is used for AKB/PostgreSQL and a production bundled Secret Manager.
export STORAGE_CLASS=standard

# 2. Edit ingress.yaml and the app ConfigMap's public_base_url and
#    local_session_issuer to the same real origin.
$EDITOR deploy/k8s/ingress.yaml
$EDITOR deploy/k8s/backend.yaml

# 3. Provide a ClusterIssuer named `letsencrypt-prod` (or change the
#    annotation in ingress.yaml). cert-manager + your DNS provider.

# 4. Choose one coherent installation profile. This example creates a
#    brand-new local-auth/manual-Secret contract; production should provision
#    `akb-secret` out-of-band instead.
export AKB_PROFILE=standalone
export GENERATE_MANUAL_SECRETS=true

# 5. Apply.
bash deploy/k8s/deploy.sh
```

After the script finishes:

```bash
kubectl edit configmap akb-app-config -n akb   # set embed_*, llm_*, s3_*, public_base_url
kubectl get secret akb-secret -n akb           # stable Secret Contract v1
```

The placeholder ConfigMap in `backend.yaml` matches `config/app.yaml.example`
defaults (OpenAI embeddings, no LLM, no Redis, no S3) so the stack can
boot for smoke-testing before you wire in real providers.

## Operator-specific overlay (`internal/`)

The `internal/` directory is gitignored and intended for environment-
specific overrides — real hostnames, internal registries, ClusterIssuers
with DNS-01 credentials, ConfigMap with private endpoints. Make it a
Kustomize overlay over the public base so hostname and runtime configuration
are applied in one render, then use a small wrapper script:

```bash
# deploy/k8s/internal/deploy-internal.sh
export REGISTRY=my-registry.internal:5000
export PUBLIC_URL=https://akb.mycorp.example
kubectl apply -f "$(dirname "$0")/cluster-issuer.yaml"
KUSTOMIZE_DIR="$(dirname "$0")" bash "$(dirname "$0")/../deploy.sh"
```

Anything you put under `internal/` is automatically excluded by the
top-level `.gitignore`. Treat it as your private operations folder —
secrets management of choice (sealed-secrets, vault, cluster-bound
Secrets) goes here too.

## Secrets

The base no longer commits a PostgreSQL `change-me` Secret. It consumes the
same unified `akb-secret` shape as current `akb-platform` workspaces:

- PostgreSQL reads `akb-secret/db_password`.
- Backend and worker mount `akb-secret/secret.yaml`.
- Local auth projects `local-session-private.pem` and
  `local-session-jwks.json` from that same Secret.
- The backend disables automatic ServiceAccount-token mounting and has no
  Secret Manager or Kubernetes Secret API permission.

Choose one public `AKB_PROFILE`: `standalone`, `standalone-sso`,
`standalone-secret-manager`, or `standalone-sso-secret-manager`. The two
`*-secret-manager` profiles bundle OpenBao or HashiCorp Vault; the other two
can either use an operator-owned Kubernetes Secret or adapt an existing
Vault-compatible endpoint with `SECRET_MODE=external`. See the complete
contracts, pinned chart versions, TLS/Raft profiles, native Shamir/PGP/Auto
Seal operations, rotation boundary, and migration notes in
[`secrets/README.md`](secrets/README.md).

The SSO profiles select the standalone SSO Kustomize tree and require coherent
`SSO_AKB_PUBLIC_URL` and `SSO_KEYCLOAK_PUBLIC_URL` origins plus the
product-admin identity. Lower-level `AUTH_PROFILE` and `SECRET_MODE` remain
compatibility/adapter inputs, but new installations should use
`AKB_PROFILE` so an auth/Secret Manager combination cannot be assembled
accidentally.
