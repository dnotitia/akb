#!/usr/bin/env bash
set -euo pipefail

fixture_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$fixture_root/../../.." && pwd)"
cert_dir="$(mktemp -d "${TMPDIR:-/tmp}/akb-sso-broker-chain.XXXXXX")"
fixture_run_dir="$(mktemp -d "${TMPDIR:-/tmp}/akb-sso-broker-run.XXXXXX")"
project_name="akb-sso-broker-chain-$$"
compose_file="$fixture_root/compose.yaml"

cleanup() {
  result=$?
  trap - EXIT INT TERM
  set +e
  AKB_SSO_FIXTURE_CERT_DIR="$cert_dir" docker compose \
    --project-name "$project_name" --file "$compose_file" \
    down --volumes --remove-orphans >/dev/null 2>&1
  down_status=$?
  container_leftovers="$(
    docker ps --all --quiet \
      --filter "label=com.docker.compose.project=$project_name"
  )"
  container_status=$?
  network_leftovers="$(
    docker network ls --quiet \
      --filter "label=com.docker.compose.project=$project_name"
  )"
  network_status=$?
  volume_leftovers="$(
    docker volume ls --quiet \
      --filter "label=com.docker.compose.project=$project_name"
  )"
  volume_status=$?
  if [[ "$down_status" -ne 0 || "$container_status" -ne 0 || \
        "$network_status" -ne 0 || "$volume_status" -ne 0 ]]; then
    echo "Broker-chain fixture teardown could not be verified" >&2
    result=1
  fi
  if [[ -n "$container_leftovers$network_leftovers$volume_leftovers" ]]; then
    echo "Broker-chain fixture teardown left Compose resources" >&2
    result=1
  fi
  if [[ "$cert_dir" == "${TMPDIR:-/tmp}/akb-sso-broker-chain."* ]]; then
    rm -rf -- "$cert_dir"
  else
    echo "Refusing to remove unexpected certificate directory" >&2
    result=1
  fi
  if [[ "$fixture_run_dir" == "${TMPDIR:-/tmp}/akb-sso-broker-run."* ]]; then
    rm -rf -- "$fixture_run_dir"
  else
    echo "Refusing to remove unexpected fixture directory" >&2
    result=1
  fi
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 1 \
  -subj "/CN=AKB SSO broker-chain fixture" \
  -addext "subjectAltName=DNS:broker.localhost,DNS:upstream.localhost" \
  -keyout "$cert_dir/tls.key" -out "$cert_dir/tls.crt" >/dev/null 2>&1
chmod 600 "$cert_dir/tls.key"

mkdir -p "$fixture_run_dir/config"
cat >"$fixture_run_dir/config/app.yaml" <<'YAML'
auth_mode: sso
keycloak_enabled: true
keycloak_server_url: https://broker.localhost:19443
keycloak_realm: akb
keycloak_client_id: fixture-browser
keycloak_public_client: true
keycloak_enrollment_mode: invite_only
keycloak_verify_ssl: false
public_base_url: https://broker.localhost:19443
api_oauth_audience: https://broker.localhost:19443/api
db_host: localhost
db_port: 19445
db_name: akb
db_user: akb
db_password: akb # pragma: allowlist secret
YAML

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

postgres_ready=false
for _attempt in $(seq 1 60); do
  if AKB_SSO_FIXTURE_CERT_DIR="$cert_dir" docker compose \
    --project-name "$project_name" --file "$compose_file" \
    exec -T postgres pg_isready --username akb --dbname akb >/dev/null 2>&1; then
    postgres_ready=true
    break
  fi
  sleep 1
done
if [[ "$postgres_ready" != true ]]; then
  echo "Postgres fixture did not become ready" >&2
  exit 1
fi

cd "$fixture_run_dir"
PYTHONPATH="$repo_root/backend" uv run --project "$repo_root/backend" \
  --locked python "$fixture_root/exercise.py"
