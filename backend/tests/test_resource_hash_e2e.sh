#!/usr/bin/env bash
#
# Focused resource-integrity E2E:
# - document put/get/update/browse expose body content_hash
# - expected_content_hash rejects stale body updates
# - file upload confirmation computes and returns byte content_hash

set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
STAMP="$(date +%s)"
VAULT="hash-e2e-$STAMP"
E2E_USER="hash-user-$STAMP"
PASS=0
FAIL=0
ERRORS=()

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

json_get() {
  local expr=$1
  python3 -c "import sys,json; d=json.load(sys.stdin); print($expr)" 2>/dev/null
}

sha256_text() {
  python3 -c "import sys,hashlib; print(hashlib.sha256(sys.stdin.read().encode('utf-8')).hexdigest())"
}

sha256_file() {
  python3 -c "import sys,hashlib; print(hashlib.sha256(open(sys.argv[1], 'rb').read()).hexdigest())" "$1"
}

echo "╔══════════════════════════════════════════╗"
echo "║   AKB Resource Hash E2E                  ║"
echo "║   Target: $BASE_URL"
echo "╚══════════════════════════════════════════╝"
echo ""

echo "▸ 0. Setup"
curl -sk -X POST "$BASE_URL/api/v1/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$E2E_USER\",\"email\":\"$E2E_USER@test.dev\",\"password\":\"test1234\"}" >/dev/null 2>&1

JWT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$E2E_USER\",\"password\":\"test1234\"}" | json_get "d['token']")

PAT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" \
  -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' \
  -d '{"name":"resource-hash-e2e"}' | json_get "d['token']")

[ -n "$PAT" ] && pass "PAT acquired" || { fail "PAT" "could not get PAT"; exit 1; }

INIT_RESP=$(curl -sk -i -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"hash-e2e","version":"1.0"}}}' 2>&1)

SID=$(echo "$INIT_RESP" | grep -i "mcp-session-id" | tr -d '\r' | awk '{print $2}')
[ -n "$SID" ] && pass "MCP session acquired" || { fail "MCP initialize" "missing session id"; exit 1; }

curl -sk -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null 2>&1

MCP_ID=10
mcp_call() {
  local tool=$1 args=$2
  MCP_ID=$((MCP_ID+1))
  curl -sk -X POST "$BASE_URL/mcp/" \
    -H "Authorization: Bearer $PAT" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -H "mcp-session-id: $SID" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":$MCP_ID,\"method\":\"tools/call\",\"params\":{\"name\":\"$tool\",\"arguments\":$args}}" 2>&1
}

mcp_result() {
  python3 -c "import sys,json; print(json.load(sys.stdin)['result']['content'][0]['text'])" 2>/dev/null
}

echo ""
echo "▸ 1. Document hash contract"
R=$(mcp_call akb_create_vault "{\"name\":\"$VAULT\",\"description\":\"hash e2e\"}" | mcp_result)
VAULT_ID=$(echo "$R" | json_get "d['vault_id']")
[ -n "$VAULT_ID" ] && pass "vault created" || fail "vault" "missing vault_id"

DOC_BODY=$'# Hash Contract\n\nStable body for hashing.'
EXPECTED_HASH=$(printf "%s" "$DOC_BODY" | sha256_text)

R=$(mcp_call akb_put "{\"vault\":\"$VAULT\",\"collection\":\"specs\",\"title\":\"Hash Contract\",\"content\":\"# Hash Contract\\n\\nStable body for hashing.\",\"type\":\"spec\"}" | mcp_result)
DOC_URI=$(echo "$R" | json_get "d['uri']")
PUT_HASH=$(echo "$R" | json_get "d['content_hash']")
PUT_COMMIT=$(echo "$R" | json_get "d['current_commit']")

[ "$PUT_HASH" = "$EXPECTED_HASH" ] && pass "akb_put returns body content_hash" || fail "akb_put hash" "got $PUT_HASH expected $EXPECTED_HASH"
[ -n "$PUT_COMMIT" ] && pass "akb_put returns current_commit" || fail "akb_put commit" "missing"

