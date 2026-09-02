#!/usr/bin/env bash
# Common entry point used by the four discoverable profile directories.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
: "${AKB_PROFILE:?Profile wrapper must set AKB_PROFILE}"

if [[ ! -f "${SCRIPT_DIR}/${AKB_PROFILE}/profile.env" ]]; then
  echo "Unknown AKB Kubernetes profile: ${AKB_PROFILE}" >&2
  exit 2
fi

exec env AKB_PROFILE="${AKB_PROFILE}" bash "${SCRIPT_DIR}/../deploy.sh"
