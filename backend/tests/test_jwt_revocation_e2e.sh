#!/bin/bash
#
# JWT revocation E2E.
# Covers:
#   T1  self revoke-all-sessions invalidates the in-flight JWT
#   T2  new login AFTER revoke yields a working JWT
#   T3  password change auto-revokes prior JWT
#   T4  admin force-logout invalidates target user's JWT
#   T5  admin force-logout requires admin role (non-admin gets 403)
#   T6  PAT survives revoke-all-sessions (PATs are a separate lifecycle)
#   T7  admin password reset also invalidates target user's JWT
#
# Run:
#   AKB_URL=http://localhost:8001 bash backend/tests/test_jwt_revocation_e2e.sh
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8001}"
TS=$(date +%s)
PASS=0; FAIL=0; ERRORS=()

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

H_JSON="Content-Type: application/json"
jq_field() { python3 -c "import sys,json; d=json.load(sys.stdin); print(d$1)" 2>/dev/null; }

register_and_login() {
  # Usage: register_and_login <username> -> echoes JWT
  local u=$1 pw="${2:-test1234}"
  curl -sk -X POST "$BASE_URL/api/v1/auth/register" -H "$H_JSON" \
    -d "{\"username\":\"$u\",\"email\":\"$u@t.dev\",\"password\":\"$pw\"}" >/dev/null
  curl -sk -X POST "$BASE_URL/api/v1/auth/login" -H "$H_JSON" \
    -d "{\"username\":\"$u\",\"password\":\"$pw\"}" | jq_field "['token']"
}

echo "▸ Setup"
U="jwtrev-$TS"
JWT=$(register_and_login "$U")
[ -n "$JWT" ] && pass "user $U + JWT acquired" || { fail "setup" "no JWT"; exit 1; }

