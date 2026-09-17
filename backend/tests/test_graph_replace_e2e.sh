#!/bin/bash
#
# AKB E2E: Cross-Vault SQL and SQL row mutation boundaries
#
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
PASS=0
FAIL=0
ERRORS=()
MCP_ID=10

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

echo "╔══════════════════════════════════════════╗"
echo "║   Cross-Vault SQL E2E Tests              ║"
echo "║   Target: $BASE_URL"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── Setup ────────────────────────────────────────────────────
echo "▸ 0. Setup"

setup_user() {
  local user=$1
  curl -sk -X POST "$BASE_URL/api/v1/auth/register" \
    -H 'Content-Type: application/json' \
    -d "{\"username\":\"$user\",\"email\":\"$user@test.dev\",\"password\":\"test1234\"}" >/dev/null 2>&1
  local jwt=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" \
    -H 'Content-Type: application/json' \
    -d "{\"username\":\"$user\",\"password\":\"test1234\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)
  curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" \
    -H "Authorization: Bearer $jwt" \
    -H 'Content-Type: application/json' \
    -d '{"name":"e2e"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null
}

USER1="graph-e2e-u1-$(date +%s)"
PAT1=$(setup_user "$USER1")
[ -n "$PAT1" ] && pass "user created" || { fail "Setup" "user creation failed"; exit 1; }

setup_mcp() {
  local pat=$1
  local tmpfile=$(mktemp)
  curl -sk -i -X POST "$BASE_URL/mcp/" \
    -H "Authorization: Bearer $pat" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"graph-e2e","version":"1.0"}}}' > "$tmpfile" 2>/dev/null
  local sid=$(grep -i "mcp-session-id" "$tmpfile" | tr -d '\r' | awk '{print $2}')
  rm -f "$tmpfile"
  curl -sk -X POST "$BASE_URL/mcp/" \
    -H "Authorization: Bearer $pat" \
    -H "Content-Type: application/json" \
    -H "mcp-session-id: $sid" \
    -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null 2>&1
  echo "$sid"
}

SID1=$(setup_mcp "$PAT1")

mc() {
  local pat=$1 sid=$2 tool=$3 args=$4
  MCP_ID=$((MCP_ID+1))
  curl -sk -X POST "$BASE_URL/mcp/" \
    -H "Authorization: Bearer $pat" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -H "mcp-session-id: $sid" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":$MCP_ID,\"method\":\"tools/call\",\"params\":{\"name\":\"$tool\",\"arguments\":$args}}" 2>&1
}

mr() { python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(d['result']['content'][0]['text'])" 2>/dev/null; }

# Shorthand for the SQL test user
m1() { mc "$PAT1" "$SID1" "$1" "$2" | mr; }

VAULT1="graph-e2e-$(date +%s)"
VAULT2="graph-e2e2-$(($(date +%s)+1))"

m1 "akb_create_vault" "{\"name\":\"$VAULT1\",\"description\":\"graph test\"}" >/dev/null
m1 "akb_create_vault" "{\"name\":\"$VAULT2\",\"description\":\"cross vault test\"}" >/dev/null
pass "2 vaults created"

# ── 1. Cross-Vault SQL ───────────────────────────────────────
echo ""
echo "▸ 1. Cross-Vault SQL"

# Create tables in both vaults for the same authenticated SQL user.
m1 "akb_create_table" "{\"vault\":\"$VAULT1\",\"name\":\"products\",\"columns\":[{\"name\":\"name\",\"type\":\"text\"},{\"name\":\"price\",\"type\":\"number\"}]}" >/dev/null
m1 "akb_create_table" "{\"vault\":\"$VAULT2\",\"name\":\"orders\",\"columns\":[{\"name\":\"product\",\"type\":\"text\"},{\"name\":\"qty\",\"type\":\"number\"}]}" >/dev/null
pass "Tables in 2 vaults"

# Insert data
m1 "akb_sql" "{\"vault\":\"$VAULT1\",\"sql\":\"INSERT INTO products (name, price) VALUES ('Widget', 100), ('Gadget', 200)\"}" >/dev/null
m1 "akb_sql" "{\"vault\":\"$VAULT2\",\"sql\":\"INSERT INTO orders (product, qty) VALUES ('Widget', 5), ('Gadget', 3)\"}" >/dev/null
pass "Data inserted in both"

# Cross-vault query using vault__table syntax
VAULT1_SAFE=$(echo "$VAULT1" | tr '-' '_')
VAULT2_SAFE=$(echo "$VAULT2" | tr '-' '_')
R=$(m1 "akb_sql" "{\"vaults\":[\"$VAULT1\",\"$VAULT2\"],\"sql\":\"SELECT p.name, p.price, o.qty FROM ${VAULT1_SAFE}__products p JOIN ${VAULT2_SAFE}__orders o ON p.name = o.product ORDER BY p.name\"}")
CROSS_ROWS=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('items',[])))" 2>/dev/null)
[ "$CROSS_ROWS" = "2" ] && pass "Cross-vault JOIN: $CROSS_ROWS rows" || fail "Cross-vault SQL" "$R"

