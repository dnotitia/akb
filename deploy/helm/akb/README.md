# AKB Helm chart

This chart is the declarative, production-oriented AKB stack. Unlike the
historical `deploy/all-in-one` single-container demo, one Helm release may own
AKB, PostgreSQL, Keycloak, and OpenBao or HashiCorp Vault while preserving
separate Pods, PVCs, ServiceAccounts, and security boundaries. Cluster-scoped
prerequisites such as Vault Secrets Operator are deliberately owned by the
separate `akb-cluster` release.

## Profiles

Profiles are explicit values files, not copied manifest trees:

| Profile | AKB auth | Secret source | Workloads |
|---|---|---|---|
| `standalone` | local | existing Kubernetes Secret | AKB + PostgreSQL |
| `standalone-sso` | owned Keycloak | existing Kubernetes Secret | AKB + PostgreSQL + Keycloak + Keycloak DB |
| `standalone-secret-manager` | local | bundled OpenBao/Vault | AKB + PostgreSQL + Secret Manager |
| `standalone-sso-secret-manager` | owned Keycloak | bundled OpenBao/Vault | complete standalone stack |

The same templates render all profiles. `profile`, `sso.enabled`, Secret
Manager mode, engine dependency, and namespaced VSO projection are validated as one
contract so incompatible combinations fail during `helm template`.

## Source-tree install

The installer does not build images. Supply published images or use the chart
defaults when those names resolve in the cluster.

Manual Secret profile:

```bash
kubectl create namespace akb
# Create Secret/akb-secret out-of-band first. It is deliberately not stored in
# Helm values or release metadata.

RELEASE=akb \
NAMESPACE=akb \
AKB_PROFILE=standalone \
BACKEND_IMAGE=ghcr.io/example/akb-backend:0.14.2 \
FRONTEND_IMAGE=ghcr.io/example/akb-frontend:0.14.1 \
BOOTSTRAP_DOCKER_PLATFORM=linux/amd64 \
PUBLIC_URL=https://akb.example.com \
bash deploy/helm/akb/install.sh
```

The installer runs the backend image briefly on the operator workstation only
when it must generate new bootstrap material. Set
`BOOTSTRAP_DOCKER_PLATFORM=linux/arm64` when that image is ARM64; the supported
values are `linux/amd64` and `linux/arm64`. It does not change Kubernetes node
scheduling or an already published image manifest.

Complete SSO + OpenBao example:

```bash
RELEASE=akb \
NAMESPACE=akb \
AKB_PROFILE=standalone-sso-secret-manager \
SECRET_ENGINE=openbao \
SECRET_PROFILE=production \
SECRET_SEAL_MODE=plaintext \
SECRET_TOPOLOGY=production-ha \
VSO_MODE=managed \
SECRET_STORE_CERT_ISSUER_NAME=internal-ca \
PUBLIC_URL=https://akb.example.com \
SSO_KEYCLOAK_PUBLIC_URL=https://auth.akb.example.com \
BACKEND_IMAGE=ghcr.io/example/akb-backend:0.14.2 \
FRONTEND_IMAGE=ghcr.io/example/akb-frontend:0.14.1 \
bash deploy/helm/akb/install.sh
```

The first Helm pass creates the declarative release without waiting for a
sealed server. The installer then performs native init/unseal/bootstrap in the
trusted terminal, waits for VSO to project Secret Contract v1, and executes a
second idempotent `helm upgrade --install --wait` to gate the complete stack.

PGP mode requires the public keys before any namespace or release mutation:

```bash
SECRET_SEAL_MODE=pgp \
SECRET_KEY_SHARES=5 \
SECRET_KEY_THRESHOLD=3 \
SECRET_PGP_KEYS=/secure/a.asc,/secure/b.asc,/secure/c.asc,/secure/d.asc,/secure/e.asc \
SECRET_ROOT_TOKEN_PGP_KEY=/secure/bootstrap-root.asc \
... \
bash deploy/helm/akb/install.sh
```

The encrypted shares are native OpenBao/Vault output. Threshold holders still
decrypt and submit them interactively; no private key enters Helm or the
installer.

## Direct Helm usage

Dependencies are pinned and vendored for reproducible source installs:

```bash
helm dependency build deploy/helm/akb
helm upgrade --install akb deploy/helm/akb \
  --namespace akb --create-namespace \
  --values deploy/helm/akb/profiles/standalone.yaml \
  --set-string images.backend.repository=ghcr.io/example/akb-backend \
  --set-string images.backend.tag=0.14.2 \
  --set-string images.frontend.repository=ghcr.io/example/akb-frontend \
  --set-string images.frontend.tag=0.14.1 \
  --wait
```

For bundled production modes, raw `helm install` correctly leaves the server
sealed. Run `scripts/initialize-secret-manager.sh` afterward, or use
`install.sh` for the complete two-phase flow. A Helm hook is intentionally not
used because hook logs and release history are inappropriate custody channels
for root or recovery material.

## VSO ownership

VSO is a cluster-scoped prerequisite, not a child of an AKB instance release.
The pinned `akb-cluster` chart owns it once per Kubernetes cluster:

```bash
helm dependency build deploy/helm/akb-cluster
helm upgrade --install akb-cluster deploy/helm/akb-cluster \
  --namespace vault-secrets-operator --create-namespace --wait
```

`install.sh` performs the same prerequisite step automatically. Bundled profiles
default to `VSO_MODE=managed`, which installs or upgrades only the dedicated
`vault-secrets-operator/akb-cluster` release. External Secret Manager mode
defaults to `VSO_MODE=external`, which requires an existing compatible Ready
controller and never changes it. Manual Secret profiles use
`VSO_MODE=disabled` and do not need VSO.

Every AKB release still owns its namespace-local `VaultConnection`,
`VaultAuth`, ServiceAccount, `VaultStaticSecret`, CA reference, and destination
Secrets. A normal `helm uninstall akb` never removes or upgrades the shared
controller. Clusters belonging to different security administrators should use
separate Kubernetes clusters rather than competing VSO controllers against the
same cluster-wide CRDs.

A bundled Vault/OpenBao server also needs Kubernetes TokenReview permission.
AKB therefore owns one narrowly scoped ClusterRoleBinding to the built-in
`system:auth-delegator` role. Its name contains both namespace and Helm release,
and its only subject is that instance's `akb-secret-store` ServiceAccount, so
multiple AKB instances cannot collide or authenticate as one another.

## HashiCorp Vault and Auto Seal

HashiCorp Vault requires explicit acknowledgement:

```bash
SECRET_ENGINE=hashicorp-vault \
HASHICORP_LICENSE_ACKNOWLEDGED=true \
bash deploy/helm/akb/install.sh
```

For native Auto Seal, create the namespace-local Secret containing `seal.hcl`,
set `SECRET_STORE_SEAL_CONFIG_SECRET`, and pass workload-identity or
credential-reference overrides as additional Helm arguments. Recovery shares
remain native engine output.

## Upgrade and uninstall

Use the same profile values on every upgrade. The chart never generates or
rotates application secrets. `akb-vaultdata` and Secret Manager Raft PVCs are
retained; deleting retained data is a separate, explicit operator action. VSO
is upgraded only through the `akb-cluster` release. Use
`deploy/cluster/status-vso.sh` to list AKB consumers and
`deploy/cluster/uninstall-vso.sh` for guarded removal; the latter refuses to
run while any VSO custom resource remains.
