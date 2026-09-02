#!/usr/bin/env bash
# Optional convenience wrapper for the AKB Helm chart. The chart-managed
# bootstrap Job owns native init/unseal/bootstrap; this script only validates
# source-tree inputs, reconciles the shared VSO prerequisite, and passes values.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
RELEASE="${RELEASE:-akb}"
NAMESPACE="${NAMESPACE:-akb}"
AKB_PROFILE="${AKB_PROFILE:-standalone}"
KUBE_CONTEXT="${KUBE_CONTEXT:-}"
SECRET_ENGINE="${SECRET_ENGINE:-openbao}"
SECRET_PROFILE="${SECRET_PROFILE:-production}"
SECRET_SEAL_MODE="${SECRET_SEAL_MODE:-plaintext}"
SECRET_TOPOLOGY="${SECRET_TOPOLOGY:-production-ha}"
SECRET_KEY_SHARES="${SECRET_KEY_SHARES:-5}"
SECRET_KEY_THRESHOLD="${SECRET_KEY_THRESHOLD:-3}"
SECRET_STORE_SEAL_CONFIG_SECRET="${SECRET_STORE_SEAL_CONFIG_SECRET:-}"
BOOTSTRAP_DOCKER_PLATFORM="${BOOTSTRAP_DOCKER_PLATFORM:-linux/amd64}"

case "${AKB_PROFILE}" in
  standalone|standalone-sso)
    SECRET_MODE="${SECRET_MODE:-manual}"
    ;;
  standalone-secret-manager|standalone-sso-secret-manager)
    SECRET_MODE="bundled" # pragma: allowlist secret
    ;;
  *)
    echo "Unsupported AKB_PROFILE: ${AKB_PROFILE}" >&2
    exit 2
    ;;
esac

if [[ -z "${VSO_MODE:-}" ]]; then
  case "${SECRET_MODE}" in
    manual) VSO_MODE="disabled" ;;
    bundled) VSO_MODE="managed" ;;
    external) VSO_MODE="external" ;;
  esac
fi
case "${VSO_MODE}" in
  managed|external|disabled) ;;
  *)
    echo "VSO_MODE must be managed, external, or disabled" >&2
    exit 2
    ;;
esac
if [[ "${SECRET_MODE}" == "manual" && "${VSO_MODE}" != "disabled" ]]; then
  echo "Manual Secret profiles require VSO_MODE=disabled" >&2
  exit 2
fi
if [[ "${SECRET_MODE}" != "manual" && "${VSO_MODE}" == "disabled" ]]; then
  echo "Bundled and external Secret Manager modes require VSO" >&2
  exit 2
fi

if [[ ! "${RELEASE}" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] ||
   [[ ! "${NAMESPACE}" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]]; then
  echo "RELEASE and NAMESPACE must be DNS labels" >&2
  exit 2
fi
if [[ "${BOOTSTRAP_DOCKER_PLATFORM}" != "linux/amd64" &&
      "${BOOTSTRAP_DOCKER_PLATFORM}" != "linux/arm64" ]]; then
  echo "BOOTSTRAP_DOCKER_PLATFORM must be linux/amd64 or linux/arm64" >&2
  exit 2
fi

SECRET_MODE="${SECRET_MODE}" SECRET_PROFILE="${SECRET_PROFILE}" \
SECRET_SEAL_MODE="${SECRET_SEAL_MODE}" \
SECRET_KEY_SHARES="${SECRET_KEY_SHARES}" \
SECRET_KEY_THRESHOLD="${SECRET_KEY_THRESHOLD}" \
SECRET_PGP_KEYS="${SECRET_PGP_KEYS:-}" \
SECRET_ROOT_TOKEN_PGP_KEY="${SECRET_ROOT_TOKEN_PGP_KEY:-}" \
SECRET_STORE_SEAL_CONFIG_SECRET="${SECRET_STORE_SEAL_CONFIG_SECRET}" \
  bash "${REPO_ROOT}/deploy/k8s/secrets/validate-seal-inputs.sh"

if [[ "${SECRET_MODE}" == "external" ]]; then
  : "${SECRET_STORE_ADDRESS:?Set SECRET_STORE_ADDRESS for external mode}"
  case "${SECRET_ENGINE}" in
    openbao|hashicorp-vault) ;;
    *)
      echo "External mode supports openbao or hashicorp-vault" >&2
      exit 2
      ;;
  esac
  if [[ "${SECRET_STORE_ADDRESS}" != https://* && "${ALLOW_INSECURE_EXTERNAL_HTTP:-false}" != "true" ]]; then
    echo "External Secret Manager must use HTTPS unless ALLOW_INSECURE_EXTERNAL_HTTP=true." >&2
    exit 2
  fi
elif [[ "${SECRET_MODE}" == "bundled" ]]; then
  case "${SECRET_ENGINE}" in
    openbao) ;;
    hashicorp-vault)
      if [[ "${HASHICORP_LICENSE_ACKNOWLEDGED:-false}" != "true" ]]; then
        echo "Set HASHICORP_LICENSE_ACKNOWLEDGED=true after reviewing HashiCorp Vault terms" >&2
        exit 2
      fi
      ;;
    *)
      echo "SECRET_ENGINE must be openbao or hashicorp-vault" >&2
      exit 2
      ;;
  esac
