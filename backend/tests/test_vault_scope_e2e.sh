#!/bin/bash
#
# AKB per-PAT vault-scope (Option B) E2E
#
# Proves the token-scoping backstop end-to-end against a live backend:
# a PAT minted with a vault_scope can only MUTATE vaults inside that
# scope, even when its user OWNS the target vault. Reads are unaffected.
# The user owns both test vaults, so the out-of-scope-write denial also
# demonstrates that ownership does not bypass the token scope.
#
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
source "$(dirname "$0")/mcp_modern.sh"
PASS=0
FAIL=0
ERRORS=()
MCP_ID=10

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

echo "╔══════════════════════════════════════════╗"
echo "║   AKB per-PAT vault-scope (Option B) E2E  ║"
echo "║   Target: $BASE_URL"
echo "╚══════════════════════════════════════════╝"
echo ""

TS=$(date +%s)
USER="scope-e2e-u-$TS"

# ── 0. Setup: user + JWT ─────────────────────────────────────
echo "▸ 0. Setup (user + scoped/unscoped PATs)"
curl -sk -X POST "$BASE_URL/api/v1/auth/register" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER\",\"email\":\"$USER@test.dev\",\"password\":\"test1234\"}" >/dev/null 2>&1
JWT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER\",\"password\":\"test1234\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)
[ -n "$JWT" ] && pass "user + JWT" || { fail "Setup" "login failed"; exit 1; }

mint_pat() {  # $1 = json body → prints token (empty on non-200)
  curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" -H "Authorization: Bearer $JWT" \
    -H 'Content-Type: application/json' -d "$1" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin).get("token",""))' 2>/dev/null
}

SCOPED_PAT=$(mint_pat '{"name":"scoped","vault_scope":{"prefixes":["gdn-"],"extra_vaults":[]}}')
UNSCOPED_PAT=$(mint_pat '{"name":"unscoped"}')
[ -n "$SCOPED_PAT" ] && pass "scoped PAT minted (gdn-)" || fail "Mint scoped" "no token"
[ -n "$UNSCOPED_PAT" ] && pass "unscoped PAT minted" || fail "Mint unscoped" "no token"

# ── 0b. mint validation (malformed scope → 400) ──────────────
CODE=$(curl -sk -o /dev/null -w '%{http_code}' -X POST "$BASE_URL/api/v1/auth/tokens" \
  -H "Authorization: Bearer $JWT" -H 'Content-Type: application/json' \
  -d '{"name":"bad","vault_scope":{"prefixes":["BAD UPPER"],"extra_vaults":[]}}')
[ "$CODE" = "422" ] && pass "malformed scope rejected at mint (422)" || fail "Mint validation" "expected 422, got $CODE"

EMPTY_CODE=$(curl -sk -o /dev/null -w '%{http_code}' -X POST "$BASE_URL/api/v1/auth/tokens" \
  -H "Authorization: Bearer $JWT" -H 'Content-Type: application/json' \
  -d '{"name":"empty","vault_scope":{"prefixes":[],"extra_vaults":[]}}')
[ "$EMPTY_CODE" = "422" ] && pass "empty scope rejected at mint (422)" || fail "Mint validation" "empty scope expected 422, got $EMPTY_CODE"

# ── MCP stateless helpers ───────────────────────────────────
setup_mcp() {
  mcp_modern_discover "$1" scope-e2e >/dev/null && echo modern
}
mcp_as() {
  local pat=$1 _sid=$2 tool=$3 args=$4
  MCP_ID=$((MCP_ID+1))
  mcp_modern_call "$pat" "$tool" "$args" "$MCP_ID" scope-e2e
}
mr() { python3 -c "import sys,json; print(json.loads(sys.stdin.read())['result']['content'][0]['text'])" 2>/dev/null; }

SID_S=$(setup_mcp "$SCOPED_PAT")
SID_U=$(setup_mcp "$UNSCOPED_PAT")

# ── Setup vaults (via UNSCOPED PAT → owner; scope must not gate setup) ──
GDN_VAULT="gdn-scope-e2e-$TS"
NONGDN_VAULT="scope-e2e-other-$TS"
R=$(mcp_as "$UNSCOPED_PAT" "$SID_U" "akb_create_vault" "{\"name\":\"$GDN_VAULT\"}" | mr)
echo "$R" | python3 -c "import sys,json; json.load(sys.stdin)['vault_id']" >/dev/null 2>&1 && pass "gdn vault created ($GDN_VAULT)" || fail "gdn vault" "$R"
R=$(mcp_as "$UNSCOPED_PAT" "$SID_U" "akb_create_vault" "{\"name\":\"$NONGDN_VAULT\"}" | mr)
echo "$R" | python3 -c "import sys,json; json.load(sys.stdin)['vault_id']" >/dev/null 2>&1 && pass "non-gdn vault created ($NONGDN_VAULT, owner=user)" || fail "non-gdn vault" "$R"

