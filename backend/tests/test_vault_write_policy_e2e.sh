#!/bin/bash
#
# AKB vault_write_policy E2E — admin marking/grant API round trip (P0 S3,
# Task 10). Modeled on test_external_git_e2e.sh's boot/auth pattern
# (register -> login -> PAT) and test_jwt_revocation_e2e.sh /
# test_events_emit_e2e.sh's direct-psql admin-bootstrap + events-table
# verification convention (there is no self-service "become admin" API by
# design, so DB access is required — same as those two siblings).
#
# Full round trip:
#   create vault + PATs (admin, owner) -> mark -> JWT write 403 (naming
#   managed_by) -> owner PAT (ungranted) write 403 -> grant the PAT ->
#   write 200 -> admin bypass write while still marked (200 + audit
#   event) -> validation edge cases (409 unmarked-grant, 404 missing
#   token/vault) -> unmark -> ungranted write 200
#   (ROLLBACK PROOF — the final assertion IS the kill-switch evidence).
#
# Uses REST (not MCP) throughout: check_vault_access's guard is
# transport-agnostic, and REST gives a literal HTTP status code to assert
# on ("expect 403") instead of an MCP JSON-RPC 200-wrapped error envelope.
#
# Run (local disposable stack, e.g. via the recipe in task-10-report.md):
#   AKB_URL=http://localhost:8010 \
#   AKB_PG_EXEC="docker exec -i akb-vwp-task10-pg" AKB_PG_USER=akb AKB_PG_DB=akb \
#   bash backend/tests/test_vault_write_policy_e2e.sh
#
# Run (cluster, matching the CLAUDE.md deploy-checklist convention):
#   AKB_URL=https://<host> AKB_NS=akb bash backend/tests/test_vault_write_policy_e2e.sh
#
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
TS="$(date +%s)-$$"
VAULT="wpe2e-$TS"
OTHER_VAULT="wpe2e-other-$TS"
ADMIN="wpe2e-admin-$TS"
OWNER="wpe2e-owner-$TS"
PASS=0; FAIL=0; ERRORS=()

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

# Portable DB access (admin bootstrap + events assertions). Defaults to
# the cluster via kubectl; override AKB_PG_EXEC for a local stack, e.g.
# AKB_PG_EXEC="docker exec -i akb-vwp-task10-pg" (disposable container) or
# AKB_PG_EXEC="docker compose exec -T postgres" (docker-compose stack).
PG_NS="${AKB_NS:-akb}"
PG_POD="${AKB_PG_POD:-postgres-0}"
PG_USER="${AKB_PG_USER:-akbuser}"
PG_DB="${AKB_PG_DB:-akb}"
run_psql() {
  if [ -n "${AKB_PG_EXEC:-}" ]; then
    ${AKB_PG_EXEC} psql -U "$PG_USER" -d "$PG_DB" -tAc "$1" 2>/dev/null
  else
    kubectl exec -n "$PG_NS" "$PG_POD" -- psql -U "$PG_USER" -d "$PG_DB" -tAc "$1" 2>/dev/null
  fi
}

if [ -z "${AKB_PG_EXEC:-}" ] && ! command -v kubectl >/dev/null 2>&1; then
  echo "no AKB_PG_EXEC and kubectl unavailable — cannot bootstrap an admin user (no self-service API by design); skipping"
  exit 0
fi

echo "▸ AKB vault_write_policy e2e → $BASE_URL"

jq_field() { python3 -c "import sys,json; d=json.load(sys.stdin); print(d$1)" 2>/dev/null; }

