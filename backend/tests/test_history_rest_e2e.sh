#!/bin/bash
#
# AKB E2E: Activity, recent changes, document history, and diff over REST.
#
# Pins the public OpenAPI contract and runtime envelopes for GET /activity,
# /recent, /history, and /diff while retaining history lineage, author,
# filtering, visibility, and access-boundary coverage.
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
echo "║   Activity / History / Diff REST E2E     ║"
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
aget()      { curl -sk "$BASE_URL/api/v1/activity/$2${3:-}" -H "Authorization: Bearer $1"; }
aget_code() { curl -sk -o /dev/null -w '%{http_code}' "$BASE_URL/api/v1/activity/$2${3:-}" -H "Authorization: Bearer $1"; }
rget()      { curl -sk "$BASE_URL/api/v1/recent${2:-}" -H "Authorization: Bearer $1"; }
rget_code() { curl -sk -o /dev/null -w '%{http_code}' "$BASE_URL/api/v1/recent${2:-}" -H "Authorization: Bearer $1"; }
dget()      { curl -sk "$BASE_URL/api/v1/diff/$2?commit=$3" -H "Authorization: Bearer $1"; }
dget_code() { curl -sk -o /dev/null -w '%{http_code}' "$BASE_URL/api/v1/diff/$2?commit=$3" -H "Authorization: Bearer $1"; }

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
json_field() { python3 -c "import sys,json; print(json.load(sys.stdin).get('$1',''))" 2>/dev/null; }
contains_vault() { python3 -c "import sys,json; print(any(x.get('vault') == '$1' for x in json.load(sys.stdin).get('changes',[])))" 2>/dev/null; }

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

# ── 1. Public OpenAPI contract ───────────────────────────────
echo ""
echo "▸ 1. public OpenAPI contract"

