#!/bin/bash
#
# AKB MCP OAuth Resource Server — E2E
#
# Narrow Resource Server checks using a separately provisioned direct tool
# test client. Configure its exact MCP audience mapper, optional AKB scopes,
# password grant and mcp_oauth_direct_client_ids admission explicitly. Keep
# browser/API clients separate and never password-enable the native client.
# Native browser, S256 and rotation proof lives in the native-oauth fixture.
#
# What this script proves (in order):
#   1. The resource-specific protected-resource metadata has the expected shape
#   2. `/mcp` rejects no-auth with 401 + WWW-Authenticate including a
#      `resource_metadata=` parameter (RFC 9728 §5 client tip-off)
#   3. `/mcp` refuses a token without an admitted tool scope with 403
#   4. `/mcp` accepts a human access token with both scopes
#      and dispatches a read-grade tool (akb_list_vaults)
#   5. `/mcp` rejects a local session JWT and accepts a real `akb_` PAT
#   6. A token with only `akb:vault:read` is refused at a write-grade
#      tool with `insufficient_scope`
#
# Stack required:
#   docker compose -f docker-compose.yaml -f docker-compose.keycloak.yaml up -d
#   # config/app.yaml must have:
#   #   auth_mode: local
#   #   keycloak_enabled: true
#   #   mcp_oauth_enabled: true
#   #   public_base_url: http://localhost:8000
#
# Run:
#   bash backend/tests/test_mcp_oauth_e2e.sh
#
# Tunable via env:
#   AKB_URL=http://localhost:8000
#   KC_URL=http://localhost:8080
#   KC_REALM=akb
#   KC_TEST_CLIENT_ID=<separate admitted direct tool test client>
#   KC_TEST_CLIENT_SECRET=<inject when the test client is confidential>
#   KC_TEST_USERNAME=<lab human username>
#   KC_TEST_PASSWORD=<inject lab human password>
set -uo pipefail

AKB_URL="${AKB_URL:-http://localhost:8000}"
AKB_URL="${AKB_URL%/}"
KC_URL="${KC_URL:-http://localhost:8080}"
KC_REALM="${KC_REALM:-akb}"
: "${KC_TEST_CLIENT_ID:?set a separate admitted direct tool test client}"
KC_TEST_CLIENT_SECRET="${KC_TEST_CLIENT_SECRET:-}"
: "${KC_TEST_USERNAME:?inject the lab human username}"
: "${KC_TEST_PASSWORD:?inject the lab human password}"

PASS=0
FAIL=0
ERRORS=()

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

section() { echo; echo "── $1 ──"; }

# ── Helpers ──────────────────────────────────────────────────

mint_token() {
  # $1 = scope (space-delimited). Empty → realm-default scopes only.
  local scope="$1"
  local args=(--data-urlencode "grant_type=password" --data-urlencode "client_id=${KC_TEST_CLIENT_ID}" --data-urlencode "username=${KC_TEST_USERNAME}" --data-urlencode "password=${KC_TEST_PASSWORD}")
  if [ -n "$KC_TEST_CLIENT_SECRET" ]; then args+=(--data-urlencode "client_secret=${KC_TEST_CLIENT_SECRET}"); fi
  if [ -n "$scope" ]; then args+=(--data-urlencode "scope=${scope}"); fi
  curl -s -X POST "${KC_URL}/realms/${KC_REALM}/protocol/openid-connect/token" \
       -H "Content-Type: application/x-www-form-urlencoded" \
       "${args[@]}" \
    | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))"
}

mcp_initialize_call() {
  # Sends a minimal MCP `initialize` then a `tools/list` call.
  local token="$1"
  curl -s -X POST "${AKB_URL}/mcp/" \
       -H "Authorization: Bearer ${token}" \
       -H "Content-Type: application/json" \
       -H "Accept: application/json, text/event-stream" \
       -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"e2e","version":"0"}}}'
}

