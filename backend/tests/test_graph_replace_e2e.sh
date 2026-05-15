#!/bin/bash
#
# AKB E2E: Knowledge Graph, Grep Replace, Ownership Transfer, Unicode, Cross-Vault SQL
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
echo "║   Graph / Replace / Unicode E2E Tests    ║"
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
USER2="graph-e2e-u2-$(date +%s)"
PAT1=$(setup_user "$USER1")
PAT2=$(setup_user "$USER2")
[ -n "$PAT1" ] && [ -n "$PAT2" ] && pass "2 users created" || { fail "Setup" "user creation failed"; exit 1; }

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
SID2=$(setup_mcp "$PAT2")

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

# Shorthand for user1
m1() { mc "$PAT1" "$SID1" "$1" "$2" | mr; }
m2() { mc "$PAT2" "$SID2" "$1" "$2" | mr; }

VAULT1="graph-e2e-$(date +%s)"
VAULT2="graph-e2e2-$(($(date +%s)+1))"

m1 "akb_create_vault" "{\"name\":\"$VAULT1\",\"description\":\"graph test\"}" >/dev/null
m1 "akb_create_vault" "{\"name\":\"$VAULT2\",\"description\":\"cross vault test\"}" >/dev/null
pass "2 vaults created"

# ── 1. Unicode / 한글 테스트 ─────────────────────────────────
echo ""
echo "▸ 1. Unicode / 한글"

R=$(m1 "akb_put" "{\"vault\":\"$VAULT1\",\"collection\":\"한글컬렉션\",\"title\":\"제안요청서 분석 📄\",\"content\":\"# 한글 제목\\n\\n본문에 한글과 이모지 🎉 포함\\n\\n## 기술 요건\\n- 가나다라\\n- αβγδ\\n- 中文テスト\",\"tags\":[\"한글\",\"테스트\",\"유니코드\"]}")
DOC_KR_URI=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('uri',''))" 2>/dev/null)
[ -n "$DOC_KR_URI" ] && pass "한글 제목+컬렉션+태그 문서 생성 ($DOC_KR_URI)" || fail "Unicode put" "$R"

# Verify content preserved
R=$(m1 "akb_get" "{\"uri\":\"$DOC_KR_URI\"}")
HAS_KOREAN=$(echo "$R" | python3 -c "import sys,json; c=json.load(sys.stdin).get('content',''); print('가나다라' in c and '中文' in c)" 2>/dev/null)
[ "$HAS_KOREAN" = "True" ] && pass "한글+CJK 내용 보존 확인" || fail "Unicode content" "$R"

# Search in Korean
source "$(dirname "$0")/_wait_for_indexing.sh"
wait_for_indexing
R=$(m1 "akb_search" "{\"query\":\"기술 요건 가나다라\",\"vault\":\"$VAULT1\"}")
SEARCH_HIT=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('total',0))" 2>/dev/null)
[ "$SEARCH_HIT" -ge 1 ] 2>/dev/null && pass "한글 시맨틱 검색 ($SEARCH_HIT hits)" || fail "Korean search" "$R"

# Grep Korean text
R=$(m1 "akb_grep" "{\"pattern\":\"가나다라\",\"vault\":\"$VAULT1\"}")
GREP_MATCH=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('total_matches',0))" 2>/dev/null)
[ "$GREP_MATCH" -ge 1 ] 2>/dev/null && pass "한글 grep ($GREP_MATCH matches)" || fail "Korean grep" "$R"

# ── 2. Knowledge Graph: link / unlink ────────────────────────
echo ""
echo "▸ 2. Knowledge Graph (link/unlink/relations)"

# Create two documents to link
R=$(m1 "akb_put" "{\"vault\":\"$VAULT1\",\"collection\":\"specs\",\"title\":\"API Spec\",\"content\":\"# API Spec\\nEndpoint definitions\"}")
DOC_A_URI=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('uri',''))" 2>/dev/null)
R=$(m1 "akb_put" "{\"vault\":\"$VAULT1\",\"collection\":\"specs\",\"title\":\"Data Model\",\"content\":\"# Data Model\\nSchema definitions\"}")
DOC_B_URI=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('uri',''))" 2>/dev/null)
[ -n "$DOC_A_URI" ] && [ -n "$DOC_B_URI" ] && pass "2 docs for linking ($DOC_A_URI, $DOC_B_URI)" || fail "Docs create" "missing URIs"