R=$(mcp_call akb_get "{\"uri\":\"$DOC_URI\"}" | mcp_result)
GET_HASH=$(echo "$R" | json_get "d['content_hash']")
GET_COMMIT=$(echo "$R" | json_get "d['current_commit']")
[ "$GET_HASH" = "$EXPECTED_HASH" ] && pass "akb_get returns matching content_hash" || fail "akb_get hash" "got $GET_HASH"
[ "$GET_COMMIT" = "$PUT_COMMIT" ] && pass "akb_get current_commit matches put" || fail "akb_get commit" "got $GET_COMMIT"

R=$(mcp_call akb_update "{\"uri\":\"$DOC_URI\",\"summary\":\"metadata only\",\"expected_content_hash\":\"$GET_HASH\",\"message\":\"metadata-only hash check\"}" | mcp_result)
UPDATE_HASH=$(echo "$R" | json_get "d['content_hash']")
UPDATE_COMMIT=$(echo "$R" | json_get "d['current_commit']")
[ "$UPDATE_HASH" = "$EXPECTED_HASH" ] && pass "metadata-only update keeps body hash" || fail "metadata update hash" "got $UPDATE_HASH"
[ "$UPDATE_COMMIT" != "$PUT_COMMIT" ] && pass "metadata-only update advances commit" || fail "metadata update commit" "commit did not change"

STALE=$(mcp_call akb_update "{\"uri\":\"$DOC_URI\",\"content\":\"changed\",\"expected_content_hash\":\"$(printf stale | sha256_text)\"}" | mcp_result)
STALE_ERR=$(echo "$STALE" | json_get "d.get('error','')")
case "$STALE_ERR" in
  *content_hash*) pass "stale expected_content_hash is rejected" ;;
  *) fail "expected_content_hash conflict" "unexpected response: $STALE" ;;
esac

R=$(mcp_call akb_browse "{\"vault\":\"$VAULT\",\"content_type\":\"documents\",\"include_hashes\":true}" | mcp_result)
BROWSE_HASH=$(echo "$R" | json_get "next(i['content_hash'] for i in d['items'] if i.get('uri') == '$DOC_URI')")
[ "$BROWSE_HASH" = "$EXPECTED_HASH" ] && pass "akb_browse include_hashes returns document hash" || fail "browse hash" "got $BROWSE_HASH"

echo ""
echo "▸ 2. File hash contract"
TMP_FILE="$(mktemp)"
printf "AKB file hash e2e\n" > "$TMP_FILE"
FILE_HASH=$(sha256_file "$TMP_FILE")

INIT=$(curl -sk -X POST "$BASE_URL/api/v1/files/$VAULT/upload?filename=hash.txt&collection=files&description=hash-e2e&mime_type=text/plain" \
  -H "Authorization: Bearer $PAT")
UPLOAD_URL=$(echo "$INIT" | json_get "d['upload_url']")
FILE_URI=$(echo "$INIT" | json_get "d['uri']")
FILE_ID=$(echo "$FILE_URI" | awk -F'/file/' '{print $2}')

curl -sk -X PUT "$UPLOAD_URL" -H "Content-Type: text/plain" --data-binary "@$TMP_FILE" >/dev/null

CONFIRM=$(curl -sk -X POST "$BASE_URL/api/v1/files/$VAULT/$FILE_ID/confirm?content_hash=$FILE_HASH&hash_algorithm=sha256" \
  -H "Authorization: Bearer $PAT")
CONFIRM_HASH=$(echo "$CONFIRM" | json_get "d['content_hash']")
[ "$CONFIRM_HASH" = "$FILE_HASH" ] && pass "file confirm returns verified byte hash" || fail "file confirm hash" "got $CONFIRM_HASH expected $FILE_HASH"

DL=$(curl -sk "$BASE_URL/api/v1/files/$VAULT/$FILE_ID/download" -H "Authorization: Bearer $PAT")
DL_HASH=$(echo "$DL" | json_get "d['content_hash']")
[ "$DL_HASH" = "$FILE_HASH" ] && pass "file download metadata returns byte hash" || fail "file download hash" "got $DL_HASH"

rm -f "$TMP_FILE"

echo ""
echo "Passed: $PASS  Failed: $FAIL"
if [ "$FAIL" -gt 0 ]; then
  printf ' - %s\n' "${ERRORS[@]}"
  exit 1
fi
