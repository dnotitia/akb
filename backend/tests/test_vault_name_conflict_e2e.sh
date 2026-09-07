#!/bin/bash
#
# Vault-name confidentiality and create-race contract.
# Runs in the repository-owned HTTP E2E runtime and uses only ephemeral data.

set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
SUFFIX=$(python3 -c 'import uuid; print(uuid.uuid4().hex[:12])')
PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')
ALICE="vault-name-a-$SUFFIX"
BOB="vault-name-b-$SUFFIX"
HIDDEN_VAULT="hidden-vault-$SUFFIX"
RACE_VAULT="race-vault-$SUFFIX"
PASS=0
FAIL=0
ERRORS=()
TMP=$(mktemp -d)

cleanup() {
  rm -r "$TMP"
}
trap cleanup EXIT

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

register_and_pat() {
  local username=$1
  local jwt
  curl -sk -X POST "$BASE_URL/api/v1/auth/register" \
    -H 'Content-Type: application/json' \
    -d "{\"username\":\"$username\",\"email\":\"$username@test.invalid\",\"password\":\"$PASSWORD\"}" \
    >/dev/null
  jwt=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" \
    -H 'Content-Type: application/json' \
    -d "{\"username\":\"$username\",\"password\":\"$PASSWORD\"}" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("token", ""))' 2>/dev/null)
  curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" \
    -H "Authorization: Bearer $jwt" \
    -H 'Content-Type: application/json' \
    -d '{"name":"vault-name-contract"}' \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("token", ""))' 2>/dev/null
}

rest_conflict_is_safe() {
  local body_file=$1
  local attempted_name=$2
  python3 -c '
import json, sys
body = json.load(open(sys.argv[1]))
attempted = sys.argv[2]
fixed = "Vault name is unavailable. Choose a different name."
expected = {
    "message": fixed,
    "error": fixed,
    "code": "vault_name_unavailable",
    "detail": {
        "message": fixed,
        "code": "vault_name_unavailable",
    },
}
ok = body == expected and attempted not in json.dumps(body)
raise SystemExit(0 if ok else 1)
' "$body_file" "$attempted_name"
}

mcp_call() {
  local pat=$1 sid=$2 id=$3 tool=$4 args=$5
  curl -sk -X POST "$BASE_URL/mcp/" \
    -H "Authorization: Bearer $pat" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -H "mcp-session-id: $sid" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":$id,\"method\":\"tools/call\",\"params\":{\"name\":\"$tool\",\"arguments\":$args}}"
}

echo "▸ Setup isolated users"
ALICE_PAT=$(register_and_pat "$ALICE")
BOB_PAT=$(register_and_pat "$BOB")
if [ -n "$ALICE_PAT" ] && [ -n "$BOB_PAT" ]; then
  pass "two users received independent PATs"
else
  fail "setup" "PAT creation failed"
  exit 1
fi

ALICE_AUTH="Authorization: Bearer $ALICE_PAT"
BOB_AUTH="Authorization: Bearer $BOB_PAT"

echo ""
echo "▸ Hidden Vault collision"
CREATE_STATUS=$(curl -sk -o "$TMP/hidden-create" -w '%{http_code}' -X POST \
  "$BASE_URL/api/v1/vaults?name=$HIDDEN_VAULT" -H "$ALICE_AUTH")
[ "$CREATE_STATUS" = "200" ] && pass "Alice created a private Vault" \
  || fail "private Vault create" "expected 200, got $CREATE_STATUS"

BOB_SEES_HIDDEN=$(curl -sk -H "$BOB_AUTH" "$BASE_URL/api/v1/my/vaults" \
  | python3 -c "import json,sys; print(any(v['name']=='$HIDDEN_VAULT' for v in json.load(sys.stdin)['vaults']))" \
  2>/dev/null)
[ "$BOB_SEES_HIDDEN" = "False" ] && pass "private Vault is absent from Bob's list" \
  || fail "private Vault listing" "hidden Vault appeared in Bob's list"

REST_STATUS=$(curl -sk -o "$TMP/rest-hidden-conflict" -w '%{http_code}' -X POST \
  "$BASE_URL/api/v1/vaults?name=$HIDDEN_VAULT" -H "$BOB_AUTH")
if [ "$REST_STATUS" = "409" ] && rest_conflict_is_safe "$TMP/rest-hidden-conflict" "$HIDDEN_VAULT"; then
  pass "REST returns the fixed non-disclosing conflict"
else
  fail "REST hidden conflict" "status=$REST_STATUS body=$(head -c 180 "$TMP/rest-hidden-conflict")"
fi

MCP_INIT=$(curl -sk -i -X POST "$BASE_URL/mcp/" \
  -H "$BOB_AUTH" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"vault-name-contract","version":"1"}}}')
