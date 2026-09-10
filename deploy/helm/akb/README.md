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

## Optional million-vector pgvector tuning

Two measured reference overlays are available for roughly one million
1,024-dimensional embeddings. They compose with either application profile and
do not change the standalone/SSO shape:

| Tuning overlay | PostgreSQL limit | Startup behavior | Intended trade-off |
|---|---:|---|---|
| `pgvector-million-1024d.yaml` | 36 GiB | Loads the measured search relation set before readiness | Lowest and most predictable first-query latency when the full working set fits |
| `pgvector-million-bounded-1024d.yaml` | 8 GiB | Restores only the previous 3 GiB shared-buffer hot set | Fixed memory while the corpus remains disk-backed; unseen paths can be slower |

Full-working-set example:

```bash
helm upgrade --install akb deploy/helm/akb \
  --namespace akb \
  --values deploy/helm/akb/profiles/standalone.yaml \
  --values deploy/helm/akb/tuning/pgvector-million-1024d.yaml \
  --wait
```

It enables bounded startup/autoprewarm and an opt-in hybrid BM25 common-term
cutoff. The database limit is 36 GiB because the reference fixture used about
32.4 GB including page cache; a 24 GiB trial reached its cgroup ceiling. Do not
apply this overlay by corpus row count alone. First validate relation sizes,
available node memory, search relevance, write load and latency on your own
data. See the [search performance design](../../../docs/designs/search-performance.md#million-vector-target-profile).

Fixed-memory example:

```bash
helm upgrade --install akb deploy/helm/akb \
  --namespace akb \
  --values deploy/helm/akb/profiles/standalone.yaml \
  --values deploy/helm/akb/tuning/pgvector-million-bounded-1024d.yaml \
  --wait
```

The bounded overlay keeps vectors on disk, dedicates 3 GiB of the 8 GiB
PostgreSQL limit to shared buffers, and uses PostgreSQL autoprewarm to restore
only the pages that were actually hot before restart. It deliberately disables
the application's full-relation startup prewarm. Since `pg_isready` can succeed
while PostgreSQL is still restoring those buffers in the background, the
reference overlay delays its database readiness probe for 45 seconds; the
measured 3 GiB restore took about 29 seconds after connections first became
available. Re-measure this delay on materially different storage or corpora.

Its 90-second retrieval budget applies only to pgvector search statements so an
otherwise valid cold read is not returned as a false empty result; ordinary
database commands remain at 30 seconds. A 60-second trial still timed out on an
unseen path. The larger timeout is a completion guard, not an acceleration or
the steady-state latency target.

This profile allows the corpus to exceed RAM, but it does not promise constant
latency for unlimited growth. Validate the hot-query hit rate, cold p95/p99,
storage IOPS, concurrent search, OOM counters and relevance at each expected
capacity tier. If unseen-query latency no longer meets the service objective,
partition by tenant/access scope or evaluate a vector engine with disk-resident
vectors and a memory-sized navigation index instead of raising memory forever.

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
