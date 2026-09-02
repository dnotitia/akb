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

A direct upgrade or uninstall of an individual AKB release never mutates this
release. The complete AKB installers can reconcile it as a separate step: use
`deploy/cluster/ensure-vso.sh` with `VSO_MODE=managed` when the AKB installation
process is also authorized to manage cluster prerequisites, or
`VSO_MODE=external` for read-only compatibility and readiness checks against a
controller owned by another operations team. The default chart runs two
leader-elected replicas with a PodDisruptionBudget.

Inspect current AKB consumers without reading Secret values:

```bash
bash deploy/cluster/status-vso.sh
```

Remove the cluster release only after every consumer has been removed:

```bash
bash deploy/cluster/uninstall-vso.sh
```

The uninstall helper refuses to continue while any VSO custom resource remains.
Helm retains the VSO CRDs when removing the release. Review and remove retained
CRDs only as a separate cluster-administrator operation after confirming that
no controller or custom resource needs them.