mcp_tool_call() {
  # $1 = bearer token, $2 = MCP method (tools/call etc.), $3 = JSON args
  local token="$1" method="$2" args="$3"
  curl -s -X POST "${AKB_URL}/mcp/" \
       -H "Authorization: Bearer ${token}" \
       -H "Content-Type: application/json" \
       -H "Accept: application/json, text/event-stream" \
       -d "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"${method}\",\"params\":${args}}"
}

# ── 1. Protected-resource metadata shape ─────────────────────

section "Protected-resource metadata"
metadata_url=$(python3 -c 'from urllib.parse import urlsplit,urlunsplit; import sys; url=urlsplit(sys.argv[1]); print(urlunsplit((url.scheme,url.netloc,"/.well-known/oauth-protected-resource"+url.path.rstrip("/")+"/mcp","","")))' "$AKB_URL")
meta=$(curl -s "$metadata_url")
echo "    ${meta}"
res=$(echo "$meta" | python3 -c "import sys,json; print(json.load(sys.stdin).get('resource',''))" 2>/dev/null) || true
if [ "$res" = "${AKB_URL}/mcp" ]; then pass "canonical resource metadata served"; else fail "canonical resource metadata served" "unexpected resource"; fi
as=$(echo "$meta" | python3 -c "import sys,json; print(','.join(json.load(sys.stdin).get('authorization_servers',[])))" 2>/dev/null) || true
case "$as" in *"realms/${KC_REALM}"*) pass "authorization_servers points at realm" ;;
                  *) fail "authorization_servers points at realm" "got '$as'" ;;
esac
scopes=$(echo "$meta" | python3 -c "import sys,json; print(','.join(json.load(sys.stdin).get('scopes_supported',[])))" 2>/dev/null) || true
case "$scopes" in *"akb:vault:read"*) pass "scopes_supported lists akb:vault:read" ;;
                      *) fail "scopes_supported lists akb:vault:read" "got '$scopes'" ;; esac
case "$scopes" in *"akb:vault:write"*) pass "scopes_supported lists akb:vault:write" ;;
                       *) fail "scopes_supported lists akb:vault:write" "got '$scopes'" ;; esac
case "$scopes" in *"offline_access"*) fail "offline_access is not advertised" "unexpected scope" ;;
                      *) pass "offline_access is not advertised" ;; esac

# ── 2. No-auth → 401 + WWW-Authenticate with resource_metadata ──

section "401 carries WWW-Authenticate (RFC 9728 §5)"
hdrs=$(curl -s -i -X POST "${AKB_URL}/mcp/" -H "Content-Type: application/json" -d '{}' | tr -d '\r')
status=$(echo "$hdrs" | head -1 | awk '{print $2}')
wa=$(echo "$hdrs" | grep -i "^WWW-Authenticate:" | head -1 | sed 's/^[Ww][Ww][Ww]-[Aa][Uu][Tt][Hh][Ee][Nn][Tt][Ii][Cc][Aa][Tt][Ee]:[[:space:]]*//')
if [ "$status" = "401" ]; then pass "no-auth → 401"; else fail "no-auth → 401" "got $status"; fi
case "$wa" in *"resource_metadata="*) pass "WWW-Authenticate carries resource_metadata=" ;;
                  *) fail "WWW-Authenticate carries resource_metadata=" "got '$wa'" ;; esac

# ── 3. Token without an admitted tool scope → 403 ─────────────

section "Token without tool scopes → 403"
unscoped_token=$(mint_token "")
if [ -z "$unscoped_token" ]; then fail "mint unscoped token" "Keycloak returned no access_token"; else
  status=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${AKB_URL}/mcp/" \
             -H "Authorization: Bearer ${unscoped_token}" \
             -H "Content-Type: application/json" \
             -H "Accept: application/json, text/event-stream" \
             -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"e2e","version":"0"}}}')
  if [ "$status" = "403" ]; then pass "missing tool scopes → 403"; else fail "missing tool scopes → 403" "got $status"; fi
