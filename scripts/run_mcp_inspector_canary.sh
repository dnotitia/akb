#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PACKAGE_ROOT="${REPO_ROOT}/packages/akb-mcp-client"
RUNTIME_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/akb-mcp-inspector-canary.XXXXXX")"
DESCRIPTOR_PATH="${RUNTIME_ROOT}/descriptor.json"
RUNTIME_LOG="${RUNTIME_ROOT}/runtime.log"
RUNTIME_PID=""

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  if [[ -n "${RUNTIME_PID}" ]] && kill -0 "${RUNTIME_PID}" 2>/dev/null; then
    kill -TERM "${RUNTIME_PID}" 2>/dev/null || true
    wait "${RUNTIME_PID}" 2>/dev/null || true
  fi
  rm -rf -- "${RUNTIME_ROOT}"
  exit "${status}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

uv sync --locked --extra dev --project backend >/dev/null
npm ci --prefix "${PACKAGE_ROOT}" >/dev/null

if [[ -z "${AKB_E2E_USERNAME:-}" ]]; then
  AKB_E2E_USERNAME="$(uv run --locked --project backend python -c 'import secrets; print(f"akb-e2e-{secrets.token_hex(8)}")')"
  export AKB_E2E_USERNAME
fi
if [[ -z "${AKB_E2E_PASSWORD:-}" ]]; then
  AKB_E2E_PASSWORD="$(uv run --locked --project backend python -c 'import secrets; print(secrets.token_urlsafe(24))')"
  export AKB_E2E_PASSWORD
fi

uv run --locked --project backend python \
  "${REPO_ROOT}/backend/scripts/ci/e2e_runtime.py" serve \
  --scenario empty \
  --profile tool-only \
  --checkout "${REPO_ROOT}" \
  --runtime-root "${RUNTIME_ROOT}" \
  >"${DESCRIPTOR_PATH}" 2>"${RUNTIME_LOG}" &
RUNTIME_PID=$!

for _ in $(seq 1 900); do
  if [[ -s "${DESCRIPTOR_PATH}" ]]; then
    break
  fi
  if ! kill -0 "${RUNTIME_PID}" 2>/dev/null; then
    echo "MCP E2E runtime exited before publishing its ready descriptor" >&2
    exit 1
  fi
  sleep 0.2
done

if [[ ! -s "${DESCRIPTOR_PATH}" ]]; then
  echo "MCP E2E runtime did not publish a ready descriptor" >&2
  exit 1
fi

npm --prefix "${PACKAGE_ROOT}" run --silent inspect -- \
  --intent canary \
  --target http \
  --descriptor "${DESCRIPTOR_PATH}"
