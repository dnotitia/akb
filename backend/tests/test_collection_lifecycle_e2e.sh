#!/bin/bash
#
# AKB Collection REST Boundary E2E Tests
# Detailed MCP collection behavior lives in the authenticated SDK pytest suite;
# this shell lane retains the REST lifecycle and permission contracts.
#
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
VAULT="coll-life-$(date +%s)"
E2E_USER="coll-life-u1-$(date +%s)"
READER_USER="coll-life-u2-$(date +%s)"
PASS=0
FAIL=0
ERRORS=()

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

echo "╔══════════════════════════════════════════╗"
echo "║   AKB Collection Lifecycle E2E Tests     ║"
echo "║   Target: $BASE_URL/mcp/"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── 0. Setup: register user + get PAT ───────────────────────
echo "▸ 0. Setup"

curl -sk -X POST "$BASE_URL/api/v1/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$E2E_USER\",\"email\":\"$E2E_USER@test.dev\",\"password\":\"test1234\"}" >/dev/null 2>&1

JWT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$E2E_USER\",\"password\":\"test1234\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

PAT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" \
  -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' \
  -d '{"name":"coll-life-e2e"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

[ -n "$PAT" ] && pass "PAT acquired" || { fail "PAT" "could not get PAT"; exit 1; }

# Register a second user (reader) for the REST ACL test
curl -sk -X POST "$BASE_URL/api/v1/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$READER_USER\",\"email\":\"$READER_USER@test.dev\",\"password\":\"test1234\"}" >/dev/null 2>&1

JWT2=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$READER_USER\",\"password\":\"test1234\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

PAT2=$(curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" \
  -H "Authorization: Bearer $JWT2" \
  -H 'Content-Type: application/json' \
  -d '{"name":"coll-life-reader"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

[ -n "$PAT2" ] && pass "Reader PAT acquired" || { fail "Reader PAT" "could not get PAT"; exit 1; }

# ── 1. MCP Initialize ───────────────────────────────────────
echo ""
echo "▸ 1. MCP Initialize"

INIT_RESP=$(curl -sk -i -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"coll-life-e2e","version":"1.0"}}}' 2>&1)

SID=$(echo "$INIT_RESP" | grep -i "mcp-session-id" | tr -d '\r' | awk '{print $2}')
[ -n "$SID" ] && pass "Session ID received ($SID)" || { fail "Session ID" "missing"; exit 1; }

# Send initialized notification
curl -sk -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null 2>&1

# Helper: MCP tool call
MCP_ID=10
mcp_call() {
  local tool=$1 args=$2
  MCP_ID=$((MCP_ID+1))
  curl -sk -X POST "$BASE_URL/mcp/" \
    -H "Authorization: Bearer $PAT" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -H "mcp-session-id: $SID" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":$MCP_ID,\"method\":\"tools/call\",\"params\":{\"name\":\"$tool\",\"arguments\":$args}}" 2>&1
}

mcp_result() {
  python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(d['result']['content'][0]['text'])" 2>/dev/null
}

# ── 2. Vault Setup ──────────────────────────────────────────
echo ""
echo "▸ 2. Vault setup"

R=$(mcp_call akb_create_vault "{\"name\":\"$VAULT\",\"description\":\"collection lifecycle E2E\"}" | mcp_result)
VAULT_ID=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['vault_id'])" 2>/dev/null)
[ -n "$VAULT_ID" ] && pass "vault created ($VAULT)" || { fail "create_vault" "no vault_id"; exit 1; }

# ── 2b. Typed REST lifecycle contract ───────────────────────
echo ""
echo "▸ 2b. Typed REST lifecycle contract"

REST_BODY=$(mktemp)
trap 'rm -f "$REST_BODY"' EXIT

REST_HTTP=$(curl -sk -o "$REST_BODY" -w "%{http_code}" \
  -X POST "$BASE_URL/api/v1/collections/$VAULT" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d '{"path":"rest-contract","summary":null}' 2>/dev/null)
REST_CREATE=$(python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("kind"), d.get("ok"), d.get("created"), d.get("collection",{}).get("summary"), d.get("collection",{}).get("doc_count"))' <"$REST_BODY" 2>/dev/null)
[ "$REST_HTTP" = "200" ] && [ "$REST_CREATE" = "collection_create True True None 0" ] \
  && pass "REST create → typed collection_create with null summary and zero doc_count" \
  || fail "REST typed create" "http=$REST_HTTP values=$REST_CREATE; body=$(cat "$REST_BODY")"

