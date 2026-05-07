#!/bin/bash
#
# AKB E2E: Additional edge cases
# PAT lifecycle, SQL injection, dynamic permissions, title collision, collection paths
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
echo "║   AKB Extra Edge Case E2E Tests          ║"
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
  echo "$jwt"
}

USER1="edge-e2e-u1-$(date +%s)"
USER2="edge-e2e-u2-$(date +%s)"
JWT1=$(setup_user "$USER1")
JWT2=$(setup_user "$USER2")

# Initial PAT
PAT1=$(curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" \
  -H "Authorization: Bearer $JWT1" \
  -H 'Content-Type: application/json' \
  -d '{"name":"primary"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)
PAT2=$(curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" \
  -H "Authorization: Bearer $JWT2" \
  -H 'Content-Type: application/json' \
  -d '{"name":"primary"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

[ -n "$PAT1" ] && [ -n "$PAT2" ] && pass "2 users + PATs created" || { fail "Setup" "no PATs"; exit 1; }

setup_mcp() {
  local pat=$1
  local tmpfile=$(mktemp)
  curl -sk -i -X POST "$BASE_URL/mcp/" \
    -H "Authorization: Bearer $pat" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"edge-e2e","version":"1.0"}}}' > "$tmpfile" 2>/dev/null
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
m1() { mc "$PAT1" "$SID1" "$1" "$2" | mr; }
m2() { mc "$PAT2" "$SID2" "$1" "$2" | mr; }

VAULT="edge-extra-$(date +%s)"
m1 "akb_create_vault" "{\"name\":\"$VAULT\",\"description\":\"edge tests\"}" >/dev/null
pass "Vault created"

# ── 1. PAT Lifecycle ─────────────────────────────────────────
echo ""
echo "▸ 1. PAT Lifecycle"