# ─────────────────────────────────────────────────────────────
echo ""
echo "▸ T1: revoke-all-sessions invalidates the in-flight JWT"
CODE_BEFORE=$(curl -sk -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $JWT" "$BASE_URL/api/v1/auth/me")
[ "$CODE_BEFORE" = "200" ] && pass "T1 pre-revoke /auth/me → 200" || fail "T1 pre" "got $CODE_BEFORE"

REVOKE=$(curl -sk -X POST "$BASE_URL/api/v1/auth/revoke-all-sessions" \
  -H "Authorization: Bearer $JWT")
echo "$REVOKE" | grep -q "revoked_before" && pass "T1 revoke returns revoked_before" \
  || fail "T1 revoke" "$REVOKE"

CODE_AFTER=$(curl -sk -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $JWT" "$BASE_URL/api/v1/auth/me")
[ "$CODE_AFTER" = "401" ] && pass "T1 post-revoke /auth/me → 401" \
  || fail "T1 post" "expected 401, got $CODE_AFTER"

# ─────────────────────────────────────────────────────────────
echo ""
echo "▸ T2: new login after revoke yields a working JWT"
# iat is whole-second resolution; sleep 1s so the new iat strictly > revoke cutoff.
sleep 1
JWT2=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" -H "$H_JSON" \
  -d "{\"username\":\"$U\",\"password\":\"test1234\"}" | jq_field "['token']")
CODE=$(curl -sk -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $JWT2" "$BASE_URL/api/v1/auth/me")
[ "$CODE" = "200" ] && pass "T2 fresh login works post-revoke" || fail "T2" "got $CODE"

# ─────────────────────────────────────────────────────────────
echo ""
echo "▸ T3: password change auto-revokes prior JWT"
U3="jwtpw-$TS"
JWT3=$(register_and_login "$U3")
[ -n "$JWT3" ] && pass "T3 user $U3 + JWT" || fail "T3 setup" "no JWT"

CODE_OK=$(curl -sk -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $JWT3" "$BASE_URL/api/v1/auth/me")
[ "$CODE_OK" = "200" ] && pass "T3 pre-change /me → 200" || fail "T3 pre" "$CODE_OK"

# Change password using JWT3
RESP=$(curl -sk -X POST "$BASE_URL/api/v1/auth/change-password" \
  -H "Authorization: Bearer $JWT3" -H "$H_JSON" \
  -d '{"current_password":"test1234","new_password":"new12345"}')
echo "$RESP" | grep -q '"ok":true' && pass "T3 password changed" \
  || fail "T3 change" "$RESP"

CODE_REV=$(curl -sk -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $JWT3" "$BASE_URL/api/v1/auth/me")
[ "$CODE_REV" = "401" ] && pass "T3 post-change old JWT → 401" \
  || fail "T3 post" "expected 401, got $CODE_REV"

# New password works to issue a fresh JWT.
sleep 1
JWT3_NEW=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" -H "$H_JSON" \
  -d "{\"username\":\"$U3\",\"password\":\"new12345\"}" | jq_field "['token']")
CODE_NEW=$(curl -sk -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $JWT3_NEW" "$BASE_URL/api/v1/auth/me")
[ "$CODE_NEW" = "200" ] && pass "T3 new password issues working JWT" \
  || fail "T3 new login" "got $CODE_NEW"

# ─────────────────────────────────────────────────────────────
echo ""
echo "▸ T4: admin force-logout invalidates target user's JWT"
# Setup target + admin. Mark admin via PG since registration doesn't create admins.
TARGET="jwtft-$TS"
TGT_JWT=$(register_and_login "$TARGET")

ADMINU="jwtad-$TS"
register_and_login "$ADMINU" >/dev/null
docker compose -p akb-audit \
  -f /Users/kwoo2/Desktop/storage/akb-audit/docker-compose.yaml \
  -f /Users/kwoo2/Desktop/storage/akb-audit/docker-compose.audit.yaml \
  exec -T postgres psql -U akb -d akb -c \
  "UPDATE users SET is_admin = true WHERE username = '$ADMINU';" >/dev/null

# Login again to pick up the now-admin flag (is_admin is read live, but
# need a JWT to call admin endpoints).
sleep 1
ADM_JWT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" -H "$H_JSON" \
  -d "{\"username\":\"$ADMINU\",\"password\":\"test1234\"}" | jq_field "['token']")

# Resolve target's user_id
TARGET_ID=$(curl -sk -H "Authorization: Bearer $TGT_JWT" "$BASE_URL/api/v1/auth/me" | jq_field "['user_id']")

# Verify target JWT works before admin acts
PRE=$(curl -sk -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TGT_JWT" "$BASE_URL/api/v1/auth/me")
[ "$PRE" = "200" ] && pass "T4 target JWT pre-force → 200" || fail "T4 pre" "$PRE"

RESP=$(curl -sk -X POST "$BASE_URL/api/v1/admin/users/$TARGET_ID/revoke-sessions" \
  -H "Authorization: Bearer $ADM_JWT")
echo "$RESP" | grep -q "revoked_before" && pass "T4 admin force-logout call" \
  || fail "T4 admin call" "$RESP"

POST=$(curl -sk -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TGT_JWT" "$BASE_URL/api/v1/auth/me")
[ "$POST" = "401" ] && pass "T4 target JWT post-force → 401" \
  || fail "T4 post" "expected 401, got $POST"

# ─────────────────────────────────────────────────────────────
echo ""
echo "▸ T5: non-admin cannot force-logout"
U5="jwtnonadm-$TS"
NON_ADM_JWT=$(register_and_login "$U5")
sleep 1  # so the new JWT isn't pre-revoke cutoff (TARGET was just revoked)
CODE=$(curl -sk -o /dev/null -w '%{http_code}' -X POST \
  "$BASE_URL/api/v1/admin/users/$TARGET_ID/revoke-sessions" \
  -H "Authorization: Bearer $NON_ADM_JWT")
[ "$CODE" = "403" ] && pass "T5 non-admin → 403" || fail "T5" "got $CODE"

# ─────────────────────────────────────────────────────────────
echo ""
echo "▸ T6: PAT survives revoke-all-sessions"
U6="jwtpat-$TS"
PW6="test1234"
JWT6=$(register_and_login "$U6" "$PW6")
PAT6=$(curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" -H "Authorization: Bearer $JWT6" \
  -H "$H_JSON" -d '{"name":"survives"}' | jq_field "['token']")
[ -n "$PAT6" ] && pass "T6 PAT created" || fail "T6 PAT create" ""

# Now revoke JWTs
curl -sk -X POST "$BASE_URL/api/v1/auth/revoke-all-sessions" -H "Authorization: Bearer $JWT6" >/dev/null
# Old JWT dead
CODE_J=$(curl -sk -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $JWT6" "$BASE_URL/api/v1/auth/me")
[ "$CODE_J" = "401" ] && pass "T6 JWT now 401" || fail "T6 jwt" "$CODE_J"
# PAT still works
CODE_P=$(curl -sk -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $PAT6" "$BASE_URL/api/v1/auth/me")
[ "$CODE_P" = "200" ] && pass "T6 PAT still 200" || fail "T6 pat" "$CODE_P"

# ─────────────────────────────────────────────────────────────
echo ""
echo "▸ T7: admin password reset invalidates target's JWT"
U7="jwtrst-$TS"
JWT7=$(register_and_login "$U7")
U7_ID=$(curl -sk -H "Authorization: Bearer $JWT7" "$BASE_URL/api/v1/auth/me" | jq_field "['user_id']")
# admin (re-using ADM_JWT from T4; if expired, re-login)
sleep 1
ADM_JWT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" -H "$H_JSON" \
  -d "{\"username\":\"$ADMINU\",\"password\":\"test1234\"}" | jq_field "['token']")
RESET=$(curl -sk -X POST "$BASE_URL/api/v1/admin/users/$U7_ID/reset-password" \
  -H "Authorization: Bearer $ADM_JWT")
echo "$RESET" | grep -q "temporary_password" && pass "T7 admin reset issued" \
  || fail "T7 admin reset" "$RESET"

CODE_OLD=$(curl -sk -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $JWT7" "$BASE_URL/api/v1/auth/me")
[ "$CODE_OLD" = "401" ] && pass "T7 victim's old JWT → 401" \
  || fail "T7 victim" "expected 401, got $CODE_OLD"

# ─────────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════"
echo "Results: $PASS passed, $FAIL failed"
echo "═══════════════════════════════════════════"
if [ "$FAIL" -gt 0 ]; then
  printf '  - %s\n' "${ERRORS[@]}"
  exit 1
fi