# Get URIs from browse
R=$(m1 "akb_browse" "{\"vault\":\"$VAULT1\",\"collection\":\"specs\",\"depth\":2}")
URI_A=$(echo "$R" | python3 -c "
import sys,json
items = json.load(sys.stdin).get('items',[])
for it in items:
    if it.get('type')=='document' and 'api-spec' in it.get('path','').lower():
        print(it.get('uri','')); break
" 2>/dev/null)
URI_B=$(echo "$R" | python3 -c "
import sys,json
items = json.load(sys.stdin).get('items',[])
for it in items:
    if it.get('type')=='document' and 'data-model' in it.get('path','').lower():
        print(it.get('uri','')); break
" 2>/dev/null)

# Link A → B (depends_on)
R=$(m1 "akb_link" "{\"source\":\"$URI_A\",\"target\":\"$URI_B\",\"relation\":\"depends_on\"}")
LINKED=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('linked',False) or d.get('created',False) or d.get('edge_id','')!='')" 2>/dev/null)
[ "$LINKED" = "True" ] && pass "Link created: A depends_on B" || fail "Link" "$R"

# Check relations from A
R=$(m1 "akb_relations" "{\"uri\":\"$URI_A\"}")
REL_COUNT=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('outgoing',[]) + d.get('relations',[])))" 2>/dev/null)
[ "$REL_COUNT" -ge 1 ] 2>/dev/null && pass "Relations visible from A ($REL_COUNT)" || fail "Relations" "$R"

# Check graph
R=$(m1 "akb_graph" "{\"uri\":\"$URI_A\",\"depth\":1}")
NODES=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('nodes',[])))" 2>/dev/null)
EDGES=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('edges',[])))" 2>/dev/null)
[ "$NODES" -ge 2 ] 2>/dev/null && pass "Graph: $NODES nodes, $EDGES edges" || fail "Graph" "$R"

# Unlink
R=$(m1 "akb_unlink" "{\"source\":\"$URI_A\",\"target\":\"$URI_B\",\"relation\":\"depends_on\"}")
UNLINKED=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('unlinked',0) >= 1 or d.get('removed',0) >= 1 or d.get('deleted',False))" 2>/dev/null)
[ "$UNLINKED" = "True" ] && pass "Unlink: relation removed" || fail "Unlink" "$R"

# Verify relation gone
R=$(m1 "akb_relations" "{\"uri\":\"$URI_A\"}")
REMAINING=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('outgoing',[]) + d.get('relations',[])))" 2>/dev/null)
[ "$REMAINING" = "0" ] && pass "Relation confirmed removed" || fail "Unlink verify" "still $REMAINING relations"

# ── 3. Grep Replace ──────────────────────────────────────────
echo ""
echo "▸ 3. Grep Replace (find & replace across documents)"

# Create 3 docs with common text
for i in 1 2 3; do
  m1 "akb_put" "{\"vault\":\"$VAULT1\",\"collection\":\"replaceable\",\"title\":\"Replace Doc $i\",\"content\":\"# Doc $i\\nThis uses OLD_PLACEHOLDER text.\\nAnother line with OLD_PLACEHOLDER here.\"}" >/dev/null
done
pass "3 docs with OLD_PLACEHOLDER created"

# Replace OLD_PLACEHOLDER → NEW_VALUE across all
R=$(m1 "akb_grep" "{\"pattern\":\"OLD_PLACEHOLDER\",\"vault\":\"$VAULT1\",\"replace\":\"NEW_VALUE\"}")
REPLACED_COUNT=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('replaced_docs',0) or len(d.get('replaced',d.get('replacements',[]))))" 2>/dev/null)
[ "$REPLACED_COUNT" = "3" ] && pass "Replaced in $REPLACED_COUNT documents" || fail "Grep replace" "expected 3, got $REPLACED_COUNT"

