#!/usr/bin/env bash
# Show the shared VSO controller and the AKB resources it currently manages.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
KUBE_CONTEXT="${KUBE_CONTEXT:-}"
KUBECTL=(kubectl)
if [[ -n "${KUBE_CONTEXT}" ]]; then
  KUBECTL+=(--context "${KUBE_CONTEXT}")
fi

VSO_MODE=external KUBE_CONTEXT="${KUBE_CONTEXT}" \
  bash "${SCRIPT_DIR}/ensure-vso.sh"

echo "=== AKB VSO resources ==="
"${KUBECTL[@]}" get \
  vaultconnections.secrets.hashicorp.com,vaultauths.secrets.hashicorp.com,vaultstaticsecrets.secrets.hashicorp.com \
  --all-namespaces --selector app.kubernetes.io/part-of=akb
