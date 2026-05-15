#!/usr/bin/env bash
#
# AKB Kubernetes deploy — builds + pushes images, applies a kustomize tree.
#
# Required env:
#   REGISTRY      Docker registry to push to (e.g. ghcr.io/myorg or
#                 my-registry.local:5000). Images are tagged
#                 ${REGISTRY}/akb-backend:latest and akb-frontend:latest.
#
# Optional env:
#   NAMESPACE     K8s namespace (default: akb).
#   KUSTOMIZE_DIR Directory passed to `kubectl kustomize`. Defaults to
#                 the script's own directory (= base manifests). Set to
#                 an overlay (e.g. deploy/k8s/internal) to apply private
#                 hostnames, ClusterIssuers, and ConfigMap overrides in
#                 a single atomic apply — no placeholder window.
#   PUBLIC_URL    Printed at the end. Cosmetic only — the actual host
#                 lives in ingress.yaml (or its overlay patch).
#
# See deploy/k8s/README.md for the operator-overlay pattern.

set -euo pipefail

: "${REGISTRY:?Set REGISTRY env (e.g. REGISTRY=ghcr.io/myorg)}"
NAMESPACE="${NAMESPACE:-akb}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
KUSTOMIZE_DIR="${KUSTOMIZE_DIR:-${SCRIPT_DIR}}"
ROOT_DIR="${SCRIPT_DIR}/../.."

# Product version is the single source of truth in backend/pyproject.toml.
# Each build publishes :${VERSION} (immutable, for rollback / pin) and :latest
# (what the running Deployment references, so `kubectl rollout restart`
# picks it up under imagePullPolicy: Always).
VERSION="$(awk -F'"' '/^version = /{print $2; exit}' "${ROOT_DIR}/backend/pyproject.toml")"
: "${VERSION:?Could not read [project].version from backend/pyproject.toml}"

echo "=== Building Docker images (linux/amd64) — version ${VERSION} ==="
docker buildx build --platform linux/amd64 \
  -t "${REGISTRY}/akb-backend:${VERSION}" \
  -t "${REGISTRY}/akb-backend:latest" \
  --push \
  "${ROOT_DIR}/backend/"

docker buildx build --platform linux/amd64 \
  -t "${REGISTRY}/akb-frontend:${VERSION}" \
  -t "${REGISTRY}/akb-frontend:latest" \
  --push \
  "${ROOT_DIR}/frontend/"

echo "=== Creating namespace ==="
kubectl apply -f "${SCRIPT_DIR}/namespace.yaml"

echo "=== Applying manifests (kustomize: ${KUSTOMIZE_DIR}) ==="
# --load-restrictor=LoadRestrictionsNone lets an overlay reference the
# base via `../foo.yaml`. No-op for the base (which only references local
# files), needed when KUSTOMIZE_DIR is an overlay sitting inside the
# base tree.
kubectl kustomize --load-restrictor=LoadRestrictionsNone "${KUSTOMIZE_DIR}" | \
  sed "s|image: akb-backend:latest|image: ${REGISTRY}/akb-backend:latest|g" | \
  sed "s|image: akb-frontend:latest|image: ${REGISTRY}/akb-frontend:latest|g" | \
  kubectl apply -f -

echo "=== Rolling restart to pick up :latest image ==="
# `imagePullPolicy: Always` only pulls on pod creation; if the Deployment
# spec is unchanged k8s doesn't reschedule, so `:latest` edits silently
# no-op. Trigger a rollout so the new image is actually deployed.
kubectl rollout restart "deployment/backend"  -n "${NAMESPACE}"
kubectl rollout restart "deployment/frontend" -n "${NAMESPACE}"

echo "=== Waiting for pods ==="
kubectl wait --for=condition=ready pod -l app=akb-postgres -n "${NAMESPACE}" --timeout=180s || echo "PG not ready yet, continuing..."
kubectl wait --for=condition=ready pod -l app=akb-backend  -n "${NAMESPACE}" --timeout=120s || echo "Backend not ready yet"
kubectl wait --for=condition=ready pod -l app=akb-frontend -n "${NAMESPACE}" --timeout=120s || echo "Frontend not ready yet"

echo ""
echo "=== Deployment complete ==="
[ -n "${PUBLIC_URL:-}" ] && echo "URL: ${PUBLIC_URL}"
echo "Status:"
kubectl get pods -n "${NAMESPACE}"
echo ""
echo "Next steps if not done:"
echo "  kubectl edit configmap akb-app-config -n ${NAMESPACE}  # Adjust app.yaml"
echo "  kubectl edit secret    akb-secret-config -n ${NAMESPACE}  # Set secret.yaml"
