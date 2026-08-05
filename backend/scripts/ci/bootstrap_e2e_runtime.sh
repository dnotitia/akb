#!/usr/bin/env bash
# Prepare an Ubuntu host and exec the repository-owned HTTP E2E runtime.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../../.."

readonly UV_VERSION="0.12.1"
readonly UV_VERSION_PATTERN="^uv ${UV_VERSION//./\\.}( \\([^[:space:]]+\\))?$"
readonly DATA_ROOT="${AKB_E2E_BOOTSTRAP_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/akb-e2e-bootstrap}"
readonly UV_BIN="$DATA_ROOT/bin/uv"
readonly VENV="$DATA_ROOT/venv"

uv_version_is_expected() {
  [[ "$1" =~ $UV_VERSION_PATTERN ]]
}

if ! command -v curl >/dev/null 2>&1 \
  || ! command -v docker >/dev/null 2>&1 \
  || ! { docker compose version >/dev/null 2>&1 \
    || sudo -n docker compose version >/dev/null 2>&1; }; then
  sudo -n apt-get update
  sudo -n env DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1 \
    apt-get install -y ca-certificates curl docker.io docker-compose-v2
fi

if ! docker info >/dev/null 2>&1 && ! sudo -n docker info >/dev/null 2>&1; then
  sudo -n systemctl start docker
fi

if [[ -z "${AKB_E2E_DOCKER_ARGV:-}" ]]; then
  if docker info >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    export AKB_E2E_DOCKER_ARGV="docker"
  elif sudo -n docker info >/dev/null 2>&1 \
    && sudo -n docker compose version >/dev/null 2>&1; then
    export AKB_E2E_DOCKER_ARGV="sudo -n docker"
  else
    echo "Docker Engine with Compose is unavailable" >&2
    exit 1
  fi
fi

mkdir -p "$DATA_ROOT/bin" "$DATA_ROOT/python" "$DATA_ROOT/cache"
uv_output="$($UV_BIN --version 2>/dev/null || true)"
if ! uv_version_is_expected "$uv_output"; then
  curl --proto '=https' --tlsv1.2 -LsSf "https://astral.sh/uv/$UV_VERSION/install.sh" \
    | env UV_UNMANAGED_INSTALL="$DATA_ROOT/bin" sh
fi
uv_output="$($UV_BIN --version 2>/dev/null || true)"
if ! uv_version_is_expected "$uv_output"; then
  echo "expected uv $UV_VERSION at $UV_BIN" >&2
  exit 1
fi

export UV_CACHE_DIR="$DATA_ROOT/cache"
export UV_PYTHON_INSTALL_DIR="$DATA_ROOT/python"
export UV_PROJECT_ENVIRONMENT="$VENV"

"$UV_BIN" python install 3.14
if ! "$VENV/bin/python" -c \
  'import sys; raise SystemExit(sys.version_info[:2] != (3, 14))' \
  >/dev/null 2>&1; then
  "$UV_BIN" venv --clear --python 3.14 "$VENV"
fi
"$UV_BIN" sync --project backend --locked --no-dev --python 3.14

exec "$VENV/bin/python" backend/scripts/ci/e2e_runtime.py "$@"
