#!/usr/bin/env bash
# Prepare the stable AKB Kubernetes Secret Contract v1.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
: "${NAMESPACE:?Set NAMESPACE}"
: "${BACKEND_IMAGE:?Set BACKEND_IMAGE}"

SECRET_MODE="${SECRET_MODE:-manual}"
SECRET_ENGINE="${SECRET_ENGINE:-}"
SECRET_PROFILE="${SECRET_PROFILE:-development}"
SECRET_SEAL_MODE="${SECRET_SEAL_MODE:-plaintext}"
SECRET_TOPOLOGY="${SECRET_TOPOLOGY:-production-ha}"
AUTH_PROFILE="${AUTH_PROFILE:-local}"
BOOTSTRAP_DOCKER_PLATFORM="${BOOTSTRAP_DOCKER_PLATFORM:-linux/amd64}"
SECRET_STORE_RELEASE="${SECRET_STORE_RELEASE:-}"
KUBE_CONTEXT="${KUBE_CONTEXT:-}"
if [[ -z "${VSO_MODE:-}" ]]; then
  if [[ "${SECRET_MODE}" == "manual" ]]; then
    VSO_MODE="disabled"
  else
    VSO_MODE="auto"
  fi
fi
KV_MOUNT="${KV_MOUNT:-kv}"
KV_PATH="${KV_PATH:-akb/runtime}"
KUBERNETES_AUTH_MOUNT="${KUBERNETES_AUTH_MOUNT:-kubernetes}"
VAULT_ROLE="${VAULT_ROLE:-akb-runtime-reader}"
SECRET_STORE_CERT_ISSUER_NAME="${SECRET_STORE_CERT_ISSUER_NAME:-}"
SECRET_STORE_CERT_ISSUER_KIND="${SECRET_STORE_CERT_ISSUER_KIND:-ClusterIssuer}"
SECRET_STORE_CLUSTER_DOMAIN="${SECRET_STORE_CLUSTER_DOMAIN:-cluster.local}"
SECRET_STORE_SEAL_CONFIG_SECRET="${SECRET_STORE_SEAL_CONFIG_SECRET:-}"

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
if [[ "${AUTH_PROFILE}" != "local" && "${AUTH_PROFILE}" != "sso" ]]; then
  echo "AUTH_PROFILE must be local or sso" >&2
  exit 2
fi
if [[ "${BOOTSTRAP_DOCKER_PLATFORM}" != "linux/amd64" &&
      "${BOOTSTRAP_DOCKER_PLATFORM}" != "linux/arm64" ]]; then
  echo "BOOTSTRAP_DOCKER_PLATFORM must be linux/amd64 or linux/arm64" >&2
  exit 2
fi
case "${VSO_MODE}" in
  auto|install|reuse|disabled) ;;
  *)
    echo "VSO_MODE must be auto, install, reuse, or disabled" >&2
    exit 2
    ;;
esac
if [[ "${SECRET_MODE}" == "manual" && "${VSO_MODE}" != "disabled" ]]; then
  echo "Manual Secret mode requires VSO_MODE=disabled" >&2
  exit 2
fi
if [[ "${SECRET_MODE}" != "manual" && "${VSO_MODE}" == "disabled" ]]; then
  echo "Bundled and external Secret Manager modes require VSO" >&2
  exit 2
fi
if [[ "${SECRET_SEAL_MODE}" != "plaintext" &&
      "${SECRET_SEAL_MODE}" != "pgp" &&
      "${SECRET_SEAL_MODE}" != "auto" ]]; then
  echo "SECRET_SEAL_MODE must be plaintext, pgp, or auto" >&2
  exit 2
fi
if [[ "${SECRET_TOPOLOGY}" != "onprem-small" &&
      "${SECRET_TOPOLOGY}" != "production-ha" ]]; then
  echo "SECRET_TOPOLOGY must be onprem-small or production-ha" >&2
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
  local runtime_ready
  runtime_ready="$("${KUBECTL[@]}" get secret akb-secret -n "${NAMESPACE}" -o json 2>/dev/null | \
    jq -r --arg profile "${AUTH_PROFILE}" '
      .data.db_password and
      .data.system_hmac_secret and
      .data["secret.yaml"] and
      .data["local-session-private.pem"] and
      .data["local-session-jwks.json"] and
      ((.data.auth_runtime_mode | @base64d) == $profile) and
      (
        $profile == "local" or
        (
          .data.keycloak_client_secret and
          .data.keycloak_admin_client_secret and
          .data.keycloak_management_client_secret and
          .data.sso_browser_session_encryption_key and
          .data.sso_session_epoch
        )
      )
    ' 2>/dev/null || true)"
  [[ "${runtime_ready}" == "true" ]] || return 1
  if [[ "${AUTH_PROFILE}" == "sso" ]]; then
    "${KUBECTL[@]}" get secret \
      akb-keycloak-db-credentials akb-keycloak-bootstrap \
      akb-product-admin-bootstrap -n "${NAMESPACE}" >/dev/null 2>&1
  fi
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
  VSO_MODE="${VSO_MODE}" KUBE_CONTEXT="${KUBE_CONTEXT}" \
    bash "${SCRIPT_DIR}/../../cluster/ensure-vso.sh"
}

