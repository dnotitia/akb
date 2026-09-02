# AKB Kubernetes Secret management

AKB has one application-facing Secret contract and several interchangeable
producers. The backend never receives a Vault/OpenBao token and never calls a
Secret Manager API.

```text
manual input ───────────────────────┐
bundled OpenBao ─┐                  │
                 ├─ VSO ───────────┼─> Kubernetes Secret `akb-secret` ─> AKB
bundled Vault ───┘                  │
external Vault-compatible store ───┘
```

This layout follows the current `akb-platform` workspace contract: PostgreSQL
reads `akb-secret/db_password`, while the backend mounts
`akb-secret/secret.yaml`. Standalone local auth additionally projects
`local-session-private.pem` and `local-session-jwks.json` from the same Secret.
The two SSO profiles make the same producer project the durable browser
session and three-client Keycloak contract, the dedicated Keycloak database
credential, and two narrowly scoped first-install Secrets.

It also follows the platform's tenancy boundary: one workspace is one
Kubernetes namespace. A bundled engine lives inside that workspace namespace;
an external engine may be shared, but its Kubernetes-auth role is bound to the
exact `akb-secret-sync` ServiceAccount and namespace and its KV path must be
unique to that workspace.

Exactly one controller may own `akb-secret`. In a current
`akb-platform`-managed namespace the `AkbInstance` reconciler already owns that
Secret, so do not install this VSO destination beside it. Use these profiles
for standalone AKB namespaces, or first add an explicit Secret-producer choice
to the platform controller and transfer ownership in a coordinated rollout.

## Secret Contract v1

Required keys in `akb-secret`:

| Key | Consumer | Rotation behavior |
|---|---|---|
| `secret.yaml` | backend API and worker | VSO restarts only `deployment/backend` |
| `db_password` | PostgreSQL initialization | durable; use the database rotation runbook, not a blind overwrite |
| `system_hmac_secret` | compatibility projection and audit | value also lives inside `secret.yaml` |
| `local-session-private.pem` | local-session signer | mounted as mode `0400` |
| `local-session-jwks.json` | local-session verifier | retain prior public keys during planned rotation |

The SSO profile adds these durable `akb-secret` keys (and the same values in
`secret.yaml`):

- `keycloak_client_secret` for `akb-web`
- `keycloak_admin_client_secret` for `akb-admin`
- `keycloak_management_client_secret` for `akb-sso-manager`
- `sso_browser_session_encryption_key` (32 random bytes, unpadded base64url)
- `sso_session_epoch` (installation UUID, not a credential)

It creates separate projections for `akb-keycloak-db-credentials`,
`akb-keycloak-bootstrap/client-secret`, and
`akb-product-admin-bootstrap/password`. The latter two are one-time inputs and
are never mounted into the serving containers.

The generated contract also carries the non-runtime projections
`jwt_secret`, `auth_runtime_contract`, `auth_runtime_generation`, and
`auth_runtime_mode`. These align standalone Secret ownership with the newer
platform-managed workspace shape without reintroducing `jwt_secret` into
`secret.yaml`.

`redis-credentials/password` is a compatibility destination for the optional
Redis Operator resource. It is generated from the same source record but is
not mounted by AKB.

## Installation profiles

The profile directory is the public installation choice. Authentication is
fixed by that profile; only the two non-bundled profiles accept the intentional
`SECRET_MODE=external` adapter option.

| Profile directory | Human auth | Bundled Secret Manager |
|---|---|---|
| `standalone` | local | no |
| `standalone-sso` | owned Keycloak | no |
| `standalone-secret-manager` | local | yes |
| `standalone-sso-secret-manager` | owned Keycloak | yes |

`standalone` and `standalone-sso` may set `SECRET_MODE=external`; that connects
an existing endpoint and does not add another workload. The two
`*-secret-manager` profiles require `SECRET_MODE=bundled`, which they select by
default.

These are routing profiles, not four copied deployment trees. `standalone` and
`standalone-secret-manager` compose `deploy/k8s/base`; `standalone-sso` and
`standalone-sso-secret-manager` compose that same base with
`deploy/k8s/components/sso`. A `*-secret-manager` profile
first prepares the bundled Secret Manager and VSO Secret contract, then applies
the corresponding existing AKB tree. `deploy/all-in-one` remains the separate
single-container demo image and does not bundle this production Secret Manager
lifecycle.

### Manual Secret ownership

