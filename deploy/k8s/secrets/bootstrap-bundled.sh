#!/usr/bin/env bash
# Configure a development-only bundled Vault-compatible server and seed one
# AKB runtime payload. Secret values travel through pipes and are never logged.

set -euo pipefail

: "${NAMESPACE:?}"
: "${SECRET_ENGINE:?}"
: "${BACKEND_IMAGE:?}"

# Development keeps accepting ROOT_TOKEN for compatibility with the dev chart.
# Production callers pass the one-time token over stdin so it is not exposed in
# the parent process environment or command arguments.
if [[ -z "${ROOT_TOKEN:-}" ]]; then
  IFS= read -r ROOT_TOKEN
fi
if [[ -z "${ROOT_TOKEN}" ]]; then
  echo "A bootstrap token is required on stdin" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
KUBE_CONTEXT="${KUBE_CONTEXT:-}"
KV_MOUNT="${KV_MOUNT:-kv}"
KV_PATH="${KV_PATH:-akb/runtime}"
AUTH_MOUNT="${KUBERNETES_AUTH_MOUNT:-kubernetes}"
VAULT_ROLE="${VAULT_ROLE:-akb-runtime-reader}"
OPERATOR_ROLE="${SECRET_OPERATOR_ROLE:-akb-operator-admin}"
OPERATOR_SERVICE_ACCOUNT="${SECRET_OPERATOR_SERVICE_ACCOUNT:-akb-secret-admin}"
OPERATOR_TOKEN_TTL="${SECRET_OPERATOR_TOKEN_TTL:-30m}"
OPERATOR_TOKEN_MAX_TTL="${SECRET_OPERATOR_TOKEN_MAX_TTL:-4h}"
AUTH_PROFILE="${AUTH_PROFILE:-local}"
BOOTSTRAP_DOCKER_PLATFORM="${BOOTSTRAP_DOCKER_PLATFORM:-linux/amd64}"
REVOKE_ROOT_TOKEN="${REVOKE_ROOT_TOKEN:-false}"
SECRET_STORE_LOCAL_ADDRESS="${SECRET_STORE_LOCAL_ADDRESS:-}"
SECRET_STORE_CACERT="${SECRET_STORE_CACERT:-}"
SECRET_STORE_TLS_SERVER_NAME="${SECRET_STORE_TLS_SERVER_NAME:-}"

if [[ ! "${OPERATOR_ROLE}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] ||
   [[ ! "${OPERATOR_SERVICE_ACCOUNT}" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] ||
   [[ ! "${OPERATOR_TOKEN_TTL}" =~ ^[1-9][0-9]*[smh]$ ]] ||
   [[ ! "${OPERATOR_TOKEN_MAX_TTL}" =~ ^[1-9][0-9]*[smh]$ ]]; then
  echo "Secret Manager operator role, ServiceAccount, or TTL is invalid" >&2
  exit 2
fi
if [[ "${BOOTSTRAP_DOCKER_PLATFORM}" != "linux/amd64" &&
      "${BOOTSTRAP_DOCKER_PLATFORM}" != "linux/arm64" ]]; then
  echo "BOOTSTRAP_DOCKER_PLATFORM must be linux/amd64 or linux/arm64" >&2
  exit 2
fi

KUBECTL=(kubectl)
if [[ -n "${KUBE_CONTEXT}" ]]; then
  KUBECTL+=(--context "${KUBE_CONTEXT}")
fi

case "${SECRET_ENGINE}" in
  openbao)
    POD="${SECRET_STORE_POD:-akb-secret-store-openbao-0}"
    CLI="bao"
    TOKEN_ENV="BAO_TOKEN"
    ADDR_ENV="BAO_ADDR"
    CACERT_ENV="BAO_CACERT"
    TLS_SERVER_NAME_ENV="BAO_TLS_SERVER_NAME"
    LOCAL_ADDR="${SECRET_STORE_LOCAL_ADDRESS:-http://127.0.0.1:8200}"
    ;;
  hashicorp-vault)
    POD="${SECRET_STORE_POD:-akb-secret-store-vault-0}"
    CLI="vault"
    TOKEN_ENV="VAULT_TOKEN"
    ADDR_ENV="VAULT_ADDR"
    CACERT_ENV="VAULT_CACERT"
    TLS_SERVER_NAME_ENV="VAULT_TLS_SERVER_NAME"
    LOCAL_ADDR="${SECRET_STORE_LOCAL_ADDRESS:-http://127.0.0.1:8200}"
    ;;
  *)
    echo "Unsupported bundled secret engine: ${SECRET_ENGINE}" >&2
    exit 2
    ;;
esac

REMOTE_ENV=("${ADDR_ENV}=${LOCAL_ADDR}")
if [[ -n "${SECRET_STORE_CACERT}" ]]; then
  REMOTE_ENV+=("${CACERT_ENV}=${SECRET_STORE_CACERT}")
fi
if [[ -n "${SECRET_STORE_TLS_SERVER_NAME}" ]]; then
  REMOTE_ENV+=("${TLS_SERVER_NAME_ENV}=${SECRET_STORE_TLS_SERVER_NAME}")