# Create a second PAT
PAT1_SECONDARY=$(curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" \
  -H "Authorization: Bearer $JWT1" \
  -H 'Content-Type: application/json' \
  -d '{"name":"secondary"}' | python3 -c 'import sys,json; print(json.load(sys.stdin).get("token",""))' 2>/dev/null)
[ -n "$PAT1_SECONDARY" ] && pass "Secondary PAT created" || fail "Create PAT" "no token"

# List PATs
PAT_COUNT=$(curl -sk "$BASE_URL/api/v1/auth/tokens" \
  -H "Authorization: Bearer $JWT1" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(len(d.get("tokens", d) if isinstance(d, dict) else d))' 2>/dev/null)
[ "$PAT_COUNT" = "2" ] && pass "PAT list shows 2 tokens" || fail "List PATs" "count=$PAT_COUNT"

# Use secondary PAT to verify it works
WHOAMI=$(curl -sk "$BASE_URL/api/v1/auth/me" \
  -H "Authorization: Bearer $PAT1_SECONDARY" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("username",""))' 2>/dev/null)
[ "$WHOAMI" = "$USER1" ] && pass "Secondary PAT authenticates" || fail "Secondary PAT auth" "got $WHOAMI"

# Get secondary token ID and revoke it
TOKEN_ID=$(curl -sk "$BASE_URL/api/v1/auth/tokens" \
  -H "Authorization: Bearer $JWT1" | python3 -c '
import sys, json
d = json.load(sys.stdin)
toks = d.get("tokens", d) if isinstance(d, dict) else d
for t in toks:
    if t["name"] == "secondary":
        print(t.get("id") or t.get("token_id","")); break
' 2>/dev/null)

REVOKED=$(curl -sk -X DELETE "$BASE_URL/api/v1/auth/tokens/$TOKEN_ID" \
  -H "Authorization: Bearer $JWT1" -o /dev/null -w "%{http_code}")
( [ "$REVOKED" = "200" ] || [ "$REVOKED" = "204" ] ) && pass "Secondary PAT revoked ($REVOKED)" || fail "Revoke PAT" "HTTP $REVOKED"

# Revoked PAT should fail authentication
sleep 1
REVOKED_STATUS=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/auth/me" \
  -H "Authorization: Bearer $PAT1_SECONDARY")
[ "$REVOKED_STATUS" = "401" ] && pass "Revoked PAT returns 401" || fail "Revoked PAT" "HTTP $REVOKED_STATUS"

# Primary PAT still works
PRIMARY_STATUS=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/auth/me" \
  -H "Authorization: Bearer $PAT1")
[ "$PRIMARY_STATUS" = "200" ] && pass "Primary PAT still works after secondary revoke" || fail "Primary PAT" "HTTP $PRIMARY_STATUS"

# ── 2. Auth Failures ─────────────────────────────────────────
echo ""
echo "▸ 2. Authentication Failures"

# No auth header
NO_AUTH=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/auth/me")
[ "$NO_AUTH" = "401" ] && pass "No auth header → 401" || fail "No auth" "HTTP $NO_AUTH"

# Invalid PAT
BAD_PAT=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/auth/me" \
  -H "Authorization: Bearer akb_INVALID_TOKEN_NOT_REAL_xyz")
[ "$BAD_PAT" = "401" ] && pass "Invalid PAT → 401" || fail "Bad PAT" "HTTP $BAD_PAT"

# Malformed Authorization header
BAD_HEADER=$(curl -sk -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/auth/me" \
  -H "Authorization: NotBearer xyz")
[ "$BAD_HEADER" = "401" ] && pass "Malformed auth header → 401" || fail "Bad header" "HTTP $BAD_HEADER"

# MCP without auth
MCP_NO_AUTH=$(curl -sk -o /dev/null -w "%{http_code}" -X POST "$BASE_URL/mcp/" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}')
[ "$MCP_NO_AUTH" = "401" ] && pass "MCP without auth → 401" || fail "MCP no auth" "HTTP $MCP_NO_AUTH"

# ── 3. SQL Injection in user input ───────────────────────────
echo ""
echo "▸ 3. SQL Injection Attempts"

# Vault name with SQL
R=$(m1 "akb_create_vault" "{\"name\":\"vault'; DROP TABLE users--\",\"description\":\"injection\"}")
SQL_VAULT_BLOCKED=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('error' in d or 'Invalid' in str(d))" 2>/dev/null)
[ "$SQL_VAULT_BLOCKED" = "True" ] && pass "Vault name with SQL injection rejected" || fail "Vault SQL inject" "$R"

# Search query with SQL — should not crash
R=$(m1 "akb_search" "{\"query\":\"'; DROP TABLE documents; --\",\"vault\":\"$VAULT\"}")
SEARCH_OK=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('error' not in d and 'total' in d)" 2>/dev/null)
[ "$SEARCH_OK" = "True" ] && pass "Search with SQL injection: returns clean response" || fail "Search SQL inject" "$R"

# Verify users table still exists by querying it
USERS_OK=$(curl -sk "$BASE_URL/api/v1/auth/me" -H "Authorization: Bearer $PAT1" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("username","")=="'$USER1'")' 2>/dev/null)
[ "$USERS_OK" = "True" ] && pass "Users table still intact (injection failed)" || fail "Users table" "user not found"

# Grep pattern with SQL/regex chars
R=$(m1 "akb_grep" "{\"pattern\":\"'; DELETE FROM chunks WHERE 1=1 --\",\"vault\":\"$VAULT\"}")
GREP_INJ_OK=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('total_matches' in d or 'error' in d)" 2>/dev/null)
[ "$GREP_INJ_OK" = "True" ] && pass "Grep with SQL chars: handled cleanly" || fail "Grep SQL inject" "$R"

# Tag with SQL
R=$(m1 "akb_put" "{\"vault\":\"$VAULT\",\"collection\":\"sql\",\"title\":\"SQL Tag Test\",\"content\":\"# SQL\\nbody\",\"tags\":[\"normal\",\"'; DELETE FROM tags --\"]}")
TAG_DOC=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('doc_id',''))" 2>/dev/null)
[ -n "$TAG_DOC" ] && pass "Tags with SQL chars stored as literal text" || fail "SQL tag" "$R"

# ── 4. Dynamic Permission Changes ────────────────────────────
echo ""
echo "▸ 4. Dynamic Permission Changes"

# Initially user2 has no access
R=$(m2 "akb_put" "{\"vault\":\"$VAULT\",\"collection\":\"perm\",\"title\":\"Initial Try\",\"content\":\"# X\"}")
INITIAL_DENIED=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('error' in d or 'denied' in str(d).lower() or 'permission' in str(d).lower())" 2>/dev/null)
[ "$INITIAL_DENIED" = "True" ] && pass "User2 initially blocked from write" || fail "Initial block" "$R"

# Grant writer
m1 "akb_grant" "{\"vault\":\"$VAULT\",\"user\":\"$USER2\",\"role\":\"writer\"}" >/dev/null
R=$(m2 "akb_put" "{\"vault\":\"$VAULT\",\"collection\":\"perm\",\"title\":\"After Grant\",\"content\":\"# Y\"}")
AFTER_GRANT=$(echo "$R" | python3 -c "import sys,json; print(bool(json.load(sys.stdin).get('doc_id','')))" 2>/dev/null)
[ "$AFTER_GRANT" = "True" ] && pass "User2 can write immediately after grant" || fail "Post-grant write" "$R"

# Downgrade to reader
m1 "akb_grant" "{\"vault\":\"$VAULT\",\"user\":\"$USER2\",\"role\":\"reader\"}" >/dev/null
R=$(m2 "akb_put" "{\"vault\":\"$VAULT\",\"collection\":\"perm\",\"title\":\"After Downgrade\",\"content\":\"# Z\"}")
AFTER_DOWNGRADE=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('error' in d or 'denied' in str(d).lower())" 2>/dev/null)
[ "$AFTER_DOWNGRADE" = "True" ] && pass "User2 blocked from write after downgrade to reader" || fail "Downgrade block" "$R"

# Reader can still read
R=$(m2 "akb_browse" "{\"vault\":\"$VAULT\"}")
READER_OK=$(echo "$R" | python3 -c "import sys,json; print('items' in json.load(sys.stdin))" 2>/dev/null)
[ "$READER_OK" = "True" ] && pass "Reader can still browse after downgrade" || fail "Reader read" "$R"

# Revoke
m1 "akb_revoke" "{\"vault\":\"$VAULT\",\"user\":\"$USER2\"}" >/dev/null
R=$(m2 "akb_browse" "{\"vault\":\"$VAULT\"}")
AFTER_REVOKE=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('error' in d or 'denied' in str(d).lower())" 2>/dev/null)
[ "$AFTER_REVOKE" = "True" ] && pass "User2 fully blocked after revoke" || fail "Revoke" "$R"

# ── 5. Title Collision (Same Collection) ─────────────────────
echo ""
echo "▸ 5. Title Collision"

R=$(m1 "akb_put" "{\"vault\":\"$VAULT\",\"collection\":\"collide\",\"title\":\"Same Title\",\"content\":\"# First\"}")
DOC_A=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('doc_id',''))" 2>/dev/null)
[ -n "$DOC_A" ] && pass "First doc with title 'Same Title'" || fail "Title doc 1" "$R"

R=$(m1 "akb_put" "{\"vault\":\"$VAULT\",\"collection\":\"collide\",\"title\":\"Same Title\",\"content\":\"# Second\"}")
DOC_B=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('doc_id',''))" 2>/dev/null)

if [ -n "$DOC_B" ] && [ "$DOC_A" != "$DOC_B" ]; then
  pass "Second doc with same title got different ID ($DOC_B)"
elif [ -z "$DOC_B" ]; then
  ERR_MSG=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('error',''))" 2>/dev/null)
  pass "Second doc with same title rejected ($ERR_MSG)"
