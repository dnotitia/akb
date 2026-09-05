#!/usr/bin/env bash
# Provision the small Ubuntu 24.04 host layer, then hand control to the
# Python E2E supervisor.  Runtime lifecycle belongs in e2e_runtime.py.
set -Eeuo pipefail

# Provisioning output belongs on stderr.  Keep the original stdout open so
# the final supervisor can use it for its single JSON descriptor line.
exec 3>&1 1>&2

die() {
  echo "provisioning failure: $*" >&2
  exit 1
}

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
DEFAULT_CHECKOUT=$(cd -- "$SCRIPT_DIR/../../.." && pwd)
MODE="${1:-}"
[ "$MODE" = "gate" ] || [ "$MODE" = "serve" ] \
  || die "usage: $0 {gate|serve} [--with-frontend] [--profile tool-only|transport-proxy|oidc-resource-server|transport-oidc] [--capability stdio|oidc] [--scenario empty|app-installation-lifecycle|app-release-rollout|app-control-plane] [--checkout PATH] [--runtime-root PATH] [supervisor options]"
shift

CHECKOUT="${AKB_CHECKOUT:-$DEFAULT_CHECKOUT}"
RUNTIME_ROOT="${AKB_RUNTIME_ROOT:-}"
WITH_FRONTEND=0
FORWARD_ARGS=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --checkout)
      [ "$#" -ge 2 ] || die "--checkout requires a path"
      CHECKOUT=$2
      shift 2
      ;;
    --runtime-root)
      [ "$#" -ge 2 ] || die "--runtime-root requires a path"
      RUNTIME_ROOT=$2
      shift 2
      ;;
    --with-frontend)
      WITH_FRONTEND=1
      FORWARD_ARGS+=("$1")
      shift
      ;;
    *)
      FORWARD_ARGS+=("$1")
      shift
      ;;
  esac
done

CHECKOUT=$(cd -- "$CHECKOUT" 2>/dev/null && pwd) \
  || die "checkout does not exist: $CHECKOUT"
[ -f "$CHECKOUT/backend/pyproject.toml" ] \
  || die "checkout is missing backend/pyproject.toml: $CHECKOUT"
[ -f "$CHECKOUT/backend/uv.lock" ] \
  || die "checkout is missing backend/uv.lock: $CHECKOUT"

if [ -z "$RUNTIME_ROOT" ]; then
  RUNTIME_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/akb-e2e-bootstrap.XXXXXX") \
    || die "could not create private runtime root"
else
  mkdir -p -- "$RUNTIME_ROOT" || die "could not create runtime root: $RUNTIME_ROOT"
fi
RUNTIME_ROOT=$(cd -- "$RUNTIME_ROOT" && pwd) \
  || die "could not resolve runtime root: $RUNTIME_ROOT"
case "$RUNTIME_ROOT/" in
  "$CHECKOUT/"*) die "runtime root must be outside the checkout" ;;
esac
chmod 700 -- "$RUNTIME_ROOT" || die "could not make runtime root private"

if [ "$(id -u)" -eq 0 ]; then
  SUDO=()
  RUN_USER=$(id -un)
else
  command -v sudo >/dev/null 2>&1 || die "sudo is required for Ubuntu provisioning"
  SUDO=(sudo)
  RUN_USER=$(id -un)
fi

# The VM contract is intentionally narrow.  A different base image is a
# provisioning error rather than an invitation to guess package names.
source /etc/os-release 2>/dev/null || die "cannot inspect /etc/os-release"
[ "${ID:-}" = "ubuntu" ] && [ "${VERSION_ID:-}" = "24.04" ] \
  || die "Ubuntu 24.04 is required (found ${ID:-unknown} ${VERSION_ID:-unknown})"

"${SUDO[@]}" apt-get update \
  || die "apt package index update failed; check VM networking/DNS"
"${SUDO[@]}" apt-get install -y curl ca-certificates \
  || die "curl/CA package installation failed"

# The transport profiles execute the repository's real zero-dependency Node
# consumer after this host layer completes.  Use the Ubuntu 24.04 archive
# rather than an unpinned third-party installer so a clean VM gets the same
# package-managed node/npm toolchain as the rest of its base image.
"${SUDO[@]}" apt-get install -y nodejs npm \
  || die "Node.js/npm package installation failed"
command -v node >/dev/null 2>&1 \
  || die "Node.js executable is unavailable after package installation"
command -v npm >/dev/null 2>&1 \
  || die "npm executable is unavailable after package installation"
NODE_VERSION=$(node --version) \
  || die "Node.js version check failed"
NPM_VERSION=$(npm --version) \
  || die "npm version check failed"
echo "Node.js ${NODE_VERSION}, npm ${NPM_VERSION} ready" >&2

if ! command -v docker >/dev/null 2>&1; then
  "${SUDO[@]}" apt-get install -y docker.io \
    || die "Docker Engine package installation failed"
fi

if ! docker compose version >/dev/null 2>&1; then
  if ! "${SUDO[@]}" apt-get install -y docker-compose-v2 >/dev/null 2>&1; then
    "${SUDO[@]}" apt-get install -y docker-compose-plugin \
      || die "Docker Compose package installation failed"
  fi
