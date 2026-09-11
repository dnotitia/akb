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
DEFAULT_CHECKOUT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
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

# VPN-backed Ubuntu runners may start with a path MTU below the host default.
# Persist and apply PLPMTUD mode 2 before the first network provisioning
# operation so uv/apt downloads proactively use the kernel's black-hole
# recovery instead of a fixed MTU.
command -v sysctl >/dev/null 2>&1 \
  || die "sysctl is required to enable TCP MTU probing"
SYSCTL_CONF=/etc/sysctl.d/99-akb-e2e-tcp-mtu-probing.conf
printf '%s\n' 'net.ipv4.tcp_mtu_probing = 2' \
  | "${SUDO[@]}" tee "$SYSCTL_CONF" >/dev/null \
  || die "could not persist TCP MTU probing configuration"
"${SUDO[@]}" sysctl --load "$SYSCTL_CONF" >/dev/null \
  || die "could not apply TCP MTU probing configuration"
SYSCTL_VALUE=$("${SUDO[@]}" sysctl -n net.ipv4.tcp_mtu_probing) \
  || die "could not verify TCP MTU probing configuration"
[ "$SYSCTL_VALUE" = "2" ] \
  || die "TCP MTU probing verification failed (found $SYSCTL_VALUE)"

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
  FRONTEND_PACKAGE_JSON="$CHECKOUT/frontend/package.json"
  [ -f "$FRONTEND_PACKAGE_JSON" ] \
    || die "frontend package.json is required for frontend toolchain discovery"
  FRONTEND_NODE_VERSION=$(node -e \
    'const fs=require("fs"); const p=JSON.parse(fs.readFileSync(process.argv[1], "utf8")); process.stdout.write(p.engines?.node ?? "")' \
    "$FRONTEND_PACKAGE_JSON") \
    || die "frontend Node.js version could not be read from package.json"
  FRONTEND_PACKAGE_MANAGER=$(node -e \
    'const fs=require("fs"); const p=JSON.parse(fs.readFileSync(process.argv[1], "utf8")); process.stdout.write(p.packageManager ?? "")' \
    "$FRONTEND_PACKAGE_JSON") \
    || die "frontend package manager could not be read from package.json"
  case "$FRONTEND_PACKAGE_MANAGER" in
    pnpm@*) FRONTEND_PNPM_VERSION="${FRONTEND_PACKAGE_MANAGER#pnpm@}" ;;
    *) die "frontend packageManager must declare a pinned pnpm version" ;;
  esac
  [ -n "$FRONTEND_NODE_VERSION" ] \
    || die "frontend engines.node must declare a pinned Node.js version"
  [ -n "$FRONTEND_PNPM_VERSION" ] \
    || die "frontend packageManager must declare a pinned pnpm version"

  "${SUDO[@]}" npm install --global --prefix /usr/local \
    "node@$FRONTEND_NODE_VERSION" "pnpm@$FRONTEND_PNPM_VERSION" \
    || die "Node.js/pnpm frontend toolchain installation failed"
  export PATH="/usr/local/bin:$PATH"
  NODE_VERSION=$(node --version) \
    || die "Node.js version check failed for the frontend runtime"
  [ "$NODE_VERSION" = "v$FRONTEND_NODE_VERSION" ] \
    || die "frontend Node.js version verification failed (expected $FRONTEND_NODE_VERSION, found $NODE_VERSION)"

  command -v pnpm >/dev/null 2>&1 \
    || die "pnpm is unavailable after frontend toolchain installation"
  PNPM_VERSION=$(pnpm --version) \
    || die "pnpm version check failed for the frontend runtime"
  [ "$PNPM_VERSION" = "$FRONTEND_PNPM_VERSION" ] \
    || die "frontend pnpm version verification failed (expected $FRONTEND_PNPM_VERSION, found $PNPM_VERSION)"
  (cd -- "$CHECKOUT/frontend" && NPM_CONFIG_LEGACY_PEER_DEPS=true pnpm install --frozen-lockfile) \
    || die "frontend pnpm install --frozen-lockfile failed"
fi

SUPERVISOR_COMMAND=(
  "$UV_BIN" run --locked --project "$CHECKOUT/backend" python
  "$CHECKOUT/scripts/ci/e2e_runtime.py" "$MODE"
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
