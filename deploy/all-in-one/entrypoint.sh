#!/usr/bin/env bash
# AKB all-in-one entrypoint. Initializes (idempotent):
#   * Postgres cluster at /data/pgsql + pgvector extension
#   * MinIO root credentials + akb-files bucket
#   * YAML configuration from persistent state and optional operator overrides
# Then hands off to supervisord.
set -euo pipefail

PG_BIN=/usr/lib/postgresql/16/bin
PGDATA=/data/pgsql
LOG_DIR=/var/log/akb
mkdir -p "${LOG_DIR}"

# --- Secret generation (stable across restarts; persisted under /var/lib/akb) ---
SECRET_STATE=/var/lib/akb/state.env
mkdir -p /var/lib/akb
/opt/venv/bin/python /usr/local/bin/akb-demo-configure.py state
# shellcheck disable=SC1090
. "${SECRET_STATE}"

# One-release compatibility for an existing all-in-one data volume.  The old
# JWT secret becomes internal HMAC material only; it is never used to accept or
# issue a human session after this image starts.
if [ -z "${SYSTEM_HMAC_SECRET:-}" ]; then
  if [ -n "${JWT_SECRET:-}" ]; then
    SYSTEM_HMAC_SECRET="${JWT_SECRET}"
  else
    SYSTEM_HMAC_SECRET="$(python3 -c 'import secrets;print(secrets.token_hex(32))')"
    printf '\nSYSTEM_HMAC_SECRET=%s\n' "${SYSTEM_HMAC_SECRET}" >> "${SECRET_STATE}"
  fi
fi

# Caller-supplied API keys override (still optional — Glama introspection
# works without them).
EMBED_API_KEY="${EMBED_API_KEY:-}"
EMBED_BASE_URL="${EMBED_BASE_URL:-}"
EMBED_MODEL="${EMBED_MODEL:-}"
EMBED_DIMENSIONS="${EMBED_DIMENSIONS:-}"
LLM_API_KEY="${LLM_API_KEY:-}"
LLM_BASE_URL="${LLM_BASE_URL:-}"
LLM_MODEL="${LLM_MODEL:-}"
RERANK_API_KEY="${RERANK_API_KEY:-}"

export POSTGRES_DB=akb POSTGRES_USER=akb POSTGRES_PASSWORD="${DB_PASSWORD}"
export DB_PASSWORD SYSTEM_HMAC_SECRET S3_ACCESS_KEY S3_SECRET_KEY \
       EMBED_API_KEY LLM_API_KEY RERANK_API_KEY \
       DEMO_USERNAME DEMO_EMAIL DEMO_PASSWORD DEMO_VAULT DEMO_PAT

# Show the demo PAT prominently on first boot so the operator can hand it
# to Glama / Claude Desktop / Cursor without digging through logs.
if [ ! -f /var/lib/akb/.pat-printed ]; then
  echo ""
  echo "================================================================"
  echo "AKB all-in-one — demo credentials (override via -e on docker run):"
  echo "  DEMO_USERNAME=${DEMO_USERNAME}"
  echo "  DEMO_PASSWORD=${DEMO_PASSWORD}"
  echo "  DEMO_VAULT=${DEMO_VAULT}"
  echo "  DEMO_PAT=${DEMO_PAT}"
  echo "Use:  Authorization: Bearer ${DEMO_PAT}"
  echo "================================================================"
  echo ""
  touch /var/lib/akb/.pat-printed
fi

# Render the same app.yaml + secret.yaml contract consumed by Kubernetes.
/opt/venv/bin/python /usr/local/bin/akb-demo-configure.py render
# Validate all supplied setting names, types and cross-field constraints.
/opt/venv/bin/python -c "from app.config import settings"

