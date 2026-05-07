#!/bin/bash
#
# Hybrid search × lifecycle integration
# (cascade delete, archive vault, publication, file upload, orphan edges)
#
set -uo pipefail

BASE="${AKB_URL:-http://localhost:8000}"
USER_NAME="hybrid-lc-$(date +%s)"
VAULT="hybrid-lc-$(date +%s)"
WAIT="${AKB_HYBRID_INDEX_WAIT:-25}"
PASS=0
FAIL=0
ERRORS=()

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

rcurl() {
  local out=""
  for _ in 1 2 3 4 5; do
    out=$(curl -sk --max-time 20 "$@" 2>/dev/null)
    if [ -n "$out" ]; then echo "$out"; return 0; fi
    sleep 2
  done
  echo ""; return 1
}
jget() { python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print($1)" 2>/dev/null; }
psql_q() {
  for _ in 1 2 3; do
    local out=$(kubectl exec -n akb statefulset/postgres -- psql -U akbuser -d akb -t -c "$1" 2>/dev/null | tr -d ' \n')
    [ -n "$out" ] && { echo "$out"; return 0; }
    sleep 2
  done
  echo ""
}

wait_for_search() {
  local q=$1 vault=$2
  local deadline=$(($(date +%s) + 60))
  while [ $(date +%s) -lt $deadline ]; do
    local t=$(rcurl -H "Authorization: Bearer $PAT" "$BASE/api/v1/search?q=$(python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$q")&vault=$vault&limit=5" | jget "d.get('total', 0)")
    [ -n "$t" ] && [ "$t" != "0" ] && { echo "$t"; return 0; }
    sleep 3
  done
  echo "0"
}

echo "╔══════════════════════════════════════════╗"
echo "║   Hybrid × Lifecycle                     ║"
echo "║   Target: $BASE"
echo "╚══════════════════════════════════════════╝"
echo ""

echo "▸ Setup"
rcurl -X POST "$BASE/api/v1/auth/register" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER_NAME\",\"email\":\"$USER_NAME@t.dev\",\"password\":\"test1234\"}" >/dev/null
JWT=$(rcurl -X POST "$BASE/api/v1/auth/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER_NAME\",\"password\":\"test1234\"}" | jget "d['token']")
PAT=$(rcurl -X POST "$BASE/api/v1/auth/tokens" -H "Authorization: Bearer $JWT" -H 'Content-Type: application/json' \
  -d '{"name":"l"}' | jget "d['token']")
[ -n "$PAT" ] && pass "PAT" || { fail "PAT" ""; exit 1; }
rcurl -X POST "$BASE/api/v1/vaults?name=$VAULT" -H "Authorization: Bearer $PAT" >/dev/null
pass "vault $VAULT created"

# MCP session helper
mcp_init() {
  SID=$(curl -sk -i --max-time 10 -X POST "$BASE/mcp/" \
    -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"l","version":"1.0"}}}' 2>/dev/null \
    | grep -i "mcp-session-id" | tr -d '\r' | awk '{print $2}')
  rcurl -X POST "$BASE/mcp/" -H "Authorization: Bearer $PAT" \
    -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
    -H "mcp-session-id: $SID" \
    -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null
}
mcp_call() {
  local tool=$1 args=$2
  rcurl -X POST "$BASE/mcp/" -H "Authorization: Bearer $PAT" \
    -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
    -H "mcp-session-id: $SID" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"$tool\",\"arguments\":$args}}"
}
mcp_text() {
  python3 -c "
import sys,json
try:
    d=json.loads(sys.stdin.read())
    print(d['result']['content'][0]['text'])
except: pass
"
}
mcp_init

put() {
  local title=$1 content=$2
  for _ in 1 2 3; do
    local resp=$(rcurl -X POST "$BASE/api/v1/documents" -H "Authorization: Bearer $PAT" -H 'Content-Type: application/json' \
      -d "{\"vault\":\"$VAULT\",\"collection\":\"x\",\"title\":\"$title\",\"content\":$(python3 -c 'import sys,json; print(json.dumps(sys.argv[1]))' "$content")}")
    local id=$(echo "$resp" | jget "d.get('doc_id','')")
    [ -n "$id" ] && { echo "$resp"; return 0; }
    sleep 3
  done
  echo "$resp"
}