OPENAPI=$(curl -sk "$BASE_URL/openapi.json")
OPENAPI_RESULT=$(echo "$OPENAPI" | python3 -c '
import json, sys
s = json.load(sys.stdin)
expected = {
  "/api/v1/activity/{vault}": ("activityList", "activity", "AkbActivityEnvelope", "activity"),
  "/api/v1/recent": ("activityRecent", "activity", "AkbRecentChangesEnvelope", "recent_changes"),
  "/api/v1/history/{vault}/{doc_id}": ("documentsHistory", "documents", "AkbDocumentHistoryEnvelope", "document_history"),
  "/api/v1/diff/{vault}/{doc_id}": ("documentsDiff", "documents", "AkbDocumentDiffEnvelope", "document_diff"),
}
schemas = s["components"]["schemas"]
union = schemas["AkbSuccessEnvelope"]
ids = []
for item in s["paths"].values():
  ids.extend(op.get("operationId") for method, op in item.items() if method in {"get","post","put","patch","delete"})
for path, (op_id, tag, model, kind) in expected.items():
  op = s["paths"][path]["get"]
  assert op["operationId"] == op_id and ids.count(op_id) == 1
  assert op["tags"] == [tag]
  assert op["responses"]["200"]["content"]["application/json"]["schema"] == {"$ref": f"#/components/schemas/{model}"}
  for status in ("400","401","403","404","409","422","500"):
    assert op["responses"][status]["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/AkbError"}
  leaf = schemas[model]
  assert "kind" in leaf["required"] and leaf["properties"]["kind"]["enum"] == [kind]
  ref = f"#/components/schemas/{model}"
  assert sum(x.get("$ref") == ref for x in union["oneOf"]) == 1
  assert union["discriminator"]["mapping"][kind] == ref
assert schemas["ActivityEntry"]["properties"]["files"]["items"]["$ref"].endswith("ActivityFileChange")
assert schemas["DocumentHistoryEntry"]["properties"]["date"]["format"] == "date-time"
assert schemas["AkbDocumentDiffEnvelope"]["properties"]["diff"]["type"] == "string"
print("PASS")')
[ "$OPENAPI_RESULT" = "PASS" ] && pass "four operations expose exact typed OpenAPI contracts" || fail "OpenAPI" "contract mismatch"

# ── 2. GET /history — version list + entry shape ─────────────
echo ""
echo "▸ 2. GET /history (version list)"

R=$(hget "$PAT1" "$VAULT/$DOCPATH")
[ "$(echo "$R" | json_field kind)" = "document_history" ] && pass "history kind=document_history" || fail "history kind" "missing or wrong"
CNT=$(echo "$R" | hist_count)
[ "$CNT" -ge 2 ] 2>/dev/null && pass "lists >= 2 versions (count=$CNT)" || fail "history list" "expected >=2, got $CNT"

HASH=$(echo "$R" | first_field hash)
DATE=$(echo "$R" | first_field date)
[ -n "$HASH" ] && pass "entry carries a commit hash ($HASH)" || fail "entry hash" "missing"
[ -n "$DATE" ] && pass "entry carries a date" || fail "entry date" "missing"

# ── 3. author_name resolution ────────────────────────────────
echo ""
echo "▸ 3. author_name resolution"

AUTHOR=$(echo "$R" | first_field author)
AUTHORNAME=$(echo "$R" | first_field author_name)
[ "$AUTHOR" = "$USER1" ] && pass "raw git author is the actor username" || fail "author" "expected $USER1, got '$AUTHOR'"
# author_name is added ONLY by the resolver — raw file_log has no such key.
# Its presence proves the username branch matched; no display_name set on the
# user, so COALESCE(display_name, username) == username.
[ "$AUTHORNAME" = "$USER1" ] && pass "author_name resolved (username→display_name)" || fail "author_name" "expected $USER1, got '$AUTHORNAME'"
[ "$(echo "$R" | all_annotated)" = "True" ] && pass "every entry annotated with author_name" || fail "annotate all" "some entries missing author_name"

# ── 4. activity filters and envelope ─────────────────────────
echo ""
echo "▸ 4. activity filters and envelope"

A=$(aget "$PAT1" "$VAULT")
[ "$(echo "$A" | json_field kind)" = "activity" ] && pass "activity kind=activity" || fail "activity kind" "missing or wrong"
ACNT=$(echo "$A" | python3 -c 'import sys,json; print(len(json.load(sys.stdin).get("activity",[])))' 2>/dev/null)
[ "$ACNT" -ge 2 ] 2>/dev/null && pass "activity lists document commits" || fail "activity list" "expected >=2, got $ACNT"
[ "$(aget "$PAT1" "$VAULT" "?limit=1" | python3 -c 'import sys,json; print(len(json.load(sys.stdin).get("activity",[])))')" = "1" ] && pass "activity limit=1" || fail "activity limit" "not enforced"
[ "$(aget "$PAT1" "$VAULT" "?collection=history-test" | python3 -c 'import sys,json; print(len(json.load(sys.stdin).get("activity",[])))')" -ge 2 ] 2>/dev/null && pass "activity collection filter" || fail "activity collection" "expected matching commits"
[ "$(aget "$PAT1" "$VAULT" "?author=$USER1" | python3 -c 'import sys,json; print(len(json.load(sys.stdin).get("activity",[])))')" -ge 2 ] 2>/dev/null && pass "activity author filter" || fail "activity author" "expected matching commits"
[ "$(aget "$PAT1" "$VAULT" "?since=2030-01-01T00:00:00Z" | python3 -c 'import sys,json; print(len(json.load(sys.stdin).get("activity",[])))')" = "0" ] && pass "activity since filter" || fail "activity since" "future bound should be empty"

# ── 5. recent visibility and envelope ────────────────────────
echo ""
echo "▸ 5. recent visibility and envelope"

RECENT1=$(rget "$PAT1" "?vault=$VAULT")
[ "$(echo "$RECENT1" | json_field kind)" = "recent_changes" ] && pass "recent kind=recent_changes" || fail "recent kind" "missing or wrong"
[ "$(echo "$RECENT1" | contains_vault "$VAULT")" = "True" ] && pass "owner sees private vault recent changes" || fail "recent owner" "vault missing"
[ "$(rget "$PAT2" "?vault=$VAULT" | contains_vault "$VAULT")" = "True" ] && pass "granted reader sees recent changes" || fail "recent reader" "vault missing"
[ "$(rget "$PAT3" | contains_vault "$VAULT")" = "False" ] && pass "private vault does not leak via global recent" || fail "recent privacy" "private vault leaked"
[ "$(rget_code "$PAT3" "?vault=$VAULT")" = "403" ] && pass "non-member private recent filter → 403" || fail "recent private access" "expected 403"
m1 "akb_set_public" "{\"vault\":\"$VAULT\",\"level\":\"reader\"}" >/dev/null
[ "$(rget "$PAT3" | contains_vault "$VAULT")" = "True" ] && pass "public reader sees vault in global recent" || fail "recent public" "public vault missing"
m1 "akb_set_public" "{\"vault\":\"$VAULT\",\"level\":\"none\"}" >/dev/null

# ── 6. diff normal/unknown compatibility ─────────────────────
echo ""
echo "▸ 6. document diff"

DIFF=$(dget "$PAT1" "$VAULT/$DOCPATH" "$HASH")
[ "$(echo "$DIFF" | json_field kind)" = "document_diff" ] && pass "diff kind=document_diff" || fail "diff kind" "missing or wrong"
[ "$(echo "$DIFF" | json_field type)" = "modified" ] && pass "known update commit → modified" || fail "known diff" "unexpected type"
UNKNOWN=$(dget "$PAT1" "$VAULT/$DOCPATH" "not-a-commit")
[ "$(echo "$UNKNOWN" | json_field type)" = "unknown" ] && [ "$(echo "$UNKNOWN" | json_field error)" = "commit not found" ] && pass "unknown commit stays HTTP 200 unknown+error" || fail "unknown diff" "compatibility response changed"
[ "$(dget_code "$PAT1" "$VAULT/$DOCPATH" "not-a-commit")" = "200" ] && pass "unknown commit status remains 200" || fail "unknown diff status" "expected 200"
[ "$(echo "$DIFF" | python3 -c 'import sys,json; print("error" in json.load(sys.stdin))')" = "False" ] && pass "normal diff omits optional error" || fail "diff error absence" "error should be absent"

# ── 7. query bounds ──────────────────────────────────────────
echo ""
echo "▸ 7. query bounds"

ONE=$(hget "$PAT1" "$VAULT/$DOCPATH" "?limit=1" | hist_count)
[ "$ONE" = "1" ] && pass "limit=1 returns exactly 1 entry" || fail "limit=1" "got $ONE"
CODE=$(hget_code "$PAT1" "$VAULT/$DOCPATH" "?limit=0")
[ "$CODE" = "422" ] && pass "limit=0 → 422 (below ge=1)" || fail "limit=0" "got $CODE"
CODE=$(hget_code "$PAT1" "$VAULT/$DOCPATH" "?limit=500")
[ "$CODE" = "422" ] && pass "limit=500 → 422 (above le=100)" || fail "limit=500" "got $CODE"

# ── 8. access matrix ─────────────────────────────────────────
echo ""
echo "▸ 8. access matrix"

CODE=$(hget_code "$PAT1" "$VAULT/$DOCPATH")
[ "$CODE" = "200" ] && pass "owner → 200" || fail "owner" "got $CODE"
CODE=$(hget_code "$PAT2" "$VAULT/$DOCPATH")
[ "$CODE" = "200" ] && pass "granted reader → 200" || fail "reader" "got $CODE"
CODE=$(hget_code "$PAT3" "$VAULT/$DOCPATH")
[ "$CODE" = "403" ] && pass "non-member → 403" || fail "non-member" "got $CODE"
CODE=$(curl -sk -o /dev/null -w '%{http_code}' "$BASE_URL/api/v1/history/$VAULT/$DOCPATH")
[ "$CODE" = "401" ] && pass "unauthenticated → 401" || fail "no auth" "got $CODE"
[ "$(aget_code "$PAT2" "$VAULT")" = "200" ] && pass "activity reader → 200" || fail "activity reader" "expected 200"
[ "$(aget_code "$PAT3" "$VAULT")" = "403" ] && pass "activity non-member → 403" || fail "activity non-member" "expected 403"
[ "$(curl -sk -o /dev/null -w '%{http_code}' "$BASE_URL/api/v1/activity/$VAULT")" = "401" ] && pass "activity unauthenticated → 401" || fail "activity unauth" "expected 401"
[ "$(dget_code "$PAT2" "$VAULT/$DOCPATH" "$HASH")" = "200" ] && pass "diff reader → 200" || fail "diff reader" "expected 200"
[ "$(dget_code "$PAT3" "$VAULT/$DOCPATH" "$HASH")" = "403" ] && pass "diff non-member → 403" || fail "diff non-member" "expected 403"
[ "$(curl -sk -o /dev/null -w '%{http_code}' "$BASE_URL/api/v1/diff/$VAULT/$DOCPATH?commit=$HASH")" = "401" ] && pass "diff unauthenticated → 401" || fail "diff unauth" "expected 401"

# ── 9. not found ─────────────────────────────────────────────
echo ""
echo "▸ 9. not found"

CODE=$(hget_code "$PAT1" "$VAULT/history-test/does-not-exist.md")
[ "$CODE" = "404" ] && pass "missing document → 404" || fail "missing doc" "got $CODE"
CODE=$(hget_code "$PAT1" "ghost-vault-$(date +%s)/history-test/hist-doc.md")
[ "$CODE" = "404" ] && pass "missing vault → 404" || fail "missing vault" "got $CODE"
[ "$(dget_code "$PAT1" "$VAULT/history-test/does-not-exist.md" "$HASH")" = "404" ] && pass "diff missing document → 404" || fail "diff missing doc" "expected 404"
[ "$(aget_code "$PAT1" "ghost-vault-$(date +%s)")" = "404" ] && pass "activity missing vault → 404" || fail "activity missing vault" "expected 404"

# ── 10. created_at lineage boundary (parity with MCP akb_history T6) ─
# Delete the doc and recreate it at the same path; the new document's
# created_at trims the prior lineage so only the recreate commit shows.
# sleep 1 keeps the delete commit strictly older than the new created_at
# (the boundary is `committed_date >= created_at`, second-granular).
echo ""
echo "▸ 10. lineage boundary (recreate at same path)"

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
