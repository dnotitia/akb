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
`deploy/cluster/ensure-vso.sh` to select `auto`, `install`, or `reuse` behavior
with version, readiness, CRD, and ownership checks. Removing this release is a
separate cluster-administrator action and can affect every AKB namespace.