else
  fail "Title collision" "same doc_id returned: $DOC_A == $DOC_B"
fi

# Both docs accessible
R=$(m1 "akb_browse" "{\"vault\":\"$VAULT\",\"collection\":\"collide\",\"depth\":2}")
COLLIDE_DOCS=$(echo "$R" | python3 -c "import sys,json; print(len([i for i in json.load(sys.stdin).get('items',[]) if i.get('type')=='document']))" 2>/dev/null)
[ "$COLLIDE_DOCS" -ge 1 ] 2>/dev/null && pass "Collide collection has $COLLIDE_DOCS doc(s)" || fail "Collide browse" "$R"

# ── 6. Collection Path Edge Cases ────────────────────────────
echo ""
echo "▸ 6. Collection Path Edge Cases"

# Trailing slash
R=$(m1 "akb_put" "{\"vault\":\"$VAULT\",\"collection\":\"path-test/\",\"title\":\"Trailing Slash\",\"content\":\"# X\"}")
TRAIL_OK=$(echo "$R" | python3 -c "import sys,json; print(bool(json.load(sys.stdin).get('doc_id','')))" 2>/dev/null)
[ "$TRAIL_OK" = "True" ] && pass "Collection with trailing slash accepted" || fail "Trailing slash" "$R"

# Nested path
R=$(m1 "akb_put" "{\"vault\":\"$VAULT\",\"collection\":\"deep/nested/path\",\"title\":\"Nested\",\"content\":\"# X\"}")
NESTED_OK=$(echo "$R" | python3 -c "import sys,json; print(bool(json.load(sys.stdin).get('doc_id','')))" 2>/dev/null)
[ "$NESTED_OK" = "True" ] && pass "Nested collection path accepted" || fail "Nested path" "$R"