# Generate the local-session signer exactly once on persistent storage.  The
# CLI is non-overwriting, so a partial or conflicting keyset fails the boot
# instead of silently replacing the installation identity.
LOCAL_SESSION_KEY_DIR=/var/lib/akb/local-session
if [ ! -d "${LOCAL_SESSION_KEY_DIR}" ]; then
  python3 -m app.cli generate-local-session-keyset \
    --output-dir "${LOCAL_SESSION_KEY_DIR}"
fi


# --- Postgres: initdb on first boot, then create role/db + vector ext ---
if [ ! -s "${PGDATA}/PG_VERSION" ]; then
  echo "[entrypoint] initdb at ${PGDATA}"
  chown -R postgres:postgres "${PGDATA}"
  # initdb runs as postgres via gosu; process substitution (<()) creates
  # a root-owned fd the postgres uid can't read, so use a temp file.
  PW_FILE="$(mktemp)"
  printf '%s' "${DB_PASSWORD}" > "${PW_FILE}"
  chown postgres:postgres "${PW_FILE}"
  chmod 600 "${PW_FILE}"
  gosu postgres "${PG_BIN}/initdb" -D "${PGDATA}" \
      --auth-host=scram-sha-256 --auth-local=trust \
      --username=postgres --pwfile="${PW_FILE}"
  rm -f "${PW_FILE}"
  echo "listen_addresses = '127.0.0.1'" >> "${PGDATA}/postgresql.conf"
  echo "unix_socket_directories = '/tmp'" >> "${PGDATA}/postgresql.conf"
fi

# Start postgres temporarily for bootstrap.
echo "[entrypoint] bootstrap: starting postgres"
gosu postgres "${PG_BIN}/pg_ctl" -D "${PGDATA}" -l /tmp/pg-bootstrap.log \
    -o "-c listen_addresses='127.0.0.1' -c unix_socket_directories='/tmp'" -w start

bootstrap_sql() {
  gosu postgres "${PG_BIN}/psql" -h /tmp -U postgres -tAc "$1"
}

if [ "$(bootstrap_sql "SELECT 1 FROM pg_roles WHERE rolname='akb'")" != "1" ]; then
  echo "[entrypoint] creating role + db"
  bootstrap_sql "CREATE ROLE akb LOGIN PASSWORD '${DB_PASSWORD}';"
  bootstrap_sql "CREATE DATABASE akb OWNER akb;"
fi
gosu postgres "${PG_BIN}/psql" -h /tmp -U postgres -d akb \
    -c "CREATE EXTENSION IF NOT EXISTS vector;" >/dev/null

# The installer, not ordinary registration, owns the first administrator.
# Re-running converges on the exact designated identity and never prints the
# password material.
ADMIN_PASSWORD_FILE="$(mktemp)"
printf '%s\n' "${DEMO_PASSWORD}" > "${ADMIN_PASSWORD_FILE}"
chmod 600 "${ADMIN_PASSWORD_FILE}"
if ! python3 -m app.cli provision-recovery-admin local \
  --username "${DEMO_USERNAME}" \
  --email "${DEMO_EMAIL}" \
  --password-file "${ADMIN_PASSWORD_FILE}"; then
  rm -f "${ADMIN_PASSWORD_FILE}"
  exit 1
fi
rm -f "${ADMIN_PASSWORD_FILE}"

gosu postgres "${PG_BIN}/pg_ctl" -D "${PGDATA}" -w stop

# --- MinIO bucket bootstrap (run once MinIO is up — done in background) ---
(
  set +e
  echo "[entrypoint] minio bucket bootstrap (background)"
  until /usr/local/bin/mc alias set local http://127.0.0.1:9000 \
        "${S3_ACCESS_KEY}" "${S3_SECRET_KEY}" >/dev/null 2>&1; do
    sleep 1
  done
  /usr/local/bin/mc mb -p local/akb-files >/dev/null 2>&1 || true
  echo "[entrypoint] minio bucket ready"
) &

echo "[entrypoint] handing off to supervisord"
exec "$@"
