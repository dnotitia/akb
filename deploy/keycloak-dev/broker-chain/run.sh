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
# Keycloak runs as uid 1000 and reads both files through the bind mount, so
# `mktemp -d`'s 0700 directory silently fails the fixture on every host whose
# invoking user is not uid 1000 -- the server refuses to start with
# "Failed to initialize truststore ... Permission denied" and the runner reports
# only that Keycloak never became ready. 0711 lets the container traverse
# without letting anyone list the directory; the files themselves are a one-day
# self-signed certificate generated per run and destroyed on teardown, and the
# runner already refuses to reuse them anywhere else.
chmod 711 "$cert_dir"
chmod 644 "$cert_dir/tls.key" "$cert_dir/tls.crt"

mkdir -p "$fixture_run_dir/config"
sso_session_epoch="$(python3 -c 'import uuid; print(uuid.uuid4())')"
cat >"$fixture_run_dir/config/app.yaml" <<YAML
auth_mode: sso
auth_runtime_generation: 1
sso_session_epoch: "$sso_session_epoch"
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

# The transient authority the rotation phase uses is created the way a
# deployment creates one: Keycloak's own `bootstrap-admin service` command, in a
# separate short-lived process against the running broker's database. Nothing
# standing in the realm can create it -- that is the boundary the phase exists
# to prove -- so it cannot be minted through the Admin REST API here either.
#
# The client id carries a per-run nonce so a repeated run never inherits a
# previous one, and the secret is generated here and never written to disk.
rotation_nonce="$(openssl rand -hex 6)"
rotation_client_id="akb-rotation-$rotation_nonce"
rotation_client_secret="$(openssl rand -base64 32 | tr -d '\n')"
if ! AKB_SSO_FIXTURE_CERT_DIR="$cert_dir" docker compose \
  --project-name "$project_name" --file "$compose_file" \
  run --rm --no-deps \
  --env "AKB_FIXTURE_ROTATION_SECRET=$rotation_client_secret" \
  broker bootstrap-admin service \
  --client-id "$rotation_client_id" \
  --client-secret:env=AKB_FIXTURE_ROTATION_SECRET \
  --no-prompt >/dev/null 2>&1
then
  echo "Transient rotation authority could not be created" >&2
  exit 1
fi

cd "$fixture_run_dir"
# Every endpoint this fixture talks to is loopback. On a host with a proxy
# configured, an HTTP client that honours the environment sends
# broker.localhost through it and the run fails as "unreachable" -- a proxy's
# 403 wearing the fixture's error message. Name the hosts explicitly rather
# than relying on the ambient no_proxy list to already contain them.
NO_PROXY="broker.localhost,upstream.localhost,localhost,127.0.0.1,::1${NO_PROXY:+,$NO_PROXY}" \
no_proxy="broker.localhost,upstream.localhost,localhost,127.0.0.1,::1${no_proxy:+,$no_proxy}" \
AKB_FIXTURE_ROTATION_CLIENT_ID="$rotation_client_id" \
AKB_FIXTURE_ROTATION_CLIENT_SECRET="$rotation_client_secret" \
PYTHONPATH="$repo_root/backend" uv run --project "$repo_root/backend" \
  --locked python "$fixture_root/exercise.py"