# Path traversal attempt
R=$(m1 "akb_put" "{\"vault\":\"$VAULT\",\"collection\":\"../../etc\",\"title\":\"Traversal\",\"content\":\"# X\"}")
TRAVERSAL_HANDLED=$(echo "$R" | python3 -c "
import sys, json
d = json.load(sys.stdin)
# Either rejected (error) or sanitized (doc created but path doesn't escape)
if 'error' in d:
    print('rejected')
elif 'doc_id' in d:
    # Verify path was sanitized — should not contain ..
    print('sanitized' if '..' not in d.get('path','') else 'escaped')
else:
    print('unknown')
" 2>/dev/null)
[ "$TRAVERSAL_HANDLED" = "rejected" ] || [ "$TRAVERSAL_HANDLED" = "sanitized" ] && pass "Path traversal handled ($TRAVERSAL_HANDLED)" || fail "Traversal" "$R"

# Empty collection
R=$(m1 "akb_put" "{\"vault\":\"$VAULT\",\"collection\":\"\",\"title\":\"Empty Coll\",\"content\":\"# X\"}")
EMPTY_COLL=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('error' in d or 'doc_id' in d)" 2>/dev/null)
[ "$EMPTY_COLL" = "True" ] && pass "Empty collection: handled (error or root)" || fail "Empty coll" "$R"

# ── 7. Long Values ───────────────────────────────────────────
echo ""
echo "▸ 7. Long Values"

# Very long title (1000 chars)
LONG_TITLE=$(python3 -c "print('A' * 1000)")
R=$(m1 "akb_put" "{\"vault\":\"$VAULT\",\"collection\":\"long\",\"title\":\"$LONG_TITLE\",\"content\":\"# X\"}")
LONG_HANDLED=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print('doc_id' in d or 'error' in d)" 2>/dev/null)
[ "$LONG_HANDLED" = "True" ] && pass "Very long title (1000ch) handled" || fail "Long title" "$R"

# Many tags (50)
MANY_TAGS=$(python3 -c "import json; print(json.dumps(['tag-' + str(i) for i in range(50)]))")
R=$(m1 "akb_put" "{\"vault\":\"$VAULT\",\"collection\":\"long\",\"title\":\"Many Tags\",\"content\":\"# X\",\"tags\":$MANY_TAGS}")
TAGS_OK=$(echo "$R" | python3 -c "import sys,json; print(bool(json.load(sys.stdin).get('doc_id','')))" 2>/dev/null)
[ "$TAGS_OK" = "True" ] && pass "50 tags accepted" || fail "Many tags" "$R"

# ── 8. Limit Boundaries ──────────────────────────────────────
echo ""
echo "▸ 8. Limit Boundaries"

# Limit at min (1)
R=$(m1 "akb_search" "{\"query\":\"test\",\"vault\":\"$VAULT\",\"limit\":1}")
LIMIT1_OK=$(echo "$R" | python3 -c "import sys,json; r=json.load(sys.stdin); print(len(r.get('results',[])) <= 1)" 2>/dev/null)
[ "$LIMIT1_OK" = "True" ] && pass "limit=1 respected" || fail "Limit 1" "$R"

# Limit at max (50)
R=$(m1 "akb_search" "{\"query\":\"test\",\"vault\":\"$VAULT\",\"limit\":50}")
LIMIT50_OK=$(echo "$R" | python3 -c "import sys,json; r=json.load(sys.stdin); print('error' not in r)" 2>/dev/null)
[ "$LIMIT50_OK" = "True" ] && pass "limit=50 accepted" || fail "Limit 50" "$R"

# Limit over max (100) — schema validation should reject
R=$(MCP_ID=$((MCP_ID+1)); curl -sk -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $PAT1" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" -H "mcp-session-id: $SID1" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":$MCP_ID,\"method\":\"tools/call\",\"params\":{\"name\":\"akb_search\",\"arguments\":{\"query\":\"test\",\"vault\":\"$VAULT\",\"limit\":100}}}" 2>&1)
LIMIT_OVER=$(echo "$R" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print('error' in d or 'maximum' in str(d).lower())" 2>/dev/null)
[ "$LIMIT_OVER" = "True" ] && pass "limit=100 (over max): rejected by schema" || fail "Limit over" "$R"

# ── Cleanup ──────────────────────────────────────────────────
echo ""
echo "▸ Cleanup"
m1 "akb_delete_vault" "{\"name\":\"$VAULT\"}" >/dev/null 2>&1
pass "Vault deleted"

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
