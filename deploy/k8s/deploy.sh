#!/usr/bin/env bash
#
# AKB Kubernetes deploy — builds + pushes images, applies a kustomize tree.
#
# Image inputs:
#   Normal build: REGISTRY is required and images are built/pushed there.
#   Existing images: set SKIP_BUILD=true plus BACKEND_IMAGE and FRONTEND_IMAGE.
#
# Optional env:
#   NAMESPACE     K8s namespace (default: akb).
#   KUBE_CONTEXT  Explicit kubectl context. Defaults to the current context.
#   KUSTOMIZE_DIR Directory passed to `kubectl kustomize`. Defaults to
#                 the script's own directory (= base manifests). Set to
#                 an overlay (e.g. deploy/k8s/internal) to apply private
#                 hostnames, ClusterIssuers, and ConfigMap overrides in
#                 a single atomic apply — no placeholder window.
#   PUBLIC_URL    Printed at the end. Cosmetic only — the actual host
#                 lives in ingress.yaml (or its overlay patch).
#   AKB_PROFILE    standalone (default), standalone-sso,
#                  standalone-secret-manager, or
#                  standalone-sso-secret-manager. This is the public profile;
#                  AUTH_PROFILE and SECRET_MODE remain compatibility inputs.
#   SECRET_MODE    manual (default), bundled, or external. Bundled is selected
#                  by the *-secret-manager profiles; external is an adapter
#                  override for standalone / standalone-sso.
#   SECRET_ENGINE  openbao or hashicorp-vault for bundled/external modes.
#   SECRET_PROFILE development (default) or production for bundled mode.
#   SECRET_SEAL_MODE plaintext (default), pgp, or auto for production bundles.
#   SECRET_TOPOLOGY onprem-small (one Raft member) or production-ha (three).
#   SECRET_STORE_CERT_ISSUER_NAME Optional existing cert-manager CA issuer;
#                  otherwise provision akb-secret-store-tls out-of-band.
#   STORAGE_CLASS StorageClass for AKB/PostgreSQL and bundled-manager PVCs.
#
# See deploy/k8s/README.md for the operator-overlay pattern.

set -euo pipefail

NAMESPACE="${NAMESPACE:-akb}"
KUBE_CONTEXT="${KUBE_CONTEXT:-}"
SKIP_BUILD="${SKIP_BUILD:-false}"
SECRET_PROFILE="${SECRET_PROFILE:-development}"
AKB_PROFILE="${AKB_PROFILE:-}"

# Keep the original AUTH_PROFILE/SECRET_MODE inputs as a compatibility layer,
# but expose only four coherent top-level installation profiles going forward.
# External Secret Managers are an ownership adapter, not another workload
# profile, so standalone and standalone-sso may override manual with external.
if [[ -z "${AKB_PROFILE}" ]]; then
  AUTH_PROFILE="${AUTH_PROFILE:-local}"
  SECRET_MODE="${SECRET_MODE:-manual}"
  if [[ "${AUTH_PROFILE}" == "sso" && "${SECRET_MODE}" == "bundled" ]]; then
    AKB_PROFILE="standalone-sso-secret-manager"
  elif [[ "${AUTH_PROFILE}" == "sso" ]]; then
    AKB_PROFILE="standalone-sso"
  elif [[ "${SECRET_MODE}" == "bundled" ]]; then
    AKB_PROFILE="standalone-secret-manager"
  else
    AKB_PROFILE="standalone"
  fi
