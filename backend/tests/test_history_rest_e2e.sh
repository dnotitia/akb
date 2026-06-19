#!/bin/bash
#
# AKB E2E: Document version history over HTTP REST.
#
# Covers GET /api/v1/history/{vault}/{doc} — the REST twin of the
# akb_history MCP tool. Both surfaces share DocumentService.history(), so
# this asserts parity: the version list, the `limit` bound, the created_at
# lineage boundary (recreate-at-same-path drops old commits), author_name
# resolution (git author username → display_name), and the access matrix
# (reader 200 / non-member 403 / unauth 401 / missing 404).
#
# Docs are created via the REST write path (POST /documents) so the git
# author is the actor's username — exercising the username branch of the
# id-OR-username author resolver. Bootstrap mirrors test_relations_rest_e2e.sh.
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
echo "║   Document History REST E2E              ║"
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

setup_mcp() {
  local pat=$1
  local tmpfile=$(mktemp)
  curl -sk -i -X POST "$BASE_URL/mcp/" \
    -H "Authorization: Bearer $pat" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"hist-rest-e2e","version":"1.0"}}}' > "$tmpfile" 2>/dev/null
  local sid=$(grep -i "mcp-session-id" "$tmpfile" | tr -d '\r' | awk '{print $2}')
  rm -f "$tmpfile"
  curl -sk -X POST "$BASE_URL/mcp/" \
    -H "Authorization: Bearer $pat" \
    -H "Content-Type: application/json" \
    -H "mcp-session-id: $sid" \
    -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null 2>&1
  echo "$sid"
}

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

# ── REST helpers (PAT-authenticated) ─────────────────────────
rput()    { curl -sk -X POST "$BASE_URL/api/v1/documents" -H "Authorization: Bearer $1" -H 'Content-Type: application/json' -d "$2"; }
rpatch()  { curl -sk -X PATCH "$BASE_URL/api/v1/documents/$2" -H "Authorization: Bearer $1" -H 'Content-Type: application/json' -d "$3"; }
rdeldoc() { curl -sk -X DELETE "$BASE_URL/api/v1/documents/$2" -H "Authorization: Bearer $1"; }
# GET /history/{vault}/{doc}[?query]
hget()      { curl -sk "$BASE_URL/api/v1/history/$2${3:-}" -H "Authorization: Bearer $1"; }
hget_code() { curl -sk -o /dev/null -w '%{http_code}' "$BASE_URL/api/v1/history/$2${3:-}" -H "Authorization: Bearer $1"; }