In manual mode, the operator owns the Secret lifecycle. Before adopting this
contract, an existing installation must consolidate its prior secret material
into `akb-secret`. Do not decode values into shell history.

For a brand-new disposable installation, the deploy script can generate the
contract and pipe it directly to the Kubernetes API:

```bash
NAMESPACE=akb-dev \
GENERATE_MANUAL_SECRETS=true \
REGISTRY=registry.example.com \
bash deploy/k8s/profiles/standalone/deploy.sh
```

Omit `GENERATE_MANUAL_SECRETS` for production. The script then fails closed
unless the complete out-of-band Secret already exists.

The development bundled profiles also auto-generate first-install material.
They are not an in-place migration tool: if a PostgreSQL PVC already exists,
seed the external KV record with that database's current password and the
existing signing/HMAC material before switching producers. Never let the
development bootstrap invent a new password for an initialized database.

For a new SSO + Secret Manager bundle, choose the combined profile path and
provide the public origins:

```bash
NAMESPACE=akb-sso-dev \
SSO_AKB_PUBLIC_URL=https://akb-sso.example.com \
SSO_KEYCLOAK_PUBLIC_URL=https://auth-akb-sso.example.com \
SSO_PRODUCT_ADMIN_USERNAME=admin \
SSO_PRODUCT_ADMIN_EMAIL=admin@example.com \
SECRET_ENGINE=openbao \
SECRET_PROFILE=development \
REGISTRY=registry.example.com \
bash deploy/k8s/profiles/standalone-sso-secret-manager/deploy.sh
```

The generated product-admin password remains in the selected Secret Manager
until the installer hands it to the administrator. After the first successful
`bundled-keycloak-v3` report, follow the standalone SSO retirement procedure;
do not erase it before handoff or before the receipt exists.

### Bundled OpenBao

```bash
NAMESPACE=akb-openbao \
SECRET_ENGINE=openbao \
SECRET_PROFILE=development \
REGISTRY=registry.example.com \
bash deploy/k8s/profiles/standalone-secret-manager/deploy.sh
```

Pinned distribution:

- official `openbao/openbao` Helm chart `0.29.3`
- OpenBao `v2.6.2`

### Bundled HashiCorp Vault

The AKB repository does not copy or mirror Vault. Helm fetches the official
HashiCorp chart and image only when the operator chooses this mode. Review the
BSL terms before enabling it.

```bash
NAMESPACE=akb-vault \
SECRET_ENGINE=hashicorp-vault \
SECRET_PROFILE=development \
HASHICORP_LICENSE_ACKNOWLEDGED=true \
REGISTRY=registry.example.com \
bash deploy/k8s/profiles/standalone-secret-manager/deploy.sh
```

Pinned distribution:

- official `hashicorp/vault` Helm chart `0.34.1`
- HashiCorp Vault `2.0.4`

### External OpenBao or Vault

External mode installs no Secret Manager server. It creates only a
namespace-local ServiceAccount, `VaultConnection`, `VaultAuth`, and the
`VaultStaticSecret` projections required by the selected auth profile.

```bash
NAMESPACE=akb-external \
SECRET_MODE=external \
SECRET_ENGINE=hashicorp-vault \
SECRET_STORE_ADDRESS=https://vault.example.com \
SECRET_STORE_CA_SECRET=vault-ca \
KUBERNETES_AUTH_MOUNT=kubernetes \
VAULT_ROLE=akb-runtime-reader \
KV_MOUNT=kv \
KV_PATH=akb/production/runtime \
REGISTRY=registry.example.com \
bash deploy/k8s/profiles/standalone/deploy.sh
```

The external server must already have Kubernetes auth, a least-privilege role,
and a KV v2 record with the source fields used by
`vso-vault-compatible.yaml` (local) or
`vso-vault-compatible-sso.yaml` (SSO). HTTP is rejected unless the explicitly
unsafe development override is set.

## Vault Secrets Operator

VSO is a cluster-scoped dependency owned separately from every AKB namespace.
The Kustomize and Helm installers share the same prerequisite policy:

```bash
VSO_MODE=managed ... bash deploy/k8s/profiles/standalone-secret-manager/deploy.sh
```

`managed` installs or upgrades only the dedicated
`vault-secrets-operator/akb-cluster` release. `external` is read-only and fails
if VSO is absent, unavailable, ambiguous, missing required CRDs, or outside the
supported version range. Bundled profiles default to `managed`; external Secret
Manager mode defaults to `external`; `disabled` is valid only for manual Secret
profiles.

