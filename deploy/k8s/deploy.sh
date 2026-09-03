#!/usr/bin/env bash
# Build or reuse images, then apply the standalone or standalone-sso manifests.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="${SCRIPT_DIR}/../.."
NAMESPACE="${NAMESPACE:-akb}"
KUBE_CONTEXT="${KUBE_CONTEXT:-}"
AKB_PROFILE="${AKB_PROFILE:-standalone}"
SKIP_BUILD="${SKIP_BUILD:-false}"
IMAGE_PLATFORM="${IMAGE_PLATFORM:-linux/amd64}"

case "${AKB_PROFILE}" in
  standalone)
    AUTH_PROFILE=local
    PROFILE_DIR="${SCRIPT_DIR}"
    ;;
  standalone-sso)
    AUTH_PROFILE=sso
    PROFILE_DIR="${SCRIPT_DIR}/standalone-sso"
    ;;
  *)
    echo "AKB_PROFILE must be standalone or standalone-sso" >&2
    exit 2
    ;;
esac
KUSTOMIZE_DIR="${KUSTOMIZE_DIR:-${PROFILE_DIR}}"

KUBECTL=(kubectl)
if [[ -n "${KUBE_CONTEXT}" ]]; then
  KUBECTL+=(--context "${KUBE_CONTEXT}")
fi

if [[ ! "${NAMESPACE}" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]]; then
  echo "NAMESPACE is not a valid DNS label" >&2
  exit 2
fi
if [[ "${IMAGE_PLATFORM}" != "linux/amd64" && "${IMAGE_PLATFORM}" != "linux/arm64" ]]; then
  echo "IMAGE_PLATFORM must be linux/amd64 or linux/arm64" >&2
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

echo "=== Creating namespace ==="
"${KUBECTL[@]}" create namespace "${NAMESPACE}" --dry-run=client -o yaml | \
  "${KUBECTL[@]}" apply -f -

required_secrets=(akb-secret)
if [[ "${AUTH_PROFILE}" == "sso" ]]; then
  required_secrets+=(
    akb-keycloak-db-credentials
    akb-keycloak-bootstrap
    akb-product-admin-bootstrap
  )
fi

echo "=== Checking operator-owned Kubernetes Secrets ==="
for secret_name in "${required_secrets[@]}"; do
  if ! "${KUBECTL[@]}" get secret "${secret_name}" -n "${NAMESPACE}" >/dev/null 2>&1; then
    echo "Secret/${secret_name} is required in namespace ${NAMESPACE}." >&2
    echo "Provision it out of band before deploying AKB; this script never generates credentials." >&2
    exit 2
  fi
  owners="$("${KUBECTL[@]}" get secret "${secret_name}" -n "${NAMESPACE}" \
    -o 'jsonpath={range .metadata.ownerReferences[*]}{.apiVersion}{"|"}{.kind}{"\n"}{end}')"
  if printf '%s\n' "${owners}" | grep -qE '^secrets\.hashicorp\.com/.+\|VaultStaticSecret$'; then
    echo "Secret/${secret_name} is still owned by VaultStaticSecret." >&2
    echo "Follow the legacy bundle removal procedure in deploy/k8s/README.md before deploying." >&2
    exit 2
  fi
done

# Redis is optional, but preserve the same legacy-owner guard when its Secret
# exists so removing an old projection cannot silently delete that credential.
if "${KUBECTL[@]}" get secret redis-credentials -n "${NAMESPACE}" >/dev/null 2>&1; then
  redis_owners="$("${KUBECTL[@]}" get secret redis-credentials -n "${NAMESPACE}" \
    -o 'jsonpath={range .metadata.ownerReferences[*]}{.apiVersion}{"|"}{.kind}{"\n"}{end}')"
  if printf '%s\n' "${redis_owners}" | grep -qE '^secrets\.hashicorp\.com/.+\|VaultStaticSecret$'; then
    echo "Secret/redis-credentials is still owned by VaultStaticSecret." >&2
    echo "Follow the legacy bundle removal procedure in deploy/k8s/README.md before deploying." >&2
    exit 2
  fi
