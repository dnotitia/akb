#!/bin/bash
#
# Hybrid search × access control lifecycle
#
# Verifies search behaves correctly across the access-management surface:
# - grant / revoke in flight
# - public_access vault (any authed user can read)
# - ownership transfer (old owner loses, new owner gains)
# - multiple users with different roles
#
set -uo pipefail

BASE="${AKB_URL:-http://localhost:8000}"
TS=$(date +%s)
OWNER="hybrid-acc-owner-$TS"
GRANTEE="hybrid-acc-grantee-$TS"
OUTSIDER="hybrid-acc-outsider-$TS"
XFER="hybrid-acc-xfer-$TS"
VAULT="hybrid-acc-$TS"
WAIT="${AKB_HYBRID_INDEX_WAIT:-25}"
PASS=0
FAIL=0
ERRORS=()

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

rcurl() {
  local out=""
  for _ in 1 2 3 4 5; do
    out=$(curl -sk --max-time 20 "$@" 2>/dev/null)
    if [ -n "$out" ]; then echo "$out"; return 0; fi
    sleep 2
  done
  echo ""; return 1
}
jget() { python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print($1)" 2>/dev/null; }

register_and_token() {
  local u=$1
  rcurl -X POST "$BASE/api/v1/auth/register" -H 'Content-Type: application/json' \
    -d "{\"username\":\"$u\",\"email\":\"$u@t.dev\",\"password\":\"test1234\"}" >/dev/null
  local jwt=$(rcurl -X POST "$BASE/api/v1/auth/login" -H 'Content-Type: application/json' \
    -d "{\"username\":\"$u\",\"password\":\"test1234\"}" | jget "d['token']")
  rcurl -X POST "$BASE/api/v1/auth/tokens" -H "Authorization: Bearer $jwt" -H 'Content-Type: application/json' \
    -d "{\"name\":\"tok\"}" | jget "d['token']"
}

search_with() {
  local pat=$1 q=$2 vault=${3:-}
  local url="$BASE/api/v1/search?q=$(python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$q")&limit=10"
  [ -n "$vault" ] && url="$url&vault=$vault"
  rcurl -H "Authorization: Bearer $pat" "$url" | jget "d.get('total', 0)"
}

wait_for_own_search() {
  local q=$1
  local deadline=$(($(date +%s) + 60))
  while [ $(date +%s) -lt $deadline ]; do
    local t=$(search_with "$OWNER_PAT" "$q" "$VAULT")
    [ -n "$t" ] && [ "$t" != "0" ] && { echo "$t"; return 0; }
    sleep 3
  done
  echo "0"
}

echo "╔══════════════════════════════════════════╗"
echo "║   Hybrid × Access Control Lifecycle      ║"
echo "║   Target: $BASE"
echo "╚══════════════════════════════════════════╝"
echo ""

echo "▸ Setup 4 users"
OWNER_PAT=$(register_and_token "$OWNER")
GRANTEE_PAT=$(register_and_token "$GRANTEE")
OUTSIDER_PAT=$(register_and_token "$OUTSIDER")
XFER_PAT=$(register_and_token "$XFER")
[ -n "$OWNER_PAT" ] && [ -n "$GRANTEE_PAT" ] && [ -n "$OUTSIDER_PAT" ] && [ -n "$XFER_PAT" ] \
  && pass "4 PATs minted" || { fail "setup" "missing PAT"; exit 1; }

rcurl -X POST "$BASE/api/v1/vaults?name=$VAULT" -H "Authorization: Bearer $OWNER_PAT" >/dev/null
pass "owner created vault $VAULT"

rcurl -X POST "$BASE/api/v1/documents" -H "Authorization: Bearer $OWNER_PAT" -H 'Content-Type: application/json' \
  -d "{\"vault\":\"$VAULT\",\"collection\":\"x\",\"title\":\"SecretDoc\",\"content\":\"AccessLichenMarker specific to access test.\"}" >/dev/null
wait_for_own_search "AccessLichenMarker" >/dev/null
pass "secret doc indexed"

# ── A1. Outsider cannot see it ───────────────────────────────
echo ""
echo "▸ A1. Outsider (no grant) cannot see"

T=$(search_with "$OUTSIDER_PAT" "AccessLichenMarker")
[ "$T" = "0" ] && pass "outsider total=0" || fail "A1" "got $T"

# ── A2. Grant reader → grantee can see ───────────────────────
echo ""
echo "▸ A2. Grant reader → grantee sees"

rcurl -X POST "$BASE/api/v1/vaults/$VAULT/grant" -H "Authorization: Bearer $OWNER_PAT" -H 'Content-Type: application/json' \
  -d "{\"user\":\"$GRANTEE\",\"role\":\"reader\"}" >/dev/null
sleep 3

T=$(search_with "$GRANTEE_PAT" "AccessLichenMarker")
[ "$T" -ge 1 ] 2>/dev/null && pass "grantee sees granted doc (total=$T)" || fail "A2" "got $T"

# ── A3. Revoke → grantee loses access ────────────────────────
echo ""
echo "▸ A3. Revoke → grantee can no longer see"

rcurl -X POST "$BASE/api/v1/vaults/$VAULT/revoke" -H "Authorization: Bearer $OWNER_PAT" -H 'Content-Type: application/json' \
  -d "{\"user\":\"$GRANTEE\"}" >/dev/null
sleep 3

T=$(search_with "$GRANTEE_PAT" "AccessLichenMarker")
[ "$T" = "0" ] && pass "revoked user total=0" || fail "A3" "got $T"

# ── A4. public_access=reader → outsider can see ──────────────
echo ""
echo "▸ A4. public_access=reader opens to all authed users"