REST_HTTP=$(curl -sk -o "$REST_BODY" -w "%{http_code}" \
  -X POST "$BASE_URL/api/v1/collections/$VAULT" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d '{"path":"rest-contract","summary":"ignored"}' 2>/dev/null)
REST_REPEAT=$(python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("kind"), d.get("created"), d.get("collection",{}).get("summary"), d.get("collection",{}).get("doc_count"))' <"$REST_BODY" 2>/dev/null)
[ "$REST_HTTP" = "200" ] && [ "$REST_REPEAT" = "collection_create False None 0" ] \
  && pass "REST idempotent create preserves stored summary and created=false" \
  || fail "REST idempotent create" "http=$REST_HTTP values=$REST_REPEAT; body=$(cat "$REST_BODY")"

R=$(mcp_call akb_put "{\"vault\":\"$VAULT\",\"collection\":\"rest-contract\",\"title\":\"RestContractDoc\",\"content\":\"## body\",\"type\":\"note\",\"tags\":[]}" | mcp_result)
REST_DOC_URI=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin)['uri'])" 2>/dev/null)
[ -n "$REST_DOC_URI" ] && pass "seeded REST contract collection" \
  || fail "REST contract seed" "no uri; raw=$R"

REST_HTTP=$(curl -sk -o "$REST_BODY" -w "%{http_code}" \
  -X DELETE "$BASE_URL/api/v1/collections/$VAULT/rest-contract" \
  -H "Authorization: Bearer $PAT" 2>/dev/null)
REST_409=$(python3 -c 'import json,sys; d=json.load(sys.stdin); x=d.get("details",{}); legacy=d.get("detail",{}); print(d.get("code"), x.get("doc_count"), x.get("file_count"), x.get("sub_collection_count"), x.get("table_count"), legacy.get("file_count"))' <"$REST_BODY" 2>/dev/null)
[ "$REST_HTTP" = "409" ] && [ "$REST_409" = "conflict 1 0 0 0 0" ] \
  && pass "REST non-empty delete → 409 details and legacy detail preserve all counts" \
  || fail "REST non-empty 409" "http=$REST_HTTP values=$REST_409; body=$(cat "$REST_BODY")"

REST_HTTP=$(curl -sk -o "$REST_BODY" -w "%{http_code}" \
  -X DELETE "$BASE_URL/api/v1/collections/$VAULT/rest-contract?recursive=true" \
  -H "Authorization: Bearer $PAT" 2>/dev/null)
REST_DELETE=$(python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("kind"), d.get("ok"), d.get("collection"), d.get("deleted_docs"), d.get("deleted_files"), d.get("deleted_sub_collections"), d.get("deleted_tables"))' <"$REST_BODY" 2>/dev/null)
[ "$REST_HTTP" = "200" ] && [ "$REST_DELETE" = "collection_delete True rest-contract 1 0 0 0" ] \
  && pass "REST recursive delete → typed collection_delete with exact counts" \
  || fail "REST typed recursive delete" "http=$REST_HTTP values=$REST_DELETE; body=$(cat "$REST_BODY")"

REST_HTTP=$(curl -sk -o "$REST_BODY" -w "%{http_code}" \
  -X DELETE "$BASE_URL/api/v1/collections/$VAULT/rest-contract" \
  -H "Authorization: Bearer $PAT" 2>/dev/null)
[ "$REST_HTTP" = "404" ] && pass "REST missing collection → 404" \
  || fail "REST missing collection 404" "http=$REST_HTTP; body=$(cat "$REST_BODY")"

# The REST ACL checks below need a live collection to target. Keep this MCP
# setup call as preparation; the lifecycle assertions themselves run through
# the REST surface or the SDK pytest suite.
mcp_call akb_create_collection "{\"vault\":\"$VAULT\",\"path\":\"keepempty\"}" | mcp_result >/dev/null

# ── 1. REST ACL — reader cannot create or delete ─────────────
echo ""
echo "▸ 1. REST ACL"

# Reader has NO access at all (no grant) — expect 403 on create
HTTP_CODE=$(curl -sk -o /dev/null -w "%{http_code}" \
  -X POST "$BASE_URL/api/v1/collections/$VAULT" \
  -H "Authorization: Bearer $PAT2" \
  -H 'Content-Type: application/json' \
  -d '{"path":"unauthorized"}' 2>/dev/null)
