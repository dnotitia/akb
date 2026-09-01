#!/usr/bin/env bash
# Thin source-tree installer for the AKB Helm chart. Helm owns declarative
# resources; the native Secret Manager script owns only init/unseal/bootstrap.

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

if [[ ! "${RELEASE}" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] ||
   [[ ! "${NAMESPACE}" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]]; then
  echo "RELEASE and NAMESPACE must be DNS labels" >&2
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

HELM=(helm)
KUBECTL=(kubectl)
if [[ -n "${KUBE_CONTEXT}" ]]; then
  HELM+=(--kube-context "${KUBE_CONTEXT}")
  KUBECTL+=(--context "${KUBE_CONTEXT}")
fi

helm dependency build "${SCRIPT_DIR}" >/dev/null
"${KUBECTL[@]}" create namespace "${NAMESPACE}" --dry-run=client -o yaml | \
  "${KUBECTL[@]}" apply -f -

PROFILE_VALUES="${SCRIPT_DIR}/profiles/${AKB_PROFILE}.yaml"
HELM_ARGS=(
  upgrade --install "${RELEASE}" "${SCRIPT_DIR}"
  --namespace "${NAMESPACE}"
  --values "${PROFILE_VALUES}"
  --set-string "secretManager.profile=${SECRET_PROFILE}"
  --set-string "secretManager.sealMode=${SECRET_SEAL_MODE}"
  --set-string "secretManager.topology=${SECRET_TOPOLOGY}"
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
    docker run --rm --platform linux/amd64 \
      -v "${REPO_ROOT}/deploy/k8s/secrets/bootstrap_material.py:/opt/akb/bootstrap_material.py:ro" \
      "${backend_image}" python /opt/akb/bootstrap_material.py \
      --format kubernetes --auth-profile "${auth_profile}" --namespace "${NAMESPACE}" | \
      "${KUBECTL[@]}" apply -f -
  fi
  "${HELM[@]}" "${HELM_ARGS[@]}" --wait --timeout 10m "$@"
  exit 0
fi

if [[ "${SECRET_MODE}" == "external" ]]; then
  : "${SECRET_STORE_ADDRESS:?Set SECRET_STORE_ADDRESS for external mode}"
  case "${SECRET_ENGINE}" in
    openbao|hashicorp-vault) ;;
    *)
      echo "External mode supports openbao or hashicorp-vault" >&2
      exit 2
      ;;
  esac
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
  if [[ "${INSTALL_VSO:-false}" == "true" ]]; then
    HELM_ARGS+=(--set vso.enabled=true)
  fi
  "${HELM[@]}" "${HELM_ARGS[@]}" --wait --timeout 10m "$@"
  exit 0
fi

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
if [[ -n "${SECRET_STORE_CERT_ISSUER_NAME:-}" ]]; then
  HELM_ARGS+=(
    --set secretManager.tls.certificate.create=true
    --set-string "secretManager.tls.certificate.issuerName=${SECRET_STORE_CERT_ISSUER_NAME}"
    --set-string "secretManager.tls.certificate.issuerKind=${SECRET_STORE_CERT_ISSUER_KIND:-ClusterIssuer}"
  )
fi
if [[ "${INSTALL_VSO:-true}" == "false" ]]; then
  HELM_ARGS+=(--set vso.enabled=false)
fi

# The first pass intentionally does not --wait: a production Vault-compatible
# server is sealed and AKB workloads wait for the Secret contract.
"${HELM[@]}" "${HELM_ARGS[@]}" "$@"

RELEASE="${RELEASE}" NAMESPACE="${NAMESPACE}" KUBE_CONTEXT="${KUBE_CONTEXT}" \
SECRET_KEY_SHARES="${SECRET_KEY_SHARES}" \
SECRET_KEY_THRESHOLD="${SECRET_KEY_THRESHOLD}" \
SECRET_PGP_KEYS="${SECRET_PGP_KEYS:-}" \
SECRET_ROOT_TOKEN_PGP_KEY="${SECRET_ROOT_TOKEN_PGP_KEY:-}" \
SECRET_RECOVERY_PGP_KEYS="${SECRET_RECOVERY_PGP_KEYS:-}" \
  bash "${SCRIPT_DIR}/scripts/initialize-secret-manager.sh"

# Reconcile once more and gate the complete stack after the Secret contract is
# available. Helm remains the sole owner of declarative resources.
"${HELM[@]}" "${HELM_ARGS[@]}" --wait --timeout 15m "$@"