fi

cli() {
  printf '%s\n' "${ROOT_TOKEN}" | \
    "${KUBECTL[@]}" exec -i -n "${NAMESPACE}" "${POD}" -- \
    env "${REMOTE_ENV[@]}" sh -ec '
      IFS= read -r bootstrap_token
      export "$1=${bootstrap_token}"
      bootstrap_token=""
      shift
      exec "$@"
    ' sh "${TOKEN_ENV}" "${CLI}" "$@"
}

cli_stdin() {
  { printf '%s\n' "${ROOT_TOKEN}"; cat; } | \
    "${KUBECTL[@]}" exec -i -n "${NAMESPACE}" "${POD}" -- \
    env "${REMOTE_ENV[@]}" sh -ec '
      IFS= read -r bootstrap_token
      export "$1=${bootstrap_token}"
      bootstrap_token=""
      shift
      exec "$@"
    ' sh "${TOKEN_ENV}" "${CLI}" "$@"
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
printf 'path "%s" {\n  capabilities = ["read"]\n}\n%s\n' \
  "${POLICY_PATH}" \
  'path "auth/token/lookup-self" {
  capabilities = ["read"]
}
path "auth/token/renew-self" {
  capabilities = ["update"]
}
path "auth/token/revoke-self" {
  capabilities = ["update"]
}' | cli_stdin policy write akb-runtime-reader - >/dev/null

cli write "auth/${AUTH_MOUNT}/role/${VAULT_ROLE}" \
  bound_service_account_names=akb-secret-sync \
  bound_service_account_namespaces="${NAMESPACE}" \
  audience=vault \
  token_policies=akb-runtime-reader \
  token_ttl=10m \
  token_max_ttl=1h \
  token_no_default_policy=true >/dev/null

# Replace day-to-day root-token use with a short-lived Kubernetes-auth role.
# The ServiceAccount never receives an auto-mounted token; an authorized
# Kubernetes operator must explicitly request a bounded `vault` audience JWT.
printf '%s\n' \
  'path "*" {' \
  '  capabilities = ["create", "read", "update", "patch", "delete", "list", "sudo"]' \
  '}' | cli_stdin policy write akb-operator-admin - >/dev/null
cli write "auth/${AUTH_MOUNT}/role/${OPERATOR_ROLE}" \
  bound_service_account_names="${OPERATOR_SERVICE_ACCOUNT}" \
  bound_service_account_namespaces="${NAMESPACE}" \
  audience=vault \
  token_policies=akb-operator-admin \
  token_ttl="${OPERATOR_TOKEN_TTL}" \
  token_max_ttl="${OPERATOR_TOKEN_MAX_TTL}" \
  token_no_default_policy=true >/dev/null

if cli kv get -mount="${KV_MOUNT}" "${KV_PATH}" >/dev/null 2>&1; then
  EXISTING_AUTH_PROFILE="$(cli kv get -format=json -mount="${KV_MOUNT}" "${KV_PATH}" | \
    jq -r '.data.data.auth_runtime_mode // "local"')"
  if [[ "${EXISTING_AUTH_PROFILE}" != "${AUTH_PROFILE}" ]]; then
    echo "Existing secret material uses auth profile ${EXISTING_AUTH_PROFILE}; refusing ${AUTH_PROFILE} projection" >&2
    echo "Use a new KV_PATH or follow the explicit authentication cutover runbook." >&2
    exit 2
  fi
  echo "Preserving existing AKB runtime material"
else
  echo "Generating AKB Secret Contract v1 material (${AUTH_PROFILE})"
  { printf '%s\n' "${ROOT_TOKEN}"; \
    docker run --rm --platform "${BOOTSTRAP_DOCKER_PLATFORM}" \
      -v "${SCRIPT_DIR}/bootstrap_material.py:/opt/akb/bootstrap_material.py:ro" \
      "${BACKEND_IMAGE}" python /opt/akb/bootstrap_material.py \
        --format vault --auth-profile "${AUTH_PROFILE}"; } | \
    "${KUBECTL[@]}" exec -i -n "${NAMESPACE}" "${POD}" -- \
      env "${REMOTE_ENV[@]}" sh -ec '
        IFS= read -r bootstrap_token
        export "$1=${bootstrap_token}"
        bootstrap_token=""
        shift
        material="$(mktemp)"
        trap '\''rm -f "${material}"'\'' EXIT
        chmod 600 "${material}"
        cat >"${material}"
        "$@" @"${material}"
      ' sh "${TOKEN_ENV}" "${CLI}" kv put \
        -mount="${KV_MOUNT}" "${KV_PATH}" >/dev/null
fi

if [[ "${REVOKE_ROOT_TOKEN}" == "true" ]]; then
  echo "Revoking the one-time initial root token"
  cli token revoke -self >/dev/null
  if cli token lookup >/dev/null 2>&1; then
    echo "Initial root token still works after revoke" >&2
    exit 1
  fi
  echo "Initial root token revocation verified"
fi