fi

HELM=(helm)
KUBECTL=(kubectl)
if [[ -n "${KUBE_CONTEXT}" ]]; then
  HELM+=(--kube-context "${KUBE_CONTEXT}")
  KUBECTL+=(--context "${KUBE_CONTEXT}")
fi

helm dependency build "${SCRIPT_DIR}" >/dev/null
"${KUBECTL[@]}" create namespace "${NAMESPACE}" --dry-run=client -o yaml | \
  "${KUBECTL[@]}" apply -f -
if [[ "${SECRET_MODE}" == "bundled" && "${SECRET_PROFILE}" == "production" &&
      -z "${SECRET_STORE_CERT_ISSUER_NAME:-}" ]] &&
   ! "${KUBECTL[@]}" get secret akb-secret-store-tls -n "${NAMESPACE}" >/dev/null 2>&1; then
  echo "Production bundle requires an existing akb-secret-store-tls Secret or SECRET_STORE_CERT_ISSUER_NAME" >&2
  exit 2
fi
if [[ "${SECRET_MODE}" != "manual" ]]; then
  VSO_MODE="${VSO_MODE}" KUBE_CONTEXT="${KUBE_CONTEXT}" \
    bash "${REPO_ROOT}/deploy/cluster/ensure-vso.sh"
fi

PROFILE_VALUES="${SCRIPT_DIR}/profiles/${AKB_PROFILE}.yaml"
HELM_ARGS=(
  upgrade --install "${RELEASE}" "${SCRIPT_DIR}"
  --namespace "${NAMESPACE}"
  --values "${PROFILE_VALUES}"
  --set-string "secretManager.profile=${SECRET_PROFILE}"
  --set-string "secretManager.sealMode=${SECRET_SEAL_MODE}"
  --set-string "secretManager.topology=${SECRET_TOPOLOGY}"
  --set "secretManager.bootstrap.keyShares=${SECRET_KEY_SHARES}"
  --set "secretManager.bootstrap.keyThreshold=${SECRET_KEY_THRESHOLD}"
)

if [[ -n "${BACKEND_IMAGE:-}" ]]; then
  HELM_ARGS+=(--set-string "images.backend.repository=${BACKEND_IMAGE%:*}")
  HELM_ARGS+=(--set-string "images.backend.tag=${BACKEND_IMAGE##*:}")
fi
if [[ -n "${FRONTEND_IMAGE:-}" ]]; then
  HELM_ARGS+=(--set-string "images.frontend.repository=${FRONTEND_IMAGE%:*}")
  HELM_ARGS+=(--set-string "images.frontend.tag=${FRONTEND_IMAGE##*:}")