else
  case "${AKB_PROFILE}" in
    standalone)
      PROFILE_AUTH="local"
      PROFILE_SECRET="manual" # pragma: allowlist secret
      ;;
    standalone-sso)
      PROFILE_AUTH="sso"
      PROFILE_SECRET="manual" # pragma: allowlist secret
      ;;
    standalone-secret-manager)
      PROFILE_AUTH="local"
      PROFILE_SECRET="bundled" # pragma: allowlist secret
      ;;
    standalone-sso-secret-manager)
      PROFILE_AUTH="sso"
      PROFILE_SECRET="bundled" # pragma: allowlist secret
      ;;
    *)
      echo "AKB_PROFILE must be standalone, standalone-sso, standalone-secret-manager, or standalone-sso-secret-manager" >&2
      exit 2
      ;;
  esac
  AUTH_PROFILE="${AUTH_PROFILE:-${PROFILE_AUTH}}"
  SECRET_MODE="${SECRET_MODE:-${PROFILE_SECRET}}"
  if [[ "${AUTH_PROFILE}" != "${PROFILE_AUTH}" ]]; then
    echo "AUTH_PROFILE=${AUTH_PROFILE} conflicts with AKB_PROFILE=${AKB_PROFILE}" >&2
    exit 2
  fi
  case "${AKB_PROFILE}" in
    standalone|standalone-sso)
      if [[ "${SECRET_MODE}" != "manual" && "${SECRET_MODE}" != "external" ]]; then
        echo "${AKB_PROFILE} supports SECRET_MODE=manual or external; use a *-secret-manager profile for bundled" >&2
        exit 2
      fi
      ;;
    *-secret-manager)
      if [[ "${SECRET_MODE}" != "bundled" ]]; then
        echo "${AKB_PROFILE} requires SECRET_MODE=bundled" >&2
        exit 2
      fi
      ;;
  esac
fi

SECRET_SEAL_MODE="${SECRET_SEAL_MODE:-plaintext}"
SECRET_TOPOLOGY="${SECRET_TOPOLOGY:-production-ha}"
SECRET_KEY_SHARES="${SECRET_KEY_SHARES:-5}"
SECRET_KEY_THRESHOLD="${SECRET_KEY_THRESHOLD:-3}"
SECRET_PGP_KEYS="${SECRET_PGP_KEYS:-}"
SECRET_ROOT_TOKEN_PGP_KEY="${SECRET_ROOT_TOKEN_PGP_KEY:-}"
SECRET_RECOVERY_PGP_KEYS="${SECRET_RECOVERY_PGP_KEYS:-}"
SECRET_STORE_SEAL_CONFIG_SECRET="${SECRET_STORE_SEAL_CONFIG_SECRET:-}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ "${AUTH_PROFILE}" == "sso" ]]; then
  KUSTOMIZE_DIR="${KUSTOMIZE_DIR:-${SCRIPT_DIR}/standalone-sso}"
else
  KUSTOMIZE_DIR="${KUSTOMIZE_DIR:-${SCRIPT_DIR}}"
fi
ROOT_DIR="${SCRIPT_DIR}/../.."

KUBECTL=(kubectl)
if [[ -n "${KUBE_CONTEXT}" ]]; then
  KUBECTL+=(--context "${KUBE_CONTEXT}")
fi

if [[ ! "${NAMESPACE}" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]]; then
  echo "NAMESPACE is not a valid DNS label" >&2
  exit 2
fi
if [[ "${AUTH_PROFILE}" != "local" && "${AUTH_PROFILE}" != "sso" ]]; then
  echo "AUTH_PROFILE must be local or sso" >&2
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
if [[ ! "${SECRET_KEY_SHARES}" =~ ^[1-9][0-9]*$ ]] ||
   [[ ! "${SECRET_KEY_THRESHOLD}" =~ ^[1-9][0-9]*$ ]] ||
   (( SECRET_KEY_THRESHOLD > SECRET_KEY_SHARES )); then
  echo "SECRET_KEY_SHARES and SECRET_KEY_THRESHOLD must form a valid quorum" >&2
  exit 2
fi
if [[ "${SECRET_SEAL_MODE}" != "plaintext" &&
      ( "${SECRET_MODE}" != "bundled" || "${SECRET_PROFILE}" != "production" ) ]]; then
  echo "SECRET_SEAL_MODE=${SECRET_SEAL_MODE} requires a bundled production Secret Manager profile" >&2
  exit 2
