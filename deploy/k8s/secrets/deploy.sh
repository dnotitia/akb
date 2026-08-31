#!/usr/bin/env bash
# Prepare the stable AKB Kubernetes Secret Contract v1.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
: "${NAMESPACE:?Set NAMESPACE}"
: "${BACKEND_IMAGE:?Set BACKEND_IMAGE}"

SECRET_MODE="${SECRET_MODE:-manual}"
SECRET_ENGINE="${SECRET_ENGINE:-}"
SECRET_PROFILE="${SECRET_PROFILE:-development}"
KUBE_CONTEXT="${KUBE_CONTEXT:-}"
INSTALL_VSO="${INSTALL_VSO:-false}"
KV_MOUNT="${KV_MOUNT:-kv}"
KV_PATH="${KV_PATH:-akb/runtime}"
KUBERNETES_AUTH_MOUNT="${KUBERNETES_AUTH_MOUNT:-kubernetes}"
VAULT_ROLE="${VAULT_ROLE:-akb-runtime-reader}"

KUBECTL=(kubectl)
HELM=(helm)
if [[ -n "${KUBE_CONTEXT}" ]]; then
  KUBECTL+=(--context "${KUBE_CONTEXT}")
  HELM+=(--kube-context "${KUBE_CONTEXT}")
fi

if [[ ! "${NAMESPACE}" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]]; then
  echo "NAMESPACE is not a valid DNS label" >&2
  exit 2