getpath()    { python3 -c "import sys,json; print(json.load(sys.stdin).get('path',''))" 2>/dev/null; }
hist_count() { python3 -c "import sys,json
try:
  print(len(json.load(sys.stdin).get('history',[])))
except Exception: print(-1)" 2>/dev/null; }
first_field() { python3 -c "import sys,json
try:
  h=json.load(sys.stdin).get('history',[]); print(h[0].get('$1','') if h else '')
except Exception: print('')" 2>/dev/null; }
all_annotated() { python3 -c "import sys,json
try:
  h=json.load(sys.stdin).get('history',[]); print(bool(h) and all('author_name' in e for e in h))
except Exception: print(False)" 2>/dev/null; }

USER1="hist-rest-u1-$(date +%s)"     # owner (writer) of the vault
USER2="hist-rest-u2-$(date +%s)"     # reader on the vault
USER3="hist-rest-u3-$(date +%s)"     # no access
PAT1=$(setup_user "$USER1")
PAT2=$(setup_user "$USER2")
PAT3=$(setup_user "$USER3")
[ -n "$PAT1" ] && [ -n "$PAT2" ] && [ -n "$PAT3" ] && pass "3 users created" || { fail "Setup" "user creation failed"; exit 1; }

SID1=$(setup_mcp "$PAT1")
m1() { mc "$PAT1" "$SID1" "$1" "$2" | mr; }

VAULT="hist-rest-$(date +%s)"
m1 "akb_create_vault" "{\"name\":\"$VAULT\",\"description\":\"history rest test\"}" >/dev/null
m1 "akb_grant" "{\"vault\":\"$VAULT\",\"user\":\"$USER2\",\"role\":\"reader\"}" >/dev/null
pass "vault created, USER2 granted reader"

# Create a doc via the REST write path (author = USER1 username), then
# update it so the history has >= 2 commits.
DOCPATH=$(rput "$PAT1" "{\"vault\":\"$VAULT\",\"collection\":\"history-test\",\"title\":\"History Doc\",\"content\":\"## V1\",\"slug\":\"hist-doc\"}" | getpath)
[ -n "$DOCPATH" ] && pass "doc created via POST /documents ($DOCPATH)" || { fail "Doc" "POST /documents returned no path"; exit 1; }
rpatch "$PAT1" "$VAULT/$DOCPATH" "{\"content\":\"## V2 updated\",\"message\":\"history rest e2e update\"}" >/dev/null
pass "doc updated (v2)"

# ── 1. GET /history — version list + entry shape ─────────────
echo ""
echo "▸ 1. GET /history (version list)"

R=$(hget "$PAT1" "$VAULT/$DOCPATH")
CNT=$(echo "$R" | hist_count)
[ "$CNT" -ge 2 ] 2>/dev/null && pass "lists >= 2 versions (count=$CNT)" || fail "history list" "expected >=2, got $CNT"

HASH=$(echo "$R" | first_field hash)
DATE=$(echo "$R" | first_field date)
[ -n "$HASH" ] && pass "entry carries a commit hash ($HASH)" || fail "entry hash" "missing"
[ -n "$DATE" ] && pass "entry carries a date" || fail "entry date" "missing"

# ── 2. author_name resolution (the new annotation) ───────────
echo ""
echo "▸ 2. author_name resolution"

AUTHOR=$(echo "$R" | first_field author)
AUTHORNAME=$(echo "$R" | first_field author_name)
[ "$AUTHOR" = "$USER1" ] && pass "raw git author is the actor username" || fail "author" "expected $USER1, got '$AUTHOR'"
# author_name is added ONLY by the resolver — raw file_log has no such key.
# Its presence proves the username branch matched; no display_name set on the
# user, so COALESCE(display_name, username) == username.
[ "$AUTHORNAME" = "$USER1" ] && pass "author_name resolved (username→display_name)" || fail "author_name" "expected $USER1, got '$AUTHORNAME'"
[ "$(echo "$R" | all_annotated)" = "True" ] && pass "every entry annotated with author_name" || fail "annotate all" "some entries missing author_name"

# ── 3. limit bound ───────────────────────────────────────────
echo ""
echo "▸ 3. limit query"

ONE=$(hget "$PAT1" "$VAULT/$DOCPATH" "?limit=1" | hist_count)
[ "$ONE" = "1" ] && pass "limit=1 returns exactly 1 entry" || fail "limit=1" "got $ONE"
CODE=$(hget_code "$PAT1" "$VAULT/$DOCPATH" "?limit=0")
[ "$CODE" = "422" ] && pass "limit=0 → 422 (below ge=1)" || fail "limit=0" "got $CODE"
CODE=$(hget_code "$PAT1" "$VAULT/$DOCPATH" "?limit=500")
[ "$CODE" = "422" ] && pass "limit=500 → 422 (above le=100)" || fail "limit=500" "got $CODE"

# ── 4. access matrix ─────────────────────────────────────────
echo ""
echo "▸ 4. access matrix"

CODE=$(hget_code "$PAT1" "$VAULT/$DOCPATH")
[ "$CODE" = "200" ] && pass "owner → 200" || fail "owner" "got $CODE"
CODE=$(hget_code "$PAT2" "$VAULT/$DOCPATH")
[ "$CODE" = "200" ] && pass "granted reader → 200" || fail "reader" "got $CODE"
CODE=$(hget_code "$PAT3" "$VAULT/$DOCPATH")
[ "$CODE" = "403" ] && pass "non-member → 403" || fail "non-member" "got $CODE"
CODE=$(curl -sk -o /dev/null -w '%{http_code}' "$BASE_URL/api/v1/history/$VAULT/$DOCPATH")
[ "$CODE" = "401" ] && pass "unauthenticated → 401" || fail "no auth" "got $CODE"

# ── 5. not found ─────────────────────────────────────────────
echo ""
echo "▸ 5. not found"

CODE=$(hget_code "$PAT1" "$VAULT/history-test/does-not-exist.md")
[ "$CODE" = "404" ] && pass "missing document → 404" || fail "missing doc" "got $CODE"
CODE=$(hget_code "$PAT1" "ghost-vault-$(date +%s)/history-test/hist-doc.md")
[ "$CODE" = "404" ] && pass "missing vault → 404" || fail "missing vault" "got $CODE"

# ── 6. created_at lineage boundary (parity with MCP akb_history T6) ─
# Delete the doc and recreate it at the same path; the new document's
# created_at trims the prior lineage so only the recreate commit shows.
# sleep 1 keeps the delete commit strictly older than the new created_at
# (the boundary is `committed_date >= created_at`, second-granular).
echo ""
echo "▸ 6. lineage boundary (recreate at same path)"

rdeldoc "$PAT1" "$VAULT/$DOCPATH" >/dev/null
sleep 1
rput "$PAT1" "{\"vault\":\"$VAULT\",\"collection\":\"history-test\",\"title\":\"History Doc\",\"content\":\"## reborn\",\"slug\":\"hist-doc\"}" >/dev/null
AFTER=$(hget "$PAT1" "$VAULT/$DOCPATH" | hist_count)
[ "$AFTER" = "1" ] && pass "recreate → count=1 (old commits trimmed, was $CNT)" || fail "lineage" "expected 1, got $AFTER"

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