# Verify data correctness
FIRST_QTY=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['items'][0].get('qty',0))" 2>/dev/null)
[ "$FIRST_QTY" = "3" ] && pass "Cross-vault data correct (Gadget qty=3)" || fail "Cross-vault data" "qty=$FIRST_QTY"

# ── 2. SQL UPDATE/DELETE rows ────────────────────────────────
echo ""
echo "▸ 2. SQL row UPDATE/DELETE"

R=$(m1 "akb_sql" "{\"vault\":\"$VAULT1\",\"sql\":\"UPDATE products SET price = 150 WHERE name = 'Widget'\"}")
UPDATE_OK=$(echo "$R" | python3 -c "import sys,json; print('UPDATE' in json.load(sys.stdin).get('result',''))" 2>/dev/null)
[ "$UPDATE_OK" = "True" ] && pass "UPDATE row" || fail "UPDATE" "$R"

R=$(m1 "akb_sql" "{\"vault\":\"$VAULT1\",\"sql\":\"SELECT price FROM products WHERE name = 'Widget'\"}")
NEW_PRICE=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['items'][0]['price'])" 2>/dev/null)
[ "$NEW_PRICE" = "150" ] && pass "Updated value verified (150)" || fail "UPDATE verify" "price=$NEW_PRICE"

R=$(m1 "akb_sql" "{\"vault\":\"$VAULT1\",\"sql\":\"DELETE FROM products WHERE name = 'Gadget'\"}")
DELETE_OK=$(echo "$R" | python3 -c "import sys,json; print('DELETE' in json.load(sys.stdin).get('result',''))" 2>/dev/null)
[ "$DELETE_OK" = "True" ] && pass "DELETE row" || fail "DELETE" "$R"

R=$(m1 "akb_sql" "{\"vault\":\"$VAULT1\",\"sql\":\"SELECT count(*) as cnt FROM products\"}")
REMAINING=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['items'][0]['cnt'])" 2>/dev/null)
[ "$REMAINING" = "1" ] && pass "1 row remaining after DELETE" || fail "DELETE verify" "cnt=$REMAINING"

# ── Cleanup ──────────────────────────────────────────────────
echo ""
echo "▸ Cleanup"
m1 "akb_delete_vault" "{\"name\":\"$VAULT1\"}" >/dev/null 2>&1
m1 "akb_delete_vault" "{\"name\":\"$VAULT2\"}" >/dev/null 2>&1
pass "Vaults deleted"

# ── Summary ──────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════"
echo "  Results: $PASS passed, $FAIL failed"
if [ $FAIL -gt 0 ]; then
  echo "  Failures:"
  for e in "${ERRORS[@]}"; do echo "    - $e"; done
  echo "════════════════════════════════════════════"
  exit 1
fi
echo "════════════════════════════════════════════"
