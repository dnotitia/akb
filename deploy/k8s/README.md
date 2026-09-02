# AKB Kubernetes deployment

AKB exposes the same four deployment profile names through Helm and
Kustomize, but the interfaces do not perform the same amount of lifecycle
automation:

| Entry point | What it manages |
|---|---|
| `profiles/<name>/kustomization.yaml` | Static AKB application resources only; suitable for rendering or an operator-owned overlay |
| `profiles/<name>/deploy.sh` | Image build/reuse, Secret Contract preparation, optional VSO and bundled Secret Manager lifecycle, then Kustomize apply |
| `deploy/helm/akb` | Declarative AKB, PostgreSQL, optional Keycloak, optional Vault/OpenBao, and namespace-local VSO resources |
| `deploy/helm/akb/install.sh` | Helm release plus optional VSO prerequisite and native Vault/OpenBao init/unseal/bootstrap |
| `deploy/helm/akb-cluster` | Cluster-scoped Vault Secrets Operator shared by AKB namespaces |

Kustomize operators can compose the canonical base and reusable components
without copying complete manifest trees. Static YAML cannot safely encode a
production Vault/OpenBao initialization ceremony, so use a complete installer
or perform that lifecycle separately.

Vault Secrets Operator is a cluster prerequisite shared across AKB namespaces.
It is owned by the separate
[`deploy/helm/akb-cluster`](../helm/akb-cluster/README.md) release rather than
an individual AKB Helm release. A complete installer running with
`VSO_MODE=managed` installs or upgrades that cluster release; with
`VSO_MODE=external` it only validates a compatible controller. Each AKB
instance owns its namespace-local Vault connection, authentication,
ServiceAccount, and Secret projection resources.

The bundled Vault/OpenBao chart also creates a TokenReview
`system:auth-delegator` binding for its own ServiceAccount. Kustomize profile
release names are namespace-derived, while the AKB Helm chart qualifies this
ClusterRoleBinding with both namespace and release to prevent cross-instance
name collisions.

Treat one standalone AKB instance as one Kubernetes namespace and pass that
boundary explicitly with `NAMESPACE`. The application and Secret Manager
identity stay bound to that namespace. `VSO_MODE=managed` is the deliberate
exception to namespace-only mutation because it also reconciles the shared
cluster prerequisite.

## Prerequisites

- `kubectl`, `jq`, and standard POSIX command-line tools for a complete profile
  installer. Helm 3 is additionally required for bundled Secret Manager or
  managed-VSO workflows; bundled bootstrap also uses OpenSSL.
- Docker Buildx when the profile installer builds images, and Docker when it
  generates application bootstrap material from the backend image.
- A default StorageClass, or an explicit `STORAGE_CLASS`.
- An ingress controller and operator-managed DNS/TLS. Production bundled
  Secret Manager mode also requires either `akb-secret-store-tls` or an
  existing cert-manager `Issuer`/`ClusterIssuer`.
- The AKB Helm chart requires Kubernetes 1.29 or later. The bundled OpenBao
  chart raises the effective minimum to Kubernetes 1.30. Check the selected
  dependency chart when using raw Kustomize installers because Kustomize does
  not enforce `kubeVersion` metadata.

## Layout

```
deploy/k8s/
├── deploy.sh              # internal execution engine used by profile wrappers
├── base/                  # canonical AKB + PostgreSQL resource composition
├── components/
│   └── sso/               # reusable Keycloak resources + AKB SSO patches
├── profiles/              # public, directly executable deployment combinations
│   ├── standalone/
│   ├── standalone-sso/
│   ├── standalone-secret-manager/
│   └── standalone-sso-secret-manager/
├── postgres.yaml          # pgvector/pgvector:pg16 StatefulSet — hosts both
│                          # the main DB and the vector_index schema
├── qdrant.yaml            # optional Qdrant StatefulSet — add from an
│                          # operator overlay when vector_store_driver=qdrant
├── redis.yaml             # optional event-stream Redis; add from an overlay
│                          # only when the OT Redis Operator is installed
├── backend.yaml           # Deployment + ConfigMap (vector_store_driver: pgvector)
├── frontend.yaml          # Deployment + Service
├── ingress.yaml           # placeholder host (akb.example.com)
├── secrets/               # stable Secret Contract v1 + manual/bundled/external producers
└── internal/              # gitignored — operator-private overlays
    ├── deploy-internal.sh
    ├── cluster-issuer.yaml
    ├── ingress-patch.yaml
    └── backend-config-patch.yaml
```

**Vector store**: the base ships with `vector_store_driver: pgvector`
inside the Postgres pod. Other options:

- **Qdrant** as a separate StatefulSet — add `qdrant.yaml` from an
  operator overlay and patch `akb-app-config` to set
  `vector_store_driver: qdrant` + `vector_url: http://qdrant:6333`.
- **Seahorse Cloud** (managed) — no extra StatefulSet; patch
  `akb-app-config` with `vector_store_driver: seahorse` +
  `seahorse_tenant_uuid` + `seahorse_table_name`/`seahorse_table_uuid`,
  and put `seahorse_token: shsk_<...>` in the secret. AKB calls the
  Seahorse BFF (`https://console.seahorse.dnotitia.ai/bff`) for table
  lifecycle and the per-table host for data CRUD/search.

An operator-owned `internal/` overlay can add the Qdrant pattern for its own
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
the [`profiles/standalone-sso`](profiles/standalone-sso) entry point. It
composes the reusable [`components/sso`](components/sso/README.md) resources,
which own the Keycloak lifecycle and dedicated database. Do not apply them to
a managed tenant that reuses a platform-owned/shared Keycloak realm.