fi
if [[ "${SECRET_SEAL_MODE}" == "pgp" ]]; then
  if [[ -z "${SECRET_PGP_KEYS}" || -z "${SECRET_ROOT_TOKEN_PGP_KEY}" ]]; then
    echo "PGP mode requires SECRET_PGP_KEYS and SECRET_ROOT_TOKEN_PGP_KEY before deployment" >&2
    exit 2
  fi
  pgp_key_count=0
  old_ifs="${IFS}"
  IFS=','
  for pgp_key_ref in ${SECRET_PGP_KEYS}; do
    IFS="${old_ifs}"
    pgp_key_ref="${pgp_key_ref#${pgp_key_ref%%[![:space:]]*}}"
    pgp_key_ref="${pgp_key_ref%${pgp_key_ref##*[![:space:]]}}"
    [[ -n "${pgp_key_ref}" ]] || continue
    if [[ "${pgp_key_ref}" != keybase:* && ! -s "${pgp_key_ref}" ]]; then
      echo "PGP public key does not exist or is empty: ${pgp_key_ref}" >&2
      exit 2
    fi
    pgp_key_count=$((pgp_key_count + 1))
  done
  IFS="${old_ifs}"
  if (( pgp_key_count != SECRET_KEY_SHARES )); then
    echo "SECRET_PGP_KEYS requires ${SECRET_KEY_SHARES} comma-separated public keys; received ${pgp_key_count}" >&2
    exit 2
  fi
  if [[ "${SECRET_ROOT_TOKEN_PGP_KEY}" != keybase:* &&
        ! -s "${SECRET_ROOT_TOKEN_PGP_KEY}" ]]; then
    echo "Bootstrap root-token PGP public key does not exist or is empty: ${SECRET_ROOT_TOKEN_PGP_KEY}" >&2
    exit 2
  fi
fi
if [[ "${SECRET_SEAL_MODE}" == "auto" &&
      ! "${SECRET_STORE_SEAL_CONFIG_SECRET}" =~ ^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$ ]]; then
  echo "Auto Seal mode requires SECRET_STORE_SEAL_CONFIG_SECRET before deployment" >&2
  exit 2
fi
if [[ -n "${STORAGE_CLASS:-}" &&
      ! "${STORAGE_CLASS}" =~ ^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$ ]]; then
  echo "STORAGE_CLASS is not a valid StorageClass name" >&2
  exit 2
fi

if [[ "${AUTH_PROFILE}" == "sso" ]]; then
  : "${SSO_AKB_PUBLIC_URL:?Set SSO_AKB_PUBLIC_URL to the public AKB origin}"
  : "${SSO_KEYCLOAK_PUBLIC_URL:?Set SSO_KEYCLOAK_PUBLIC_URL to the public Keycloak origin}"
  SSO_PRODUCT_ADMIN_USERNAME="${SSO_PRODUCT_ADMIN_USERNAME:-admin}"
  SSO_PRODUCT_ADMIN_EMAIL="${SSO_PRODUCT_ADMIN_EMAIL:-admin@example.com}"
  SSO_ORIGIN_PATTERN='^https://([A-Za-z0-9.-]+)(:[0-9]+)?$'
  if [[ "${ALLOW_INSECURE_SSO_HTTP:-false}" == "true" ]]; then
    SSO_ORIGIN_PATTERN='^https?://([A-Za-z0-9.-]+)(:[0-9]+)?$'
  fi
  if [[ ! "${SSO_AKB_PUBLIC_URL}" =~ ${SSO_ORIGIN_PATTERN} ]]; then
    echo "SSO_AKB_PUBLIC_URL must be a plain HTTPS origin" >&2
    exit 2
  fi
  SSO_AKB_HOST="${BASH_REMATCH[1]}"
  if [[ ! "${SSO_KEYCLOAK_PUBLIC_URL}" =~ ${SSO_ORIGIN_PATTERN} ]]; then
    echo "SSO_KEYCLOAK_PUBLIC_URL must be a plain HTTPS origin" >&2
    exit 2
  fi
  SSO_KEYCLOAK_HOST="${BASH_REMATCH[1]}"
  if [[ ! "${SSO_PRODUCT_ADMIN_USERNAME}" =~ ^[A-Za-z0-9._-]{1,64}$ ]] ||
     [[ ! "${SSO_PRODUCT_ADMIN_EMAIL}" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]]; then
    echo "SSO product-admin username or email has an unsupported format" >&2
    exit 2
  fi
fi

# Product version is the single source of truth in backend/pyproject.toml.
# Each build publishes :${VERSION} (immutable, for rollback / pin) and :latest
# (what the running Deployment references, so `kubectl rollout restart`
# picks it up under imagePullPolicy: Always).
VERSION="$(awk -F'"' '/^version = /{print $2; exit}' "${ROOT_DIR}/backend/pyproject.toml")"
: "${VERSION:?Could not read [project].version from backend/pyproject.toml}"

