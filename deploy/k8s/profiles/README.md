# AKB Kubernetes deployment profiles

Every supported Kubernetes combination has one discoverable, directly
executable path. The directories compose the canonical AKB base and optional
SSO component; they do not copy complete manifest trees.

| Directory | Authentication | Secret source |
|---|---|---|
| `standalone/` | local | existing Kubernetes Secret or external adapter |
| `standalone-sso/` | owned Keycloak | existing Kubernetes Secret or external adapter |
| `standalone-secret-manager/` | local | bundled OpenBao or HashiCorp Vault |
| `standalone-sso-secret-manager/` | owned Keycloak | bundled OpenBao or HashiCorp Vault |

Run a complete profile from its own path:

```bash
NAMESPACE=akb-example \
SKIP_BUILD=true \
BACKEND_IMAGE=registry.example.com/akb-backend:0.14.2 \
FRONTEND_IMAGE=registry.example.com/akb-frontend:0.14.1 \
IMAGE_PLATFORM=linux/amd64 \
bash deploy/k8s/profiles/standalone/deploy.sh
```

Set `IMAGE_PLATFORM=linux/arm64` for ARM64 builds and bootstrap helpers. When
`SKIP_BUILD=true`, the supplied backend and frontend images must support the
same target architecture.

The `kustomization.yaml` in each directory is the application layer and can be
rendered independently. In `*-secret-manager` profiles, the sibling
`deploy.sh` additionally installs and initializes the selected official
Secret Manager chart and waits for VSO to produce Secret Contract v1 before
applying that layer. Native init/unseal cannot be represented safely as a
static manifest.

`deploy/k8s/deploy.sh` is the internal common execution engine. It deliberately
has no default profile and is not a public installation entry point. Profile
discovery, authentication selection, and manifest composition are owned by
these directories rather than hidden in that script. A non-bundled profile may
set `SECRET_MODE=external` to connect its unchanged Secret Contract to an
existing Vault-compatible endpoint.

Secret Manager profiles use the cluster prerequisite policy in
`deploy/cluster/ensure-vso.sh`. `VSO_MODE=auto` installs VSO once through the
separate `akb-cluster` Helm release or reuses a compatible Ready installation;
the AKB profile itself owns only namespace-scoped connection, authentication,
and Secret synchronization resources.
