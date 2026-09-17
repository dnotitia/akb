# AKB Helm chart

This dependency-free chart installs one of two AKB application shapes:

| Values file | Workloads | Human authentication |
|---|---|---|
| `profiles/standalone.yaml` | AKB + PostgreSQL | Local |
| `profiles/standalone-sso.yaml` | AKB + PostgreSQL + Keycloak + Keycloak PostgreSQL | SSO |

The chart does not create credentials, install a credentials server, or
install a cluster-scoped synchronization controller. It consumes existing
Kubernetes Secrets described in the
[`Kubernetes deployment guide`](../../k8s/README.md).

## Prerequisites

- Kubernetes 1.29 or later
- Helm 3
- the target namespace
- a default StorageClass, or explicit chart storage-class values
- the required Kubernetes Secrets in the target namespace
- an ingress controller and operator-managed DNS/TLS for browser access

## Standalone

```bash
kubectl create namespace akb
# Provision Secret/akb-secret through the operator's credential process.

helm upgrade --install akb deploy/helm/akb \
  --namespace akb \
  --values deploy/helm/akb/profiles/standalone.yaml \
  --set-string images.backend.repository=ghcr.io/example/akb-backend \
  --set-string images.backend.tag=0.14.2 \
  --set-string images.frontend.repository=ghcr.io/example/akb-frontend \
  --set-string images.frontend.tag=0.14.1 \
  --set-string global.publicUrl=https://akb.example.com \
  --set-string ingress.host=akb.example.com \
  --wait
```

Set `secretContract.name` when the operator-owned runtime Secret uses a
different name.

## Standalone SSO

Provision `akb-secret` and the Keycloak/first-install Secrets listed in the
Kubernetes guide, then run:

```bash
helm upgrade --install akb deploy/helm/akb \
  --namespace akb \
  --values deploy/helm/akb/profiles/standalone-sso.yaml \
  --set-string images.backend.repository=ghcr.io/example/akb-backend \
  --set-string images.backend.tag=0.14.2 \
  --set-string images.frontend.repository=ghcr.io/example/akb-frontend \
  --set-string images.frontend.tag=0.14.1 \
  --set-string global.publicUrl=https://akb.example.com \
  --set-string ingress.host=akb.example.com \
  --set-string sso.keycloakPublicUrl=https://auth.akb.example.com \
  --set-string sso.ingress.host=auth.akb.example.com \
  --set-string sso.productAdmin.username=admin \
  --set-string sso.productAdmin.email=admin@example.com \
  --wait
```

Public origins and the initial product-administrator identity are installation
inputs. Configure DNS first and replace every example value.

## Render and inspect

```bash
helm lint deploy/helm/akb
helm template akb deploy/helm/akb \
  --namespace akb \
  --values deploy/helm/akb/profiles/standalone.yaml \
  > rendered-akb.yaml
```

The chart contains no dependency archives and needs no `helm dependency`
step. It never stores Secret values in Helm release metadata.

## Upgrade from chart 0.1.x

Chart `0.1.x` could own projection objects whose generated Kubernetes Secrets
were garbage-collected with those objects. Before upgrading, follow
[`Removing a legacy bundled credential service`](../../k8s/README.md#removing-a-legacy-bundled-credential-service).

The chart inspects each required Secret during an online Helm install or
upgrade. If a Secret still has a `VaultStaticSecret` owner reference, rendering
fails before Helm removes old resources. Orphan the projection object and
verify that the Secret remains before retrying.

The removed `secretManager`, `secretSync`, `openbao`, and `hashicorpVault`
values are rejected with an explicit migration error instead of being silently
ignored. Replace an old profile with `standalone` or `standalone-sso`.

## Uninstall

```bash
helm uninstall akb --namespace akb
```

The chart does not delete operator-owned Secrets. The AKB Git-data PVC carries
the Helm keep policy; deleting retained application data remains a separate,
explicit operator action.