fi

# ── 4. Human access token with both scopes → 200 + tool list ─

section "Read+write token → /mcp accepts, tools/list returns AKB tools"
good_token=$(mint_token "akb:vault:read akb:vault:write")
if [ -z "$good_token" ]; then fail "mint scoped token" "Keycloak returned no access_token"; else
  init_resp=$(mcp_initialize_call "$good_token")
  # Streamable HTTP returns SSE; just check the response carried a JSON-RPC frame
  case "$init_resp" in *'"jsonrpc"'*) pass "initialize accepted" ;;
                            *) fail "initialize accepted" "got '${init_resp:0:200}'" ;; esac
  list_resp=$(mcp_tool_call "$good_token" "tools/list" "{}")
  case "$list_resp" in *"akb_list_vaults"*) pass "tools/list includes akb_list_vaults" ;;
                            *) fail "tools/list includes akb_list_vaults" "got '${list_resp:0:200}'" ;; esac
fi

# ── 5. Local session rejected; actual PAT works ─────────────────

section "Regression — local session rejected, actual PAT accepted"
# Register a throwaway local user, log in, then mint an actual namespaced PAT.
ts=$(date +%s)
user="mcp-oauth-e2e-${ts}"
email="${user}@example.com"
curl -s -X POST "${AKB_URL}/api/v1/auth/register" \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"${user}\",\"email\":\"${email}\",\"password\":\"test-password-1\"}" \
  >/dev/null
session_jwt=$(curl -s -X POST "${AKB_URL}/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"${user}\",\"password\":\"test-password-1\"}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))")
if [ -z "$session_jwt" ]; then
  fail "local login" "no session JWT returned"
else
  status=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${AKB_URL}/mcp/" \
    -H "Authorization: Bearer ${session_jwt}" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"e2e","version":"0"}}}')
  if [ "$status" = "401" ]; then pass "local session JWT rejected at /mcp"; else fail "local session JWT rejected at /mcp" "got $status"; fi
fi

pat=$(curl -s -X POST "${AKB_URL}/api/v1/auth/tokens" \
  -H "Authorization: Bearer ${session_jwt}" \
  -H "Content-Type: application/json" \
  -d '{"name":"mcp-oauth-e2e-pat","scopes":["read","write"]}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))")
case "$pat" in
  akb_*)
    pass "actual namespaced PAT minted"
    ;;
  *)
    fail "actual namespaced PAT minted" "token is empty or lacks akb_ prefix"
    ;;
esac
if [ -n "$pat" ]; then
  init_resp=$(mcp_initialize_call "$pat")
  case "$init_resp" in *'"jsonrpc"'*) pass "PAT initialize works" ;;
                            *) fail "PAT initialize works" "got '${init_resp:0:200}'" ;; esac
fi

# ── 6. Read-only token → write tool returns insufficient_scope ──

section "Read-only token → write tool refused with insufficient_scope"
read_only=$(mint_token "akb:vault:read")
if [ -z "$read_only" ]; then fail "mint read-only token" "no access_token"; else
  # Try akb_put — should fail with insufficient_scope at dispatch.
  put_args='{"name":"akb_put","arguments":{"vault":"nope","title":"x","content":"y"}}'
  put_resp=$(mcp_tool_call "$read_only" "tools/call" "$put_args")
  case "$put_resp" in *"insufficient_scope"*) pass "akb_put refused with insufficient_scope" ;;
                          *) fail "akb_put refused with insufficient_scope" "got '${put_resp:0:300}'" ;; esac
fi

# ── Summary ───────────────────────────────────────────────────

echo
echo "════════════════════════════════════════"
echo "MCP OAuth E2E — ${PASS} passed, ${FAIL} failed"
echo "════════════════════════════════════════"
if [ "$FAIL" -gt 0 ]; then
  for e in "${ERRORS[@]}"; do echo "  - $e"; done
  exit 1
fi
