#!/bin/bash
#
# AKB s3_delete_outbox E2E
# Verifies that file deletion enqueues an s3_delete_outbox row in the
# same TX as the vault_files DELETE, and that the worker drains it.
#
# Reads the outbox via psql exec inside the postgres pod, so this test
# requires kubectl access — skipped otherwise.
#
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
NS="${AKB_NS:-akb}"
PG_POD="${AKB_PG_POD:-postgres-0}"
PG_USER="${AKB_PG_USER:-akbuser}"
PG_DB="${AKB_PG_DB:-akb}"

VAULT="s3out-$(date +%s)"
USER="s3out-$(date +%s)"
PASS=0
FAIL=0
ERRORS=()

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

if ! command -v kubectl >/dev/null 2>&1; then
  echo "kubectl not available — skipping outbox verification"
  exit 0
fi

run_psql() {
  kubectl exec -n "$NS" "$PG_POD" -- psql -U "$PG_USER" -d "$PG_DB" -tAc "$1" 2>/dev/null
}

echo "▸ Setup"

curl -sk -X POST "$BASE_URL/api/v1/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER\",\"email\":\"$USER@test.dev\",\"password\":\"test1234\"}" >/dev/null 2>&1

JWT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER\",\"password\":\"test1234\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

PAT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" \
  -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' \
  -d '{"name":"s3out"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

[ -n "$PAT" ] || { echo "FATAL: could not get PAT"; exit 1; }

curl -sk -X POST "$BASE_URL/api/v1/vaults" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"$VAULT\"}" >/dev/null

# Insert a synthetic vault_files row directly (skip S3 upload step —
# we're testing the DB-side outbox enqueue, not the S3 round-trip).
VAULT_ID=$(run_psql "SELECT id FROM vaults WHERE name = '$VAULT'")
FILE_ID=$(run_psql "INSERT INTO vault_files (vault_id, collection, name, s3_key, mime_type, size_bytes, description, created_by) VALUES ('$VAULT_ID', '', 'synthetic.txt', '$VAULT/$(date +%s)_synthetic.txt', 'text/plain', 12, '', '$USER') RETURNING id")
S3_KEY=$(run_psql "SELECT s3_key FROM vault_files WHERE id = '$FILE_ID'")

[ -n "$FILE_ID" ] && pass "synthetic file row inserted ($FILE_ID)" || { fail "setup" "no file row"; exit 1; }

echo ""
echo "▸ 1. DELETE → outbox row appears in same TX"

PRE_OUTBOX=$(run_psql "SELECT COUNT(*) FROM s3_delete_outbox WHERE s3_key = '$S3_KEY'")
[ "$PRE_OUTBOX" = "0" ] && pass "outbox empty before delete" || fail "pre-state" "found $PRE_OUTBOX rows"

curl -sk -X DELETE "$BASE_URL/api/v1/files/$VAULT/$FILE_ID" \
  -H "Authorization: Bearer $PAT" >/dev/null

# vault_files row gone
ROW_GONE=$(run_psql "SELECT COUNT(*) FROM vault_files WHERE id = '$FILE_ID'")
[ "$ROW_GONE" = "0" ] && pass "vault_files row removed" || fail "row" "still $ROW_GONE rows"

# outbox row enqueued
POST_OUTBOX=$(run_psql "SELECT COUNT(*) FROM s3_delete_outbox WHERE s3_key = '$S3_KEY'")
[ "$POST_OUTBOX" = "1" ] && pass "outbox enqueued (1 row)" || fail "outbox" "expected 1, got $POST_OUTBOX"

echo ""
echo "▸ 2. Worker drains the outbox (within ~30s)"

# Synthetic key won't actually exist in S3, so the worker hits NoSuchKey.
# That is treated as success by s3_delete_worker (idempotent), so the
# row should flip to processed.
DEADLINE=$((SECONDS + 30))
PROCESSED=""
while [ $SECONDS -lt $DEADLINE ]; do
  PROCESSED=$(run_psql "SELECT processed_at IS NOT NULL FROM s3_delete_outbox WHERE s3_key = '$S3_KEY'")
  if [ "$PROCESSED" = "t" ]; then break; fi
  sleep 2
done

[ "$PROCESSED" = "t" ] && pass "outbox row processed by worker" || fail "drain" "row not processed within 30s"

echo ""
echo "── Cleanup ──"
# Worker leaves the processed row alone until the 1-day grace sweep;
# leave it there. Tear down the vault.
curl -sk -X DELETE "$BASE_URL/api/v1/vaults/$VAULT" \
  -H "Authorization: Bearer $PAT" >/dev/null 2>&1 || true

echo ""
echo "═══════════════════════════════════════════"
echo "  Passed: $PASS   Failed: $FAIL"
if [ $FAIL -gt 0 ]; then
  echo "  Failures:"
  printf '    - %s\n' "${ERRORS[@]}"
  exit 1
fi
exit 0
