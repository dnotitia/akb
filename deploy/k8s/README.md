# Kubernetes deploy

Generic kustomize base for deploying AKB to a Kubernetes cluster. Pair
with an operator-specific overlay for real hostnames, registries, and
TLS issuers.

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
├── redis.yaml             # event-stream Redis (optional, gated by app.yaml)
├── backend.yaml           # Deployment + ConfigMap (vector_store_driver: pgvector)
├── frontend.yaml          # Deployment + Service
├── ingress.yaml           # placeholder host (akb.example.com)
└── internal/              # gitignored — operator-private overlays
    ├── deploy-internal.sh
    ├── cluster-issuer.yaml
    ├── ingress-patch.yaml
    └── backend-config-patch.yaml
```

**Vector store**: the base ships with `vector_store_driver: pgvector`
inside the Postgres pod. To run Qdrant as a separate StatefulSet
instead, add `qdrant.yaml` to `kustomization.yaml` and patch
`akb-app-config` to set `vector_store_driver: qdrant` and
`vector_url: http://qdrant:6333`. The internal/ overlay does exactly
that for the production cluster.

## Quickstart (generic)

```bash
# 1. Provide a registry to push images to.
export REGISTRY=ghcr.io/myorg          # or my-registry.local:5000
export PUBLIC_URL=https://akb.example.com    # printed at the end; optional

# 2. Edit ingress.yaml to set your real hostname.
$EDITOR deploy/k8s/ingress.yaml

# 3. Provide a ClusterIssuer named `letsencrypt-prod` (or change the
#    annotation in ingress.yaml). cert-manager + your DNS provider.

# 4. Apply.
bash deploy/k8s/deploy.sh
```

After the script finishes:

```bash
kubectl edit configmap akb-app-config -n akb   # set embed_*, llm_*, s3_*, public_base_url
kubectl edit secret    akb-secret-config -n akb # set jwt_secret, embed_api_key, …
```

The placeholder ConfigMap in `backend.yaml` matches `config/app.yaml.example`
defaults (OpenAI embeddings, no LLM, no Redis, no S3) so the stack can
boot for smoke-testing before you wire in real providers.

## Operator-specific overlay (`internal/`)

The `internal/` directory is gitignored and intended for environment-
specific overrides — real hostnames, internal registries, ClusterIssuers
with DNS-01 credentials, ConfigMap with private endpoints. The simplest
pattern is a small wrapper script:

```bash
# deploy/k8s/internal/deploy-internal.sh
export REGISTRY=my-registry.internal:5000
export PUBLIC_URL=https://akb.mycorp.example
kubectl apply -f "$(dirname "$0")/cluster-issuer.yaml"
bash "$(dirname "$0")/../deploy.sh"
kubectl apply -f "$(dirname "$0")/ingress.yaml"        # overrides base
kubectl apply -f "$(dirname "$0")/backend-config.yaml" # overrides base ConfigMap
kubectl rollout restart deployment/backend -n akb
```

Anything you put under `internal/` is automatically excluded by the
top-level `.gitignore`. Treat it as your private operations folder —
secrets management of choice (sealed-secrets, vault, cluster-bound
Secrets) goes here too.

## Secrets

Two Secrets are NOT created by `deploy.sh` — manage them out-of-band so
re-runs don't clobber real credentials with placeholders:

- `akb-secret-config` — `secret.yaml` mounted at `/etc/akb/secret.yaml`
  in the backend pod. Required keys: `db_password`, `jwt_secret`,
  `embed_api_key` (and optionally `llm_api_key`, `rerank_api_key`,
  `s3_*_key`, `vector_api_key`, `redis_password`).
- `redis-credentials` — single key `password`, referenced by the Redis
  CR. Skip if you disable the event stream (`redis_url: ""`).

```bash
kubectl create secret generic akb-secret-config -n akb \
  --from-file=secret.yaml=./secret.yaml

PW=$(openssl rand -base64 32)
kubectl create secret generic redis-credentials -n akb \
  --from-literal=password="$PW"
```