MCP_SID=$(echo "$MCP_INIT" | grep -i 'mcp-session-id' | tr -d '\r' | awk '{print $2}')
curl -sk -X POST "$BASE_URL/mcp/" \
  -H "$BOB_AUTH" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H "mcp-session-id: $MCP_SID" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null
MCP_RESPONSE=$(mcp_call "$BOB_PAT" "$MCP_SID" 2 akb_create_vault "{\"name\":\"$HIDDEN_VAULT\"}")
MCP_SAFE=$(echo "$MCP_RESPONSE" | python3 -c '
import json, sys
outer = json.load(sys.stdin)
expected = {
    "error": "Vault name is unavailable. Choose a different name.",
    "code": "vault_name_unavailable",
}
result = outer.get("result", {})
content = result.get("content", [])
safe = (
    set(outer) == {"jsonrpc", "id", "result"}
    and outer.get("jsonrpc") == "2.0"
    and outer.get("id") == 2
    and set(result) <= {"content", "isError"}
    and len(content) == 1
    and set(content[0]) == {"type", "text"}
    and content[0].get("type") == "text"
    and json.loads(content[0].get("text", "")) == expected
)
print(safe)
' 2>/dev/null)
[ "$MCP_SAFE" = "True" ] && pass "MCP returns exactly error + stable code" \
  || fail "MCP hidden conflict" "unexpected envelope"

echo ""
echo "▸ Concurrent create and storage rollback"
for i in 1 2 3 4 5 6 7 8 9 10; do
  (
    curl -sk -o "$TMP/race-body-$i" -w '%{http_code}' -X POST \
      "$BASE_URL/api/v1/vaults?name=$RACE_VAULT" -H "$BOB_AUTH" \
      > "$TMP/race-status-$i"
  ) &
done
wait

WINNERS=0
CONFLICTS=0
BAD=0
for i in 1 2 3 4 5 6 7 8 9 10; do
  STATUS=$(cat "$TMP/race-status-$i")
  if [ "$STATUS" = "200" ]; then
    WINNERS=$((WINNERS+1))
  elif [ "$STATUS" = "409" ] \
    && rest_conflict_is_safe "$TMP/race-body-$i" "$RACE_VAULT"; then
    CONFLICTS=$((CONFLICTS+1))
  else
    BAD=$((BAD+1))
  fi
done
if [ "$WINNERS" = "1" ] && [ "$CONFLICTS" = "9" ] && [ "$BAD" = "0" ]; then
  pass "ten concurrent creates produce one winner and nine stable 409s"
else
  fail "concurrent create" "winner=$WINNERS conflicts=$CONFLICTS bad=$BAD"
fi

DOC_STATUS=$(curl -sk -o "$TMP/race-doc" -w '%{http_code}' -X POST \
  "$BASE_URL/api/v1/documents" \
  -H "$BOB_AUTH" -H 'Content-Type: application/json' \
  -d "{\"vault\":\"$RACE_VAULT\",\"collection\":\"checks\",\"title\":\"Winner remains usable\",\"content\":\"The winning repository survived every losing rollback.\",\"type\":\"note\"}")
if [ "$DOC_STATUS" = "200" ] && grep -q '"commit_hash"' "$TMP/race-doc"; then
  pass "the winning Vault remains writable after losing rollbacks"
else
  fail "winner usability" "status=$DOC_STATUS body=$(head -c 180 "$TMP/race-doc")"
fi

DELETE_STATUS=$(curl -sk -o "$TMP/race-delete" -w '%{http_code}' -X DELETE \
  "$BASE_URL/api/v1/vaults/$RACE_VAULT" -H "$BOB_AUTH")
RECREATE_STATUS=$(curl -sk -o "$TMP/race-recreate" -w '%{http_code}' -X POST \
  "$BASE_URL/api/v1/vaults?name=$RACE_VAULT" -H "$BOB_AUTH")
if [ "$DELETE_STATUS" = "200" ] && [ "$RECREATE_STATUS" = "200" ]; then
  pass "delete then recreate succeeds without a stale Git orphan"
else
  fail "orphan cleanup" "delete=$DELETE_STATUS recreate=$RECREATE_STATUS"
fi

curl -sk -X DELETE "$BASE_URL/mcp/" -H "$BOB_AUTH" -H "mcp-session-id: $MCP_SID" >/dev/null
curl -sk -X DELETE "$BASE_URL/api/v1/vaults/$HIDDEN_VAULT" -H "$ALICE_AUTH" >/dev/null
curl -sk -X DELETE "$BASE_URL/api/v1/vaults/$RACE_VAULT" -H "$BOB_AUTH" >/dev/null

echo ""
echo "Results: $PASS passed, $FAIL failed"
if [ "$FAIL" -gt 0 ]; then
  for error in "${ERRORS[@]}"; do echo "  ✗ $error"; done
  exit 1
fi
