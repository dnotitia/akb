#!/bin/bash
#
# AKB E2E: actor → display-name resolution across read surfaces.
#
# Regression for the latent gap where author/created_by resolution only
# matched a user UUID. The document write path stores the actor's *username*
# (GIT_AUTHOR_NAME = agent_id = username), so the UUID-only resolvers returned
# nothing for app-authored content. The shared user_directory.resolve_display_names
# (id OR username) now backs every surface:
#   1. GET /api/v1/documents/{vault}/{doc}     → created_by_name
#   2. GET /api/v1/activity/{vault}            → activity[].author_name
#   3. GET /api/v1/history/{vault}/{doc}       → history[].author_name
#   4. GET /api/v1/public/{slug} (document)    → created_by_name (publication)
#
# A distinct display_name (set via PATCH /auth/me) is used so each assertion
# proves username→display_name resolution rather than just echoing the username.
# Bootstrap mirrors test_history_rest_e2e.sh.
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
echo "║   Author / display-name resolution E2E   ║"
echo "║   Target: $BASE_URL"
echo "╚══════════════════════════════════════════╝"
echo ""
echo "▸ 0. Setup"

DISPLAY="Younglo Display E2E"

# register → login → PAT; returns "PAT JWT"
setup_user() {
  local user=$1
  curl -sk -X POST "$BASE_URL/api/v1/auth/register" \
    -H 'Content-Type: application/json' \
    -d "{\"username\":\"$user\",\"email\":\"$user@test.dev\",\"password\":\"test1234\"}" >/dev/null 2>&1
  local jwt=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" \
    -H 'Content-Type: application/json' \
    -d "{\"username\":\"$user\",\"password\":\"test1234\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)
  local pat=$(curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" \
    -H "Authorization: Bearer $jwt" \
    -H 'Content-Type: application/json' \
    -d '{"name":"e2e"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)
  echo "$pat"
}

setup_mcp() {
  local pat=$1
  local tmpfile=$(mktemp)
  curl -sk -i -X POST "$BASE_URL/mcp/" \
    -H "Authorization: Bearer $pat" -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"author-res-e2e","version":"1.0"}}}' > "$tmpfile" 2>/dev/null
  local sid=$(grep -i "mcp-session-id" "$tmpfile" | tr -d '\r' | awk '{print $2}')
  rm -f "$tmpfile"
  curl -sk -X POST "$BASE_URL/mcp/" \
    -H "Authorization: Bearer $pat" -H "Content-Type: application/json" \
    -H "mcp-session-id: $sid" \
    -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null 2>&1
  echo "$sid"
}

mc() {
  local pat=$1 sid=$2 tool=$3 args=$4
  MCP_ID=$((MCP_ID+1))
  curl -sk -X POST "$BASE_URL/mcp/" \
    -H "Authorization: Bearer $pat" -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" -H "mcp-session-id: $sid" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":$MCP_ID,\"method\":\"tools/call\",\"params\":{\"name\":\"$tool\",\"arguments\":$args}}" 2>&1
}
mr() { python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(d['result']['content'][0]['text'])" 2>/dev/null; }

jget() { python3 -c "import sys,json
try:
  d=json.load(sys.stdin)
  for k in '$1'.split('.'):
    d = d[int(k)] if k.lstrip('-').isdigit() else d.get(k)
  print(d if d is not None else '')
except Exception: print('')" 2>/dev/null; }

USER1="authres-u1-$(date +%s)"
PAT1=$(setup_user "$USER1")
[ -n "$PAT1" ] && pass "user created" || { fail "Setup" "user creation failed"; exit 1; }

# Set a distinct display_name so resolution is provable (not username echo).
DN_OK=$(curl -sk -X PATCH "$BASE_URL/api/v1/auth/me" \
  -H "Authorization: Bearer $PAT1" -H 'Content-Type: application/json' \
  -d "{\"display_name\":\"$DISPLAY\"}" | jget display_name)
[ "$DN_OK" = "$DISPLAY" ] && pass "display_name set to '$DISPLAY'" || fail "display_name" "got '$DN_OK'"

SID1=$(setup_mcp "$PAT1")
VAULT="authres-$(date +%s)"
mc "$PAT1" "$SID1" "akb_create_vault" "{\"name\":\"$VAULT\",\"description\":\"author resolution test\"}" >/dev/null
pass "vault created"

# Create a doc via the REST write path → documents.created_by = USER1 username.
PUT=$(curl -sk -X POST "$BASE_URL/api/v1/documents" \
  -H "Authorization: Bearer $PAT1" -H 'Content-Type: application/json' \
  -d "{\"vault\":\"$VAULT\",\"collection\":\"authres\",\"title\":\"Author Doc\",\"content\":\"# Hi\",\"slug\":\"author-doc\"}")
DOCPATH=$(echo "$PUT" | jget path)
DOCURI=$(echo "$PUT" | jget uri)
[ -n "$DOCPATH" ] && [ -n "$DOCURI" ] && pass "doc created ($DOCPATH)" || { fail "Doc" "POST /documents returned no path/uri"; exit 1; }

# ── 1. GET /documents → created_by_name ──────────────────────
echo ""
echo "▸ 1. document created_by_name"
CBN=$(curl -sk "$BASE_URL/api/v1/documents/$VAULT/$DOCPATH" -H "Authorization: Bearer $PAT1" | jget created_by_name)
[ "$CBN" = "$DISPLAY" ] && pass "created_by_name resolves username→display_name" || fail "created_by_name" "expected '$DISPLAY', got '$CBN'"

# ── 2. GET /activity → author_name ───────────────────────────
echo ""
echo "▸ 2. activity author_name"
AN=$(curl -sk "$BASE_URL/api/v1/activity/$VAULT" -H "Authorization: Bearer $PAT1" | jget activity.0.author_name)
[ "$AN" = "$DISPLAY" ] && pass "activity[0].author_name resolves to display_name" || fail "activity author_name" "expected '$DISPLAY', got '$AN'"

# ── 3. GET /history → author_name (shared resolver, regression) ─
echo ""
echo "▸ 3. history author_name"
HN=$(curl -sk "$BASE_URL/api/v1/history/$VAULT/$DOCPATH" -H "Authorization: Bearer $PAT1" | jget history.0.author_name)
[ "$HN" = "$DISPLAY" ] && pass "history[0].author_name resolves to display_name" || fail "history author_name" "expected '$DISPLAY', got '$HN'"

# ── 4. publication created_by_name (public viewer) ───────────
echo ""
echo "▸ 4. publication created_by_name"
SLUG=$(curl -sk -X POST "$BASE_URL/api/v1/publications/$VAULT/create" \
  -H "Authorization: Bearer $PAT1" -H 'Content-Type: application/json' \
  -d "{\"resource_type\":\"document\",\"uri\":\"$DOCURI\"}" | jget slug)
if [ -n "$SLUG" ]; then
  pass "document published (slug=$SLUG)"
  PCBN=$(curl -sk "$BASE_URL/api/v1/public/$SLUG" | jget created_by_name)
  [ "$PCBN" = "$DISPLAY" ] && pass "public publication created_by_name resolves to display_name" || fail "pub created_by_name" "expected '$DISPLAY', got '$PCBN'"
else
  fail "publish" "no slug returned"
fi

# ── Cleanup ──────────────────────────────────────────────────
echo ""
echo "▸ Cleanup"
mc "$PAT1" "$SID1" "akb_delete_vault" "{\"name\":\"$VAULT\"}" >/dev/null 2>&1
pass "Vault deleted"

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