fi
if [[ ! "${KV_MOUNT}" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]] ||
   [[ ! "${KUBERNETES_AUTH_MOUNT}" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]] ||
   [[ ! "${VAULT_ROLE}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; then
  echo "KV/auth mount names and VAULT_ROLE contain unsupported characters" >&2
  exit 2
fi
if [[ ! "${KV_PATH}" =~ ^[A-Za-z0-9][A-Za-z0-9_./-]*$ ]] ||
   [[ "${KV_PATH}" == *".."* ]]; then
  echo "KV_PATH must be a relative Vault path without '..' segments" >&2
  exit 2
fi

secret_contract_ready() {
  "${KUBECTL[@]}" get secret akb-secret -n "${NAMESPACE}" -o json 2>/dev/null | \
    jq -e '
      .data.db_password and
      .data.system_hmac_secret and
      .data["secret.yaml"] and
      .data["local-session-private.pem"] and
      .data["local-session-jwks.json"]
    ' >/dev/null
}

wait_for_contract() {
  local attempt
  for attempt in $(seq 1 90); do
    if secret_contract_ready; then
      echo "AKB Secret Contract v1 is ready"
      return 0
    fi
    sleep 2
  done
  echo "Timed out waiting for akb-secret contract in ${NAMESPACE}" >&2
  "${KUBECTL[@]}" get vaultconnection,vaultauth,vaultstaticsecret -n "${NAMESPACE}" 2>/dev/null || true
  return 1
}

ensure_vso() {
  if "${KUBECTL[@]}" get crd vaultstaticsecrets.secrets.hashicorp.com >/dev/null 2>&1; then
    return
  fi
  if [[ "${INSTALL_VSO}" != "true" ]]; then
    echo "Vault Secrets Operator CRDs are missing." >&2
    echo "Set INSTALL_VSO=true to install the pinned official cluster-scoped chart." >&2
    exit 2
  fi
  "${HELM[@]}" repo add hashicorp https://helm.releases.hashicorp.com --force-update >/dev/null
  "${HELM[@]}" upgrade --install vault-secrets-operator hashicorp/vault-secrets-operator \
    --version 1.5.1 \
    --namespace vault-secrets-operator \
    --create-namespace \
    --wait --timeout 5m
}

render_vso() {
  local address="$1"
  local skip_tls="$2"
  local ca_ref="${3:-}"
  local rendered
  local address_pattern='^https?://([A-Za-z0-9._-]+|\[[0-9A-Fa-f:]+\])(:[0-9]+)?(/[A-Za-z0-9._/-]*)?$'
  rendered="$(mktemp "${TMPDIR:-/tmp}/akb-vso.XXXXXX.yaml")"
  if [[ ! "${address}" =~ ${address_pattern} ]]; then
    echo "Secret-store address must be a plain HTTP(S) origin without query data" >&2
    exit 2
  fi
  if [[ -n "${ca_ref}" && ! "${ca_ref}" =~ ^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$ ]]; then
    echo "SECRET_STORE_CA_SECRET is not a valid Secret name" >&2
    exit 2
  fi
  local ca_line="  # caCertSecretRef omitted; use the system trust store"
  if [[ -n "${ca_ref}" ]]; then
    ca_line="  caCertSecretRef: ${ca_ref}"
  fi
  sed \
    -e "s|__SECRET_STORE_ADDRESS__|${address}|g" \
    -e "s|__SKIP_TLS_VERIFY__|${skip_tls}|g" \
    -e "s|  # __CA_CERT_SECRET_REF__|${ca_line}|g" \
    -e "s|__KUBERNETES_AUTH_MOUNT__|${KUBERNETES_AUTH_MOUNT}|g" \
    -e "s|__VAULT_ROLE__|${VAULT_ROLE}|g" \
    -e "s|__KV_MOUNT__|${KV_MOUNT}|g" \
    -e "s|__KV_PATH__|${KV_PATH}|g" \
    "${SCRIPT_DIR}/vso-vault-compatible.yaml" >"${rendered}"
  "${KUBECTL[@]}" apply -n "${NAMESPACE}" -f "${rendered}"
  rm -f "${rendered}"
}

case "${SECRET_MODE}" in
  manual)
    if ! secret_contract_ready; then
      if [[ "${GENERATE_MANUAL_SECRETS:-false}" == "true" ]]; then
        echo "Generating namespace-local manual Secret Contract v1"
        docker run --rm --platform linux/amd64 \
          -v "${SCRIPT_DIR}/bootstrap_material.py:/opt/akb/bootstrap_material.py:ro" \
          "${BACKEND_IMAGE}" python /opt/akb/bootstrap_material.py \
          --format kubernetes --namespace "${NAMESPACE}" | \
          "${KUBECTL[@]}" apply -f -
      else
        echo "Required Secret akb-secret is missing or incomplete in ${NAMESPACE}." >&2
        echo "Create it out-of-band or set GENERATE_MANUAL_SECRETS=true for a new installation." >&2
        exit 2
      fi
    fi
    ;;
  bundled)
    ensure_vso
    case "${SECRET_ENGINE}" in
      openbao)
        CHART="openbao/openbao"
        CHART_VERSION="0.29.3"
        CHART_REPO_NAME="openbao"
        CHART_REPO_URL="https://openbao.github.io/openbao-helm"
        SERVICE="akb-secret-store-openbao"
        STATEFULSET="akb-secret-store-openbao"
        ;;
      hashicorp-vault)
        if [[ "${HASHICORP_LICENSE_ACKNOWLEDGED:-false}" != "true" ]]; then
          echo "Set HASHICORP_LICENSE_ACKNOWLEDGED=true after reviewing HashiCorp Vault BSL terms." >&2
          exit 2
        fi
        CHART="hashicorp/vault"
        CHART_VERSION="0.34.1"
        CHART_REPO_NAME="hashicorp"
        CHART_REPO_URL="https://helm.releases.hashicorp.com"
        SERVICE="akb-secret-store-vault"
        STATEFULSET="akb-secret-store-vault"
        ;;
      *)
        echo "SECRET_ENGINE must be openbao or hashicorp-vault for bundled mode" >&2
        exit 2
        ;;
    esac
    "${HELM[@]}" repo add "${CHART_REPO_NAME}" "${CHART_REPO_URL}" --force-update >/dev/null
    VALUES="${SCRIPT_DIR}/values/${SECRET_ENGINE}-${SECRET_PROFILE}.yaml"
    if [[ ! -f "${VALUES}" ]]; then
      echo "Unsupported SECRET_PROFILE: ${SECRET_PROFILE}" >&2
      exit 2
    fi
    HELM_ARGS=(
      upgrade --install akb-secret-store "${CHART}"
      --version "${CHART_VERSION}"
      --namespace "${NAMESPACE}"
      --values "${VALUES}"
    )
    if [[ -n "${STORAGE_CLASS:-}" ]]; then
      HELM_ARGS+=(
        --set-string "server.dataStorage.storageClass=${STORAGE_CLASS}"
        --set-string "server.auditStorage.storageClass=${STORAGE_CLASS}"
      )
    fi
    if [[ -n "${SECRET_STORE_EXTRA_VALUES:-}" ]]; then
      HELM_ARGS+=(--values "${SECRET_STORE_EXTRA_VALUES}")
    fi
    if [[ "${SECRET_PROFILE}" == "production" ]]; then
      if ! "${KUBECTL[@]}" get secret akb-secret-store-tls -n "${NAMESPACE}" >/dev/null 2>&1; then
        echo "Production bundle requires akb-secret-store-tls with tls.crt, tls.key, and ca.crt." >&2
        exit 2
      fi
      "${HELM[@]}" "${HELM_ARGS[@]}"
      echo "Production ${SECRET_ENGINE} is installed but intentionally uninitialized/sealed." >&2
      echo "Complete the documented init/unseal ceremony, configure KV/Auth, then rerun in external mode." >&2
      exit 3
    fi
    # Reuse the development token recorded in this namespace's Helm release.
    # Generating a new token on every idempotent deploy breaks OnDelete chart
    # pods: their current environment still contains the previous token.
    ROOT_TOKEN=""
    if "${HELM[@]}" status akb-secret-store -n "${NAMESPACE}" >/dev/null 2>&1; then
      ROOT_TOKEN="$("${HELM[@]}" get values akb-secret-store -n "${NAMESPACE}" -o json | \
        jq -r '.server.dev.devRootToken // empty')"
    fi
    if [[ -z "${ROOT_TOKEN}" ]]; then
      ROOT_TOKEN="$(openssl rand -hex 32)"
    fi
    HELM_ARGS+=(
      --set-string "server.dev.devRootToken=${ROOT_TOKEN}"
      --wait --timeout 5m
    )
    "${HELM[@]}" "${HELM_ARGS[@]}"
    # The official charts use an OnDelete StatefulSet strategy, for which
    # `kubectl rollout status` exits immediately with an error. Helm's --wait
    # has already gated the release; wait on the concrete server pod as the
    # portable readiness contract shared by both charts.
    "${KUBECTL[@]}" wait --for=condition=Ready "pod/${STATEFULSET}-0" \
      -n "${NAMESPACE}" --timeout=5m
    NAMESPACE="${NAMESPACE}" KUBE_CONTEXT="${KUBE_CONTEXT}" \
      SECRET_ENGINE="${SECRET_ENGINE}" ROOT_TOKEN="${ROOT_TOKEN}" \
      BACKEND_IMAGE="${BACKEND_IMAGE}" KV_MOUNT="${KV_MOUNT}" KV_PATH="${KV_PATH}" \
      KUBERNETES_AUTH_MOUNT="${KUBERNETES_AUTH_MOUNT}" VAULT_ROLE="${VAULT_ROLE}" \
      bash "${SCRIPT_DIR}/bootstrap-bundled.sh"
    render_vso "http://${SERVICE}.${NAMESPACE}.svc:8200" false ""
    wait_for_contract
    ;;
  external)
    ensure_vso
    case "${SECRET_ENGINE}" in
      openbao|hashicorp-vault) ;;
      *)
        echo "External v1 supports openbao or hashicorp-vault through VSO." >&2
        exit 2
        ;;
    esac
    : "${SECRET_STORE_ADDRESS:?Set SECRET_STORE_ADDRESS for external mode}"
    if [[ "${SECRET_STORE_ADDRESS}" != https://* && "${ALLOW_INSECURE_EXTERNAL_HTTP:-false}" != "true" ]]; then
      echo "External Secret Manager must use HTTPS unless ALLOW_INSECURE_EXTERNAL_HTTP=true." >&2
      exit 2
    fi
    render_vso "${SECRET_STORE_ADDRESS}" false "${SECRET_STORE_CA_SECRET:-}"
    wait_for_contract
    ;;
  *)
    echo "SECRET_MODE must be manual, bundled, or external" >&2
    exit 2
    ;;
esac

secret_contract_ready
