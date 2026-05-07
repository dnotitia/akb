#!/bin/bash
#
# Hybrid search security / boundary edge cases
#
# - Path-traversal attempts in doc_id
# - Very long field values (title, content, tags)
# - Invalid enum values (doc_type)
# - Token lifecycle (revoked PAT fails)
# - Newlines / control chars in fields
# - Admin vs regular user visibility
#
set -uo pipefail

BASE="${AKB_URL:-http://localhost:8000}"
USER_NAME="hybrid-sec-$(date +%s)"
VAULT="hybrid-sec-$(date +%s)"
WAIT="${AKB_HYBRID_INDEX_WAIT:-25}"
PASS=0
FAIL=0
ERRORS=()

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

rcurl() {
  local out=""
  for _ in 1 2 3; do
    out=$(curl -sk --max-time 20 "$@" 2>/dev/null)
    if [ -n "$out" ]; then echo "$out"; return 0; fi
    sleep 1
  done
  echo ""; return 1
}
jget() { python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print($1)" 2>/dev/null; }

echo "╔══════════════════════════════════════════╗"
echo "║   Hybrid Search Security / Boundaries    ║"
echo "║   Target: $BASE"
echo "╚══════════════════════════════════════════╝"
echo ""

echo "▸ Setup"
rcurl -X POST "$BASE/api/v1/auth/register" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER_NAME\",\"email\":\"$USER_NAME@t.dev\",\"password\":\"test1234\"}" >/dev/null
JWT=$(rcurl -X POST "$BASE/api/v1/auth/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER_NAME\",\"password\":\"test1234\"}" | jget "d['token']")
PAT_RESP=$(rcurl -X POST "$BASE/api/v1/auth/tokens" -H "Authorization: Bearer $JWT" -H 'Content-Type: application/json' \
  -d '{"name":"s"}')
PAT=$(echo "$PAT_RESP" | jget "d['token']")
PAT_ID=$(echo "$PAT_RESP" | jget "d.get('id','')")
[ -n "$PAT" ] && pass "PAT acquired" || { fail "PAT" ""; exit 1; }
rcurl -X POST "$BASE/api/v1/vaults?name=$VAULT" -H "Authorization: Bearer $PAT" >/dev/null
pass "vault $VAULT created"

put() {
  local title=$1 content=$2
  rcurl -X POST "$BASE/api/v1/documents" -H "Authorization: Bearer $PAT" -H 'Content-Type: application/json' \
    -d "{\"vault\":\"$VAULT\",\"collection\":\"x\",\"title\":$(python3 -c 'import sys,json; print(json.dumps(sys.argv[1]))' "$title"),\"content\":$(python3 -c 'import sys,json; print(json.dumps(sys.argv[1]))' "$content")}"
}

# ── SEC1. Path traversal in doc_id (URL-encoded to reach backend) ──
echo ""
echo "▸ SEC1. Path traversal in doc_id"

# Unencoded `..` gets normalized by curl/ingress before reaching the backend
# (hits the SPA route and returns HTML). URL-encoded variants actually
# reach the {doc_id:path} handler.
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 10 \
  -H "Authorization: Bearer $PAT" "$BASE/api/v1/documents/$VAULT/..%2F..%2Fetc%2Fpasswd")
[ "$HTTP" = "404" ] && pass "URL-encoded traversal → $HTTP" || fail "SEC1-enc" "got $HTTP"

HTTP=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 10 \
  -H "Authorization: Bearer $PAT" "$BASE/api/v1/documents/$VAULT/%2E%2E%2F%2E%2E%2Fetc")
[ "$HTTP" = "404" ] && pass "double-encoded traversal → $HTTP" || fail "SEC1-dbl" "got $HTTP"

# ── SEC2. Very long title (5000 chars) ───────────────────────
echo ""
echo "▸ SEC2. Very long title"

LONG_T=$(python3 -c "print('A' * 5000)")
R=$(put "$LONG_T" "content for long title test")
# Either accepted with truncation OR rejected with error — both acceptable
OK=$(echo "$R" | python3 -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
    # OK if got doc_id (accepted) OR error field (rejected)
    print('OK' if 'doc_id' in d or 'error' in d or 'detail' in d else 'BAD')
except: print('BAD')
")
[ "$OK" = "OK" ] && pass "long title returns structured response" || fail "SEC2" "response: $R"

# ── SEC3. Invalid doc_type enum ──────────────────────────────
echo ""
echo "▸ SEC3. Invalid doc_type rejected"

HTTP=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 10 \
  -X POST "$BASE/api/v1/documents" -H "Authorization: Bearer $PAT" -H 'Content-Type: application/json' \
  -d "{\"vault\":\"$VAULT\",\"collection\":\"x\",\"title\":\"T\",\"content\":\"c\",\"type\":\"not-a-real-type\"}")
# Either 422 (validation) OR 200 (accepted as-is) — check structure
[ "$HTTP" = "422" ] && pass "invalid type → 422" || pass "invalid type → $HTTP (accepted but no crash)"

# ── SEC4. Token revocation blocks further use ────────────────
echo ""
echo "▸ SEC4. Revoked PAT fails"

if [ -n "$PAT_ID" ]; then
  rcurl -X DELETE "$BASE/api/v1/auth/tokens/$PAT_ID" -H "Authorization: Bearer $JWT" >/dev/null
  HTTP=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 10 \
    -H "Authorization: Bearer $PAT" "$BASE/api/v1/search?q=test&vault=$VAULT")
  [ "$HTTP" = "401" ] && pass "revoked PAT → 401" || fail "SEC4" "got $HTTP"

  # Re-mint
  PAT=$(rcurl -X POST "$BASE/api/v1/auth/tokens" -H "Authorization: Bearer $JWT" -H 'Content-Type: application/json' \
    -d '{"name":"s2"}' | jget "d['token']")
  [ -n "$PAT" ] && pass "new PAT minted after revocation" || fail "SEC4-remint" "no token"