fi
if [[ -n "${PUBLIC_URL:-}" ]]; then
  PUBLIC_HOST="${PUBLIC_URL#https://}"
  PUBLIC_HOST="${PUBLIC_HOST#http://}"
  PUBLIC_HOST="${PUBLIC_HOST%/}"
  HELM_ARGS+=(--set-string "global.publicUrl=${PUBLIC_URL}" --set-string "ingress.host=${PUBLIC_HOST}")
fi
if [[ -n "${SSO_KEYCLOAK_PUBLIC_URL:-}" ]]; then
  SSO_HOST="${SSO_KEYCLOAK_PUBLIC_URL#https://}"
  SSO_HOST="${SSO_HOST#http://}"
  SSO_HOST="${SSO_HOST%/}"
  HELM_ARGS+=(--set-string "sso.keycloakPublicUrl=${SSO_KEYCLOAK_PUBLIC_URL}" --set-string "sso.ingress.host=${SSO_HOST}")
fi
if [[ -n "${STORAGE_CLASS:-}" ]]; then
  HELM_ARGS+=(
    --set-string "postgres.persistence.storageClass=${STORAGE_CLASS}"
    --set-string "vaultData.storageClass=${STORAGE_CLASS}"
    --set-string "sso.postgres.persistence.storageClass=${STORAGE_CLASS}"
  )
fi

if [[ "${SECRET_MODE}" == "manual" ]]; then
  if ! "${KUBECTL[@]}" get secret akb-secret -n "${NAMESPACE}" >/dev/null 2>&1; then
    if [[ "${GENERATE_MANUAL_SECRETS:-false}" != "true" ]]; then
      echo "Create akb-secret before Helm install, or set GENERATE_MANUAL_SECRETS=true for a new installation" >&2
      exit 2
    fi
    auth_profile=local
    [[ "${AKB_PROFILE}" == *-sso ]] && auth_profile=sso
    backend_image="${BACKEND_IMAGE:-akb-backend:latest}"
    docker run --rm --platform "${BOOTSTRAP_DOCKER_PLATFORM}" \
      -v "${REPO_ROOT}/deploy/k8s/secrets/bootstrap_material.py:/opt/akb/bootstrap_material.py:ro" \
      "${backend_image}" python /opt/akb/bootstrap_material.py \
      --format kubernetes --auth-profile "${auth_profile}" --namespace "${NAMESPACE}" | \
      "${KUBECTL[@]}" apply -f -
  fi
  "${HELM[@]}" "${HELM_ARGS[@]}" --wait --timeout 10m "$@"
  exit 0
fi

if [[ "${SECRET_MODE}" == "external" ]]; then
  HELM_ARGS+=(
    --set-string secretManager.mode=external
    --set-string "secretManager.engine=${SECRET_ENGINE}"
    --set-string "secretManager.connection.address=${SECRET_STORE_ADDRESS}"
    --set secretSync.enabled=true
    --set openbao.enabled=false
    --set hashicorpVault.enabled=false
  )
  if [[ -n "${SECRET_STORE_CA_SECRET:-}" ]]; then
    HELM_ARGS+=(--set-string "secretManager.connection.caSecretName=${SECRET_STORE_CA_SECRET}")
  fi
  "${HELM[@]}" "${HELM_ARGS[@]}" --wait --timeout 10m "$@"
  exit 0
fi

append_public_key_files() {
  local setting="$1"
  local csv="$2"
  local item index=0
  local old_ifs="${IFS}"
  IFS=','
  for item in ${csv}; do
    IFS="${old_ifs}"
    item="${item#${item%%[![:space:]]*}}"
    item="${item%${item##*[![:space:]]}}"
    [[ -n "${item}" ]] || continue
    if [[ "${item}" == keybase:* || ! -s "${item}" ]]; then
      echo "Chart-native bootstrap requires a readable local PGP public-key file: ${item}" >&2
      exit 2
    fi
    HELM_ARGS+=(--set-file "${setting}[${index}]=${item}")
    index=$((index + 1))
    IFS=','
  done
  IFS="${old_ifs}"
}