if [[ "${SKIP_BUILD}" == "true" ]]; then
  : "${BACKEND_IMAGE:?Set BACKEND_IMAGE when SKIP_BUILD=true}"
  : "${FRONTEND_IMAGE:?Set FRONTEND_IMAGE when SKIP_BUILD=true}"
  echo "=== Reusing caller-supplied images ==="
else
  : "${REGISTRY:?Set REGISTRY env (e.g. REGISTRY=ghcr.io/myorg)}"
  BACKEND_IMAGE="${REGISTRY}/akb-backend:latest"
  FRONTEND_IMAGE="${REGISTRY}/akb-frontend:latest"
  echo "=== Building Docker images (linux/amd64) — version ${VERSION} ==="
  docker buildx build --platform linux/amd64 \
    -t "${REGISTRY}/akb-backend:${VERSION}" \
    -t "${BACKEND_IMAGE}" \
    --push \
    "${ROOT_DIR}/backend/"

  docker buildx build --platform linux/amd64 \
    -t "${REGISTRY}/akb-frontend:${VERSION}" \
    -t "${FRONTEND_IMAGE}" \
    --push \
    "${ROOT_DIR}/frontend/"
fi

echo "=== Creating namespace ==="
"${KUBECTL[@]}" create namespace "${NAMESPACE}" \
  --dry-run=client -o yaml | "${KUBECTL[@]}" apply -f -

echo "=== Preparing secret contract (${AKB_PROFILE}; ${SECRET_MODE}/${SECRET_ENGINE:-none}) ==="
NAMESPACE="${NAMESPACE}" \
KUBE_CONTEXT="${KUBE_CONTEXT}" \
AKB_PROFILE="${AKB_PROFILE}" \
SECRET_MODE="${SECRET_MODE}" \
SECRET_ENGINE="${SECRET_ENGINE:-}" \
SECRET_PROFILE="${SECRET_PROFILE}" \
SECRET_SEAL_MODE="${SECRET_SEAL_MODE}" \
SECRET_TOPOLOGY="${SECRET_TOPOLOGY}" \
SECRET_KEY_SHARES="${SECRET_KEY_SHARES}" \
SECRET_KEY_THRESHOLD="${SECRET_KEY_THRESHOLD}" \
SECRET_PGP_KEYS="${SECRET_PGP_KEYS}" \
SECRET_ROOT_TOKEN_PGP_KEY="${SECRET_ROOT_TOKEN_PGP_KEY}" \
SECRET_RECOVERY_PGP_KEYS="${SECRET_RECOVERY_PGP_KEYS}" \
SECRET_STORE_SEAL_CONFIG_SECRET="${SECRET_STORE_SEAL_CONFIG_SECRET}" \
AUTH_PROFILE="${AUTH_PROFILE}" \
BACKEND_IMAGE="${BACKEND_IMAGE}" \
  bash "${SCRIPT_DIR}/secrets/deploy.sh"

echo "=== Applying manifests (kustomize: ${KUSTOMIZE_DIR}) ==="
# --load-restrictor=LoadRestrictionsNone lets an overlay reference the
# base via `../foo.yaml`. No-op for the base (which only references local
# files), needed when KUSTOMIZE_DIR is an overlay sitting inside the
# base tree.
RENDER_DIR="$(mktemp -d "${TMPDIR:-/tmp}/akb-kustomize.XXXXXX")"
trap 'rm -rf "${RENDER_DIR}"' EXIT
KUSTOMIZE_SOURCE="$(cd "${KUSTOMIZE_DIR}" && pwd)"
# Kustomize rejects an absolute path in `resources` even with unrestricted
# loading. A temporary symlink keeps the generated parent overlay portable
# while preserving the caller's source tree untouched.
ln -s "${KUSTOMIZE_SOURCE}" "${RENDER_DIR}/source"
printf '%s\n' \
  'apiVersion: kustomize.config.k8s.io/v1beta1' \
  'kind: Kustomization' \
  "namespace: ${NAMESPACE}" \
  'resources:' \
  '  - source' > "${RENDER_DIR}/kustomization.yaml"

if [[ -n "${STORAGE_CLASS:-}" ]]; then
  cat >>"${RENDER_DIR}/kustomization.yaml" <<EOF
