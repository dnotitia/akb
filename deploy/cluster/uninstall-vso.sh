#!/usr/bin/env bash
# Remove the AKB-owned VSO release only when no VSO resources still use it.

set -euo pipefail

VSO_NAMESPACE="${VSO_NAMESPACE:-vault-secrets-operator}"
VSO_RELEASE="${VSO_RELEASE:-akb-cluster}"
KUBE_CONTEXT="${KUBE_CONTEXT:-}"
KUBECTL=(kubectl)
HELM=(helm)
if [[ -n "${KUBE_CONTEXT}" ]]; then
  KUBECTL+=(--context "${KUBE_CONTEXT}")
  HELM+=(--kube-context "${KUBE_CONTEXT}")
fi

consumers="$("${KUBECTL[@]}" get \
  vaultconnections.secrets.hashicorp.com,vaultauths.secrets.hashicorp.com,vaultstaticsecrets.secrets.hashicorp.com,vaultdynamicsecrets.secrets.hashicorp.com,vaultpkisecrets.secrets.hashicorp.com \
  --all-namespaces --output name)"
if [[ -n "${consumers}" ]]; then
  echo "Refusing to remove VSO while custom resources still use it:" >&2
  printf '%s\n' "${consumers}" >&2
  echo "Remove the consuming AKB releases first." >&2
  exit 1
fi

"${HELM[@]}" uninstall "${VSO_RELEASE}" --namespace "${VSO_NAMESPACE}" --wait