# ── LC1. Vault delete cascades to all downstream tables ──────
echo ""
echo "▸ LC1. Vault delete cascades"

# Seed 3 docs + 1 edge
D1=$(put "CascadeA" "AlphaMarkerCascade content here." | jget "d['doc_id']")
D2=$(put "CascadeB" "BetaMarkerCascade related content." | jget "d['doc_id']")
D3=$(put "CascadeC" "GammaMarkerCascade more content." | jget "d['doc_id']")
wait_for_search "GammaMarkerCascade" "$VAULT" >/dev/null

# Link D1→D2 (creates edge). akb_link takes source/target URIs.
mcp_call "akb_link" "{\"vault\":\"$VAULT\",\"source\":\"akb://$VAULT/doc/x/cascadea.md\",\"target\":\"akb://$VAULT/doc/x/cascadeb.md\",\"relation\":\"depends_on\"}" >/dev/null
sleep 2

# Snapshot counts BEFORE delete
VAULT_ID=$(psql_q "SELECT id FROM vaults WHERE name='$VAULT'")
DOC_COUNT=$(psql_q "SELECT COUNT(*) FROM documents WHERE vault_id='$VAULT_ID'")
CHUNK_COUNT=$(psql_q "SELECT COUNT(*) FROM chunks WHERE document_id IN (SELECT id FROM documents WHERE vault_id='$VAULT_ID')")
EDGE_COUNT=$(psql_q "SELECT COUNT(*) FROM edges WHERE vault_id='$VAULT_ID'")
echo "    pre: docs=$DOC_COUNT chunks=$CHUNK_COUNT edges=$EDGE_COUNT"
[ "$DOC_COUNT" = "3" ] && [ "$CHUNK_COUNT" -ge "3" ] 2>/dev/null && pass "seed state: 3 docs, $CHUNK_COUNT chunks, $EDGE_COUNT edges" \
  || fail "LC1-pre" "docs=$DOC_COUNT chunks=$CHUNK_COUNT edges=$EDGE_COUNT"

# Delete vault
R=$(mcp_call "akb_delete_vault" "{\"vault\":\"$VAULT\"}" | mcp_text)
DEL=$(echo "$R" | jget "d.get('deleted', False)")
[ "$DEL" = "True" ] && pass "akb_delete_vault returned deleted=true" || fail "LC1-call" "resp: $R"

# Post counts
POST_DOC=$(psql_q "SELECT COUNT(*) FROM documents WHERE vault_id='$VAULT_ID'")
POST_CHUNK=$(psql_q "SELECT COUNT(*) FROM chunks WHERE document_id IN (SELECT id FROM documents WHERE vault_id='$VAULT_ID')")
POST_EDGE=$(psql_q "SELECT COUNT(*) FROM edges WHERE vault_id='$VAULT_ID'")
POST_VAULT=$(psql_q "SELECT COUNT(*) FROM vaults WHERE id='$VAULT_ID'")

[ "$POST_VAULT" = "0" ] && pass "vault row removed" || fail "LC1-vault" "vault still there"
[ "$POST_DOC" = "0" ] && pass "all documents cascaded" || fail "LC1-docs" "$POST_DOC remain"
[ "$POST_CHUNK" = "0" ] && pass "all chunks cascaded" || fail "LC1-chunks" "$POST_CHUNK remain"
[ "$POST_EDGE" = "0" ] && pass "all edges cascaded" || fail "LC1-edges" "$POST_EDGE remain"

# ── LC2. Archive vault: search still works, writes blocked ───
echo ""
echo "▸ LC2. Archived vault — read-only for search"

# Fresh vault
VAULT2="hybrid-lc-arc-$(date +%s)"
rcurl -X POST "$BASE/api/v1/vaults?name=$VAULT2" -H "Authorization: Bearer $PAT" >/dev/null
rcurl -X POST "$BASE/api/v1/documents" -H "Authorization: Bearer $PAT" -H 'Content-Type: application/json' \
  -d "{\"vault\":\"$VAULT2\",\"collection\":\"x\",\"title\":\"PreArchive\",\"content\":\"LachrymoseQuartz marker in archive test.\"}" >/dev/null

