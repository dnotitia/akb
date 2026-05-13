#!/usr/bin/env bash
# E2E for password recovery:
#   1. Self-service /auth/change-password (correct/incorrect current, too short, same).
#   2. /admin/users/{id}/reset-password (admin happy path; 403 from non-admin; 404 on bogus id).
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
PASS=0
FAIL=0
ERRORS=()
pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

echo "╔══════════════════════════════════════════╗"
echo "║   Password Recovery E2E                  ║"
echo "║   Target: $BASE_URL"
echo "╚══════════════════════════════════════════╝"

# Helper: promote a user to admin via the backend container's Python.
# `docker compose exec` runs inside the backend image which pip-installed
# all backend deps (asyncpg is on PATH). NO uv — container does not ship it.
# stderr stays visible so failures surface in the test log instead of being
# masked as a downstream "non-admin → 403" misattribution.
promote_admin() {
  local uname="$1"
  docker compose exec -T backend python -c "
import asyncio, asyncpg
async def go():
    dsn = 'postgresql://akb:akb@postgres:5432/akb'
    conn = await asyncpg.connect(dsn)
    await conn.execute('UPDATE users SET is_admin = TRUE WHERE username = \$1', '$uname')
    await conn.close()
asyncio.run(go())
" >/dev/null
}

# Helper: register + login → echo JWT
register_and_login() {
  local uname="$1" pw="$2"
  curl -sk -X POST "$BASE_URL/api/v1/auth/register" \
    -H 'Content-Type: application/json' \
    -d "{\"username\":\"$uname\",\"email\":\"$uname@t.dev\",\"password\":\"$pw\"}" >/dev/null
  curl -sk -X POST "$BASE_URL/api/v1/auth/login" \
    -H 'Content-Type: application/json' \
    -d "{\"username\":\"$uname\",\"password\":\"$pw\"}" \
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])'
}

login_only() {
  local uname="$1" pw="$2"
  curl -sk -X POST "$BASE_URL/api/v1/auth/login" \
    -H 'Content-Type: application/json' \
    -d "{\"username\":\"$uname\",\"password\":\"$pw\"}" \
    | python3 -c 'import sys,json;print(json.load(sys.stdin).get("token",""))'
}

me_user_id() {
  curl -sk "$BASE_URL/api/v1/auth/me" -H "Authorization: Bearer $1" \
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["user_id"])'
}

TS="$(date +%s)"
USER="pw-e2e-$TS"
USER_PW_OLD="orig-secret-12"
USER_PW_NEW="brand-new-secret-99"

# ── 1. Self-service change-password ────────────────────────
echo ""
echo "▸ 1. /auth/change-password"

JWT=$(register_and_login "$USER" "$USER_PW_OLD")
[ -n "$JWT" ] && pass "bootstrap user + login" || { fail "bootstrap" "no JWT"; exit 1; }

# Happy path
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" -X POST \
  "$BASE_URL/api/v1/auth/change-password" \
  -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' \
  -d "{\"current_password\":\"$USER_PW_OLD\",\"new_password\":\"$USER_PW_NEW\"}")
[ "$HTTP" = "200" ] && pass "happy path → 200" || fail "happy path" "got $HTTP"

# Old password rejected
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" -X POST \
  "$BASE_URL/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER\",\"password\":\"$USER_PW_OLD\"}")
[ "$HTTP" = "401" ] && pass "old pw rejected" || fail "old pw" "got $HTTP"

# New password accepted
JWT=$(login_only "$USER" "$USER_PW_NEW")
[ -n "$JWT" ] && pass "new pw accepted" || fail "new pw login" "no JWT"

# Wrong current → 401
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" -X POST \
  "$BASE_URL/api/v1/auth/change-password" \
  -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' \
  -d "{\"current_password\":\"WRONG\",\"new_password\":\"another-secret-77\"}")
[ "$HTTP" = "401" ] && pass "wrong current → 401" || fail "wrong current" "got $HTTP"

# New too short → 400
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" -X POST \
  "$BASE_URL/api/v1/auth/change-password" \
  -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' \
  -d "{\"current_password\":\"$USER_PW_NEW\",\"new_password\":\"short\"}")
[ "$HTTP" = "400" ] && pass "too short → 400" || fail "too short" "got $HTTP"

# New == current → 400
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" -X POST \
  "$BASE_URL/api/v1/auth/change-password" \
  -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' \
  -d "{\"current_password\":\"$USER_PW_NEW\",\"new_password\":\"$USER_PW_NEW\"}")
[ "$HTTP" = "400" ] && pass "same as current → 400" || fail "same as current" "got $HTTP"

# ── 2. Admin reset ─────────────────────────────────────────
echo ""
echo "▸ 2. /admin/users/{id}/reset-password"

ADMIN="pw-e2e-admin-$TS"
ADMIN_PW="admin-secret-12"
register_and_login "$ADMIN" "$ADMIN_PW" >/dev/null
promote_admin "$ADMIN"
ADMIN_JWT=$(login_only "$ADMIN" "$ADMIN_PW")
[ -n "$ADMIN_JWT" ] && pass "admin bootstrap + login" || { fail "admin" "no JWT"; exit 1; }

# Need the user's UUID — me endpoint
USER_JWT=$(login_only "$USER" "$USER_PW_NEW")
USER_ID=$(me_user_id "$USER_JWT")

# Admin reset
R=$(curl -sk -X POST "$BASE_URL/api/v1/admin/users/$USER_ID/reset-password" \
  -H "Authorization: Bearer $ADMIN_JWT")
TEMP=$(echo "$R" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("temporary_password",""))')
[ -n "$TEMP" ] && pass "admin reset returns temp pw" || fail "admin reset" "$R"

# Old pw no longer works
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" -X POST \
  "$BASE_URL/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER\",\"password\":\"$USER_PW_NEW\"}")
[ "$HTTP" = "401" ] && pass "user-old-pw rejected after admin reset" \
  || fail "old user pw" "got $HTTP"

# Temp pw works
NEW_USER_JWT=$(login_only "$USER" "$TEMP")
[ -n "$NEW_USER_JWT" ] && pass "user logs in with temp pw" \
  || fail "temp pw login" "empty"

# Non-admin calls admin endpoint → 403
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" -X POST \
  "$BASE_URL/api/v1/admin/users/$USER_ID/reset-password" \
  -H "Authorization: Bearer $NEW_USER_JWT")
[ "$HTTP" = "403" ] && pass "non-admin → 403" || fail "non-admin" "got $HTTP"

# Bogus user_id → 404
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" -X POST \
  "$BASE_URL/api/v1/admin/users/00000000-0000-0000-0000-000000000000/reset-password" \
  -H "Authorization: Bearer $ADMIN_JWT")
[ "$HTTP" = "404" ] && pass "bogus user → 404" || fail "bogus user" "got $HTTP"

# Summary
echo ""
echo "═══════════════════════════════════"
if [ $FAIL -eq 0 ]; then
  echo "✓ All $PASS tests passed"
  exit 0
else
  echo "✗ $FAIL failures (of $((PASS+FAIL)) total)"
  printf '  - %s\n' "${ERRORS[@]}"
  exit 1
fi