register_login() {
  local u=$1
  curl -sk -X POST "$BASE_URL/api/v1/auth/register" -H 'Content-Type: application/json' \
    -d "{\"username\":\"$u\",\"email\":\"$u@test.dev\",\"password\":\"test1234\"}" >/dev/null 2>&1
  curl -sk -X POST "$BASE_URL/api/v1/auth/login" -H 'Content-Type: application/json' \
    -d "{\"username\":\"$u\",\"password\":\"test1234\"}" | jq_field "['token']"
}
mint_pat() {
  local jwt=$1 name=$2
  curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" -H "Authorization: Bearer $jwt" \
    -H 'Content-Type: application/json' -d "{\"name\":\"$name\"}"
}
# Usage: write_doc <bearer-token> <vault> -> echoes "HTTP_CODE|BODY"
write_doc() {
  local token=$1 vault=$2 resp
  resp=$(curl -sk -w '\n%{http_code}' -X POST "$BASE_URL/api/v1/documents" \
    -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
    -d "{\"vault\":\"$vault\",\"collection\":\"e2e\",\"title\":\"probe\",\"content\":\"x\"}")
  echo "$(echo "$resp" | tail -1)|$(echo "$resp" | sed '$d')"
}

echo ""
echo "▸ 0. Setup: admin + owner users, vault"

ADMIN_JWT=$(register_login "$ADMIN")
[ -n "$ADMIN_JWT" ] && pass "admin registered" || { fail "setup" "no admin JWT"; exit 1; }
run_psql "UPDATE users SET is_admin = true WHERE username = '$ADMIN'" >/dev/null
IS_ADMIN=$(run_psql "SELECT is_admin FROM users WHERE username = '$ADMIN'")
[ "$IS_ADMIN" = "t" ] && pass "admin bootstrapped via psql" || { fail "admin bootstrap" "is_admin=$IS_ADMIN"; exit 1; }
ADMIN_PAT_RESP=$(mint_pat "$ADMIN_JWT" "wpe2e-admin-pat")
ADMIN_PAT=$(echo "$ADMIN_PAT_RESP" | jq_field "['token']")
[ -n "$ADMIN_PAT" ] && pass "admin PAT minted (unscoped -> A3 bypass eligible)" || { fail "admin PAT" "$ADMIN_PAT_RESP"; exit 1; }

OWNER_JWT=$(register_login "$OWNER")
[ -n "$OWNER_JWT" ] && pass "owner registered" || { fail "setup" "no owner JWT"; exit 1; }
OWNER_PAT_RESP=$(mint_pat "$OWNER_JWT" "wpe2e-owner-pat")
OWNER_PAT=$(echo "$OWNER_PAT_RESP" | jq_field "['token']")
OWNER_TOKEN_ID=$(echo "$OWNER_PAT_RESP" | jq_field "['token_id']")
[ -n "$OWNER_PAT" ] && [ -n "$OWNER_TOKEN_ID" ] && pass "owner PAT minted ($OWNER_TOKEN_ID)" || { fail "owner PAT" "$OWNER_PAT_RESP"; exit 1; }

curl -sk -X POST "$BASE_URL/api/v1/vaults?name=$VAULT" -H "Authorization: Bearer $OWNER_JWT" >/dev/null
VAULT_ID=$(run_psql "SELECT id FROM vaults WHERE name = '$VAULT'")
[ -n "$VAULT_ID" ] && pass "vault created ($VAULT_ID, owner=$OWNER)" || { fail "vault create" "not in DB"; exit 1; }

echo ""
echo "▸ 1. Baseline: owner JWT write succeeds BEFORE marking (positive control)"
R=$(write_doc "$OWNER_JWT" "$VAULT")
CODE="${R%%|*}"
[ "$CODE" = "200" ] && pass "pre-mark owner write → 200 (control)" || fail "pre-mark control" "got $CODE: ${R#*|}"

echo ""
echo "▸ 2. Mark the vault"
MARK_RESP=$(curl -sk -w '\n%{http_code}' -X PUT "$BASE_URL/api/v1/admin/vaults/$VAULT/write-policy" \
  -H "Authorization: Bearer $ADMIN_PAT" -H 'Content-Type: application/json' \
  -d '{"managed_by":"collector:wpe2e-test","note":"e2e round-trip"}')
MARK_CODE=$(echo "$MARK_RESP" | tail -1)
MARK_BODY=$(echo "$MARK_RESP" | sed '$d')
[ "$MARK_CODE" = "200" ] && pass "PUT write-policy → 200" || fail "mark" "got $MARK_CODE: $MARK_BODY"
MANAGED_BY=$(echo "$MARK_BODY" | jq_field "['managed_by']")
[ "$MANAGED_BY" = "collector:wpe2e-test" ] && pass "response echoes managed_by" || fail "mark managed_by" "got '$MANAGED_BY'"

NONADMIN_MARK_CODE=$(curl -sk -o /dev/null -w '%{http_code}' -X PUT "$BASE_URL/api/v1/admin/vaults/$VAULT/write-policy" \
  -H "Authorization: Bearer $OWNER_JWT" -H 'Content-Type: application/json' \
  -d '{"managed_by":"collector:should-fail"}')
[ "$NONADMIN_MARK_CODE" = "403" ] && pass "non-admin (owner) PUT write-policy → 403" || fail "non-admin mark" "got $NONADMIN_MARK_CODE"

echo ""
echo "▸ 3. JWT write to marked vault → 403 naming managed_by"
R=$(write_doc "$OWNER_JWT" "$VAULT")
CODE="${R%%|*}"; BODY="${R#*|}"
[ "$CODE" = "403" ] && pass "JWT write → 403" || fail "JWT write" "got $CODE: $BODY"
echo "$BODY" | grep -q "collector:wpe2e-test" && pass "403 body names managed_by" || fail "403 managed_by" "$BODY"

echo ""
echo "▸ 4. Owner PAT (ungranted) write → 403"
R=$(write_doc "$OWNER_PAT" "$VAULT")
CODE="${R%%|*}"; BODY="${R#*|}"
[ "$CODE" = "403" ] && pass "ungranted PAT write → 403" || fail "ungranted PAT write" "got $CODE: $BODY"
echo "$BODY" | grep -q "collector:wpe2e-test" && pass "403 body names managed_by (PAT path)" || fail "403 managed_by (PAT)" "$BODY"

echo ""
echo "▸ 5. Grant the owner's PAT"
GRANT_RESP=$(curl -sk -w '\n%{http_code}' -X PUT \
  "$BASE_URL/api/v1/admin/vaults/$VAULT/write-policy/grants/$OWNER_TOKEN_ID" \
  -H "Authorization: Bearer $ADMIN_PAT")
GRANT_CODE=$(echo "$GRANT_RESP" | tail -1)
[ "$GRANT_CODE" = "200" ] && pass "PUT grant → 200" || fail "grant" "got $GRANT_CODE: $(echo "$GRANT_RESP" | sed '$d')"

echo ""
echo "▸ 6. Granted PAT write → 200"
R=$(write_doc "$OWNER_PAT" "$VAULT")
CODE="${R%%|*}"
[ "$CODE" = "200" ] && pass "granted PAT write → 200" || fail "granted PAT write" "got $CODE: ${R#*|}"

echo ""
echo "▸ 7. Admin bypass write while still marked → 200 + audit event"
R=$(write_doc "$ADMIN_PAT" "$VAULT")
CODE="${R%%|*}"
[ "$CODE" = "200" ] && pass "admin bypass write → 200" || fail "admin bypass write" "got $CODE: ${R#*|}"
BYPASS_COUNT=$(run_psql "SELECT COUNT(*) FROM events WHERE vault_id = '$VAULT_ID' AND kind = 'vault.write_policy_admin_bypass'")
{ [ -n "$BYPASS_COUNT" ] && [ "$BYPASS_COUNT" -ge 1 ]; } 2>/dev/null && pass "vault.write_policy_admin_bypass event recorded (count=$BYPASS_COUNT)" || fail "bypass event" "count=$BYPASS_COUNT"

echo ""
echo "▸ 8. vault.write_policy_changed audit trail (marked + grant_added)"
MARKED_COUNT=$(run_psql "SELECT COUNT(*) FROM events WHERE vault_id = '$VAULT_ID' AND kind = 'vault.write_policy_changed' AND payload->>'action' = 'marked'")
GRANT_ADDED_COUNT=$(run_psql "SELECT COUNT(*) FROM events WHERE vault_id = '$VAULT_ID' AND kind = 'vault.write_policy_changed' AND payload->>'action' = 'grant_added'")
[ "$MARKED_COUNT" = "1" ] && pass "write_policy_changed action=marked recorded" || fail "marked event" "count=$MARKED_COUNT"
[ "$GRANT_ADDED_COUNT" = "1" ] && pass "write_policy_changed action=grant_added recorded" || fail "grant_added event" "count=$GRANT_ADDED_COUNT"

echo ""
echo "▸ 9. Validation edge cases"
curl -sk -X POST "$BASE_URL/api/v1/vaults?name=$OTHER_VAULT" -H "Authorization: Bearer $OWNER_JWT" >/dev/null
CONFLICT_CODE=$(curl -sk -o /dev/null -w '%{http_code}' -X PUT \
  "$BASE_URL/api/v1/admin/vaults/$OTHER_VAULT/write-policy/grants/$OWNER_TOKEN_ID" \
  -H "Authorization: Bearer $ADMIN_PAT")
[ "$CONFLICT_CODE" = "409" ] && pass "grant on an unmarked vault → 409" || fail "grant conflict" "got $CONFLICT_CODE"

FAKE_TOKEN="00000000-0000-0000-0000-000000000000"
NOTFOUND_TOKEN_CODE=$(curl -sk -o /dev/null -w '%{http_code}' -X PUT \
  "$BASE_URL/api/v1/admin/vaults/$VAULT/write-policy/grants/$FAKE_TOKEN" \
  -H "Authorization: Bearer $ADMIN_PAT")
[ "$NOTFOUND_TOKEN_CODE" = "404" ] && pass "grant of a missing token → 404" || fail "grant missing token" "got $NOTFOUND_TOKEN_CODE"

MISSING_VAULT_CODE=$(curl -sk -o /dev/null -w '%{http_code}' -X PUT \
  "$BASE_URL/api/v1/admin/vaults/wpe2e-does-not-exist-$TS/write-policy" \
  -H "Authorization: Bearer $ADMIN_PAT" -H 'Content-Type: application/json' \
  -d '{"managed_by":"collector:x"}')
[ "$MISSING_VAULT_CODE" = "404" ] && pass "mark of a missing vault → 404" || fail "mark missing vault" "got $MISSING_VAULT_CODE"

echo ""
echo "▸ 10. Unmark — ROLLBACK PROOF"
UNMARK_RESP=$(curl -sk -w '\n%{http_code}' -X DELETE "$BASE_URL/api/v1/admin/vaults/$VAULT/write-policy" \
  -H "Authorization: Bearer $ADMIN_PAT")
UNMARK_CODE=$(echo "$UNMARK_RESP" | tail -1)
[ "$UNMARK_CODE" = "200" ] && pass "DELETE write-policy → 200" || fail "unmark" "got $UNMARK_CODE"

UNMARKED_COUNT=$(run_psql "SELECT COUNT(*) FROM events WHERE vault_id = '$VAULT_ID' AND kind = 'vault.write_policy_changed' AND payload->>'action' = 'unmarked'")
[ "$UNMARKED_COUNT" = "1" ] && pass "write_policy_changed action=unmarked recorded" || fail "unmarked event" "count=$UNMARKED_COUNT"

# THE kill-switch assertion: an ungranted JWT write now succeeds again —
# full behavioural rollback, not a one-way ratchet.
R=$(write_doc "$OWNER_JWT" "$VAULT")
CODE="${R%%|*}"
[ "$CODE" = "200" ] && pass "POST-unmark JWT write → 200 (full rollback — kill-switch proven)" || fail "rollback proof" "got $CODE: ${R#*|}"

echo ""
echo "── Cleanup ──"
curl -sk -X DELETE "$BASE_URL/api/v1/vaults/$VAULT" -H "Authorization: Bearer $OWNER_JWT" >/dev/null 2>&1 || true
curl -sk -X DELETE "$BASE_URL/api/v1/vaults/$OTHER_VAULT" -H "Authorization: Bearer $OWNER_JWT" >/dev/null 2>&1 || true

echo ""
echo "═══════════════════════════════════════════"
echo "  Results: $PASS passed, $FAIL failed"
if [ "$FAIL" -gt 0 ]; then
  echo "  Failures:"
  printf '    - %s\n' "${ERRORS[@]}"
  exit 1
fi
echo "All vault_write_policy e2e checks passed."
exit 0
