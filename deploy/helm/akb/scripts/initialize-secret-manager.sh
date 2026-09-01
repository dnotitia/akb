#!/usr/bin/env bash
# Complete native init/unseal/bootstrap for a bundled production Secret Manager
# already installed by the AKB Helm release. This boundary stays interactive;
# it is intentionally not a Helm hook whose logs could capture recovery data.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CHART_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${CHART_DIR}/../../.." && pwd)"
RELEASE="${RELEASE:-akb}"
NAMESPACE="${NAMESPACE:-akb}"
KUBE_CONTEXT="${KUBE_CONTEXT:-}"

HELM=(helm)
KUBECTL=(kubectl)
if [[ -n "${KUBE_CONTEXT}" ]]; then
  HELM+=(--kube-context "${KUBE_CONTEXT}")
  KUBECTL+=(--context "${KUBE_CONTEXT}")
fi

release_values="$(${HELM[@]} get values "${RELEASE}" -n "${NAMESPACE}" -o json --all)"
mode="$(jq -r '.secretManager.mode' <<<"${release_values}")"
engine="$(jq -r '.secretManager.engine' <<<"${release_values}")"
seal_mode="$(jq -r '.secretManager.sealMode' <<<"${release_values}")"
auth_profile="$(jq -r 'if .sso.enabled then "sso" else "local" end' <<<"${release_values}")"
backend_image="$(jq -r '.images.backend.repository + ":" + .images.backend.tag' <<<"${release_values}")"
kv_mount="$(jq -r '.secretManager.kv.mount' <<<"${release_values}")"
kv_path="$(jq -r '.secretManager.kv.path' <<<"${release_values}")"
auth_mount="$(jq -r '.secretManager.auth.mount' <<<"${release_values}")"
vault_role="$(jq -r '.secretManager.auth.role' <<<"${release_values}")"

if [[ "${mode}" != "bundled" ]]; then
  echo "Helm release ${RELEASE} does not own a bundled Secret Manager" >&2
  exit 2
fi

NAMESPACE="${NAMESPACE}" KUBE_CONTEXT="${KUBE_CONTEXT}" \
SECRET_ENGINE="${engine}" SECRET_SEAL_MODE="${seal_mode}" \
SECRET_STORE_POD=akb-secret-store-0 \
SECRET_STORE_STATEFULSET=akb-secret-store \
SECRET_STORE_SERVICE=akb-secret-store \
BACKEND_IMAGE="${backend_image}" AUTH_PROFILE="${auth_profile}" \
KV_MOUNT="${kv_mount}" KV_PATH="${kv_path}" \
KUBERNETES_AUTH_MOUNT="${auth_mount}" VAULT_ROLE="${vault_role}" \
SECRET_KEY_SHARES="${SECRET_KEY_SHARES:-5}" \
SECRET_KEY_THRESHOLD="${SECRET_KEY_THRESHOLD:-3}" \
SECRET_PGP_KEYS="${SECRET_PGP_KEYS:-}" \
SECRET_ROOT_TOKEN_PGP_KEY="${SECRET_ROOT_TOKEN_PGP_KEY:-}" \
SECRET_RECOVERY_PGP_KEYS="${SECRET_RECOVERY_PGP_KEYS:-}" \
  bash "${REPO_ROOT}/deploy/k8s/secrets/initialize-production-bundled.sh"

for attempt in $(seq 1 120); do
  if "${KUBECTL[@]}" get secret akb-secret -n "${NAMESPACE}" >/dev/null 2>&1; then
    echo "AKB Secret Contract v1 is ready"
    exit 0
  fi
  sleep 2
done

echo "Timed out waiting for VSO to project akb-secret" >&2
exit 1
