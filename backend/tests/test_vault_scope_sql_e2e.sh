#!/bin/bash
#
# AKB per-PAT vault-scope (Option B) — PG-native (akb_sql) surface E2E.
#
# This is SURFACE 2 of the backstop (M2). Surface 1 (the doc tools /
# check_vault_access) is proved by test_vault_scope_e2e.sh; here we prove the
# raw-SQL surface: a scoped PAT's `akb_sql` is confined to its vault scope by
# PostgreSQL ACL itself (SQLSTATE 42501) — NOT by an application string check.
#
# Why this is unambiguously the PG-native layer (not surface 1 leaking in):
# the `akb_sql` route gates on `check_vault_access(..., required_role="reader")`
# only, and the M1 scope guard fires for MUTATING roles exclusively — so for a
# vault the token's user can read (here: OWNS), the reader gate PASSES and the
# request reaches PG. The denial is therefore PG refusing the `akb_token_<tid>`
# role, whose membership = owner-ACL ∩ scope.
#
# The user is the first-registered account (admin on a fresh DB), so the
# out-of-scope denial ALSO demonstrates that a scoped ADMIN token does not
# bypass (the executor switches to akb_token_<tid> with precedence over the
# is_admin bypass). And reads are confined on THIS surface too (stricter than
# surface 1's read-broad doc tools — defense-in-depth on the raw-SQL power
# tool), which test 4 asserts.
#
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
PASS=0
FAIL=0
ERRORS=()
MCP_ID=10

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

echo "╔════════════════════════════════════════════════╗"
echo "║  AKB per-PAT vault-scope — PG-native akb_sql E2E ║"
echo "║  Target: $BASE_URL"
echo "╚════════════════════════════════════════════════╝"
echo ""

TS=$(date +%s)
USER="scope-sql-u-$TS"

# ── 0. Setup: user + JWT + scoped/unscoped PATs ──────────────
echo "▸ 0. Setup (user + scoped/unscoped PATs + vaults + tables)"
curl -sk -X POST "$BASE_URL/api/v1/auth/register" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER\",\"email\":\"$USER@test.dev\",\"password\":\"test1234\"}" >/dev/null 2>&1
JWT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER\",\"password\":\"test1234\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)
[ -n "$JWT" ] && pass "user + JWT" || { fail "Setup" "login failed"; exit 1; }

mint_pat() {  # $1 = json body → prints token
  curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" -H "Authorization: Bearer $JWT" \
    -H 'Content-Type: application/json' -d "$1" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin).get("token",""))' 2>/dev/null
}

SCOPED_PAT=$(mint_pat '{"name":"scoped-sql","vault_scope":{"prefixes":["gdn-"],"extra_vaults":[]}}')
UNSCOPED_PAT=$(mint_pat '{"name":"unscoped-sql"}')
[ -n "$SCOPED_PAT" ] && pass "scoped PAT minted (gdn-)" || fail "Mint scoped" "no token"
[ -n "$UNSCOPED_PAT" ] && pass "unscoped PAT minted" || fail "Mint unscoped" "no token"

# ── MCP session helpers ──────────────────────────────────────
setup_mcp() {
  local pat=$1 tmpfile sid
  tmpfile=$(mktemp)
  curl -sk -i -X POST "$BASE_URL/mcp/" -H "Authorization: Bearer $pat" \
    -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"scope-sql-e2e","version":"1.0"}}}' > "$tmpfile" 2>/dev/null
  sid=$(grep -i "mcp-session-id" "$tmpfile" | tr -d '\r' | awk '{print $2}')
  rm -f "$tmpfile"
  curl -sk -X POST "$BASE_URL/mcp/" -H "Authorization: Bearer $pat" \
    -H "Content-Type: application/json" -H "mcp-session-id: $sid" \
    -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null 2>&1
  echo "$sid"
}
mcp_as() {
  local pat=$1 sid=$2 tool=$3 args=$4
  MCP_ID=$((MCP_ID+1))
  curl -sk -X POST "$BASE_URL/mcp/" -H "Authorization: Bearer $pat" \
    -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
    -H "mcp-session-id: $sid" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":$MCP_ID,\"method\":\"tools/call\",\"params\":{\"name\":\"$tool\",\"arguments\":$args}}" 2>&1
}
mr() { python3 -c "import sys,json; print(json.loads(sys.stdin.read())['result']['content'][0]['text'])" 2>/dev/null; }