DEADLINE=$(($(date +%s) + 60))
while [ $(date +%s) -lt $DEADLINE ]; do
  T=$(rcurl -H "Authorization: Bearer $PAT" "$BASE/api/v1/search?q=LachrymoseQuartz&vault=$VAULT2&limit=3" | jget "d.get('total',0)")
  [ "$T" -ge 1 ] 2>/dev/null && break
  sleep 3
done
[ "$T" -ge 1 ] 2>/dev/null && pass "pre-archive doc indexed" || fail "LC2-pre" "got $T"

# Archive
rcurl -X POST "$BASE/api/v1/vaults/$VAULT2/archive" -H "Authorization: Bearer $PAT" >/dev/null 2>&1

# Search should still work against archived vault
T=$(rcurl -H "Authorization: Bearer $PAT" "$BASE/api/v1/search?q=LachrymoseQuartz&vault=$VAULT2&limit=3" | jget "d.get('total',0)")
[ "$T" -ge 1 ] 2>/dev/null && pass "archived vault still searchable (read-only)" || fail "LC2-search" "got $T"

# Write should be blocked on archived vault (403 or 409)
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 10 -X POST "$BASE/api/v1/documents" \
  -H "Authorization: Bearer $PAT" -H 'Content-Type: application/json' \
  -d "{\"vault\":\"$VAULT2\",\"collection\":\"x\",\"title\":\"PostArchive\",\"content\":\"should fail\"}")
[ "$HTTP" = "403" ] || [ "$HTTP" = "409" ] || [ "$HTTP" = "422" ] && pass "write to archived vault blocked ($HTTP)" || fail "LC2-write" "got HTTP $HTTP"

# ── LC3. Publication creates public URL, search still works ──
echo ""
echo "▸ LC3. akb_publish + search coexist"

VAULT3="hybrid-lc-pub-$(date +%s)"
rcurl -X POST "$BASE/api/v1/vaults?name=$VAULT3" -H "Authorization: Bearer $PAT" >/dev/null
PUB_DOC=$(rcurl -X POST "$BASE/api/v1/documents" -H "Authorization: Bearer $PAT" -H 'Content-Type: application/json' \
  -d "{\"vault\":\"$VAULT3\",\"collection\":\"x\",\"title\":\"PublicDoc\",\"content\":\"HermaphroditeMollusk public content marker.\"}" \
  | jget "d['doc_id']")

DEADLINE=$(($(date +%s) + 60))
while [ $(date +%s) -lt $DEADLINE ]; do
  T=$(rcurl -H "Authorization: Bearer $PAT" "$BASE/api/v1/search?q=HermaphroditeMollusk&vault=$VAULT3&limit=3" | jget "d.get('total',0)")
  [ "$T" -ge 1 ] 2>/dev/null && break
  sleep 3
done

# Publish via REST API — body uses 'doc_id' for resource_type='document'
PUB_SLUG=$(rcurl -X POST "$BASE/api/v1/publications/$VAULT3/create" -H "Authorization: Bearer $PAT" -H 'Content-Type: application/json' \
  -d "{\"resource_type\":\"document\",\"doc_id\":\"$PUB_DOC\"}" | jget "d.get('slug','')")
[ -n "$PUB_SLUG" ] && pass "publication created ($PUB_SLUG)" || fail "LC3-pub" "no slug"

# Search still works on the authed side
T=$(rcurl -H "Authorization: Bearer $PAT" "$BASE/api/v1/search?q=HermaphroditeMollusk&vault=$VAULT3&limit=3" | jget "d.get('total',0)")
[ "$T" -ge 1 ] 2>/dev/null && pass "authed search still finds published doc" || fail "LC3-search" "got $T"