[ "$HTTP_CODE" = "403" ] && pass "REST POST /collections without access → 403" \
  || fail "REST POST 403" "got HTTP $HTTP_CODE (expected 403)"

# Grant reader role to user2 (still insufficient for write — should still 403)
R=$(mcp_call akb_grant "{\"vault\":\"$VAULT\",\"user\":\"$READER_USER\",\"role\":\"reader\"}" | mcp_result)
GRANTED=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('granted',False))" 2>/dev/null)
[ "$GRANTED" = "True" ] && pass "granted reader role to $READER_USER" \
  || fail "grant reader" "granted=$GRANTED; raw=$R"

HTTP_CODE=$(curl -sk -o /dev/null -w "%{http_code}" \
  -X POST "$BASE_URL/api/v1/collections/$VAULT" \
  -H "Authorization: Bearer $PAT2" \
  -H 'Content-Type: application/json' \
  -d '{"path":"reader-tried"}' 2>/dev/null)
[ "$HTTP_CODE" = "403" ] && pass "reader role REST POST → 403" \
  || fail "reader POST 403" "got HTTP $HTTP_CODE (expected 403)"

# Also verify DELETE is forbidden for reader
HTTP_CODE=$(curl -sk -o /dev/null -w "%{http_code}" \
  -X DELETE "$BASE_URL/api/v1/collections/$VAULT/keepempty" \
  -H "Authorization: Bearer $PAT2" 2>/dev/null)
[ "$HTTP_CODE" = "403" ] && pass "reader role REST DELETE → 403" \
  || fail "reader DELETE 403" "got HTTP $HTTP_CODE (expected 403)"

# ── 1b. Writer cannot bypass admin-only table deletion ───────
echo ""
echo "▸ 1b. Table deletion permission boundary"

# Promote the second account to Writer, then create a table inside a
# collection as the owner. Writer must be denied both by the dedicated table
# endpoint and by recursive collection deletion; the table must survive both.
R=$(mcp_call akb_grant "{\"vault\":\"$VAULT\",\"user\":\"$READER_USER\",\"role\":\"writer\"}" | mcp_result)
WRITER_GRANTED=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('granted',False))" 2>/dev/null)
[ "$WRITER_GRANTED" = "True" ] && pass "promoted second account to writer" \
  || fail "grant writer" "granted=$WRITER_GRANTED; raw=$R"

R=$(mcp_call akb_create_table "{\"vault\":\"$VAULT\",\"collection\":\"writer-guard\",\"name\":\"writer_guard_table\",\"columns\":[{\"name\":\"value\",\"type\":\"text\"}]}" | mcp_result)
GUARD_TABLE=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('name',''))" 2>/dev/null)
[ "$GUARD_TABLE" = "writer_guard_table" ] && pass "seeded table inside writer-guard collection" \
  || fail "seed guard table" "name=$GUARD_TABLE; raw=$R"

HTTP_CODE=$(curl -sk -o "$REST_BODY" -w "%{http_code}" \
  -X DELETE "$BASE_URL/api/v1/tables/$VAULT/writer_guard_table" \
  -H "Authorization: Bearer $PAT2" 2>/dev/null)
[ "$HTTP_CODE" = "403" ] && pass "writer direct table delete → 403" \
  || fail "writer table DELETE 403" "got HTTP $HTTP_CODE; body=$(cat "$REST_BODY")"

HTTP_CODE=$(curl -sk -o "$REST_BODY" -w "%{http_code}" \
  -X DELETE "$BASE_URL/api/v1/collections/$VAULT/writer-guard?recursive=true" \
  -H "Authorization: Bearer $PAT2" 2>/dev/null)
[ "$HTTP_CODE" = "403" ] && pass "writer recursive collection delete containing table → 403" \
  || fail "writer collection table bypass" "got HTTP $HTTP_CODE; body=$(cat "$REST_BODY")"