The pinned official HashiCorp VSO version is `1.5.1`; the currently supported
reuse range is `>=1.4.0,<1.6.0`. VSO is BSL 1.1, so deployments that require an
entirely OSI-licensed stack must use and test a separate OpenBao/External
Secrets Operator adapter before declaring that profile supported. The current
OpenBao bundle intentionally uses the same Vault-compatible VSO adapter so both
engines exercise an identical AKB contract.

One VSO controller installation can connect to many per-AKB Vault/OpenBao
servers. Authentication remains namespaced: every AKB release owns a distinct
ServiceAccount, `VaultConnection`, `VaultAuth`, Vault role/policy binding, CA
reference, and destination Secret. There is no shared filesystem. Normal AKB
uninstall never removes the cluster prerequisite.

VSO authentication uses a projected, 10-minute ServiceAccount token with
audience `vault`. The AKB backend has
`automountServiceAccountToken: false` and has no Secret API or Secret Manager
permissions.

## Development and production profiles

`development` is deliberately ephemeral:

- single server
- in-memory dev storage
- HTTP inside the namespace
- generated dev root token
- no backup or upgrade guarantee

It is only for local and isolated namespace tests.
The token is retained in the namespace-local Helm release metadata so an
idempotent rerun can authenticate to the still-running OnDelete pod. This is
another reason the profile is forbidden for production.
New bundled installs use a short namespace-derived Helm release name so the
charts' cluster-scoped auth-delegator bindings do not collide across AKB
namespaces. A namespace that already has the historical `akb-secret-store`
release keeps that name on upgrade.

`production` renders a fail-closed persistent baseline:

- `SECRET_TOPOLOGY=onprem-small` for one persistent Raft member, or
  `SECRET_TOPOLOGY=production-ha` for three
- integrated Raft
- TLS from `akb-secret-store-tls`
- retained data and audit PVCs
- parallel Pod creation and automatic Raft `retry_join`
- a complete init, unseal, bootstrap, VSO projection, and AKB startup lifecycle

TLS has two supported ownership paths:

- create `akb-secret-store-tls` with `tls.crt`, `tls.key`, and `ca.crt`
  out-of-band; or
- point the installer at an existing cert-manager CA `Issuer` or
  `ClusterIssuer`. It creates and renews only the namespace-local Certificate:

```bash
SECRET_STORE_CERT_ISSUER_NAME=workspace-ca \
SECRET_STORE_CERT_ISSUER_KIND=ClusterIssuer \
... \
bash deploy/k8s/profiles/standalone-secret-manager/deploy.sh
```

The rendered Certificate covers the engine Service and all three possible Pod
DNS names under the chart's `*-internal` headless Service. The issuer must
populate `ca.crt`; a public ACME certificate is not suitable for these private
cluster DNS names. The bundled profile remains bundled on reruns and is never
reclassified as external.

### Production seal patterns

AKB uses only native Vault/OpenBao initialization formats:

```text
ordinary installation -> plaintext operator init -> administrator stores shares
multi-admin security   -> operator init -pgp-keys -> key holders decrypt/submit
unattended restart     -> KMS/HSM/Transit seal -> native Auto Unseal
```

No AKB-specific Recovery Kit, unlock code, or key-encryption format is created.

#### Plaintext Shamir (default)

Run from a trusted interactive terminal that is not recorded:

```bash
SECRET_ENGINE=openbao \
SECRET_PROFILE=production \
SECRET_SEAL_MODE=plaintext \
SECRET_TOPOLOGY=production-ha \
... \
bash deploy/k8s/profiles/standalone-sso-secret-manager/deploy.sh
```

The installer prints the official `operator init` values once and waits for
the operator to type `STORED`. It then uses the same in-memory shares to unseal
all Raft members, configures KV and Kubernetes auth, seeds Secret Contract v1,
creates the short-lived operator-admin login boundary described below, revokes
the initial root token, verifies revocation, and lets VSO project the contract
before databases or applications start. It creates no durable local key file.

#### PGP Shamir

Provide one PGP public-key path per share plus a public key for the bootstrap
administrator's initial root token. Binary, base64-encoded, and common
ASCII-armoured (`.asc`) public exports are accepted; ASCII armour is normalized
to the engine's required base64 packet form before upload:

```bash
SECRET_ENGINE=openbao \
SECRET_PROFILE=production \
SECRET_SEAL_MODE=pgp \
SECRET_KEY_SHARES=5 \
SECRET_KEY_THRESHOLD=3 \
SECRET_PGP_KEYS=/secure/a.asc,/secure/b.asc,/secure/c.asc,/secure/d.asc,/secure/e.asc \
SECRET_ROOT_TOKEN_PGP_KEY=/secure/bootstrap-admin.asc \
... \
bash deploy/k8s/profiles/standalone-sso-secret-manager/deploy.sh
```

`SECRET_PGP_KEYS`, `SECRET_ROOT_TOKEN_PGP_KEY`, a valid share count, and a valid
threshold are mandatory installation inputs. The top-level deployer validates
them before building images, creating a namespace, or installing a Helm chart.
`-pgp-keys` then encrypts each generated Unseal Share to the corresponding
public key in input order. It does not encrypt the root token; that requires the
separate `-root-token-pgp-key` option. Only after native initialization has
produced those encrypted values does the installer enter
`AwaitingKeyHolderUnseal`. Each threshold holder decrypts their own share on
their secure workstation and submits it through the native hidden
`operator unseal` prompt. The installer never receives private keys.

Each holder can decode their assigned output on their own workstation; the
result is the plaintext share accepted by the hidden prompt:

```bash
printf '%s' '<that holder encrypted share>' | \
  openssl base64 -d -A | gpg --decrypt
```

The bootstrap administrator performs the same operation for the separately
encrypted initial root token, enters it only when the installer asks, and does
not retain it as day-2 authority after revocation succeeds.

Because the installer cannot decrypt those shares, this mode intentionally
requires key-holder participation during initial installation and every
Shamir restart. Repeating one administrator's public key technically works but
does not provide multi-party control; use plaintext mode for a single-holder
installation instead.

#### Auto Seal

Create a namespace-local Secret whose `seal.hcl` key contains one native,
supported KMS/HSM/Transit `seal` stanza. The installer mounts it as a second
server config file; the repository and Helm release contain no seal
credentials. Use workload identity when possible, or use a private extra
values file only for environment-variable references to an existing
Kubernetes Secret:

```bash
kubectl create secret generic akb-secret-store-seal \
  -n akb-production \
  --from-file=seal.hcl=/secure/seal.hcl

SECRET_ENGINE=openbao \
SECRET_PROFILE=production \
SECRET_SEAL_MODE=auto \
SECRET_STORE_SEAL_CONFIG_SECRET=akb-secret-store-seal \
SECRET_STORE_EXTRA_VALUES=/secure/openbao-workload-identity.yaml \
... \
bash deploy/k8s/profiles/standalone-sso-secret-manager/deploy.sh
```

`server.extraVolumes[0]` is reserved for this mounted seal-config Secret. Do
not redefine that slot in `SECRET_STORE_EXTRA_VALUES`. The `seal.hcl` file is
the engine's native HCL, not an AKB format. For example, Transit configuration
contains the parent address, key name, mount path, and a token supplied by an
indirect mechanism; AWS/Azure/GCP configurations should rely on pod/workload
identity rather than static access keys.

The provider must be healthy before initialization. Vault/OpenBao stores its
root key wrapped by that provider and automatically unwraps it on restart.
This is not Shamir shares stored in KMS. Initialization still returns Recovery
Shares; provide `SECRET_RECOVERY_PGP_KEYS` to use native
`-recovery-pgp-keys`, and optionally `SECRET_ROOT_TOKEN_PGP_KEY` for the
initial root token.

Recovery Shares authorize privileged operations but cannot replace a missing
Auto Seal provider. Permanently deleting the provider key can make the cluster
and snapshots unrecoverable, so deletion protection and a tested provider
recovery policy are mandatory.

### Idempotency and recovery

Successful production bootstrap records only a non-sensitive ConfigMap receipt
(`akb-secret-manager-bootstrap`): cluster ID, engine, seal type, contract
version, operator-role identity, and the fact that the initial root token was
revoked. Reruns never call `operator init` again.

If a Shamir cluster is already initialized but sealed, the installer shows the
native `operator unseal` command and waits for the operator or key holders. If
an Auto Seal cluster stays sealed, the installer fails closed and directs the
operator to repair the provider. An initialized cluster without a bootstrap
receipt requires an explicitly supplied interactive root credential to resume;
the installer does not invent replacement keys.

