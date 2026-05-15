#!/usr/bin/env bash
# AKB all-in-one entrypoint. Initializes (idempotent):
#   * Postgres cluster at /data/pgsql + pgvector extension
#   * MinIO root credentials + akb-files bucket
#   * Secret file from env-var template
# Then hands off to supervisord.
set -euo pipefail

PG_BIN=/usr/lib/postgresql/16/bin
PGDATA=/data/pgsql
LOG_DIR=/var/log/akb
mkdir -p "${LOG_DIR}"

# --- Secret generation (stable across restarts; persisted under /var/lib/akb) ---
SECRET_STATE=/var/lib/akb/state.env
mkdir -p /var/lib/akb
if [ ! -f "${SECRET_STATE}" ]; then
  cat > "${SECRET_STATE}" <<EOF
DB_PASSWORD=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')
JWT_SECRET=$(python3 -c 'import secrets;print(secrets.token_hex(32))')
S3_ACCESS_KEY=akb-allinone
S3_SECRET_KEY=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')
DEMO_USERNAME=demo
DEMO_EMAIL=demo@akb.local
DEMO_PASSWORD=$(python3 -c 'import secrets;print(secrets.token_urlsafe(16))')
DEMO_VAULT=demo
DEMO_PAT=akb_$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')
EOF
  chmod 600 "${SECRET_STATE}"
fi
# shellcheck disable=SC1090
. "${SECRET_STATE}"

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
export DB_PASSWORD JWT_SECRET S3_ACCESS_KEY S3_SECRET_KEY \
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

# Render /etc/akb/secret.yaml from template via envsubst-equivalent.
python3 - <<'PY'
import os, pathlib
tpl = pathlib.Path("/etc/akb/secret.yaml.template").read_text()
out = tpl
for key in ("DB_PASSWORD", "JWT_SECRET", "EMBED_API_KEY", "LLM_API_KEY",
            "RERANK_API_KEY", "S3_ACCESS_KEY", "S3_SECRET_KEY"):
    out = out.replace("${" + key + "}", os.environ.get(key, ""))
pathlib.Path("/etc/akb/secret.yaml").write_text(out)
os.chmod("/etc/akb/secret.yaml", 0o600)
PY

# Override app.yaml fields the user supplied via ENV.
python3 - <<'PY'
import os, pathlib, re
path = pathlib.Path("/etc/akb/app.yaml")
text = path.read_text()
overrides = {
    "embed_base_url": os.environ.get("EMBED_BASE_URL", ""),
    "embed_model":    os.environ.get("EMBED_MODEL", ""),
    "embed_dimensions": os.environ.get("EMBED_DIMENSIONS", ""),
    "llm_base_url":   os.environ.get("LLM_BASE_URL", ""),
    "llm_model":      os.environ.get("LLM_MODEL", ""),
}
for key, val in overrides.items():
    if not val:
        continue
    text = re.sub(rf"^{key}:.*$", f"{key}: {val}", text, count=1, flags=re.MULTILINE)
path.write_text(text)
PY

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
