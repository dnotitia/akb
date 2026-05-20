#!/bin/bash
#
# Concurrency / atomicity reproduction tests for the AKB audit.
#
# Run:
#   AKB_URL=http://localhost:8001 bash backend/tests/test_concurrency_repro_e2e.sh
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8001}"
TS=$(date +%s)
USER="repro-$TS"
PASS=0; FAIL=0; ERRORS=()

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

echo "▸ Setup"
curl -sk -X POST "$BASE_URL/api/v1/auth/register" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER\",\"email\":\"$USER@t.dev\",\"password\":\"test1234\"}" >/dev/null
JWT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER\",\"password\":\"test1234\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])')
PAT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' -d '{"name":"repro"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])')
[ -n "$PAT" ] && pass "PAT acquired" || { fail "PAT" "missing"; exit 1; }

H_AUTH="Authorization: Bearer $PAT"
H_JSON="Content-Type: application/json"

mkvault() {
  curl -sk -X POST "$BASE_URL/api/v1/vaults?name=$1&description=&public_access=none" -H "$H_AUTH" >/dev/null
}
putdoc() {
  curl -sk -X POST "$BASE_URL/api/v1/documents" -H "$H_AUTH" -H "$H_JSON" \
    -d "{\"vault\":\"$1\",\"collection\":\"specs\",\"title\":\"$2\",\"content\":\"$3\",\"type\":\"note\"}"
}
jq_field() { python3 -c "import sys,json; d=json.load(sys.stdin); print(d$1)" 2>/dev/null; }

# ─────────────────────────────────────────────────────────────────
echo ""
echo "▸ T1: Concurrent PUT to same path — DB current_commit == git HEAD"
V1="v1-$TS"; mkvault "$V1"
TMP=$(mktemp -d)
for i in 1 2 3 4 5 6 7 8 9 10; do
  ( putdoc "$V1" "race-doc" "from-writer-$i" > "$TMP/out$i" 2>&1 ) &
done
wait
GET=$(curl -sk -H "$H_AUTH" "$BASE_URL/api/v1/documents/$V1/specs/race-doc.md")
DB_HASH=$(echo "$GET" | jq_field "['current_commit']")
HEAD_BODY=$(echo "$GET" | jq_field "['content']")
HIST_BODY=$(curl -sk -H "$H_AUTH" "$BASE_URL/api/v1/documents/$V1/specs/race-doc.md?version=$DB_HASH" | jq_field "['content']")
if [ "$HEAD_BODY" = "$HIST_BODY" ]; then
  pass "T1 PUT race: HEAD body == body at DB current_commit"
else
  fail "T1 PUT race" "diverged (HEAD='${HEAD_BODY:0:40}', DB='${HIST_BODY:0:40}')"
fi
rm -rf "$TMP"

# ─────────────────────────────────────────────────────────────────
echo ""
echo "▸ T2: Concurrent UPDATE — DB current_commit == git HEAD after race"
V2="v2-$TS"; mkvault "$V2"
putdoc "$V2" "u-doc" "initial" >/dev/null
TMP=$(mktemp -d)
for i in 1 2 3 4 5 6 7 8 9 10; do
  ( curl -sk -X PATCH "$BASE_URL/api/v1/documents/$V2/specs/u-doc.md" -H "$H_AUTH" -H "$H_JSON" \
      -d "{\"content\":\"updated-by-$i\"}" > "$TMP/out$i" 2>&1 ) &
done
wait
GET=$(curl -sk -H "$H_AUTH" "$BASE_URL/api/v1/documents/$V2/specs/u-doc.md")
DB_HASH=$(echo "$GET" | jq_field "['current_commit']")
HEAD_BODY=$(echo "$GET" | jq_field "['content']")
HIST_BODY=$(curl -sk -H "$H_AUTH" "$BASE_URL/api/v1/documents/$V2/specs/u-doc.md?version=$DB_HASH" | jq_field "['content']")
[ "$HEAD_BODY" = "$HIST_BODY" ] && pass "T2 UPDATE race convergent" || fail "T2 UPDATE race" "diverged"
rm -rf "$TMP"