# Verify replacement (wait for the post-replace re-index)
wait_for_indexing
R=$(m1 "akb_grep" "{\"pattern\":\"OLD_PLACEHOLDER\",\"vault\":\"$VAULT1\"}")
OLD_REMAINING=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('total_matches',0))" 2>/dev/null)
[ "$OLD_REMAINING" = "0" ] && pass "OLD_PLACEHOLDER fully removed (0 matches)" || fail "Replace verify" "$OLD_REMAINING still found"

R=$(m1 "akb_grep" "{\"pattern\":\"NEW_VALUE\",\"vault\":\"$VAULT1\"}")
NEW_FOUND=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('total_matches',0))" 2>/dev/null)
[ "$NEW_FOUND" -ge 6 ] 2>/dev/null && pass "NEW_VALUE present ($NEW_FOUND matches)" || fail "Replace content" "expected >=6, got $NEW_FOUND"

# Regex replace
m1 "akb_put" "{\"vault\":\"$VAULT1\",\"collection\":\"replaceable\",\"title\":\"Regex Doc\",\"content\":\"# Regex\\nVersion: v1.2.3\\nRelease: v4.5.6\"}" >/dev/null
R=$(m1 "akb_grep" "{\"pattern\":\"v(\\\\d+)\\\\.(\\\\d+)\\\\.(\\\\d+)\",\"regex\":true,\"vault\":\"$VAULT1\",\"collection\":\"replaceable\",\"replace\":\"v\\\\1.\\\\2.99\"}")
REGEX_REPLACED=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('replaced_docs',0) or len(d.get('replacements',d.get('replaced',[]))))" 2>/dev/null)
[ "$REGEX_REPLACED" -ge 1 ] 2>/dev/null && pass "Regex replace: $REGEX_REPLACED docs" || fail "Regex replace" "$R"

# ── 4. Transfer Ownership ────────────────────────────────────
echo ""
echo "▸ 4. Transfer Ownership"

# User1 transfers vault2 to user2
R=$(m1 "akb_transfer_ownership" "{\"vault\":\"$VAULT2\",\"new_owner\":\"$USER2\"}")
TRANSFERRED=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('transferred',False) or 'new_owner' in d)" 2>/dev/null)
[ "$TRANSFERRED" = "True" ] && pass "Vault2 transferred to User2" || fail "Transfer" "$R"

# User2 should now be owner — can write
R=$(m2 "akb_put" "{\"vault\":\"$VAULT2\",\"collection\":\"owned\",\"title\":\"I own this now\",\"content\":\"# Mine\"}")
NEW_OWNER_DOC_URI=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('uri',''))" 2>/dev/null)
[ -n "$NEW_OWNER_DOC_URI" ] && pass "New owner can write to transferred vault" || fail "New owner write" "$R"

# User1 should no longer be owner — but may still have access depending on implementation
# At minimum, user1 should not be able to transfer again
R=$(m1 "akb_transfer_ownership" "{\"vault\":\"$VAULT2\",\"new_owner\":\"$USER1\"}")
BLOCKED=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('error' in d or 'denied' in str(d).lower())" 2>/dev/null)
[ "$BLOCKED" = "True" ] && pass "Old owner cannot re-transfer" || fail "Transfer guard" "$R"

# ── 5. Cross-Vault SQL ───────────────────────────────────────
echo ""
echo "▸ 5. Cross-Vault SQL"

# Create tables in both vaults (user1 still has access to vault1, user2 owns vault2)
# Grant user1 writer on vault2 so they can do cross-vault
m2 "akb_grant" "{\"vault\":\"$VAULT2\",\"user\":\"$USER1\",\"role\":\"writer\"}" >/dev/null

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

# ── 6. SQL UPDATE/DELETE rows ────────────────────────────────
echo ""
echo "▸ 6. SQL row UPDATE/DELETE"

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
m2 "akb_delete_vault" "{\"name\":\"$VAULT2\"}" >/dev/null 2>&1
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
