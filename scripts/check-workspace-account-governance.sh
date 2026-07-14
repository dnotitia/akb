#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="$ROOT/backend"
PYTHON_BIN="${AKB_TEST_PYTHON:-python3}"
DSN="${AKB_TEST_DSN:-}"
BASE_REF="${AKB_COMPAT_BASE_REF:-$(tr -d '[:space:]' < "$ROOT/scripts/workspace-account-governance-base.txt")}"

if [[ -z "$DSN" ]]; then
  echo "error: AKB_TEST_DSN is required for the governance gate" >&2
  exit 2
fi
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "error: Python executable not found: $PYTHON_BIN" >&2
  exit 2
fi
if ! git -C "$ROOT" cat-file -e "$BASE_REF^{commit}"; then
  echo "error: compatibility base commit is unavailable: $BASE_REF" >&2
  exit 2
fi

"$PYTHON_BIN" -c '
import asyncio, os
import asyncpg

async def main():
    connection = await asyncpg.connect(os.environ["AKB_TEST_DSN"])
    await connection.close()

asyncio.run(main())
'

cd "$BACKEND"
"$PYTHON_BIN" -m ruff check \
  app/config.py \
  app/cli.py \
  app/exceptions.py \
  app/api/routes/auth.py \
  app/api/routes/access.py \
  app/services/auth_policy.py \
  app/services/auth_service.py \
  app/services/password_service.py \
  app/services/account_service.py \
  app/services/role_sync.py \
  app/db/postgres.py \
  app/db/migrations/043_workspace_account_governance.py \
  tests/test_auth_config_shape_unit.py \
  tests/test_local_auth_policy_unit.py \
  tests/test_workspace_account_schema.py \
  tests/test_workspace_external_identity.py \
  tests/test_account_status_auth_carriers.py \
  tests/test_workspace_account_admin_service.py \
  tests/test_workspace_account_admin_routes.py \
  tests/test_keycloak_redirect_unit.py \
  tests/test_old_image_schema_compat_unit.py \
  tests/old_image_schema_compat.py
"$PYTHON_BIN" -m mypy \
  app/services/account_service.py \
  app/services/auth_service.py \
  app/api/routes/access.py \
  app/api/routes/auth.py
"$PYTHON_BIN" -m pytest \
  tests/test_auth_config_shape_unit.py \
  tests/test_local_auth_policy_unit.py \
  tests/test_workspace_account_schema.py \
  tests/test_workspace_external_identity.py \
  tests/test_account_status_auth_carriers.py \
  tests/test_workspace_account_admin_service.py \
  tests/test_workspace_account_admin_routes.py \
  tests/test_keycloak_redirect_unit.py \
  tests/test_auth_change_password.py \
  tests/test_password_service.py \
  tests/test_mcp_oauth_unit.py \
  tests/test_old_image_schema_compat_unit.py \
  -q

tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/akb-governance-compat.XXXXXX")"
trap 'rm -rf "$tmp_dir"' EXIT
git -C "$ROOT" archive --format=tar --output="$tmp_dir/old-backend.tar" \
  "$BASE_REF" backend config
tar -xf "$tmp_dir/old-backend.tar" -C "$tmp_dir"
"$PYTHON_BIN" tests/old_image_schema_compat.py \
  --backend "$tmp_dir/backend" \
  --dsn "$DSN"

echo "workspace account governance gate: ok"
