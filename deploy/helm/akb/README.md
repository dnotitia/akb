# AKB Helm chart

This chart is the declarative, production-oriented AKB stack. Unlike the
historical `deploy/all-in-one` single-container demo, one Helm release may own
AKB, PostgreSQL, Keycloak, OpenBao or HashiCorp Vault, and Vault Secrets
Operator while preserving separate Pods, PVCs, ServiceAccounts, and security
boundaries.

## Profiles

Profiles are explicit values files, not copied manifest trees:

| Profile | AKB auth | Secret source | Workloads |
|---|---|---|---|
| `standalone` | local | existing Kubernetes Secret | AKB + PostgreSQL |
| `standalone-sso` | owned Keycloak | existing Kubernetes Secret | AKB + PostgreSQL + Keycloak + Keycloak DB |
| `standalone-secret-manager` | local | bundled OpenBao/Vault | AKB + PostgreSQL + Secret Manager + VSO |
| `standalone-sso-secret-manager` | owned Keycloak | bundled OpenBao/Vault | complete standalone stack |

The same templates render all profiles. `profile`, `sso.enabled`, Secret
Manager mode, engine dependency, and VSO projection are validated as one
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
PUBLIC_URL=https://akb.example.com \
bash deploy/helm/akb/install.sh
```

Complete SSO + OpenBao example:

```bash
RELEASE=akb \
NAMESPACE=akb \
AKB_PROFILE=standalone-sso-secret-manager \
SECRET_ENGINE=openbao \
SECRET_PROFILE=production \
SECRET_SEAL_MODE=plaintext \
SECRET_TOPOLOGY=production-ha \
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

The bundled profile enables the pinned official VSO dependency for a true
single-release on-prem installation. A shared cluster should install VSO once
under platform ownership and use:

```bash
INSTALL_VSO=false bash deploy/helm/akb/install.sh
```

Namespace-scoped `VaultConnection`, `VaultAuth`, and `VaultStaticSecret`
objects remain in each AKB release. Only the cluster-scoped controller is
shared.

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
retained; deleting retained data is a separate, explicit operator action.