fi

if command -v systemctl >/dev/null 2>&1; then
  "${SUDO[@]}" systemctl enable --now docker \
    || die "Docker Engine could not be started"
elif command -v service >/dev/null 2>&1; then
  "${SUDO[@]}" service docker start \
    || die "Docker Engine could not be started"
fi

command -v docker >/dev/null 2>&1 || die "Docker Engine command is unavailable"
docker compose version >/dev/null 2>&1 \
  || die "Docker Compose v2 command is unavailable"

# A first install may create the socket group after the login session started.
# Add the VM user idempotently and, when possible, execute the final command
# in that group immediately so a reboot is not required for this run.
DOCKER_GROUP_EXEC=()
if ! docker info >/dev/null 2>&1; then
  if ! "${SUDO[@]}" docker info >/dev/null 2>&1; then
    die "Docker Engine is installed but not reachable"
  fi
  getent group docker >/dev/null 2>&1 \
    || die "Docker socket group is unavailable"
  "${SUDO[@]}" usermod -aG docker "$RUN_USER" \
    || die "could not add $RUN_USER to the docker group"
  command -v sg >/dev/null 2>&1 \
    || die "sg is required to use the docker group without re-login"
  DOCKER_GROUP_EXEC=(sg docker -c)
fi

mkdir -p -- "$RUNTIME_ROOT/bin" "$RUNTIME_ROOT/uv-cache" \
  || die "could not create uv directories"
chmod 700 -- "$RUNTIME_ROOT/bin" "$RUNTIME_ROOT/uv-cache" \
  || die "could not make uv directories private"

UV_BIN="${UV_BIN:-}"
if [ -n "$UV_BIN" ] && command -v "$UV_BIN" >/dev/null 2>&1; then
  UV_BIN=$(command -v "$UV_BIN")
else
  UV_BIN="$RUNTIME_ROOT/bin/uv"
  if [ ! -x "$UV_BIN" ]; then
    env UV_INSTALL_DIR="$RUNTIME_ROOT/bin" \
      curl --fail --location --silent --show-error https://astral.sh/uv/install.sh \
      | env UV_INSTALL_DIR="$RUNTIME_ROOT/bin" sh \
      || die "uv installer failed; check VM networking/DNS"
  fi
fi
[ -x "$UV_BIN" ] || die "uv executable was not installed"
"$UV_BIN" --version >/dev/null \
  || die "uv is installed but cannot execute"

export UV_PROJECT_ENVIRONMENT="$RUNTIME_ROOT/venv"
export UV_CACHE_DIR="$RUNTIME_ROOT/uv-cache"

"$UV_BIN" python install 3.14 \
  || die "Python 3.14 provisioning failed through uv"
"$UV_BIN" python find 3.14 >/dev/null \
  || die "uv cannot resolve a Python 3.14 interpreter"
"$UV_BIN" sync --locked --extra dev --project "$CHECKOUT/backend" \
  || die "uv sync --locked failed; dependency or network provisioning is incomplete"

PYTHON_VERSION=$("$UV_BIN" run --locked --project "$CHECKOUT/backend" python \
  -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")') \
  || die "uv-managed Python could not execute"
case "$PYTHON_VERSION" in
  3.14.*) ;;
  *) die "uv-managed Python 3.14 verification failed (found $PYTHON_VERSION)" ;;
esac

if [ "$WITH_FRONTEND" -eq 1 ]; then
  # Vite 8 requires a modern Node runtime. Keep the frontend path explicit
  # and reproducible without changing the default backend/MCP bootstrap.
  "${SUDO[@]}" npm install --global --prefix /usr/local node@22.19.0 pnpm@11.21.0 \
    || die "Node.js/pnpm frontend toolchain installation failed"
  export PATH="/usr/local/bin:$PATH"
  NODE_VERSION=$(node --version) \
    || die "Node.js version check failed for the frontend runtime"
  case "$NODE_VERSION" in
    v22.19.*) ;;
    *) die "Node.js 22.19.0 verification failed for the frontend runtime (found $NODE_VERSION)" ;;
  esac

  command -v pnpm >/dev/null 2>&1 \
    || die "pnpm is unavailable after frontend toolchain installation"
  (cd -- "$CHECKOUT/frontend" && pnpm install --frozen-lockfile) \
    || die "frontend pnpm install --frozen-lockfile failed"
fi

SUPERVISOR_COMMAND=(
  "$UV_BIN" run --locked --project "$CHECKOUT/backend" python
  "$CHECKOUT/backend/scripts/ci/e2e_runtime.py" "$MODE"
  --checkout "$CHECKOUT"
  --runtime-root "$RUNTIME_ROOT"
  "${FORWARD_ARGS[@]}"
)

if [ "${#DOCKER_GROUP_EXEC[@]}" -gt 0 ]; then
  # `%q` keeps paths/forwarded options as argv boundaries inside `sg -c`.
  COMMAND_STRING=$(printf '%q ' "${SUPERVISOR_COMMAND[@]}")
  exec 1>&3 3>&-
  exec sg docker -c "$COMMAND_STRING"
fi

exec 1>&3 3>&-
exec "${SUPERVISOR_COMMAND[@]}"