## Quickstart (generic)

```bash
# 1. Provide a registry to push images to.
export REGISTRY=ghcr.io/myorg          # or my-registry.local:5000
# Required only when the cluster has no default StorageClass. The same value
# is used for AKB/PostgreSQL and a production bundled Secret Manager.
export STORAGE_CLASS=standard
# The default is linux/amd64. Use linux/arm64 when both the target nodes and
# caller-supplied/local bootstrap image are ARM64.
export IMAGE_PLATFORM=linux/amd64

# 2. In an operator-owned overlay, set the ingress host and make
#    public_base_url + local_session_issuer use that same public origin.
#    Pass the overlay directory as KUSTOMIZE_DIR in step 5.

# 3. Provide a ClusterIssuer named `letsencrypt-prod` (or change the
#    annotation in ingress.yaml). cert-manager + your DNS provider.

# 4. Choose one coherent installation path. This example creates a
#    brand-new local-auth/manual-Secret contract; production should provision
#    `akb-secret` out-of-band instead.
export GENERATE_MANUAL_SECRETS=true

# 5. Apply.
bash deploy/k8s/profiles/standalone/deploy.sh
```

`IMAGE_PLATFORM` controls both source builds and the short-lived local
container that generates bootstrap material. This keeps installation
architecture-consistent instead of silently forcing an AMD64 helper on an
ARM64 operator workstation or rehearsal cluster. Supported values are
`linux/amd64` and `linux/arm64`; `SKIP_BUILD=true` callers must supply images
matching the selected platform.

After the script finishes:

```bash
kubectl get pods -n "${NAMESPACE:-akb}"
kubectl get secret akb-secret -n "${NAMESPACE:-akb}"   # metadata only; do not decode into shell history
```

The placeholder ConfigMap in `backend.yaml` matches `config/app.yaml.example`
defaults (OpenAI embeddings, no LLM, no Redis, no S3) so the stack can
boot for smoke-testing before you wire in real providers. Put persistent
configuration changes in the overlay or Helm values; an imperative
`kubectl edit` will be overwritten by the next declarative deployment.

## Direct Kustomize or rendered YAML

The profile Kustomizations are valid application manifests and can be used
without `deploy.sh` when their prerequisites are already managed elsewhere:

```bash
# The public profile targets namespace `akb` and does not create it.
kubectl create namespace akb --dry-run=client -o yaml | kubectl apply -f -

# Create the complete Secret Contract v1 out-of-band before this step, then
# render a reviewable application bundle.
kubectl kustomize --load-restrictor=LoadRestrictionsNone \
  deploy/k8s/profiles/standalone > rendered-akb.yaml
kubectl apply -f rendered-akb.yaml
```

The profiles intentionally compose a canonical sibling base, so the default
load restriction used by `kubectl apply -k` rejects that source layout. Render
with the explicit load-restrictor option as shown above, review the resulting
plain YAML, and apply it with `kubectl apply -f`.

Use an overlay to change the namespace, image references, public origins,
storage classes, or provider configuration. For `*-secret-manager` profiles,
the static output still contains only the application layer: install VSO,
install and initialize Vault/OpenBao, bootstrap its policy/KV record, and wait
for `akb-secret` before applying it. The matching `deploy.sh` automates those
steps. Raw Helm has the same native initialization boundary; see the
[`AKB Helm guide`](../helm/akb/README.md#direct-helm-usage).

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
KUSTOMIZE_DIR="$(dirname "$0")" \
  bash "$(dirname "$0")/../profiles/standalone/deploy.sh"
```

Anything you put under `internal/` is automatically excluded by the
top-level `.gitignore`. Treat it as your private operations folder —
secrets management of choice (sealed-secrets, vault, cluster-bound
Secrets) goes here too.

## Secrets

The base no longer commits a PostgreSQL `change-me` Secret. It consumes the
stable, operator-independent `akb-secret` contract:

- PostgreSQL reads `akb-secret/db_password`.
- Backend and worker mount `akb-secret/secret.yaml`.
- Local auth projects `local-session-private.pem` and
  `local-session-jwks.json` from that same Secret.
- The backend disables automatic ServiceAccount-token mounting and has no
  Secret Manager or Kubernetes Secret API permission.

Choose one public path under [`profiles/`](profiles/README.md): `standalone`,
`standalone-sso`, `standalone-secret-manager`, or
`standalone-sso-secret-manager`. Each directory has its own executable
`deploy.sh`, `profile.env`, and application-layer `kustomization.yaml`. The two
`*-secret-manager` profiles bundle OpenBao or HashiCorp Vault; the other two
can either use an operator-owned Kubernetes Secret or adapt an existing
Vault-compatible endpoint with `SECRET_MODE=external`. See the complete
contracts, pinned chart versions, TLS/Raft profiles, native Shamir/PGP/Auto
Seal operations, rotation boundary, and migration notes in
[`secrets/README.md`](secrets/README.md).

The SSO profiles compose the SSO component over the canonical base and require coherent
`SSO_AKB_PUBLIC_URL` and `SSO_KEYCLOAK_PUBLIC_URL` origins plus the
product-admin identity. Authentication is fixed by the selected profile;
`SECRET_MODE=external` is an intentional adapter option for either non-bundled
profile. Always execute the matching profile path rather than the internal
common script.