# Assert a PG-side permission denial (42501) — NOT an app-layer string check.
# Accepts the structured permission_denied envelope OR a permission-related
# PG message (mirrors test_pg_rbac_e2e.sh::assert_pg_denied).
assert_pg_denied() {
  local label=$1 raw=$2 matched
  matched=$(echo "$raw" | python3 -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
    code = d.get('code', '')
    err = (d.get('error') or '').lower()
    det = json.dumps(d.get('details') or {}).lower()
    print('Y' if (code == 'permission_denied'
                  or 'permission denied' in err
                  or '42501' in det or '42501' in err) else 'N')
except Exception:
    print('N')
" 2>/dev/null)
  [ "$matched" = "Y" ] && pass "$label" || fail "$label" "expected PG 42501 denial; raw=$raw"
}

SID_S=$(setup_mcp "$SCOPED_PAT")
SID_U=$(setup_mcp "$UNSCOPED_PAT")

# Vaults + tables, created via the UNSCOPED (owner) PAT so scope never gates setup.
GDN_VAULT="gdn-sql-e2e-$TS"
NONGDN_VAULT="sql-e2e-other-$TS"
COLS='[{"name":"k","type":"text","required":true},{"name":"v","type":"text"}]'

mcp_as "$UNSCOPED_PAT" "$SID_U" "akb_create_vault" "{\"name\":\"$GDN_VAULT\"}" >/dev/null 2>&1
mcp_as "$UNSCOPED_PAT" "$SID_U" "akb_create_vault" "{\"name\":\"$NONGDN_VAULT\"}" >/dev/null 2>&1
R=$(mcp_as "$UNSCOPED_PAT" "$SID_U" "akb_create_table" "{\"vault\":\"$GDN_VAULT\",\"name\":\"state\",\"columns\":$COLS}" | mr)
echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(bool(d.get('uri') or d.get('name')=='state'))" 2>/dev/null | grep -q True \
  && pass "gdn table created ($GDN_VAULT.state)" || fail "gdn table" "$R"
R=$(mcp_as "$UNSCOPED_PAT" "$SID_U" "akb_create_table" "{\"vault\":\"$NONGDN_VAULT\",\"name\":\"facts\",\"columns\":$COLS}" | mr)
echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(bool(d.get('uri') or d.get('name')=='facts'))" 2>/dev/null | grep -q True \
  && pass "non-gdn table created ($NONGDN_VAULT.facts, owner=user)" || fail "non-gdn table" "$R"
# Seed one row in each (owner) so the scoped READ tests have a target.
mcp_as "$UNSCOPED_PAT" "$SID_U" "akb_sql" "{\"vault\":\"$GDN_VAULT\",\"sql\":\"INSERT INTO state (k,v) VALUES ('seed','0')\"}" >/dev/null 2>&1
mcp_as "$UNSCOPED_PAT" "$SID_U" "akb_sql" "{\"vault\":\"$NONGDN_VAULT\",\"sql\":\"INSERT INTO facts (k,v) VALUES ('seed','0')\"}" >/dev/null 2>&1

wrote_ok() { python3 -c "import sys,json; print('\"result\"' in sys.stdin.read())" 2>/dev/null; }
read_ok()  { python3 -c "import sys,json; d=json.load(sys.stdin); print('items' in d and 'error' not in d)" 2>/dev/null; }

# ── 1. In-scope akb_sql works (read + write) ─────────────────
echo ""
echo "▸ 1. Scoped PAT — akb_sql INSIDE scope (gdn-*)"
R=$(mcp_as "$SCOPED_PAT" "$SID_S" "akb_sql" "{\"vault\":\"$GDN_VAULT\",\"sql\":\"SELECT * FROM state\"}" | mr)
[ "$(echo "$R" | read_ok)" = "True" ] && pass "scoped PAT CAN SELECT gdn-* (in scope)" || fail "in-scope SELECT" "$R"
R=$(mcp_as "$SCOPED_PAT" "$SID_S" "akb_sql" "{\"vault\":\"$GDN_VAULT\",\"sql\":\"INSERT INTO state (k,v) VALUES ('in','1')\"}" | mr)
[ "$(echo "$R" | wrote_ok)" = "True" ] && pass "scoped PAT CAN INSERT gdn-* (in scope)" || fail "in-scope INSERT" "$R"

# ── 2. KEY: out-of-scope WRITE → PG 42501 (even though user OWNS it) ──
echo ""
echo "▸ 2. Scoped PAT — akb_sql WRITE OUTSIDE scope (owned non-gdn) → PG 42501"
R=$(mcp_as "$SCOPED_PAT" "$SID_S" "akb_sql" "{\"vault\":\"$NONGDN_VAULT\",\"sql\":\"INSERT INTO facts (k,v) VALUES ('out','9')\"}" | mr)
assert_pg_denied "scoped PAT DENIED non-gdn INSERT (akb_token_<tid> ∌ vault) — #51 surface 2" "$R"

# ── 3. Out-of-scope READ → PG 42501 (akb_sql confines reads too) ──
echo ""
echo "▸ 3. Scoped PAT — akb_sql READ OUTSIDE scope → PG 42501 (raw-SQL is scope-confined)"
R=$(mcp_as "$SCOPED_PAT" "$SID_S" "akb_sql" "{\"vault\":\"$NONGDN_VAULT\",\"sql\":\"SELECT * FROM facts\"}" | mr)
assert_pg_denied "scoped PAT DENIED non-gdn SELECT (stricter than doc-tool read-broad)" "$R"

# ── 4. Control: unscoped PAT writes the SAME non-gdn vault OK ─
echo ""
echo "▸ 4. Control — unscoped PAT akb_sql writes the same non-gdn vault"
R=$(mcp_as "$UNSCOPED_PAT" "$SID_U" "akb_sql" "{\"vault\":\"$NONGDN_VAULT\",\"sql\":\"INSERT INTO facts (k,v) VALUES ('ctl','7')\"}" | mr)
[ "$(echo "$R" | wrote_ok)" = "True" ] && pass "unscoped PAT CAN INSERT non-gdn (proves denial = scope, not ACL)" || fail "control INSERT" "$R"

# ── Cleanup ──────────────────────────────────────────────────
mcp_as "$UNSCOPED_PAT" "$SID_U" "akb_delete_vault" "{\"vault\":\"$GDN_VAULT\",\"confirm\":true}" >/dev/null 2>&1
mcp_as "$UNSCOPED_PAT" "$SID_U" "akb_delete_vault" "{\"vault\":\"$NONGDN_VAULT\",\"confirm\":true}" >/dev/null 2>&1

# ── Summary ──────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════"
echo "  PASS: $PASS    FAIL: $FAIL"
if [ "$FAIL" -gt 0 ]; then
  printf '  %s\n' "${ERRORS[@]}"
  exit 1
fi
echo "  ✓ PG-native vault-scope backstop (surface 2) verified end-to-end"
