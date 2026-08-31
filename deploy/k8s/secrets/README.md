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
With `AUTH_PROFILE=sso`, the same producer also projects the durable browser
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

## Modes

### Manual

The default is backward-compatible operator ownership. Existing installations
must migrate their old `akb-secret-config`, `akb-local-session-keys`, and
`postgres-credentials` material into `akb-secret` before applying the updated
base. Do not decode values into shell history.

For a brand-new disposable installation, the deploy script can generate the
contract and pipe it directly to the Kubernetes API:

```bash
NAMESPACE=akb-dev \
AUTH_PROFILE=local \
SECRET_MODE=manual \
GENERATE_MANUAL_SECRETS=true \
REGISTRY=registry.example.com \
bash deploy/k8s/deploy.sh
```

Omit `GENERATE_MANUAL_SECRETS` for production. The script then fails closed
unless the complete out-of-band Secret already exists.

The development bundled profiles also auto-generate first-install material.
They are not an in-place migration tool: if a PostgreSQL PVC already exists,
seed the external KV record with that database's current password and the
existing signing/HMAC material before switching producers. Never let the
development bootstrap invent a new password for an initialized database.

For a new SSO bundle, set `AUTH_PROFILE=sso` and the required public origins.
The top-level deployer then selects `deploy/k8s/standalone-sso` automatically:

```bash
NAMESPACE=akb-sso-dev \
AUTH_PROFILE=sso \
SSO_AKB_PUBLIC_URL=https://akb-sso.example.com \
SSO_KEYCLOAK_PUBLIC_URL=https://auth-akb-sso.example.com \
SSO_PRODUCT_ADMIN_USERNAME=admin \
SSO_PRODUCT_ADMIN_EMAIL=admin@example.com \
SECRET_MODE=bundled \
SECRET_ENGINE=openbao \
SECRET_PROFILE=development \
REGISTRY=registry.example.com \
bash deploy/k8s/deploy.sh
```

The generated product-admin password remains in the selected Secret Manager
until the installer hands it to the administrator. After the first successful
`bundled-keycloak-v3` report, follow the standalone SSO retirement procedure;
do not erase it before handoff or before the receipt exists.

### Bundled OpenBao

```bash
NAMESPACE=akb-openbao \
SECRET_MODE=bundled \
SECRET_ENGINE=openbao \
SECRET_PROFILE=development \
REGISTRY=registry.example.com \
bash deploy/k8s/deploy.sh
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
SECRET_MODE=bundled \
SECRET_ENGINE=hashicorp-vault \
SECRET_PROFILE=development \
HASHICORP_LICENSE_ACKNOWLEDGED=true \
REGISTRY=registry.example.com \
bash deploy/k8s/deploy.sh
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
bash deploy/k8s/deploy.sh
```

The external server must already have Kubernetes auth, a least-privilege role,
and a KV v2 record with the source fields used by
`vso-vault-compatible.yaml` (local) or
`vso-vault-compatible-sso.yaml` (SSO). HTTP is rejected unless the explicitly
unsafe development override is set.

## Vault Secrets Operator

VSO is a cluster-scoped dependency. The installer reuses an existing VSO by
default and will not silently add CRDs or cluster roles. On a new standalone
cluster, explicitly opt in:

```bash
INSTALL_VSO=true ... bash deploy/k8s/deploy.sh
```

This installs the pinned official HashiCorp VSO chart `1.5.1`. VSO is BSL 1.1,
so deployments that require an entirely OSI-licensed stack must use and test a
separate OpenBao/External Secrets Operator adapter before declaring that
profile supported. The current OpenBao bundle intentionally uses the same
Vault-compatible VSO adapter so both engines exercise an identical AKB
contract; compatibility is covered by the deployment E2E suite.

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

`production` renders a fail-closed baseline:

- three replicas
- integrated Raft
- TLS from `akb-secret-store-tls`
- retained data and audit PVCs
- explicit initialization/unseal ceremony

Create `akb-secret-store-tls` with `tls.crt`, `tls.key`, and `ca.crt` before
installing. Certificates must include the engine's service and pod DNS names.
The profile installs the HA servers, then intentionally exits before applying
AKB because recovery keys and the initial root credential must be handed off
outside the cluster. Configure KMS auto-unseal through a private
`SECRET_STORE_EXTRA_VALUES` file or initialize/unseal manually, seed KV and
Kubernetes auth, then consume the initialized service through `external` mode.

Never store KMS credentials, recovery keys, root tokens, or actual AKB Secret
values in this repository or Helm values.

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
SECRET_MODE=bundled \
SECRET_ENGINE=openbao \
SECRET_PROFILE=development \
... \
bash deploy/k8s/deploy.sh
```

Do not point a rehearsal at the existing `akb`, `akb-platform`, or managed
workspace namespaces. The scripts create and mutate only the namespace passed
through `NAMESPACE`, except for the explicit `INSTALL_VSO=true` cluster-scoped
operator installation.
