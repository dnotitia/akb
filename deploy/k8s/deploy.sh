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
#   SECRET_MODE   manual (default), bundled, or external.
#   SECRET_ENGINE openbao or hashicorp-vault for bundled/external modes.
#   SECRET_PROFILE development (default) or production for bundled mode.
#   AUTH_PROFILE   local (default) or sso. The sso profile defaults to the
#                  standalone-sso Kustomize tree and requires the SSO_* inputs.
#   STORAGE_CLASS StorageClass for AKB/PostgreSQL and bundled-manager PVCs.
#
# See deploy/k8s/README.md for the operator-overlay pattern.

set -euo pipefail

NAMESPACE="${NAMESPACE:-akb}"
KUBE_CONTEXT="${KUBE_CONTEXT:-}"
SKIP_BUILD="${SKIP_BUILD:-false}"
SECRET_MODE="${SECRET_MODE:-manual}"
SECRET_PROFILE="${SECRET_PROFILE:-development}"
AUTH_PROFILE="${AUTH_PROFILE:-local}"
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

echo "=== Preparing secret contract (${SECRET_MODE}/${SECRET_ENGINE:-none}) ==="
NAMESPACE="${NAMESPACE}" \
KUBE_CONTEXT="${KUBE_CONTEXT}" \
SECRET_MODE="${SECRET_MODE}" \
SECRET_ENGINE="${SECRET_ENGINE:-}" \
SECRET_PROFILE="${SECRET_PROFILE}" \
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