ensure_production_tls() {
  if "${KUBECTL[@]}" get secret akb-secret-store-tls -n "${NAMESPACE}" >/dev/null 2>&1; then
    return
  fi
  if [[ -z "${SECRET_STORE_CERT_ISSUER_NAME}" ]]; then
    echo "Production bundle requires akb-secret-store-tls with tls.crt, tls.key, and ca.crt." >&2
    echo "Create it out-of-band or set SECRET_STORE_CERT_ISSUER_NAME for an existing cert-manager CA issuer." >&2
    exit 2
  fi
  if [[ "${SECRET_STORE_CERT_ISSUER_KIND}" != "Issuer" &&
        "${SECRET_STORE_CERT_ISSUER_KIND}" != "ClusterIssuer" ]] ||
     [[ ! "${SECRET_STORE_CERT_ISSUER_NAME}" =~ ^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$ ]] ||
     [[ ! "${SECRET_STORE_CLUSTER_DOMAIN}" =~ ^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$ ]]; then
    echo "Secret Manager cert-manager issuer inputs are invalid" >&2
    exit 2
  fi
  if ! "${KUBECTL[@]}" get crd certificates.cert-manager.io >/dev/null 2>&1; then
    echo "SECRET_STORE_CERT_ISSUER_NAME requires an existing cert-manager installation" >&2
    exit 2
  fi
  if [[ "${SECRET_STORE_CERT_ISSUER_KIND}" == "Issuer" ]]; then
    if ! "${KUBECTL[@]}" get issuer "${SECRET_STORE_CERT_ISSUER_NAME}" \
      -n "${NAMESPACE}" >/dev/null 2>&1; then
      echo "Issuer/${SECRET_STORE_CERT_ISSUER_NAME} does not exist in ${NAMESPACE}" >&2
      exit 2
    fi
  elif ! "${KUBECTL[@]}" get clusterissuer "${SECRET_STORE_CERT_ISSUER_NAME}" >/dev/null 2>&1; then
    echo "ClusterIssuer/${SECRET_STORE_CERT_ISSUER_NAME} does not exist" >&2
    exit 2
  fi
  echo "Requesting Secret Manager TLS certificate from ${SECRET_STORE_CERT_ISSUER_KIND}/${SECRET_STORE_CERT_ISSUER_NAME}"
  sed \
    -e "s|__CERT_ISSUER_KIND__|${SECRET_STORE_CERT_ISSUER_KIND}|g" \
    -e "s|__CERT_ISSUER_NAME__|${SECRET_STORE_CERT_ISSUER_NAME}|g" \
    -e "s|__SERVICE__|${SERVICE}|g" \
    -e "s|__STATEFULSET__|${STATEFULSET}|g" \
    -e "s|__NAMESPACE__|${NAMESPACE}|g" \
    -e "s|__CLUSTER_DOMAIN__|${SECRET_STORE_CLUSTER_DOMAIN}|g" \
    "${SCRIPT_DIR}/certificate.yaml" | "${KUBECTL[@]}" apply -n "${NAMESPACE}" -f -
  "${KUBECTL[@]}" wait certificate/akb-secret-store-tls -n "${NAMESPACE}" \
    --for=condition=Ready --timeout=5m
}

render_vso() {
  local address="$1"
  local skip_tls="$2"
  local ca_ref="${3:-}"
  local rendered
  local adapter="${SCRIPT_DIR}/vso-vault-compatible.yaml"
  if [[ "${AUTH_PROFILE}" == "sso" ]]; then
    adapter="${SCRIPT_DIR}/vso-vault-compatible-sso.yaml"
  fi
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
    "${adapter}" >"${rendered}"
  "${KUBECTL[@]}" apply -n "${NAMESPACE}" -f "${rendered}"
  rm -f "${rendered}"
}

