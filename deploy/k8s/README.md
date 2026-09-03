# AKB Kubernetes deployment

The Kubernetes tree has two application shapes and no credential-service
lifecycle:

```text
deploy/k8s/
├── kustomization.yaml       # standalone AKB + PostgreSQL
├── backend.yaml
├── frontend.yaml
├── postgres.yaml
├── ingress.yaml
├── deploy.sh                # optional build/apply convenience
├── standalone-sso/          # standalone plus owned Keycloak and its database
├── qdrant.yaml              # optional operator-owned addition
└── redis.yaml               # optional operator-owned addition
```

Neither shape creates Kubernetes Secrets, installs a credentials server, or
installs a cluster-scoped synchronization controller. The operator provisions
the required Secrets before applying AKB.

## Required Secrets

Both shapes consume `Secret/akb-secret`. For local authentication it contains:

| Key | Purpose |
|---|---|
| `db_password` | PostgreSQL container password |
| `system_hmac_secret` | Stable platform compatibility projection |
| `secret.yaml` | Sensitive backend settings, including the same database and HMAC values |
| `local-session-private.pem` | Local RS256 session signer |
| `local-session-jwks.json` | Local RS256 public keyset |
| `auth_runtime_contract` | `local-session-rs256-v2` |
| `auth_runtime_generation` | Positive generation, initially `1` |
| `auth_runtime_mode` | `local` |

Generate the local key pair with the backend CLI. Create the Secret through
the operator's normal credential process; this repository does not generate or
rotate it during deployment.

The `standalone-sso` shape uses `auth_runtime_mode=sso` and requires the SSO
runtime values inside `secret.yaml`. It also uses the stable top-level
projections documented in
[`standalone-sso/README.md`](standalone-sso/README.md) and these additional
Secrets:

- `akb-keycloak-db-credentials`
- `akb-keycloak-bootstrap`
- `akb-product-admin-bootstrap`
- optional `akb-keycloak-upgrade` for the documented legacy SSO upgrade only

Kubernetes Secret data is base64-encoded, not encrypted by that encoding.
Production operators should use appropriate RBAC, etcd encryption at rest,
restricted backups, and their existing credential source.

## Render directly

Standalone:

```bash
kubectl kustomize --load-restrictor=LoadRestrictionsNone deploy/k8s \
  > rendered-akb.yaml
```

Standalone SSO:

```bash
kubectl kustomize --load-restrictor=LoadRestrictionsNone \
  deploy/k8s/standalone-sso > rendered-akb-sso.yaml
```

The checked-in hostnames are examples. Patch image references, ingress hosts,
TLS settings, storage classes, and provider configuration in an
operator-owned overlay before applying either render.

## Convenience deployer

`deploy.sh` preserves the pre-profile build/apply workflow. It never creates
or modifies credentials.

Standalone with existing images:

```bash
NAMESPACE=akb \
AKB_PROFILE=standalone \
SKIP_BUILD=true \
BACKEND_IMAGE=ghcr.io/example/akb-backend:0.14.2 \
FRONTEND_IMAGE=ghcr.io/example/akb-frontend:0.14.1 \
bash deploy/k8s/deploy.sh
```

Standalone SSO:

```bash
NAMESPACE=akb \
AKB_PROFILE=standalone-sso \
SKIP_BUILD=true \
BACKEND_IMAGE=ghcr.io/example/akb-backend:0.14.2 \
FRONTEND_IMAGE=ghcr.io/example/akb-frontend:0.14.1 \
SSO_AKB_PUBLIC_URL=https://akb.example.com \
SSO_KEYCLOAK_PUBLIC_URL=https://auth.akb.example.com \
SSO_PRODUCT_ADMIN_USERNAME=admin \
SSO_PRODUCT_ADMIN_EMAIL=admin@example.com \
bash deploy/k8s/deploy.sh
```

Without `SKIP_BUILD=true`, set `REGISTRY`; the script builds and pushes both
images. `KUBE_CONTEXT`, `IMAGE_PLATFORM`, `STORAGE_CLASS`, and
`KUSTOMIZE_DIR` remain optional operator inputs.

## Removing a legacy bundled credential service

This section applies only to an installation created by AKB chart `0.1.x` or
the former credential-service deployment profiles. Do not perform an in-place
upgrade until the AKB Kubernetes Secrets are independent from their old
`VaultStaticSecret` owners.

1. Back up the credential source, its data volume or snapshot, and the current
   Kubernetes Secrets through the approved operator process.
2. Confirm the application Secrets currently exist:

   ```bash
   kubectl get secret -n <namespace> \
     akb-secret akb-keycloak-db-credentials \
     akb-keycloak-bootstrap akb-product-admin-bootstrap
   ```

   Omit the three SSO-only names for a local installation.
3. Orphan the legacy projection objects so Kubernetes preserves their Secret
   children instead of garbage-collecting them:

   ```bash
   kubectl delete vaultstaticsecret -n <namespace> \
     akb-runtime akb-redis akb-keycloak-database \
     akb-keycloak-bootstrap akb-product-admin-bootstrap \
     --cascade=orphan --ignore-not-found
   ```

4. Verify every required Secret still exists and has no
   `VaultStaticSecret` owner reference. The current Helm chart and `deploy.sh`
   refuse to proceed while that ownership remains.
5. Deploy AKB with `standalone` or `standalone-sso`, then verify database,
   login, and application health.
6. Only after AKB is healthy, decommission the old server release and retained
   storage according to the operator's backup policy. A shared cluster
   controller must be removed only by its cluster owner after confirming that
   no other namespace consumes it.

`--cascade=orphan` is intentional: ordinary deletion would also delete the
generated Secret children and make the subsequent AKB rollout fail.

## Helm

For a chart-based installation, see [`../helm/akb`](../helm/akb/README.md).
The Helm chart renders the same standalone and standalone-SSO shapes.