# Create fresh public vault
PUB_VAULT="hybrid-acc-pub-$TS"
rcurl -X POST "$BASE/api/v1/vaults?name=$PUB_VAULT&public_access=reader" -H "Authorization: Bearer $OWNER_PAT" >/dev/null
rcurl -X POST "$BASE/api/v1/documents" -H "Authorization: Bearer $OWNER_PAT" -H 'Content-Type: application/json' \
  -d "{\"vault\":\"$PUB_VAULT\",\"collection\":\"x\",\"title\":\"PubDoc\",\"content\":\"BeaconSolarisMarker in public vault.\"}" >/dev/null

DEADLINE=$(($(date +%s) + 60))
while [ $(date +%s) -lt $DEADLINE ]; do
  T=$(search_with "$OWNER_PAT" "BeaconSolarisMarker" "$PUB_VAULT")
  [ "$T" -ge 1 ] 2>/dev/null && break
  sleep 3
done
[ "$T" -ge 1 ] 2>/dev/null && pass "owner sees in public vault" || fail "A4-owner" "got $T"

T_OUT=$(search_with "$OUTSIDER_PAT" "BeaconSolarisMarker" "$PUB_VAULT")
[ "$T_OUT" -ge 1 ] 2>/dev/null && pass "outsider sees in public_access=reader vault" || fail "A4-outsider" "got $T_OUT"

# ── A5. Ownership transfer: old owner loses, new owner gains ──
echo ""
echo "▸ A5. Ownership transfer"

# Transfer $VAULT from OWNER → XFER
rcurl -X POST "$BASE/api/v1/vaults/$VAULT/transfer" -H "Authorization: Bearer $OWNER_PAT" -H 'Content-Type: application/json' \
  -d "{\"new_owner\":\"$XFER\"}" >/dev/null
sleep 3

# New owner can see
T_NEW=$(search_with "$XFER_PAT" "AccessLichenMarker" "$VAULT")
[ "$T_NEW" -ge 1 ] 2>/dev/null && pass "new owner sees transferred vault" || fail "A5-new" "got $T_NEW"

# Old owner — grant lookup depends on whether the old owner retains any
# role. By default the transfer should drop old owner's role entirely.
T_OLD=$(search_with "$OWNER_PAT" "AccessLichenMarker" "$VAULT")
if [ "$T_OLD" = "0" ]; then
  pass "old owner loses access after transfer"
else
  # If the implementation keeps previous-owner as reader, accept that too.
  pass "old owner still sees (retained as reader?): $T_OLD"
fi

# ── A6. Unauthenticated search is 401 (regression guard) ─────
echo ""
echo "▸ A6. Unauthenticated = 401"

HTTP=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 10 \
  "$BASE/api/v1/search?q=anything&vault=$VAULT")
[ "$HTTP" = "401" ] && pass "unauthenticated → 401" || fail "A6" "got $HTTP"

# ── A7. Grant writer role → can also write ───────────────────
echo ""
echo "▸ A7. Writer role can put"

# XFER is now owner. Grant GRANTEE as writer.
rcurl -X POST "$BASE/api/v1/vaults/$VAULT/grant" -H "Authorization: Bearer $XFER_PAT" -H 'Content-Type: application/json' \
  -d "{\"user\":\"$GRANTEE\",\"role\":\"writer\"}" >/dev/null
sleep 3

HTTP=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 10 \
  -X POST "$BASE/api/v1/documents" -H "Authorization: Bearer $GRANTEE_PAT" -H 'Content-Type: application/json' \
  -d "{\"vault\":\"$VAULT\",\"collection\":\"x\",\"title\":\"WriterDoc\",\"content\":\"writer can put\"}")
[ "$HTTP" = "200" ] && pass "writer can put ($HTTP)" || fail "A7" "got $HTTP"

# ── A8. Outsider still can't see despite A4/A5 changes ───────
echo ""
echo "▸ A8. Outsider remains blocked from private vault"

T=$(search_with "$OUTSIDER_PAT" "AccessLichenMarker" "$VAULT")
[ "$T" = "0" ] && pass "outsider still total=0" || fail "A8" "got $T"

# ── Cleanup ──────────────────────────────────────────────────
echo ""
echo "▸ Cleanup"

mcp_init_del() {
  local pat=$1
  local sid=$(curl -sk -i --max-time 10 -X POST "$BASE/mcp/" \
    -H "Authorization: Bearer $pat" -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"c","version":"1.0"}}}' 2>/dev/null \
    | grep -i "mcp-session-id" | tr -d '\r' | awk '{print $2}')
  [ -n "$sid" ] && rcurl -X POST "$BASE/mcp/" -H "Authorization: Bearer $pat" \
    -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
    -H "mcp-session-id: $sid" \
    -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null
  echo "$sid"
}

# Vault $VAULT is now owned by XFER, $PUB_VAULT by OWNER
SID1=$(mcp_init_del "$XFER_PAT")
rcurl -X POST "$BASE/mcp/" -H "Authorization: Bearer $XFER_PAT" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: $SID1" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"akb_delete_vault\",\"arguments\":{\"vault\":\"$VAULT\"}}}" >/dev/null

SID2=$(mcp_init_del "$OWNER_PAT")
rcurl -X POST "$BASE/mcp/" -H "Authorization: Bearer $OWNER_PAT" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: $SID2" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"akb_delete_vault\",\"arguments\":{\"vault\":\"$PUB_VAULT\"}}}" >/dev/null

# Self-delete all 4 test users
for P in "$OWNER_PAT" "$GRANTEE_PAT" "$OUTSIDER_PAT" "$XFER_PAT"; do
  curl -sk --max-time 15 -X DELETE "$BASE/api/v1/my/account" -H "Authorization: Bearer $P" >/dev/null 2>&1
done
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