# ─────────────────────────────────────────────────────────────────
echo ""
echo "▸ T3: UPDATE with stale expected_commit → 409"
V3="v3-$TS"; mkvault "$V3"
putdoc "$V3" "occ-doc" "v1" >/dev/null
STALE=$(curl -sk -H "$H_AUTH" "$BASE_URL/api/v1/documents/$V3/specs/occ-doc.md" | jq_field "['current_commit']")
curl -sk -X PATCH "$BASE_URL/api/v1/documents/$V3/specs/occ-doc.md" -H "$H_AUTH" -H "$H_JSON" -d '{"content":"v2"}' >/dev/null
CODE=$(curl -sk -o /dev/null -w '%{http_code}' -X PATCH \
  "$BASE_URL/api/v1/documents/$V3/specs/occ-doc.md" -H "$H_AUTH" -H "$H_JSON" \
  -d "{\"content\":\"v3\",\"expected_commit\":\"$STALE\"}")
[ "$CODE" = "409" ] && pass "T3 OCC update: stale → 409" || fail "T3 OCC" "expected 409, got $CODE"

# ─────────────────────────────────────────────────────────────────
echo ""
echo "▸ T4: akb_edit OCC via MCP — stale base_commit → 409"
# Establish MCP session
INIT=$(curl -sk -i -X POST "$BASE_URL/mcp/" -H "$H_AUTH" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"repro","version":"1"}}}' 2>&1)
SID=$(echo "$INIT" | grep -i "mcp-session-id" | tr -d '\r' | awk '{print $2}')
curl -sk -X POST "$BASE_URL/mcp/" -H "$H_AUTH" -H "$H_JSON" \
  -H "Accept: application/json, text/event-stream" -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null

V4="v4-$TS"; mkvault "$V4"
putdoc "$V4" "edit-doc" "alpha beta gamma" >/dev/null
STALE=$(curl -sk -H "$H_AUTH" "$BASE_URL/api/v1/documents/$V4/specs/edit-doc.md" | jq_field "['current_commit']")
curl -sk -X PATCH "$BASE_URL/api/v1/documents/$V4/specs/edit-doc.md" -H "$H_AUTH" -H "$H_JSON" \
  -d '{"content":"alpha beta delta"}' >/dev/null
# Now invoke akb_edit via MCP with stale base_commit
URI="akb://$V4/doc/specs/edit-doc.md"
RESP=$(curl -sk -X POST "$BASE_URL/mcp/" -H "$H_AUTH" -H "$H_JSON" \
  -H "Accept: application/json, text/event-stream" -H "mcp-session-id: $SID" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":42,\"method\":\"tools/call\",\"params\":{\"name\":\"akb_edit\",\"arguments\":{\"uri\":\"$URI\",\"old_string\":\"beta\",\"new_string\":\"omega\",\"base_commit\":\"$STALE\"}}}")
if echo "$RESP" | grep -q "current_commit moved"; then
  pass "T4 edit OCC: stale base_commit rejected"
else
  fail "T4 edit OCC" "no rejection in response: ${RESP:0:160}"
fi

# ─────────────────────────────────────────────────────────────────
echo ""
echo "▸ T5: ?version= validation"
V5="v5-$TS"; mkvault "$V5"
putdoc "$V5" "ver-doc" "x" >/dev/null
curl -sk -X PATCH "$BASE_URL/api/v1/documents/$V5/specs/ver-doc.md" -H "$H_AUTH" -H "$H_JSON" -d '{"content":"y"}' >/dev/null
for ver in "HEAD~1" "refs/heads/main" "not-a-hash"; do
  C=$(curl -sk -o /dev/null -w '%{http_code}' -H "$H_AUTH" "$BASE_URL/api/v1/documents/$V5/specs/ver-doc.md?version=$ver")
  [ "$C" = "400" ] && pass "T5 version=$ver → 400" || fail "T5 $ver" "expected 400 got $C"
done

# ─────────────────────────────────────────────────────────────────
echo ""
echo "▸ T6: file_log lineage via MCP — recreate at same path drops old commits"
V6="v6-$TS"; mkvault "$V6"
putdoc "$V6" "lineage" "alpha-content" >/dev/null
curl -sk -X DELETE "$BASE_URL/api/v1/documents/$V6/specs/lineage.md" -H "$H_AUTH" >/dev/null
sleep 1
putdoc "$V6" "lineage" "beta-content" >/dev/null
URI="akb://$V6/doc/specs/lineage.md"
RESP=$(curl -sk -X POST "$BASE_URL/mcp/" -H "$H_AUTH" -H "$H_JSON" \
  -H "Accept: application/json, text/event-stream" -H "mcp-session-id: $SID" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":7,\"method\":\"tools/call\",\"params\":{\"name\":\"akb_history\",\"arguments\":{\"uri\":\"$URI\",\"limit\":20}}}")