# Seed a doc in the non-gdn vault (unscoped) so the scoped READ test has a target.
mcp_as "$UNSCOPED_PAT" "$SID_U" "akb_put" "{\"vault\":\"$NONGDN_VAULT\",\"collection\":\"c\",\"title\":\"Seed\",\"content\":\"# seed\\nhello\"}" >/dev/null 2>&1

is_denied() {  # stdin = mr output; success iff it's a scope-denial error
  python3 -c "import sys,json
d=json.loads(sys.stdin.read())
s=json.dumps(d).lower()
print('error' in d and ('scope' in s or 'permit' in s))" 2>/dev/null
}
wrote_ok() {  # stdin = mr output; success iff a doc uri came back
  python3 -c "import sys,json; print(bool(json.load(sys.stdin).get('uri')))" 2>/dev/null
}

# ── 1. In-scope write OK ─────────────────────────────────────
echo ""
echo "▸ 1. Scoped PAT — write INSIDE scope (gdn-*)"
R=$(mcp_as "$SCOPED_PAT" "$SID_S" "akb_put" "{\"vault\":\"$GDN_VAULT\",\"collection\":\"c\",\"title\":\"In\",\"content\":\"# in\\nok\"}" | mr)
[ "$(echo "$R" | wrote_ok)" = "True" ] && pass "scoped PAT CAN write gdn-* vault" || fail "in-scope write" "$R"

# ── 2. KEY: out-of-scope write DENIED (even though user OWNS it) ──
echo ""
echo "▸ 2. Scoped PAT — write OUTSIDE scope (owned non-gdn) → DENIED"
R=$(mcp_as "$SCOPED_PAT" "$SID_S" "akb_put" "{\"vault\":\"$NONGDN_VAULT\",\"collection\":\"c\",\"title\":\"Out\",\"content\":\"# out\\nnope\"}" | mr)
[ "$(echo "$R" | is_denied)" = "True" ] && pass "scoped PAT DENIED non-gdn write (owner ACL ∩ scope) — #51 backstop" || fail "out-of-scope write" "expected scope-denial, got: $R"

# ── 3. Control: unscoped PAT writes the SAME non-gdn vault OK ─
echo ""
echo "▸ 3. Control — unscoped PAT writes the same non-gdn vault"
R=$(mcp_as "$UNSCOPED_PAT" "$SID_U" "akb_put" "{\"vault\":\"$NONGDN_VAULT\",\"collection\":\"c\",\"title\":\"Ctl\",\"content\":\"# ctl\\nyes\"}" | mr)
[ "$(echo "$R" | wrote_ok)" = "True" ] && pass "unscoped PAT CAN write non-gdn (proves denial = scope, not ACL)" || fail "control write" "$R"

# ── 4. Reads are NOT scope-restricted ────────────────────────
echo ""
echo "▸ 4. Scoped PAT — READ a non-gdn vault (must be allowed)"
R=$(mcp_as "$SCOPED_PAT" "$SID_S" "akb_browse" "{\"vault\":\"$NONGDN_VAULT\"}" | mr)
echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if 'error' not in d else 1)" 2>/dev/null \
  && pass "scoped PAT CAN read non-gdn (scope bounds WRITES only)" || fail "scoped read" "read was blocked: $R"

# ── Cleanup ──────────────────────────────────────────────────
mcp_as "$UNSCOPED_PAT" "$SID_U" "akb_delete_vault" "{\"vault\":\"$GDN_VAULT\",\"confirm\":true}" >/dev/null 2>&1
mcp_as "$UNSCOPED_PAT" "$SID_U" "akb_delete_vault" "{\"vault\":\"$NONGDN_VAULT\",\"confirm\":true}" >/dev/null 2>&1

# ── Summary ──────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════"
echo "  Results: $PASS passed, $FAIL failed"
if [ "$FAIL" -gt 0 ]; then
  printf '  %s\n' "${ERRORS[@]}"
  exit 1
fi
echo "  ✓ vault-scope backstop verified end-to-end"