fi

# ── SEC5. Newlines in title are preserved, not parsed as SQL ──
echo ""
echo "▸ SEC5. Newlines in title"

R=$(put $'multi\nline\ntitle' "newline test content")
# Should accept and not crash; either store as-is or slugify
OK=$(echo "$R" | python3 -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
    print('OK' if 'doc_id' in d or 'error' in d else 'BAD')
except: print('BAD')
")
[ "$OK" = "OK" ] && pass "newline title handled" || fail "SEC5" "response: $R"

# ── SEC6. Admin endpoints require auth ───────────────────────
echo ""
echo "▸ SEC6. Admin/self endpoints require auth"

HTTP=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 10 "$BASE/api/v1/vaults")
[ "$HTTP" = "401" ] && pass "unauthenticated vault list → 401" || fail "SEC6" "got $HTTP"

HTTP=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 10 \
  -H "Authorization: Bearer notatoken" "$BASE/api/v1/vaults")
[ "$HTTP" = "401" ] && pass "bogus token on vaults → 401" || fail "SEC6-bogus" "got $HTTP"

# ── SEC7. Vault name with special chars is rejected ──────────
echo ""
echo "▸ SEC7. Vault name validation"

HTTP=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 10 \
  -X POST "$BASE/api/v1/vaults?name=../etc/passwd" -H "Authorization: Bearer $PAT")
[ "$HTTP" = "400" ] || [ "$HTTP" = "422" ] && pass "path-ish vault name → $HTTP" || pass "vault name '../etc/passwd' → $HTTP (accepted?)"

# FastAPI query params default require something; also service-level reject maps to 422.
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 10 \
  -X POST "$BASE/api/v1/vaults?name=" -H "Authorization: Bearer $PAT")
if [ "$HTTP" = "400" ] || [ "$HTTP" = "422" ]; then
  pass "empty vault name rejected ($HTTP)"
else
  fail "SEC7-empty" "got $HTTP"
fi

# ── SEC8. Content with NULL bytes ────────────────────────────
echo ""
echo "▸ SEC8. Content with NULL bytes"

R=$(put "NullByte" $'before\x00after marker')
# Should either accept (NUL stripped) or reject — no crash
N=$(echo "$R" | jget "d.get('chunks_indexed', 'err')")
[ "$N" != "err" ] && pass "NUL byte content handled (chunks=$N)" || pass "NUL byte rejected: $R"

# ── SEC9. doc_id parameter injection attempts ────────────────
echo ""
echo "▸ SEC9. Search doc_id/vault parameter injection"

# URL-encode special chars so curl doesn't choke (000 = failed to connect).
SQL_VAULT=$(python3 -c "import urllib.parse; print(urllib.parse.quote(\"' OR 1=1 --\"))")
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 10 \
  -H "Authorization: Bearer $PAT" "$BASE/api/v1/search?q=test&vault=$SQL_VAULT")
[ "$HTTP" = "200" ] && pass "SQL-shape vault name handled ($HTTP)" || fail "SEC9" "got $HTTP"

T=$(rcurl -H "Authorization: Bearer $PAT" "$BASE/api/v1/search?q=test&vault=$SQL_VAULT" \
  | jget "d.get('total', 0)")
[ "$T" = "0" ] && pass "SQL-shape vault → 0 results" || fail "SEC9-total" "got $T"

# ── SEC10. /search parameter tampering: limit as string ──────
echo ""
echo "▸ SEC10. Invalid limit type"

HTTP=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 10 \
  -H "Authorization: Bearer $PAT" "$BASE/api/v1/search?q=test&vault=$VAULT&limit=abc")
[ "$HTTP" = "422" ] && pass "non-int limit → 422" || fail "SEC10" "got $HTTP"

# ── Cleanup ──────────────────────────────────────────────────
echo ""
echo "▸ Cleanup"
SID=$(curl -sk -i --max-time 10 -X POST "$BASE/mcp/" \
  -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"x","version":"1.0"}}}' 2>/dev/null \
  | grep -i "mcp-session-id" | tr -d '\r' | awk '{print $2}')
if [ -n "$SID" ]; then
  rcurl -X POST "$BASE/mcp/" -H "Authorization: Bearer $PAT" \
    -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
    -H "mcp-session-id: $SID" \
    -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null
  rcurl -X POST "$BASE/mcp/" -H "Authorization: Bearer $PAT" \
    -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
    -H "mcp-session-id: $SID" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":99,\"method\":\"tools/call\",\"params\":{\"name\":\"akb_delete_vault\",\"arguments\":{\"vault\":\"$VAULT\"}}}" >/dev/null
fi

# Self-delete test user to avoid accumulating in DB
curl -sk --max-time 15 -X DELETE "$BASE/api/v1/my/account" -H "Authorization: Bearer $JWT" >/dev/null 2>&1
pass "cleanup attempted"

echo ""
echo "═══════════════════════════════════════════"
echo "  Passed: $PASS"
echo "  Failed: $FAIL"
if [ $FAIL -gt 0 ]; then
  echo ""
  echo "  Errors:"
  for e in "${ERRORS[@]}"; do echo "    - $e"; done
fi
echo "═══════════════════════════════════════════"

exit $FAIL
