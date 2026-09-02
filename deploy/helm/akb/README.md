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

## Prerequisites

- Helm 3 and `kubectl`; the source-tree installer also uses standard POSIX
  command-line tools.
- Kubernetes 1.29 or later for the AKB chart. Bundled OpenBao requires
  Kubernetes 1.30 or later because of the selected upstream chart.
- A default StorageClass or `STORAGE_CLASS`, plus an ingress controller and
  operator-managed DNS/TLS for browser access.
- Docker only for the optional manual-Secret generator. Bundled Secret Manager
  bootstrap runs inside the chart-managed Job from the backend image.
- Cluster-administrator authority for `VSO_MODE=managed`. Use
  `VSO_MODE=external` when a separate platform team owns an existing compatible
  Vault Secrets Operator.

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
SSO_PRODUCT_ADMIN_USERNAME=admin \
SSO_PRODUCT_ADMIN_EMAIL=admin@example.com \
BACKEND_IMAGE=ghcr.io/example/akb-backend:0.14.2 \
FRONTEND_IMAGE=ghcr.io/example/akb-frontend:0.14.1 \
bash deploy/helm/akb/install.sh
```

These public URLs and product-administrator identifiers are operator inputs,
not values discovered after Keycloak starts. Choose the DNS names and initial
administrator identity first, route them to the ingresses, and replace every
`example.com` placeholder before production use. The installer generates the
one-time password separately; the username and email are not credentials.

The installer validates inputs, reconciles VSO when requested, and performs one
`helm upgrade --install --wait --wait-for-jobs`. The chart-managed bootstrap
Job performs native init, the first unseal, policy/KV setup, Secret Contract v1
generation, initial-root-token revocation, and the VSO projection gate. The
application starts only after the projected Secrets exist.

For the default plaintext-Shamir mode, Helm stores native initialization output
once in the retained `akb-secret-manager-recovery` Secret. The Job uses those
shares in memory for the first unseal and removes the transient root token after
bootstrap. Copy `recovery.json` to an approved off-cluster password manager or
secret custody system, verify the copy, and delete the Kubernetes Secret:

```bash
kubectl get secret akb-secret-manager-recovery -n akb \
  -o jsonpath='{.data.recovery\.json}' | base64 --decode
kubectl delete secret akb-secret-manager-recovery -n akb
```

Run the first command only in a trusted, non-recorded terminal; its output is
the real native unseal/recovery material. Losing enough shares to fall below
the threshold makes Shamir-sealed data unrecoverable.

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

The encrypted shares are native OpenBao/Vault output. The Job publishes only
those encrypted values, then waits. Threshold holders decrypt and submit the
required shares directly to the engine and place the decrypted initial root
token in the named bootstrap-input Secret so the Job can finish. No private
key enters Helm, the Job, or the installer.

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
  --wait --wait-for-jobs --timeout 35m
```

The command above uses a manual-Secret profile and therefore requires an
existing namespace-local `akb-secret`. For an SSO profile it must also contain
the SSO Secret Contract fields and projections described in the
[Secret management guide](../../k8s/secrets/README.md).

Before directly installing a Secret Manager profile, install or validate VSO
as described below. Then select `standalone-secret-manager.yaml` or
`standalone-sso-secret-manager.yaml` and add `--wait --wait-for-jobs`. A normal
chart-managed Job completes the same lifecycle as `install.sh`; it is not a
Helm hook, does not log generated values, and does not place generated values
in Helm release metadata. The one-time recovery Secret is the explicit custody
handoff.

Set `secretManager.bootstrap.enabled=false` only when another operator owns
initialization. In that advanced mode, `scripts/initialize-secret-manager.sh`
remains a manual recovery/bring-your-own-bootstrap tool; Helm will install the
components but cannot make a sealed store ready on its own.

To review static resources without applying them:

```bash
helm dependency build deploy/helm/akb
helm template akb deploy/helm/akb \
  --namespace akb \
  --values deploy/helm/akb/profiles/standalone.yaml \
  > rendered-akb.yaml
```

This rendered file contains the declarative bootstrap Job but does not execute
it. Applying the complete rendered YAML does execute the Job, but unlike
`helm --wait --wait-for-jobs`, plain `kubectl apply` does not wait for or report
its completion automatically.

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
Secrets. A direct `helm upgrade` or `helm uninstall` of the AKB release never
changes the shared controller. Running `install.sh` with `VSO_MODE=managed`
does reconcile the separate cluster release before the AKB release. Clusters
belonging to different security administrators should use
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

Use the same profile values on every upgrade. Bootstrap generates application
secrets only when the configured KV record is absent and otherwise preserves
it; upgrades do not rotate values implicitly. `akb-vaultdata` and Secret
Manager Raft PVCs are retained; deleting retained data is a separate, explicit
operator action. VSO is upgraded only through the `akb-cluster` release. Use
`deploy/cluster/status-vso.sh` to list AKB consumers and
`deploy/cluster/uninstall-vso.sh` for guarded removal; the latter refuses to
run while any VSO custom resource remains.