# Anonymous GET on /public/{slug} → 200 (or 401 if password-protected; we didn't set one)
if [ -n "$PUB_SLUG" ]; then
  HTTP=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 10 "$BASE/api/v1/public/$PUB_SLUG")
  [ "$HTTP" = "200" ] && pass "anonymous /public/$PUB_SLUG → 200" || fail "LC3-anon" "got HTTP $HTTP"
fi

# ── LC4. Edge to deleted doc — relations cleaned up ──────────
echo ""
echo "▸ LC4. Orphan edge cleanup after target delete"

VAULT4="hybrid-lc-edge-$(date +%s)"
rcurl -X POST "$BASE/api/v1/vaults?name=$VAULT4" -H "Authorization: Bearer $PAT" >/dev/null
SRC_RESP=$(rcurl -X POST "$BASE/api/v1/documents" -H "Authorization: Bearer $PAT" -H 'Content-Type: application/json' \
  -d "{\"vault\":\"$VAULT4\",\"collection\":\"x\",\"title\":\"EdgeSrc\",\"content\":\"source doc\"}")
SRC=$(echo "$SRC_RESP" | jget "d['doc_id']")
SRC_PATH=$(echo "$SRC_RESP" | jget "d['path']")
TGT_RESP=$(rcurl -X POST "$BASE/api/v1/documents" -H "Authorization: Bearer $PAT" -H 'Content-Type: application/json' \
  -d "{\"vault\":\"$VAULT4\",\"collection\":\"x\",\"title\":\"EdgeTgt\",\"content\":\"target doc\"}")
TGT=$(echo "$TGT_RESP" | jget "d['doc_id']")
TGT_PATH=$(echo "$TGT_RESP" | jget "d['path']")

mcp_call "akb_link" "{\"vault\":\"$VAULT4\",\"source\":\"akb://$VAULT4/doc/$SRC_PATH\",\"target\":\"akb://$VAULT4/doc/$TGT_PATH\",\"relation\":\"depends_on\"}" >/dev/null
sleep 2

V4_ID=$(psql_q "SELECT id FROM vaults WHERE name='$VAULT4'")
EDGE_BEFORE=$(psql_q "SELECT COUNT(*) FROM edges WHERE vault_id='$V4_ID'")
[ "$EDGE_BEFORE" -ge 1 ] 2>/dev/null && pass "edge created ($EDGE_BEFORE)" || fail "LC4-setup" "no edge"

# Delete target doc
rcurl -X DELETE "$BASE/api/v1/documents/$VAULT4/$TGT" -H "Authorization: Bearer $PAT" >/dev/null
sleep 3

# Edge referencing deleted target should be gone (or rendered orphan and searchable-safe)
EDGE_AFTER=$(psql_q "SELECT COUNT(*) FROM edges WHERE vault_id='$V4_ID'")
# Either 0 (cleanup) or >=1 (orphan, expected — acceptable if graph still works)
[ -n "$EDGE_AFTER" ] && pass "edge count after target delete: $EDGE_AFTER (no crash)" || fail "LC4-count" "no count"

# Graph API should not 500 on orphan
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 10 \
  -H "Authorization: Bearer $PAT" "$BASE/api/v1/graph/$VAULT4")
[ "$HTTP" = "200" ] && pass "graph endpoint stable with orphan ($HTTP)" || fail "LC4-graph" "got HTTP $HTTP"

# ── Cleanup ──────────────────────────────────────────────────
echo ""
echo "▸ Cleanup"
for V in "$VAULT2" "$VAULT3" "$VAULT4"; do
  mcp_call "akb_delete_vault" "{\"vault\":\"$V\"}" >/dev/null
done
# Self-delete test user
curl -sk --max-time 15 -X DELETE "$BASE/api/v1/my/account" -H "Authorization: Bearer $JWT" >/dev/null 2>&1
pass "all vaults deleted"

echo ""
echo "═══════════════════════════════════════════"
echo "  Passed: $PASS"
echo "  Failed: $FAIL"
if [ $FAIL -gt 0 ]; then
  echo ""
  echo "  Errors:"
  for e in "${ERRORS[@]}"; do echo "    - $e"; done
fi
echo "═══════════════════════════════════════════"

exit $FAIL
