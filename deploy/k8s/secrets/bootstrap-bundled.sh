#!/usr/bin/env bash
# Configure a development-only bundled Vault-compatible server and seed one
# AKB runtime payload. Secret values travel through pipes and are never logged.

set -euo pipefail

: "${NAMESPACE:?}"
: "${SECRET_ENGINE:?}"
: "${ROOT_TOKEN:?}"
: "${BACKEND_IMAGE:?}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
KUBE_CONTEXT="${KUBE_CONTEXT:-}"
KV_MOUNT="${KV_MOUNT:-kv}"
KV_PATH="${KV_PATH:-akb/runtime}"
AUTH_MOUNT="${KUBERNETES_AUTH_MOUNT:-kubernetes}"
VAULT_ROLE="${VAULT_ROLE:-akb-runtime-reader}"

KUBECTL=(kubectl)
if [[ -n "${KUBE_CONTEXT}" ]]; then
  KUBECTL+=(--context "${KUBE_CONTEXT}")
fi

case "${SECRET_ENGINE}" in
  openbao)
    POD="akb-secret-store-openbao-0"
    CLI="bao"
    TOKEN_ENV="BAO_TOKEN"
    ADDR_ENV="BAO_ADDR"
    LOCAL_ADDR="http://127.0.0.1:8200"
    ;;
  hashicorp-vault)
    POD="akb-secret-store-vault-0"
    CLI="vault"
    TOKEN_ENV="VAULT_TOKEN"
    ADDR_ENV="VAULT_ADDR"
    LOCAL_ADDR="http://127.0.0.1:8200"
    ;;
  *)
    echo "Unsupported bundled secret engine: ${SECRET_ENGINE}" >&2
    exit 2
    ;;
esac

cli() {
  "${KUBECTL[@]}" exec -n "${NAMESPACE}" "${POD}" -- \
    env "${TOKEN_ENV}=${ROOT_TOKEN}" "${ADDR_ENV}=${LOCAL_ADDR}" "${CLI}" "$@"
}

echo "Configuring ${SECRET_ENGINE} KV and Kubernetes auth"
if ! cli secrets list -format=json | jq -e --arg mount "${KV_MOUNT}/" 'has($mount)' >/dev/null; then
  cli secrets enable -path="${KV_MOUNT}" kv-v2 >/dev/null
fi
if ! cli auth list -format=json | jq -e --arg mount "${AUTH_MOUNT}/" 'has($mount)' >/dev/null; then
  cli auth enable -path="${AUTH_MOUNT}" kubernetes >/dev/null
fi

cli write "auth/${AUTH_MOUNT}/config" \
  kubernetes_host="https://kubernetes.default.svc:443" \
  kubernetes_ca_cert=@/var/run/secrets/kubernetes.io/serviceaccount/ca.crt \
  token_reviewer_jwt=@/var/run/secrets/kubernetes.io/serviceaccount/token \
  disable_iss_validation=true >/dev/null

POLICY_PATH="${KV_MOUNT}/data/${KV_PATH}"
printf 'path "%s" {\n  capabilities = ["read"]\n}\n' "${POLICY_PATH}" | \
  "${KUBECTL[@]}" exec -i -n "${NAMESPACE}" "${POD}" -- \
    env "${TOKEN_ENV}=${ROOT_TOKEN}" "${ADDR_ENV}=${LOCAL_ADDR}" \
    "${CLI}" policy write akb-runtime-reader - >/dev/null

cli write "auth/${AUTH_MOUNT}/role/${VAULT_ROLE}" \
  bound_service_account_names=akb-secret-sync \
  bound_service_account_namespaces="${NAMESPACE}" \
  audience=vault \
  policies=akb-runtime-reader \
  ttl=10m >/dev/null

if cli kv get -mount="${KV_MOUNT}" "${KV_PATH}" >/dev/null 2>&1; then
  echo "Preserving existing AKB runtime material"
else
  echo "Generating AKB Secret Contract v1 material"
  docker run --rm --platform linux/amd64 \
    -v "${SCRIPT_DIR}/bootstrap_material.py:/opt/akb/bootstrap_material.py:ro" \
    "${BACKEND_IMAGE}" python /opt/akb/bootstrap_material.py --format vault | \
    "${KUBECTL[@]}" exec -i -n "${NAMESPACE}" "${POD}" -- \
      env "${TOKEN_ENV}=${ROOT_TOKEN}" "${ADDR_ENV}=${LOCAL_ADDR}" sh -ec '
        material="$(mktemp)"
        trap '\''rm -f "${material}"'\'' EXIT
        chmod 600 "${material}"
        cat >"${material}"
        '"${CLI}"' kv put -mount='"${KV_MOUNT}"' '"${KV_PATH}"' @"${material}" >/dev/null
      '
fi