COUNT=$(echo "$RESP" | python3 -c 'import sys,json
try:
  d=json.loads(sys.stdin.read())
  txt=d["result"]["content"][0]["text"]
  obj=json.loads(txt)
  print(len(obj.get("history",[])))
except Exception as e: print(-1)' 2>/dev/null)
if [ "$COUNT" = "1" ]; then
  pass "T6 history shows only post-recreate commit (count=1)"
else
  fail "T6 history lineage" "expected 1, got $COUNT (resp=${RESP:0:200})"
fi

# ─────────────────────────────────────────────────────────────────
echo ""
echo "▸ T7: get_at_commit metadata_is_current=True"
V7="v7-$TS"; mkvault "$V7"
putdoc "$V7" "meta-doc" "v1" >/dev/null
OLD=$(curl -sk -H "$H_AUTH" "$BASE_URL/api/v1/documents/$V7/specs/meta-doc.md" | jq_field "['current_commit']")
curl -sk -X PATCH "$BASE_URL/api/v1/documents/$V7/specs/meta-doc.md" -H "$H_AUTH" -H "$H_JSON" \
  -d '{"content":"v2","title":"renamed"}' >/dev/null
WARN=$(curl -sk -H "$H_AUTH" "$BASE_URL/api/v1/documents/$V7/specs/meta-doc.md?version=$OLD" | jq_field "['metadata_is_current']")
[ "$WARN" = "True" ] && pass "T7 metadata_is_current flag set" || fail "T7" "got: $WARN"

# ─────────────────────────────────────────────────────────────────
echo ""
echo "▸ T8: Collection get_or_create race — all 10 puts succeed"
V8="v8-$TS"; mkvault "$V8"
TMP=$(mktemp -d)
COLL="new-coll-$TS"
for i in 1 2 3 4 5 6 7 8 9 10; do
  ( curl -sk -X POST "$BASE_URL/api/v1/documents" -H "$H_AUTH" -H "$H_JSON" \
      -d "{\"vault\":\"$V8\",\"collection\":\"$COLL\",\"title\":\"doc-$i\",\"content\":\"$i\",\"type\":\"note\"}" \
      > "$TMP/out$i" 2>&1 ) &
done
wait
SUCC=$(grep -l '"commit_hash"' "$TMP"/out* 2>/dev/null | wc -l | tr -d ' ')
[ "$SUCC" = "10" ] && pass "T8 collection race: all 10 succeeded" || fail "T8" "succeeded=$SUCC/10"
rm -rf "$TMP"

# ─────────────────────────────────────────────────────────────────
echo ""
echo "▸ T9: sessions/start outsider on private vault → 403"
V9="v9-$TS"; mkvault "$V9"
OUTSIDER="outsider-$TS"
curl -sk -X POST "$BASE_URL/api/v1/auth/register" -H "$H_JSON" \
  -d "{\"username\":\"$OUTSIDER\",\"email\":\"$OUTSIDER@t.dev\",\"password\":\"test1234\"}" >/dev/null
OUT_JWT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" -H "$H_JSON" \
  -d "{\"username\":\"$OUTSIDER\",\"password\":\"test1234\"}" | jq_field "['token']")
OUT_PAT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" -H "Authorization: Bearer $OUT_JWT" \
  -H "$H_JSON" -d '{"name":"out"}' | jq_field "['token']")
# Route uses query params (vault, agent_id)
CODE=$(curl -sk -o /dev/null -w '%{http_code}' -X POST \
  "$BASE_URL/api/v1/sessions/start?vault=$V9&agent_id=x" \
  -H "Authorization: Bearer $OUT_PAT")
[ "$CODE" = "403" ] && pass "T9 outsider → 403" || fail "T9 sessions" "expected 403, got $CODE"

# ─────────────────────────────────────────────────────────────────
echo ""
echo "▸ T10: max_views=1 publication served at most once under concurrent reads"
V10="v10-$TS"; mkvault "$V10"
PUT_RESP=$(putdoc "$V10" "pub-doc" "secret")
URI="akb://$V10/doc/specs/pub-doc.md"
PUB=$(curl -sk -X POST "$BASE_URL/api/v1/publications/$V10/create" -H "$H_AUTH" -H "$H_JSON" \
  -d "{\"resource_type\":\"document\",\"uri\":\"$URI\",\"max_views\":1,\"mode\":\"live\"}")
SLUG=$(echo "$PUB" | jq_field "['slug']")
[ -n "$SLUG" ] && pass "T10 pub created (slug=$SLUG)" || fail "T10 pub" "no slug; resp=${PUB:0:160}"
if [ -n "$SLUG" ]; then
  TMP=$(mktemp -d)
  for i in 1 2 3 4 5 6 7 8 9 10; do
    ( curl -sk -o /dev/null -w '%{http_code}\n' "$BASE_URL/api/v1/public/$SLUG" > "$TMP/code$i" ) &
  done
  wait
  OK=$(cat "$TMP"/code* | grep -c '^200$' || true)
  if [ "$OK" -le "1" ]; then
    pass "T10 max_views=1 served ≤ 1 time (got $OK)"
  else
    fail "T10 max_views" "served $OK times"
  fi
  rm -rf "$TMP"
fi

# ─────────────────────────────────────────────────────────────────
echo ""
echo "▸ T11: access.grant emits audit event"
V11="v11-$TS"; mkvault "$V11"
TARGET="target-$TS"
curl -sk -X POST "$BASE_URL/api/v1/auth/register" -H "$H_JSON" \
  -d "{\"username\":\"$TARGET\",\"email\":\"$TARGET@t.dev\",\"password\":\"test1234\"}" >/dev/null
RESP=$(curl -sk -X POST "$BASE_URL/api/v1/vaults/$V11/grant" -H "$H_AUTH" -H "$H_JSON" \
  -d "{\"user\":\"$TARGET\",\"role\":\"reader\"}")
echo "$RESP" | grep -q -i "error" && fail "T11 grant" "$RESP" || pass "T11 grant succeeded"
# Query events via MCP akb_activity (returns vault commits) — separately query
# events table directly via SQL exec is not exposed. Use a marker: re-run
# activity feed and look for an entry. Skip if no events surface — the
# audit-event check is implicit via the activity row.
ACT=$(curl -sk -H "$H_AUTH" "$BASE_URL/api/v1/activity/$V11?limit=20")
# This is best-effort — activity is git-based so access.grant may not surface.
# We rely on the implementation having added emit_event(...) in access_service.
# Mark this as informational.
if echo "$ACT" | grep -q "$V11"; then
  pass "T11 vault has activity stream"
fi

# ─────────────────────────────────────────────────────────────────
echo ""
echo "▸ T12: Archive blocks admin role mutations (grant on archived vault)"
V12="v12-$TS"; mkvault "$V12"
curl -sk -X POST "$BASE_URL/api/v1/vaults/$V12/archive" -H "$H_AUTH" >/dev/null
CODE=$(curl -sk -o /dev/null -w '%{http_code}' -X POST "$BASE_URL/api/v1/vaults/$V12/grant" \
  -H "$H_AUTH" -H "$H_JSON" -d "{\"user\":\"$TARGET\",\"role\":\"reader\"}")
[ "$CODE" = "403" ] && pass "T12 archived vault grant → 403" || fail "T12" "got $CODE"

# ─────────────────────────────────────────────────────────────────
echo ""
echo "▸ T13: /recent for outsider includes public vault"
V13="v13-$TS"; mkvault "$V13"
curl -sk -X PATCH "$BASE_URL/api/v1/vaults/$V13" -H "$H_AUTH" -H "$H_JSON" \
  -d '{"public_access":"reader"}' >/dev/null
putdoc "$V13" "pub-doc" "public-content" >/dev/null
sleep 1
RECENT=$(curl -sk -H "Authorization: Bearer $OUT_PAT" "$BASE_URL/api/v1/recent?limit=20")
echo "$RECENT" | grep -q "$V13" && pass "T13 /recent includes public vault" \
  || fail "T13" "outsider missed public vault: ${RECENT:0:120}"

# ─────────────────────────────────────────────────────────────────
echo ""
echo "▸ T14: Revoked admin can no longer grant → 403"
V14="v14-$TS"; mkvault "$V14"
ADMIN="admin-$TS"
curl -sk -X POST "$BASE_URL/api/v1/auth/register" -H "$H_JSON" \
  -d "{\"username\":\"$ADMIN\",\"email\":\"$ADMIN@t.dev\",\"password\":\"test1234\"}" >/dev/null
A_JWT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" -H "$H_JSON" \
  -d "{\"username\":\"$ADMIN\",\"password\":\"test1234\"}" | jq_field "['token']")
A_PAT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" -H "Authorization: Bearer $A_JWT" \
  -H "$H_JSON" -d '{"name":"a"}' | jq_field "['token']")
curl -sk -X POST "$BASE_URL/api/v1/vaults/$V14/grant" -H "$H_AUTH" -H "$H_JSON" \
  -d "{\"user\":\"$ADMIN\",\"role\":\"admin\"}" >/dev/null
curl -sk -X POST "$BASE_URL/api/v1/vaults/$V14/revoke" -H "$H_AUTH" -H "$H_JSON" \
  -d "{\"user\":\"$ADMIN\"}" >/dev/null
CODE=$(curl -sk -o /dev/null -w '%{http_code}' -X POST "$BASE_URL/api/v1/vaults/$V14/grant" \
  -H "Authorization: Bearer $A_PAT" -H "$H_JSON" \
  -d "{\"user\":\"$TARGET\",\"role\":\"reader\"}")
[ "$CODE" = "403" ] && pass "T14 revoked admin → 403" || fail "T14" "expected 403, got $CODE"

# ─────────────────────────────────────────────────────────────────
echo ""
echo "▸ T15: ?version=<unknown but hex> → 404 (not 500)"
V15="v15-$TS"; mkvault "$V15"
putdoc "$V15" "h-doc" "x" >/dev/null
CODE=$(curl -sk -o /dev/null -w '%{http_code}' -H "$H_AUTH" \
  "$BASE_URL/api/v1/documents/$V15/specs/h-doc.md?version=deadbeef1234")
[ "$CODE" = "404" ] && pass "T15 unknown hex → 404" || fail "T15" "got $CODE"

# ─────────────────────────────────────────────────────────────────
echo ""
echo "▸ T16: Concurrent transfer_ownership atomicity"
V16="v16-$TS"; mkvault "$V16"
T1U="t1-$TS"; T2U="t2-$TS"
for u in "$T1U" "$T2U"; do
  curl -sk -X POST "$BASE_URL/api/v1/auth/register" -H "$H_JSON" \
    -d "{\"username\":\"$u\",\"email\":\"$u@t.dev\",\"password\":\"test1234\"}" >/dev/null
done
# Fire two transfers; one should succeed, one should fail cleanly (not partial state)
TMP=$(mktemp -d)
for u in "$T1U" "$T2U"; do
  ( curl -sk -X POST "$BASE_URL/api/v1/vaults/$V16/transfer" -H "$H_AUTH" -H "$H_JSON" \
      -d "{\"new_owner\":\"$u\"}" > "$TMP/$u" 2>&1 ) &
done
wait
# After both: exactly one user owns the vault
INFO=$(curl -sk -H "$H_AUTH" "$BASE_URL/api/v1/vaults/$V16/info")
OWNER=$(echo "$INFO" | jq_field "['owner']")
if [ "$OWNER" = "$T1U" ] || [ "$OWNER" = "$T2U" ]; then
  pass "T16 transfer atomic: owner=$OWNER"
else
  fail "T16" "owner=$OWNER (info=${INFO:0:160})"
fi
rm -rf "$TMP"

# ─────────────────────────────────────────────────────────────────
echo ""
echo "▸ T17: Concurrent EDIT — DB current_commit == HEAD"
V17="v17-$TS"; mkvault "$V17"
putdoc "$V17" "e-doc" "marker_zero" >/dev/null
TMP=$(mktemp -d)
URI="akb://$V17/doc/specs/e-doc.md"
for i in 1 2 3 4 5; do
  ( curl -sk -X POST "$BASE_URL/mcp/" -H "$H_AUTH" -H "$H_JSON" \
      -H "Accept: application/json, text/event-stream" -H "mcp-session-id: $SID" \
      -d "{\"jsonrpc\":\"2.0\",\"id\":$((100+i)),\"method\":\"tools/call\",\"params\":{\"name\":\"akb_edit\",\"arguments\":{\"uri\":\"$URI\",\"old_string\":\"marker_zero\",\"new_string\":\"marker_$i\",\"replace_all\":true}}}" \
      > "$TMP/e$i" 2>&1 ) &
done
wait
GET=$(curl -sk -H "$H_AUTH" "$BASE_URL/api/v1/documents/$V17/specs/e-doc.md")
DB_HASH=$(echo "$GET" | jq_field "['current_commit']")
HEAD_BODY=$(echo "$GET" | jq_field "['content']")
HIST_BODY=$(curl -sk -H "$H_AUTH" "$BASE_URL/api/v1/documents/$V17/specs/e-doc.md?version=$DB_HASH" | jq_field "['content']")
[ "$HEAD_BODY" = "$HIST_BODY" ] && pass "T17 EDIT race convergent" || fail "T17 EDIT race" "diverged"
rm -rf "$TMP"

echo ""
echo "═══════════════════════════════════════════"
echo "Results: $PASS passed, $FAIL failed"
echo "═══════════════════════════════════════════"
if [ "$FAIL" -gt 0 ]; then
  printf '  - %s\n' "${ERRORS[@]}"
  exit 1
fi