Never store KMS credentials, recovery keys, root tokens, or actual AKB Secret
values in this repository or Helm values.

### Day-2 operator access

The initial root token is one-time bootstrap authority, not a permanent admin
login. Before revoking it, the installer creates:

- policy `akb-operator-admin`;
- Kubernetes-auth role `akb-operator-admin` (30-minute token, 4-hour maximum);
- non-automounted ServiceAccount `akb-secret-admin`.

An operator must already have Kubernetes RBAC permission to request a token for
that exact ServiceAccount. That permission is therefore equivalent to
short-lived Secret Manager administration and must be limited to the platform
operator group. No reusable ServiceAccount JWT or Vault/OpenBao token is
stored in a Secret.

The following pattern keeps the audience JWT off command arguments and obtains
a bounded admin token. Substitute the engine command/environment names for
HashiCorp Vault:

```bash
NAMESPACE=akb-production
POD=<release>-openbao-0
TLS_NAME="${POD}.<release>-openbao-internal.${NAMESPACE}.svc"

OPERATOR_TOKEN="$(
  kubectl create token akb-secret-admin -n "${NAMESPACE}" \
    --audience=vault --duration=10m | \
  kubectl exec -i -n "${NAMESPACE}" "${POD}" -- \
    env BAO_ADDR=https://127.0.0.1:8200 \
        BAO_CACERT=/openbao/tls/ca.crt \
        BAO_TLS_SERVER_NAME="${TLS_NAME}" \
        bao write -format=json auth/kubernetes/login \
          role=akb-operator-admin jwt=- | jq -r .auth.client_token
)"

kubectl exec -n "${NAMESPACE}" "${POD}" -- \
  env BAO_ADDR=https://127.0.0.1:8200 \
      BAO_CACERT=/openbao/tls/ca.crt \
      BAO_TLS_SERVER_NAME="${TLS_NAME}" \
      BAO_TOKEN="${OPERATOR_TOKEN}" \
      bao operator raft list-peers
unset OPERATOR_TOKEN
```

The broad admin policy is intentionally paired with a short TTL and explicit
Kubernetes TokenRequest. Use it for policy/auth maintenance, Raft snapshots,
and diagnostics; AKB itself continues to use only the read-only VSO role. If
Kubernetes auth is unavailable, the stored Shamir shares (or Auto Seal
Recovery Shares) remain the break-glass path for the native generate-root
ceremony.

## Rotation boundary

VSO detects KV changes with HMAC and recreates `akb-secret`; only the backend
rollout is automatic. Database password rotation is intentionally not
automatic because changing the Kubernetes value does not change the existing
PostgreSQL role password. A safe database rotation must update PostgreSQL,
publish the new contract, validate new connections, and then revoke the old
credential.

`system_hmac_secret` is deliberately projected both as a top-level platform
compatibility key and inside `secret.yaml`. Rotate both source fields in one KV
version. The same atomic-version rule applies to any value duplicated between
the platform compatibility projection and the application configuration blob.

SSO database and one-time bootstrap projections deliberately have no automatic
rollout target. A blind Keycloak database password overwrite does not update
the PostgreSQL role, and an automatic bootstrap restart could race the
receipt-backed retirement ceremony. Rotate those values only with their
specific runbooks. An `sso_session_epoch` rotation also requires incrementing
`auth_runtime_generation`; changing only the UUID is rejected by the runtime
boundary rather than silently invalidating an unrecorded set of sessions.

## Test isolation

Always use a new namespace and explicit context for rehearsals:

```bash
KUBE_CONTEXT=kubernetes-admin@kubernetes \
NAMESPACE=akb-secret-openbao-<date> \
SECRET_ENGINE=openbao \
SECRET_PROFILE=development \
... \
bash deploy/k8s/profiles/standalone-secret-manager/deploy.sh
```

Do not point a rehearsal at the existing `akb`, `akb-platform`, or managed
workspace namespaces. The scripts create and mutate only the namespace passed
through `NAMESPACE`, except for `VSO_MODE=managed`, which owns the
cluster-scoped `vault-secrets-operator/akb-cluster` prerequisite release. That
action must be run only against the intended cluster context. Use
`deploy/cluster/status-vso.sh` for an inventory and the guarded
`deploy/cluster/uninstall-vso.sh` helper for removal.
