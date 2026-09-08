#!/bin/bash
#
# AKB OKF REST Export/Import E2E Test Suite
# Detailed MCP export/import behavior runs through the official Python SDK.
# This shell lane retains the independent REST JSON/zip and multipart
# boundaries. No S3 / no search deps, so it runs under the CI live-stack
# (embed-stubbed, MinIO-less) job.
#
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
SRC_VAULT="okf-src-$(date +%s)"
REST_VAULT="okf-rest-$(date +%s)"
E2E_USER="okf-user-$(date +%s)"
PASS=0
FAIL=0
ERRORS=()

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }
note() { echo "  • $1"; }

echo "╔══════════════════════════════════════════╗"
echo "║   AKB OKF Export/Import E2E Test Suite   ║"
echo "║   Target: $BASE_URL"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── 0. Setup ──────────────────────────────────────────────────
echo "▸ 0. Setup"

curl -sk -X POST "$BASE_URL/api/v1/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$E2E_USER\",\"email\":\"$E2E_USER@test.dev\",\"password\":\"test1234\"}" >/dev/null 2>&1

JWT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$E2E_USER\",\"password\":\"test1234\"}" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)
[ -n "$JWT" ] && pass "JWT acquired" || { fail "JWT" "could not get JWT"; exit 1; }

acurl() { curl -sk -H "Authorization: Bearer $JWT" "$@"; }

# ── 1. REST source content ────────────────────────────────────
echo ""
echo "▸ 1. Source vault content"
R=$(acurl -X POST "$BASE_URL/api/v1/vaults?name=$SRC_VAULT&description=OKF%20export%20source")
echo "$R" | python3 -c 'import sys,json; assert json.load(sys.stdin).get("name")' >/dev/null 2>&1 \
  && pass "Source vault created" || { fail "Vault" "no source vault"; exit 1; }

D1=$(acurl -X POST "$BASE_URL/api/v1/documents" \
  -H 'Content-Type: application/json' \
  -d "{\"vault\":\"$SRC_VAULT\",\"collection\":\"specs\",\"slug\":\"api-v2\",\"title\":\"API v2\",\"content\":\"# API v2\\n\\nSpec body.\",\"type\":\"spec\",\"status\":\"active\",\"tags\":[\"api\"]}")
echo "$D1" | grep -q '"uri"' && pass "Doc 1 created (specs/)" || fail "Doc1" "$D1"

D2=$(acurl -X POST "$BASE_URL/api/v1/documents" \
  -H 'Content-Type: application/json' \
  -d "{\"vault\":\"$SRC_VAULT\",\"slug\":\"readme\",\"title\":\"Readme\",\"content\":\"# Readme\\n\\nRoot doc.\",\"type\":\"note\"}")
echo "$D2" | grep -q '"uri"' && pass "Doc 2 created (root)" || fail "Doc2" "$D2"

# Table column types are AKB's set: text | number | boolean | date | json.
TBL=$(acurl -X POST "$BASE_URL/api/v1/tables/$SRC_VAULT" \
  -H 'Content-Type: application/json' \
  -d '{"name":"metrics","columns":[{"name":"region","type":"text"},{"name":"hits","type":"number"}]}')
echo "$TBL" | grep -q '"uri' \
  && pass "Table created (metrics)" || fail "Table" "$TBL"

# ── 2. REST export (json + zip) ───────────────────────────────
echo ""
echo "▸ 2. Export via REST"
REST_JSON=$(acurl "$BASE_URL/api/v1/vaults/$SRC_VAULT/export?format=okf&as=json")
echo "$REST_JSON" | python3 -c 'import sys,json; assert json.load(sys.stdin)["file_count"]>0' 2>/dev/null \
  && pass "REST export (as=json) ok" || fail "RESTjson" "$REST_JSON"

acurl "$BASE_URL/api/v1/vaults/$SRC_VAULT/export?format=okf" -o /tmp/okf_bundle.zip
[ "$(head -c 2 /tmp/okf_bundle.zip)" = "PK" ] \
  && pass "REST export (zip) returns PK archive" || fail "RESTzip" "not a zip"

GUARD=$(acurl "$BASE_URL/api/v1/vaults/$SRC_VAULT/export?format=rdf")
echo "$GUARD" | grep -qi "unsupported format" \
  && pass "Unsupported format rejected (400)" || fail "Guard" "$GUARD"

# ── 3. REST import (zip multipart) ────────────────────────────
echo ""
echo "▸ 3. Import via REST (zip upload)"
R=$(acurl -X POST "$BASE_URL/api/v1/vaults?name=$REST_VAULT&description=OKF%20REST%20import%20target")
echo "$R" | python3 -c 'import sys,json; assert json.load(sys.stdin).get("name")' >/dev/null 2>&1 \
  && pass "REST import target created" || fail "REST target" "$R"

REST_IMPORT=$(acurl -X POST "$BASE_URL/api/v1/vaults/$REST_VAULT/import?format=okf&status=active" \
  -F "file=@/tmp/okf_bundle.zip")
RC_CREATED=$(echo "$REST_IMPORT" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("created",0))' 2>/dev/null)
[ "${RC_CREATED:-0}" -ge 2 ] 2>/dev/null \
  && pass "REST import created $RC_CREATED docs" || fail "RESTimport" "$REST_IMPORT"

# ── Summary ───────────────────────────────────────────────────
echo ""
echo "═════════════════════════════════════════"
echo "Results: $PASS passed, $FAIL failed"
echo "═════════════════════════════════════════"
if [ "$FAIL" -gt 0 ]; then
  for e in "${ERRORS[@]}"; do echo "  - $e"; done
  exit 1
fi
exit 0
