#!/usr/bin/env bash
set -euo pipefail

fixture_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$fixture_root/../../.." && pwd)"
cert_dir="$(mktemp -d "${TMPDIR:-/tmp}/akb-sso-broker-chain.XXXXXX")"
project_name="akb-sso-broker-chain-$$"
compose_file="$fixture_root/compose.yaml"

cleanup() {
  AKB_SSO_FIXTURE_CERT_DIR="$cert_dir" docker compose \
    --project-name "$project_name" --file "$compose_file" \
    down --volumes --remove-orphans >/dev/null 2>&1 || true
  if [[ "$cert_dir" == "${TMPDIR:-/tmp}/akb-sso-broker-chain."* ]]; then
    rm -rf -- "$cert_dir"
  fi
}
trap cleanup EXIT INT TERM

openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 1 \
  -subj "/CN=AKB SSO broker-chain fixture" \
  -addext "subjectAltName=DNS:broker.localhost,DNS:upstream.localhost" \
  -keyout "$cert_dir/tls.key" -out "$cert_dir/tls.crt" >/dev/null 2>&1
chmod 600 "$cert_dir/tls.key"

AKB_SSO_FIXTURE_CERT_DIR="$cert_dir" docker compose \
  --project-name "$project_name" --file "$compose_file" up --detach

for endpoint in \
  "https://broker.localhost:19443/realms/master/.well-known/openid-configuration" \
  "https://upstream.localhost:19444/realms/workforce/.well-known/openid-configuration"
do
  ready=false
  for _attempt in $(seq 1 90); do
    if curl --fail --silent --insecure "$endpoint" >/dev/null 2>&1; then
      ready=true
      break
    fi
    sleep 2
  done
  if [[ "$ready" != true ]]; then
    echo "Keycloak fixture did not become ready" >&2
    exit 1
  fi
done

cd "$repo_root/backend"
uv run --locked python "$fixture_root/exercise.py"