chart_fullname() {
  local chart_name="$1"
  # Both official charts use Helm's standard `contains chartName releaseName`
  # fullname helper to avoid names such as `vault-vault`.
  if [[ "${SECRET_STORE_RELEASE}" == *"${chart_name}"* ]]; then
    printf '%s' "${SECRET_STORE_RELEASE}"
  else
    printf '%s-%s' "${SECRET_STORE_RELEASE}" "${chart_name}"
  fi
}

case "${SECRET_MODE}" in
  manual)
    if ! secret_contract_ready; then
      if [[ "${GENERATE_MANUAL_SECRETS:-false}" == "true" ]]; then
        echo "Generating namespace-local manual Secret Contract v1"
        docker run --rm --platform "${BOOTSTRAP_DOCKER_PLATFORM}" \
          -v "${SCRIPT_DIR}/bootstrap_material.py:/opt/akb/bootstrap_material.py:ro" \
          "${BACKEND_IMAGE}" python /opt/akb/bootstrap_material.py \
          --format kubernetes --auth-profile "${AUTH_PROFILE}" \
          --namespace "${NAMESPACE}" | \
          "${KUBECTL[@]}" apply -f -
      else
        echo "Required Secret akb-secret is missing or incomplete in ${NAMESPACE}." >&2
        echo "Create it out-of-band or set GENERATE_MANUAL_SECRETS=true for a new installation." >&2
        exit 2
      fi
    fi
    ;;
  bundled)
    if [[ -z "${SECRET_STORE_RELEASE}" ]]; then
      if "${HELM[@]}" status akb-secret-store -n "${NAMESPACE}" >/dev/null 2>&1; then
        # Preserve names for namespaces installed by the original profile.
        SECRET_STORE_RELEASE="akb-secret-store" # pragma: allowlist secret
      else
        NAMESPACE_DIGEST="$(printf '%s' "${NAMESPACE}" | openssl dgst -sha256 | awk '{print substr($NF, 1, 12)}')"
        SECRET_STORE_RELEASE="akb-sm-${NAMESPACE_DIGEST}" # pragma: allowlist secret
      fi
    fi
    if [[ ! "${SECRET_STORE_RELEASE}" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] ||
       (( ${#SECRET_STORE_RELEASE} > 40 )); then
      echo "SECRET_STORE_RELEASE must be a DNS label of at most 40 characters" >&2
      exit 2
    fi
    case "${SECRET_ENGINE}" in
      openbao)
        CHART="openbao/openbao"
        CHART_VERSION="0.29.3"
        CHART_REPO_NAME="openbao"
        CHART_REPO_URL="https://openbao.github.io/openbao-helm"
        SERVICE="$(chart_fullname openbao)"
        STATEFULSET="${SERVICE}"
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
        SERVICE="$(chart_fullname vault)"
        STATEFULSET="${SERVICE}"
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
      upgrade --install "${SECRET_STORE_RELEASE}" "${CHART}"
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
      if [[ ! -f "${SECRET_STORE_EXTRA_VALUES}" ]]; then
        echo "SECRET_STORE_EXTRA_VALUES does not exist: ${SECRET_STORE_EXTRA_VALUES}" >&2
        exit 2
      fi
      HELM_ARGS+=(--values "${SECRET_STORE_EXTRA_VALUES}")
    fi
    if [[ "${SECRET_PROFILE}" == "production" ]]; then
      ensure_production_tls
      for tls_key in tls.crt tls.key ca.crt; do
        if [[ -z "$("${KUBECTL[@]}" get secret akb-secret-store-tls -n "${NAMESPACE}" \
          -o "jsonpath={.data.${tls_key//./\\.}}" 2>/dev/null || true)" ]]; then
          echo "akb-secret-store-tls is missing ${tls_key}" >&2
          exit 2
        fi
      done
      if [[ "${SECRET_SEAL_MODE}" == "auto" ]]; then
        if [[ ! "${SECRET_STORE_SEAL_CONFIG_SECRET}" =~ ^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$ ]]; then
          echo "SECRET_SEAL_MODE=auto requires a valid SECRET_STORE_SEAL_CONFIG_SECRET" >&2
          exit 2
        fi
        if [[ -z "$("${KUBECTL[@]}" get secret "${SECRET_STORE_SEAL_CONFIG_SECRET}" \
          -n "${NAMESPACE}" -o 'jsonpath={.data.seal\.hcl}' 2>/dev/null || true)" ]]; then
          echo "${SECRET_STORE_SEAL_CONFIG_SECRET} must exist in ${NAMESPACE} with key seal.hcl" >&2
          exit 2
        fi
        if [[ "${SECRET_ENGINE}" == "openbao" ]]; then
          SECRET_STORE_HOME="/openbao"
        else
          SECRET_STORE_HOME="/vault"
        fi
        HELM_ARGS+=(
          --set-string "server.extraVolumes[0].type=secret"
          --set-string "server.extraVolumes[0].name=${SECRET_STORE_SEAL_CONFIG_SECRET}"
          --set-string "server.extraArgs=-config=${SECRET_STORE_HOME}/userconfig/${SECRET_STORE_SEAL_CONFIG_SECRET}/seal.hcl"
        )
      fi
      ensure_vso
      if [[ "${SECRET_TOPOLOGY}" == "onprem-small" ]]; then
        SECRET_STORE_REPLICAS=1
        HELM_ARGS+=(--set "server.ha.disruptionBudget.enabled=false")
      else
        SECRET_STORE_REPLICAS=3
      fi
      RAFT_LEADER_ADDR="https://${STATEFULSET}-0.${STATEFULSET}-internal.${NAMESPACE}.svc:8200"
      HELM_ARGS+=(
        --set "server.ha.replicas=${SECRET_STORE_REPLICAS}"
        --set-string "server.extraEnvironmentVars.RAFT_ADDR=${RAFT_LEADER_ADDR}"
      )
      "${HELM[@]}" "${HELM_ARGS[@]}"
      NAMESPACE="${NAMESPACE}" KUBE_CONTEXT="${KUBE_CONTEXT}" \
        SECRET_ENGINE="${SECRET_ENGINE}" SECRET_SEAL_MODE="${SECRET_SEAL_MODE}" \
        SECRET_STORE_POD="${STATEFULSET}-0" \
        SECRET_STORE_STATEFULSET="${STATEFULSET}" SECRET_STORE_SERVICE="${SERVICE}" \
        BACKEND_IMAGE="${BACKEND_IMAGE}" KV_MOUNT="${KV_MOUNT}" KV_PATH="${KV_PATH}" \
        AUTH_PROFILE="${AUTH_PROFILE}" KUBERNETES_AUTH_MOUNT="${KUBERNETES_AUTH_MOUNT}" \
        VAULT_ROLE="${VAULT_ROLE}" \
        bash "${SCRIPT_DIR}/initialize-production-bundled.sh"
      render_vso "https://${SERVICE}.${NAMESPACE}.svc:8200" false "akb-secret-store-tls"
      wait_for_contract
      secret_contract_ready
      exit 0
    fi
    ensure_vso
    # Reuse the development token recorded in this namespace's Helm release.
    # Generating a new token on every idempotent deploy breaks OnDelete chart
    # pods: their current environment still contains the previous token.
    ROOT_TOKEN=""
    if "${HELM[@]}" status "${SECRET_STORE_RELEASE}" -n "${NAMESPACE}" >/dev/null 2>&1; then
      ROOT_TOKEN="$("${HELM[@]}" get values "${SECRET_STORE_RELEASE}" -n "${NAMESPACE}" -o json | \
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
      BOOTSTRAP_DOCKER_PLATFORM="${BOOTSTRAP_DOCKER_PLATFORM}" \
      SECRET_STORE_POD="${STATEFULSET}-0" \
      BACKEND_IMAGE="${BACKEND_IMAGE}" KV_MOUNT="${KV_MOUNT}" KV_PATH="${KV_PATH}" \
      AUTH_PROFILE="${AUTH_PROFILE}" \
      KUBERNETES_AUTH_MOUNT="${KUBERNETES_AUTH_MOUNT}" VAULT_ROLE="${VAULT_ROLE}" \
      bash "${SCRIPT_DIR}/bootstrap-bundled.sh"
    render_vso "http://${SERVICE}.${NAMESPACE}.svc:8200" false ""
    wait_for_contract
    ;;
  external)
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
    ensure_vso
    render_vso "${SECRET_STORE_ADDRESS}" false "${SECRET_STORE_CA_SECRET:-}"
    wait_for_contract
    ;;
  *)
    echo "SECRET_MODE must be manual, bundled, or external" >&2
    exit 2
    ;;
esac

secret_contract_ready
