# AKB cluster prerequisites

This chart owns cluster-scoped dependencies shared by every AKB release in a
Kubernetes cluster. It currently installs the pinned Vault Secrets Operator
and its CRDs. It does not install AKB, Keycloak, Vault, or OpenBao.

Install it once under cluster-administrator ownership:

```bash
helm dependency build deploy/helm/akb-cluster
helm upgrade --install akb-cluster deploy/helm/akb-cluster \
  --namespace vault-secrets-operator --create-namespace \
  --wait --timeout 5m
```

Normal AKB uninstall and upgrade operations never mutate this release. Use
`deploy/cluster/ensure-vso.sh` with `VSO_MODE=managed` when this installation
owns the cluster prerequisite, or `VSO_MODE=external` to perform read-only
compatibility and readiness checks against a platform-owned VSO. The default
chart runs two leader-elected replicas with a PodDisruptionBudget.

Inspect current AKB consumers without reading Secret values:

```bash
bash deploy/cluster/status-vso.sh
```

Remove the cluster release only after every consumer has been removed:

```bash
bash deploy/cluster/uninstall-vso.sh
```

The uninstall helper refuses to continue while any VSO custom resource remains.