patches:
  - target:
      kind: PersistentVolumeClaim
      name: akb-vaultdata
    patch: |-
      - op: add
        path: /spec/storageClassName
        value: ${STORAGE_CLASS}
  - target:
      kind: StatefulSet
      name: postgres
    patch: |-
      - op: add
        path: /spec/volumeClaimTemplates/0/spec/storageClassName
        value: ${STORAGE_CLASS}
  - target:
      kind: StatefulSet
      name: keycloak-postgres
    patch: |-
      - op: add
        path: /spec/volumeClaimTemplates/0/spec/storageClassName
        value: ${STORAGE_CLASS}
EOF
fi

kubectl kustomize --load-restrictor=LoadRestrictionsNone "${RENDER_DIR}" | \
  sed "s|image: akb-backend:latest|image: ${BACKEND_IMAGE}|g" | \
  sed "s|image: akb-frontend:latest|image: ${FRONTEND_IMAGE}|g" \
  >"${RENDER_DIR}/rendered.yaml"

if [[ "${AUTH_PROFILE}" == "sso" ]]; then
  sed -i.bak \
    -e "s|https://auth.akb.example.com|${SSO_KEYCLOAK_PUBLIC_URL}|g" \
    -e "s|https://akb.example.com|${SSO_AKB_PUBLIC_URL}|g" \
    -e "s|auth.akb.example.com|${SSO_KEYCLOAK_HOST}|g" \
    -e "s|akb.example.com|${SSO_AKB_HOST}|g" \
    -e "s|product-admin-username: admin|product-admin-username: ${SSO_PRODUCT_ADMIN_USERNAME}|g" \
    -e "s|product-admin-email: admin@example.com|product-admin-email: ${SSO_PRODUCT_ADMIN_EMAIL}|g" \
    "${RENDER_DIR}/rendered.yaml"
  rm -f "${RENDER_DIR}/rendered.yaml.bak"
fi

"${KUBECTL[@]}" apply -f "${RENDER_DIR}/rendered.yaml"

echo "=== Rolling restart to pick up :latest image ==="
# `imagePullPolicy: Always` only pulls on pod creation; if the Deployment
# spec is unchanged k8s doesn't reschedule, so `:latest` edits silently
# no-op. Trigger a rollout so the new image is actually deployed.
"${KUBECTL[@]}" rollout restart "deployment/backend"  -n "${NAMESPACE}"
"${KUBECTL[@]}" rollout restart "deployment/frontend" -n "${NAMESPACE}"
if [[ "${AUTH_PROFILE}" == "sso" ]]; then
  "${KUBECTL[@]}" rollout restart "statefulset/keycloak" -n "${NAMESPACE}"
fi

echo "=== Waiting for pods ==="
"${KUBECTL[@]}" wait --for=condition=ready pod -l app=akb-postgres -n "${NAMESPACE}" --timeout=180s
if [[ "${AUTH_PROFILE}" == "sso" ]]; then
  "${KUBECTL[@]}" wait --for=condition=ready pod -l app=akb-keycloak-postgres \
    -n "${NAMESPACE}" --timeout=180s
  "${KUBECTL[@]}" rollout status statefulset/keycloak -n "${NAMESPACE}" --timeout=300s
fi
# A label-based pod wait captures the old pod during a Recreate/RollingUpdate
# transition and then waits forever for that deleted object to become Ready.
# Deployment rollout status follows the controller's current revision instead.
"${KUBECTL[@]}" rollout status deployment/backend -n "${NAMESPACE}" --timeout=180s
"${KUBECTL[@]}" rollout status deployment/frontend -n "${NAMESPACE}" --timeout=120s

echo ""
echo "=== Deployment complete ==="
[ -n "${PUBLIC_URL:-}" ] && echo "URL: ${PUBLIC_URL}"
echo "Status:"
"${KUBECTL[@]}" get pods -n "${NAMESPACE}"
echo ""
echo "Next steps if not done:"
echo "  kubectl edit configmap akb-app-config -n ${NAMESPACE}  # Adjust app.yaml"
echo "  kubectl get secret akb-secret -n ${NAMESPACE}  # Stable Secret Contract v1"
if [[ "${AUTH_PROFILE}" == "sso" ]]; then
  echo "  Preserve the one-time product-admin password, then follow the SSO retirement runbook."
fi
