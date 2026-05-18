#!/bin/bash
set -uo pipefail
BASE_URL="${AKB_URL:-http://localhost:8000}"
PASS=0
FAIL=0

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); echo "  ✗ $1 — $2"; }

echo "▸ GET /api/v1/help/skill-template"
RESP=$(curl -sk "$BASE_URL/api/v1/help/skill-template")
echo "$RESP" | grep -q "Vault Skill" && pass "Title present" || fail "T1" "missing title"
echo "$RESP" | grep -q "## Document Template" && pass "Document Template section present" || fail "T2" "missing section"
echo "$RESP" | grep -q "## Purpose" && pass "Skeleton Purpose" || fail "T3" "missing Purpose"
echo "$RESP" | grep -q "{vault}" && pass "{vault} placeholder kept intact" || fail "T4" "placeholder substituted"
echo "$RESP" | grep -q '\${{secrets.X}}' && pass "Secrets placeholder literal" || fail "T5" "secrets placeholder collapsed"

echo "▸ Content-Type is text/markdown or text/plain"
CT=$(curl -sk -I "$BASE_URL/api/v1/help/skill-template" | grep -i "^content-type:" | tr -d '\r')
echo "$CT" | grep -qiE "text/(markdown|plain)" && pass "Content-Type ok ($CT)" || fail "T6" "wrong CT: $CT"

echo ""
echo "  Passed: $PASS  Failed: $FAIL"
[ $FAIL -gt 0 ] && exit 1 || exit 0
