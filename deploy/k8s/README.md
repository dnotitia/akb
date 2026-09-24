# AKB Kubernetes deployment

The recommended new-install path is `native/`, with generated persistent
identity and explicit bootstrap. The base and standalone SSO remain explicit
Bare Git compatibility paths. None owns a credential-service lifecycle:

```text
deploy/k8s/
├── kustomization.yaml       # legacy Bare Git standalone + PostgreSQL
├── native/                  # recommended fresh Native installation
├── backend.yaml
├── frontend.yaml
├── postgres.yaml
├── ingress.yaml
├── deploy.sh                # optional build/apply convenience
├── standalone-sso/          # standalone plus owned Keycloak and its database
├── qdrant.yaml              # optional operator-owned addition
└── redis.yaml               # optional operator-owned addition
```

No profile creates Kubernetes Secrets, installs a credentials server, or
installs a cluster-scoped synchronization controller. The operator provisions
the required Secrets before applying AKB.

## Required Secrets

All profiles consume `Secret/akb-secret`. For local authentication it contains:

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

For a new Native installation, first follow the
[identity, Secret and image preparation procedure](../../docs/operations/native-installation.md#kubernetes),
then render `deploy/k8s/native`. It must never be applied to an existing Git
database. Before upgrading an existing installation,
[preserve the active revision config](../../docs/operations/native-installation.md#preserve-existing-configuration-before-changing-defaults).

Explicit legacy Standalone:

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

`deploy.sh` requires an explicit `AKB_PROFILE=standalone|standalone-sso` for
legacy deployments. It fails before any cluster operation if the profile is
omitted. New Native installations use the prepared overlay render/apply flow
above. The script never creates or modifies credentials.

Standalone with existing images:

```bash
NAMESPACE=akb \
AKB_PROFILE=standalone \
SKIP_BUILD=true \
BACKEND_IMAGE=ghcr.io/example/akb-backend:0.14.2 \
FRONTEND_IMAGE=ghcr.io/example/akb-frontend:0.14.1 \
POSTGRES_IMAGE=ghcr.io/example/akb-postgres:pg16 \
bash deploy/k8s/deploy.sh
```

Standalone SSO:

```bash
NAMESPACE=akb \
AKB_PROFILE=standalone-sso \
SKIP_BUILD=true \
BACKEND_IMAGE=ghcr.io/example/akb-backend:0.14.2 \
FRONTEND_IMAGE=ghcr.io/example/akb-frontend:0.14.1 \
POSTGRES_IMAGE=ghcr.io/example/akb-postgres:pg16 \
SSO_AKB_PUBLIC_URL=https://akb.example.com \
SSO_KEYCLOAK_PUBLIC_URL=https://auth.akb.example.com \
SSO_PRODUCT_ADMIN_USERNAME=admin \
SSO_PRODUCT_ADMIN_EMAIL=admin@example.com \
bash deploy/k8s/deploy.sh
```

Without `SKIP_BUILD=true`, set `REGISTRY`; the script builds and pushes the
backend, the frontend and `akb-postgres`. `akb-postgres` is
`deploy/postgres/Dockerfile`: the pinned pgvector image plus the `vchord_bm25`
BM25 index, which gives a new database the default `vchord` sparse shape. Its
tag comes from the Dockerfile's content, so an AKB upgrade that leaves it
unchanged does not restart PostgreSQL. With `SKIP_BUILD=true`, `POSTGRES_IMAGE`
names it. Leave it unset to keep the base manifest's stock pgvector image, on
which a new database gets `posting`. `KUBE_CONTEXT`, `IMAGE_PLATFORM`,
`STORAGE_CLASS`, and `KUSTOMIZE_DIR` remain optional operator inputs.

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

## Moving a pinned image

Container images here are pinned as `name:tag@sha256:...`. The tag is there so
the version is readable; the digest is what actually gets fetched, and it is
what makes two clusters running "the same manifest" actually run the same
bytes. A bare tag does not: `pgvector/pgvector:pg16` moved from PostgreSQL
16.13 to 16.15 without any manifest changing.

To move one, resolve the multi-architecture index digest — not a
single-platform manifest, or non-amd64 nodes stop scheduling:

```bash
docker buildx imagetools inspect pgvector/pgvector:pg16 --format '{{json .Manifest.Digest}}'
```

The plain `docker buildx imagetools inspect pgvector/pgvector:pg16` prints the
same value on its `Digest:` line, alongside a `MediaType:` that should read
`application/vnd.oci.image.index.v1+json` (or the Docker manifest-list
equivalent). If it names a single-platform manifest instead, the reference
resolved to one architecture and pinning it would strand every other node.

Then update every reference together. For PostgreSQL those are
`deploy/k8s/postgres.yaml`, `deploy/helm/akb/values.yaml`,
`deploy/all-in-one/Dockerfile`, `docker-compose.yaml`,
`eval/longmemeval/docker-compose.yaml`, `scripts/ci/dependency-compose.yaml`
and `.github/workflows/backend-pytest.yml`. Leaving one behind is worse than
pinning nothing, because CI then tests a version the deployment does not run.

## Helm

For a chart-based installation, see [`../helm/akb`](../helm/akb/README.md).
The Helm chart renders the same standalone and standalone-SSO shapes.

## Recommended PostgreSQL Native

For a never-used database, the `native/` overlay adds explicit bootstrap before
API/worker startup and uses empty read-only Git storage. Generate and preserve
installation identity, provision secrets, and pin all three backend images as
specified in [Native installation and configuration upgrades](../../docs/operations/native-installation.md).
Do not apply the new-install overlay to an existing Bare Git/cutover database.