fi

VERSION="$(awk -F'"' '/^version = /{print $2; exit}' "${ROOT_DIR}/backend/pyproject.toml")"
: "${VERSION:?Could not read [project].version from backend/pyproject.toml}"

if [[ "${SKIP_BUILD}" == "true" ]]; then
  : "${BACKEND_IMAGE:?Set BACKEND_IMAGE when SKIP_BUILD=true}"
  : "${FRONTEND_IMAGE:?Set FRONTEND_IMAGE when SKIP_BUILD=true}"
  echo "=== Reusing caller-supplied images ==="
else
  : "${REGISTRY:?Set REGISTRY env (for example, ghcr.io/myorg)}"
  BACKEND_IMAGE="${REGISTRY}/akb-backend:latest"
  FRONTEND_IMAGE="${REGISTRY}/akb-frontend:latest"
  echo "=== Building Docker images (${IMAGE_PLATFORM}) — version ${VERSION} ==="
  docker buildx build --platform "${IMAGE_PLATFORM}" \
    -t "${REGISTRY}/akb-backend:${VERSION}" -t "${BACKEND_IMAGE}" --push \
    "${ROOT_DIR}/backend/"
  docker buildx build --platform "${IMAGE_PLATFORM}" \
    -t "${REGISTRY}/akb-frontend:${VERSION}" -t "${FRONTEND_IMAGE}" --push \
    "${ROOT_DIR}/frontend/"
fi

echo "=== Rendering ${AKB_PROFILE} ==="
RENDER_DIR="$(mktemp -d "${TMPDIR:-/tmp}/akb-kustomize.XXXXXX")"
trap 'rm -rf "${RENDER_DIR}"' EXIT
KUSTOMIZE_SOURCE="$(cd "${KUSTOMIZE_DIR}" && pwd)"
ln -s "${KUSTOMIZE_SOURCE}" "${RENDER_DIR}/source"
printf '%s\n' \
  'apiVersion: kustomize.config.k8s.io/v1beta1' \
  'kind: Kustomization' \
  "namespace: ${NAMESPACE}" \
  'resources:' \
  '  - source' >"${RENDER_DIR}/kustomization.yaml"

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
EOF
  if [[ "${AUTH_PROFILE}" == "sso" ]]; then
    cat >>"${RENDER_DIR}/kustomization.yaml" <<EOF
  - target:
      kind: StatefulSet
      name: keycloak-postgres
    patch: |-
      - op: add
        path: /spec/volumeClaimTemplates/0/spec/storageClassName
        value: ${STORAGE_CLASS}
EOF
  fi
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

echo "=== Restarting application workloads ==="
"${KUBECTL[@]}" rollout restart deployment/backend -n "${NAMESPACE}"
"${KUBECTL[@]}" rollout restart deployment/frontend -n "${NAMESPACE}"
if [[ "${AUTH_PROFILE}" == "sso" ]]; then
  "${KUBECTL[@]}" rollout restart statefulset/keycloak -n "${NAMESPACE}"
fi

"${KUBECTL[@]}" wait --for=condition=ready pod -l app=akb-postgres -n "${NAMESPACE}" --timeout=180s
if [[ "${AUTH_PROFILE}" == "sso" ]]; then
  "${KUBECTL[@]}" wait --for=condition=ready pod -l app=akb-keycloak-postgres \
    -n "${NAMESPACE}" --timeout=180s
  "${KUBECTL[@]}" rollout status statefulset/keycloak -n "${NAMESPACE}" --timeout=300s
fi
"${KUBECTL[@]}" rollout status deployment/backend -n "${NAMESPACE}" --timeout=180s
"${KUBECTL[@]}" rollout status deployment/frontend -n "${NAMESPACE}" --timeout=120s

echo "=== Deployment complete ==="
[ -n "${PUBLIC_URL:-}" ] && echo "URL: ${PUBLIC_URL}"
"${KUBECTL[@]}" get pods -n "${NAMESPACE}"