TABLE_SURVIVED=$(curl -sk "$BASE_URL/api/v1/tables/$VAULT" \
  -H "Authorization: Bearer $PAT" \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print(any(i.get("name")=="writer_guard_table" for i in d.get("items",[])))' 2>/dev/null)
[ "$TABLE_SURVIVED" = "True" ] && pass "table survives both writer denials" \
  || fail "table survives writer denial" "table missing after rejected operations"

HTTP_CODE=$(curl -sk -o "$REST_BODY" -w "%{http_code}" \
  -X DELETE "$BASE_URL/api/v1/collections/$VAULT/writer-guard?recursive=true" \
  -H "Authorization: Bearer $PAT" 2>/dev/null)
ADMIN_DELETE=$(python3 -c 'import sys,json; print(json.load(sys.stdin).get("deleted_tables", -1))' <"$REST_BODY" 2>/dev/null)
[ "$HTTP_CODE" = "200" ] && [ "$ADMIN_DELETE" = "1" ] \
  && pass "owner recursive collection delete removes one table" \
  || fail "owner collection table delete" "http=$HTTP_CODE deleted_tables=$ADMIN_DELETE; body=$(cat "$REST_BODY")"

# ── 2. Nested parent delete (prefix semantics) ───────────────
echo ""
echo "▸ 2. Nested parent delete"

# Create only "nested/inner" — no row at "nested" itself. This is the
# bug reproducer: the client tree synthesizes a parent that has no
# backing row.
R=$(mcp_call akb_create_collection "{\"vault\":\"$VAULT\",\"path\":\"nested/inner\"}" | mcp_result)
NP_OK=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('ok'))" 2>/dev/null)
[ "$NP_OK" = "True" ] && pass "created 'nested/inner' (parent has no row)" \
  || fail "create nested/inner" "ok=$NP_OK; raw=$R"

# DELETE /collections/<v>/nested (no recursive) → expect 409 with sub_collection_count >= 1
NP_HTTP=$(curl -sk -o /tmp/np_body.json -w "%{http_code}" \
  -X DELETE "$BASE_URL/api/v1/collections/$VAULT/nested" \
  -H "Authorization: Bearer $PAT" 2>/dev/null)
NP_SUB=$(python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("detail",{}).get("sub_collection_count", -1))' </tmp/np_body.json 2>/dev/null)
[ "$NP_HTTP" = "409" ] && [ "$NP_SUB" -ge 1 ] 2>/dev/null \
  && pass "REST DELETE 'nested' non-recursive → 409 sub_collection_count=$NP_SUB" \
  || fail "nested non-recursive 409" "http=$NP_HTTP sub_collection_count=$NP_SUB; body=$(cat /tmp/np_body.json)"

# DELETE /collections/<v>/nested?recursive=true → 200, deleted_sub_collections >= 1
NP_HTTP=$(curl -sk -o /tmp/np_body.json -w "%{http_code}" \
  -X DELETE "$BASE_URL/api/v1/collections/$VAULT/nested?recursive=true" \
  -H "Authorization: Bearer $PAT" 2>/dev/null)
NP_DSUB=$(python3 -c 'import sys,json; print(json.load(sys.stdin).get("deleted_sub_collections", -1))' </tmp/np_body.json 2>/dev/null)
[ "$NP_HTTP" = "200" ] && [ "$NP_DSUB" -ge 1 ] 2>/dev/null \
  && pass "REST DELETE 'nested' recursive → 200 deleted_sub_collections=$NP_DSUB" \
  || fail "nested recursive 200" "http=$NP_HTTP deleted_sub_collections=$NP_DSUB; body=$(cat /tmp/np_body.json)"

# Bonus: truly-missing path still returns 404 (NotFoundError invariant)
NP_HTTP=$(curl -sk -o /dev/null -w "%{http_code}" \
  -X DELETE "$BASE_URL/api/v1/collections/$VAULT/totally-absent" \
  -H "Authorization: Bearer $PAT" 2>/dev/null)
[ "$NP_HTTP" = "404" ] && pass "REST DELETE truly-missing path → 404" \
  || fail "truly-missing 404" "got HTTP $NP_HTTP (expected 404)"

# Clean up the ephemeral Vault even when an earlier assertion failed. The
# generated test users are intentionally left to the auth lifecycle suites.
R=$(mcp_call akb_delete_vault "{\"vault\":\"$VAULT\"}" | mcp_result)
CLEANED=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('deleted'))" 2>/dev/null)
[ "$CLEANED" = "True" ] && pass "ephemeral vault cleaned up" \
  || fail "cleanup vault" "deleted=$CLEANED; raw=$R"

# ── Summary ──────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════"
if [ $FAIL -eq 0 ]; then
  echo "✓ All $PASS tests passed"
else
  echo "✗ $FAIL failures (of $((PASS+FAIL)) total)"
  printf '  - %s\n' "${ERRORS[@]}"
fi
# Canonical summary — the CI runner parses this line for the counts.
echo "  Results: $PASS passed, $FAIL failed"
[ $FAIL -eq 0 ] || exit 1