case "${SECRET_ENGINE}" in
  openbao)
    HELM_ARGS+=(
      --set-string secretManager.engine=openbao
      --set openbao.enabled=true
      --set hashicorpVault.enabled=false
    )
    engine_values_prefix=openbao
    ;;
  hashicorp-vault)
    if [[ "${HASHICORP_LICENSE_ACKNOWLEDGED:-false}" != "true" ]]; then
      echo "Set HASHICORP_LICENSE_ACKNOWLEDGED=true after reviewing HashiCorp Vault terms" >&2
      exit 2
    fi
    HELM_ARGS+=(
      --set-string secretManager.engine=hashicorp-vault
      --set secretManager.hashicorpLicenseAcknowledged=true
      --set openbao.enabled=false
      --set hashicorpVault.enabled=true
    )
    engine_values_prefix=hashicorpVault
    ;;
  *)
    echo "SECRET_ENGINE must be openbao or hashicorp-vault" >&2
    exit 2
    ;;
esac

if [[ "${SECRET_TOPOLOGY}" == "onprem-small" ]]; then
  HELM_ARGS+=(
    --set "${engine_values_prefix}.server.ha.replicas=1"
    --set "${engine_values_prefix}.server.ha.disruptionBudget.enabled=false"
  )
else
  HELM_ARGS+=(--set "${engine_values_prefix}.server.ha.replicas=3")
fi
if [[ "${SECRET_SEAL_MODE}" == "auto" ]]; then
  engine_home=/openbao
  [[ "${SECRET_ENGINE}" == "hashicorp-vault" ]] && engine_home=/vault
  HELM_ARGS+=(
    --set-string "${engine_values_prefix}.server.extraVolumes[0].type=secret"
    --set-string "${engine_values_prefix}.server.extraVolumes[0].name=${SECRET_STORE_SEAL_CONFIG_SECRET}"
    --set-string "${engine_values_prefix}.server.extraArgs=-config=${engine_home}/userconfig/${SECRET_STORE_SEAL_CONFIG_SECRET}/seal.hcl"
  )
fi
if [[ -n "${SECRET_PGP_KEYS:-}" ]]; then
  append_public_key_files "secretManager.bootstrap.pgp.unsealPublicKeys" "${SECRET_PGP_KEYS}"
fi
if [[ -n "${SECRET_RECOVERY_PGP_KEYS:-}" ]]; then
  append_public_key_files "secretManager.bootstrap.pgp.recoveryPublicKeys" "${SECRET_RECOVERY_PGP_KEYS}"
fi
if [[ -n "${SECRET_ROOT_TOKEN_PGP_KEY:-}" ]]; then
  if [[ "${SECRET_ROOT_TOKEN_PGP_KEY}" == keybase:* || ! -s "${SECRET_ROOT_TOKEN_PGP_KEY}" ]]; then
    echo "Chart-native bootstrap requires a readable root-token PGP public-key file" >&2
    exit 2
  fi
  HELM_ARGS+=(--set-file "secretManager.bootstrap.pgp.rootTokenPublicKey=${SECRET_ROOT_TOKEN_PGP_KEY}")
fi
if [[ -n "${SECRET_STORE_CERT_ISSUER_NAME:-}" ]]; then
  HELM_ARGS+=(
    --set secretManager.tls.certificate.create=true
    --set-string "secretManager.tls.certificate.issuerName=${SECRET_STORE_CERT_ISSUER_NAME}"
    --set-string "secretManager.tls.certificate.issuerKind=${SECRET_STORE_CERT_ISSUER_KIND:-ClusterIssuer}"
  )
fi
# The chart-managed Job initializes and unseals the bundled server, stores the
# one-time recovery handoff, bootstraps the Secret Contract, and lets VSO start
# the application. --wait-for-jobs makes this wrapper return only when that
# lifecycle and all workloads are ready. Plain helm install remains supported.
"${HELM[@]}" "${HELM_ARGS[@]}" --wait --wait-for-jobs \
  --timeout "${HELM_INSTALL_TIMEOUT:-35m}" "$@"
