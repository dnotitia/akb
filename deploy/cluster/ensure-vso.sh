#!/usr/bin/env bash
# Ensure the cluster-scoped Vault Secrets Operator prerequisite without making
# an individual AKB release its owner.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
CHART_DIR="${REPO_ROOT}/deploy/helm/akb-cluster"

VSO_MODE="${VSO_MODE:-auto}"
VSO_VERSION="1.5.1"
VSO_NAMESPACE="${VSO_NAMESPACE:-vault-secrets-operator}"
VSO_RELEASE="${VSO_RELEASE:-akb-cluster}"
KUBE_CONTEXT="${KUBE_CONTEXT:-}"

case "${VSO_MODE}" in
  auto|install|reuse) ;;
  disabled)
    echo "VSO prerequisite check disabled"
    exit 0
    ;;
  *)
    echo "VSO_MODE must be auto, install, reuse, or disabled" >&2
    exit 2
    ;;
esac

for command_name in kubectl helm jq; do
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "${command_name} is required to manage the VSO prerequisite" >&2
    exit 2
  fi
done

KUBECTL=(kubectl)
HELM=(helm)
if [[ -n "${KUBE_CONTEXT}" ]]; then
  KUBECTL+=(--context "${KUBE_CONTEXT}")
  HELM+=(--kube-context "${KUBE_CONTEXT}")
fi

for value in "${VSO_NAMESPACE}" "${VSO_RELEASE}"; do
  if [[ ! "${value}" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]]; then
    echo "VSO namespace and release must be DNS labels" >&2
    exit 2
  fi
done

required_crds=(
  vaultconnections.secrets.hashicorp.com
  vaultauths.secrets.hashicorp.com
  vaultstaticsecrets.secrets.hashicorp.com
)

controller_record=""
controller_release=""
controller_release_namespace=""

check_required_crds() {
  local crd
  for crd in "${required_crds[@]}"; do
    if ! "${KUBECTL[@]}" get crd "${crd}" >/dev/null 2>&1; then
      echo "VSO controller exists but required CRD/${crd} is missing" >&2
      exit 1
    fi
  done
}

validate_version() {
  local image="$1"
  local version="${image##*:}"
  version="${version%%@*}"
  version="${version#v}"
  if [[ ! "${version}" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)([-+].*)?$ ]]; then
    echo "Cannot determine the VSO version from image ${image}" >&2
    exit 1
  fi
  local major="${BASH_REMATCH[1]}"
  local minor="${BASH_REMATCH[2]}"
  if (( major != 1 || minor < 4 || minor > 5 )); then
    echo "VSO ${version} is outside the supported range >=1.4.0,<1.6.0" >&2
    exit 1
  fi
  if [[ "${version}" != "${VSO_VERSION}" ]]; then
    echo "Reusing compatible VSO ${version}; the AKB pinned install version is ${VSO_VERSION}" >&2
  fi
}

discover_controller() {
  local records count namespace name image ready desired release_name release_namespace
  records="$("${KUBECTL[@]}" get deployments -A -o json | jq -r '
    .items[]
    | . as $deployment
    | [.spec.template.spec.containers[]?.image
        | select(test("(^|/)vault-secrets-operator(:|@)"))] as $images
    | select(($images | length) > 0)
    | [
        $deployment.metadata.namespace,
        $deployment.metadata.name,
        $images[0],
        ($deployment.status.readyReplicas // 0),
        ($deployment.spec.replicas // 1),
        ($deployment.metadata.annotations["meta.helm.sh/release-name"] // ""),
        ($deployment.metadata.annotations["meta.helm.sh/release-namespace"] // "")
      ]
    | @tsv
  ')"
  count="$(printf '%s\n' "${records}" | awk 'NF {count++} END {print count+0}')"
  if (( count == 0 )); then
    local present=0 crd
    for crd in "${required_crds[@]}"; do
      if "${KUBECTL[@]}" get crd "${crd}" >/dev/null 2>&1; then
        present=$((present + 1))
      fi
    done
    if (( present > 0 )); then
      echo "VSO CRDs exist but no VSO controller Deployment was found" >&2
      exit 1
    fi
    return 1
  fi
  if (( count > 1 )); then
    echo "Multiple VSO controller Deployments were found; refusing ambiguous ownership" >&2
    printf '%s\n' "${records}" >&2
    exit 1
  fi

  IFS=$'\t' read -r namespace name image ready desired release_name release_namespace <<<"${records}"
  if (( desired < 1 || ready < 1 )); then
    echo "VSO controller ${namespace}/${name} is not Ready (${ready}/${desired})" >&2
    exit 1
  fi
  validate_version "${image}"
  check_required_crds
  controller_record="${namespace}/${name}"
  controller_release="${release_name}"
  controller_release_namespace="${release_namespace}"
  echo "Using cluster VSO ${controller_record} (${image})"
}

install_controller() {
  if [[ -n "${controller_record}" ]]; then
    if [[ "${controller_release}" != "${VSO_RELEASE}" ||
          "${controller_release_namespace}" != "${VSO_NAMESPACE}" ]]; then
      echo "Existing VSO ${controller_record} is not owned by Helm release ${VSO_NAMESPACE}/${VSO_RELEASE}" >&2
      echo "Use VSO_MODE=reuse, or let its cluster owner upgrade it" >&2
      exit 1
    fi
  fi
  helm dependency build "${CHART_DIR}" >/dev/null
  "${HELM[@]}" upgrade --install "${VSO_RELEASE}" "${CHART_DIR}" \
    --namespace "${VSO_NAMESPACE}" --create-namespace \
    --wait --timeout 5m
  controller_record=""
  discover_controller
}

case "${VSO_MODE}" in
  auto)
    if ! discover_controller; then
      echo "No VSO installation found; installing cluster prerequisite ${VSO_VERSION}"
      install_controller
    fi
    ;;
  reuse)
    if ! discover_controller; then
      echo "VSO_MODE=reuse requires an existing compatible VSO installation" >&2
      exit 1
    fi
    ;;
  install)
    discover_controller || true
    install_controller
    ;;
esac
