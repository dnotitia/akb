#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AKB_PROFILE=standalone-secret-manager exec bash "${SCRIPT_DIR}/../deploy-profile.sh"
