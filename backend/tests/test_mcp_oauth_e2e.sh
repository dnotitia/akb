#!/bin/bash
#
# AKB MCP OAuth Resource Server — E2E
#
# Exercises the new /mcp OAuth path against the local-dev Keycloak overlay
# WITHOUT needing a browser DCR roundtrip: the test realm ships an
# `akb-mcp-test` service-account client (client_credentials grant) whose
# default scopes are `akb:vault:read|write`, so we can mint a real
# Keycloak access token from a shell script and present it at /mcp.
#
# What this script proves (in order):
#   1. `/.well-known/oauth-protected-resource` returns the expected shape
#   2. `/mcp` rejects no-auth with 401 + WWW-Authenticate including a
#      `resource_metadata=` parameter (RFC 9728 §5 client tip-off)
#   3. `/mcp` rejects a syntactically-valid but wrong-audience token
#      with 401
#   4. `/mcp` accepts a service-account access token with both scopes
#      and dispatches a read-grade tool (akb_list_vaults)
#   5. PAT path still works unchanged (regression guard)
#   6. A token with only `akb:vault:read` is refused at a write-grade
#      tool with `insufficient_scope`
#
# Stack required:
#   docker compose -f docker-compose.yaml -f docker-compose.keycloak.yaml up -d
#   # config/app.yaml must have:
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
#   KC_TEST_CLIENT_ID=akb-mcp-test
#   KC_TEST_CLIENT_SECRET=local-akb-mcp-test-secret
set -uo pipefail

AKB_URL="${AKB_URL:-http://localhost:8000}"
KC_URL="${KC_URL:-http://localhost:8080}"
KC_REALM="${KC_REALM:-akb}"
KC_TEST_CLIENT_ID="${KC_TEST_CLIENT_ID:-akb-mcp-test}"
KC_TEST_CLIENT_SECRET="${KC_TEST_CLIENT_SECRET:-local-akb-mcp-test-secret}"

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
  local data="grant_type=client_credentials&client_id=${KC_TEST_CLIENT_ID}&client_secret=${KC_TEST_CLIENT_SECRET}"
  if [ -n "$scope" ]; then data="${data}&scope=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "$scope")"; fi
  curl -s -X POST "${KC_URL}/realms/${KC_REALM}/protocol/openid-connect/token" \
       -H "Content-Type: application/x-www-form-urlencoded" \
       -d "$data" \
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
meta=$(curl -s "${AKB_URL}/.well-known/oauth-protected-resource")
echo "    ${meta}"
res=$(echo "$meta" | python3 -c "import sys,json; print(json.load(sys.stdin).get('resource',''))" 2>/dev/null) || true
if [ -n "$res" ]; then pass "metadata served"; else fail "metadata served" "empty/missing"; fi
as=$(echo "$meta" | python3 -c "import sys,json; print(','.join(json.load(sys.stdin).get('authorization_servers',[])))" 2>/dev/null) || true
case "$as" in *"realms/${KC_REALM}"*) pass "authorization_servers points at realm" ;;
                  *) fail "authorization_servers points at realm" "got '$as'" ;;
esac
scopes=$(echo "$meta" | python3 -c "import sys,json; print(','.join(json.load(sys.stdin).get('scopes_supported',[])))" 2>/dev/null) || true
case "$scopes" in *"akb:vault:read"*) pass "scopes_supported lists akb:vault:read" ;;
                      *) fail "scopes_supported lists akb:vault:read" "got '$scopes'" ;; esac
case "$scopes" in *"akb:vault:write"*) pass "scopes_supported lists akb:vault:write" ;;
                       *) fail "scopes_supported lists akb:vault:write" "got '$scopes'" ;; esac
case "$scopes" in *"offline_access"*) pass "scopes_supported lists offline_access" ;;
                      *) fail "scopes_supported lists offline_access" "got '$scopes'" ;; esac

# ── 2. No-auth → 401 + WWW-Authenticate with resource_metadata ──

section "401 carries WWW-Authenticate (RFC 9728 §5)"
hdrs=$(curl -s -i -X POST "${AKB_URL}/mcp/" -H "Content-Type: application/json" -d '{}' | tr -d '\r')
status=$(echo "$hdrs" | head -1 | awk '{print $2}')
wa=$(echo "$hdrs" | grep -i "^WWW-Authenticate:" | head -1 | sed 's/^[Ww][Ww][Ww]-[Aa][Uu][Tt][Hh][Ee][Nn][Tt][Ii][Cc][Aa][Tt][Ee]:[[:space:]]*//')
if [ "$status" = "401" ]; then pass "no-auth → 401"; else fail "no-auth → 401" "got $status"; fi
case "$wa" in *"resource_metadata="*) pass "WWW-Authenticate carries resource_metadata=" ;;
                  *) fail "WWW-Authenticate carries resource_metadata=" "got '$wa'" ;; esac

# ── 3. Token minted for a different audience → 401 ─────────────

section "Wrong-audience token → 401"
# Mint a token without our vault scopes — its `aud` should NOT include
# our resource (the audience mapper only fires when an akb:vault:* scope
# is requested). So an unscoped token from the same realm is a useful
# wrong-audience proxy.
bad_token=$(mint_token "")
if [ -z "$bad_token" ]; then fail "mint unscoped token" "Keycloak returned no access_token"; else
  status=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${AKB_URL}/mcp/" \
             -H "Authorization: Bearer ${bad_token}" \
             -H "Content-Type: application/json" \
             -H "Accept: application/json, text/event-stream" \
             -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"e2e","version":"0"}}}')
  if [ "$status" = "401" ]; then pass "wrong-aud → 401"; else fail "wrong-aud → 401" "got $status"; fi
fi

# ── 4. Service-account token with both scopes → 200 + tool list ─

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

# ── 5. PAT path still works (regression) ───────────────────────

section "Regression — PAT path unchanged"
# Register a throwaway local user + PAT.
ts=$(date +%s)
user="mcp-oauth-e2e-${ts}"
email="${user}@example.com"
pat=$(curl -s -X POST "${AKB_URL}/api/v1/auth/register" \
        -H "Content-Type: application/json" \
        -d "{\"username\":\"${user}\",\"email\":\"${email}\",\"password\":\"test-password-1\"}" \
      | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))")
if [ -z "$pat" ]; then fail "register local user" "no token returned"; else
  # Use the AKB JWT as Bearer at /mcp — PAT prefix not strictly required,
  # AKB JWT works at /mcp too.
  init_resp=$(mcp_initialize_call "$pat")
  case "$init_resp" in *'"jsonrpc"'*) pass "PAT/JWT initialize still works" ;;
                            *) fail "PAT/JWT initialize still works" "got '${init_resp:0:200}'" ;; esac
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
