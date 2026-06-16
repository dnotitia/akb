#!/bin/bash
#
# AKB Document Identity (slug / collision / move / alias) Scenario Suite
# Multi-angle, end-to-end against the MCP HTTP transport.
# Covers: slug derivation & normalization, create-collision suffixing,
# title-edit path freeze, move/rename + alias resolution, lifecycle
# (reuse / delete), edge survival across move, cross-vault isolation.
#
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
SUFFIX="$(date +%s)-$$"
USER="ident-$SUFFIX"
VAULT="ident-$SUFFIX"
VAULT2="ident2-$SUFFIX"
PASS=0
FAIL=0
ERRORS=()

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

echo "╔══════════════════════════════════════════╗"
echo "║   AKB Document Identity Scenario Suite    ║"
echo "║   Target: $BASE_URL/mcp/"
echo "╚══════════════════════════════════════════╝"

# ── Setup: user + PAT + MCP session ──────────────────────────
curl -sk -X POST "$BASE_URL/api/v1/auth/register" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER\",\"email\":\"$USER@test.dev\",\"password\":\"test1234\"}" >/dev/null 2>&1
JWT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER\",\"password\":\"test1234\"}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])' 2>/dev/null)
PAT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' -d '{"name":"ident"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])' 2>/dev/null)
[ -n "$PAT" ] || { echo "FATAL: no PAT"; exit 1; }

INIT=$(curl -sk -i -X POST "$BASE_URL/mcp/" -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"ident","version":"1.0"}}}' 2>&1)
SID=$(echo "$INIT" | grep -i "mcp-session-id" | tr -d '\r' | awk '{print $2}')
[ -n "$SID" ] || { echo "FATAL: no session"; exit 1; }
curl -sk -X POST "$BASE_URL/mcp/" -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null 2>&1

MCP_ID=10
mcp() {  # tool args  -> result-text json
  MCP_ID=$((MCP_ID+1))
  curl -sk -X POST "$BASE_URL/mcp/" -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" -H "mcp-session-id: $SID" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":$MCP_ID,\"method\":\"tools/call\",\"params\":{\"name\":\"$1\",\"arguments\":$2}}" 2>&1 \
    | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['result']['content'][0]['text'])" 2>/dev/null
}
field() { python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('$1',''))" 2>/dev/null; }
# put: vault coll title content -> echoes uri
put() { mcp akb_put "{\"vault\":\"$1\",\"collection\":\"$2\",\"title\":\"$3\",\"content\":\"$4\"}"; }
geturi() { mcp akb_get "{\"uri\":\"$1\"}"; }
has() { echo "$1" | grep -q "$2" && echo y || echo n; }     # body contains tag
iserr() { echo "$1" | python3 -c "import sys,json;print('error' in json.load(sys.stdin))" 2>/dev/null; }

mcp akb_create_vault "{\"name\":\"$VAULT\",\"description\":\"ident scenarios\"}" >/dev/null
mcp akb_create_vault "{\"name\":\"$VAULT2\",\"description\":\"ident scenarios 2\"}" >/dev/null

# ════════════════════════════════════════════════════════════
echo ""; echo "▸ S1. Slug derivation & normalization"
# clean
U=$(put "$VAULT" "s1" "Clean Title" "C1" | field uri)
[ "$(echo $U | grep -c '/s1/doc/clean-title.md')" = 1 ] && pass "clean title → clean slug" || fail "clean slug" "$U"
# punctuation/spaces normalized
U=$(put "$VAULT" "s1" "Payment API v2!! (Final)" "C2" | field uri)
echo "$U" | grep -q "payment-api-v2-final.md" && pass "punctuation/space normalized" || fail "normalize punct" "$U"
# symbol-only → untitled
U=$(put "$VAULT" "s1sym" "!!!" "C3" | field uri)
echo "$U" | grep -q "/untitled.md" && pass "symbol-only title → untitled.md" || fail "untitled" "$U"
# second symbol-only → untitled-{hex}
U2=$(put "$VAULT" "s1sym" "---" "C4" | field uri)
echo "$U2" | grep -qE "/untitled-[0-9a-f]{8}.md" && pass "2nd symbol title → untitled-{hex}.md" || fail "untitled suffix" "$U2"
# unicode/Korean round-trips
UK=$(put "$VAULT" "s1" "한글 제목 테스트" "KO_BODY" | field uri)
RK=$(geturi "$UK")
[ "$(has "$RK" KO_BODY)" = y ] && pass "unicode/Korean title round-trips" || fail "unicode" "$UK"
# very long title truncates but creates
LONG=$(python3 -c "print('a'*150)")
UL=$(put "$VAULT" "s1" "$LONG" "LONG_BODY" | field uri)
[ -n "$UL" ] && [ "$(has "$(geturi "$UL")" LONG_BODY)" = y ] && pass "very long title truncates+creates" || fail "long title" "$UL"

echo ""; echo "▸ S2. Create collision suffixing"
A=$(put "$VAULT" "dup" "Same Name" "A_BODY" | field uri)
B=$(put "$VAULT" "dup" "Same Name" "B_BODY" | field uri)
C=$(put "$VAULT" "dup" "Same Name" "C_BODY" | field uri)
[ "$A" != "$B" ] && [ "$B" != "$C" ] && [ "$A" != "$C" ] && pass "3 same-title → 3 distinct paths" || fail "distinct" "$A|$B|$C"
echo "$A" | grep -q "/same-name.md" && pass "1st same-title keeps clean path" || fail "clean first" "$A"
echo "$B" | grep -qE "/same-name-[0-9a-f]{8}.md" && pass "2nd same-title → -{hex} suffix" || fail "suffix" "$B"
[ "$(has "$(geturi "$A")" A_BODY)" = y ] && [ "$(has "$(geturi "$B")" B_BODY)" = y ] && [ "$(has "$(geturi "$B")" A_BODY)" = n ] && pass "collision bodies isolated" || fail "isolation" "bleed"
# case-only differs → slugify lowercases → collide → suffix
CA=$(put "$VAULT" "case" "WidgetApi" "CASE_A" | field uri)
CB=$(put "$VAULT" "case" "widgetapi" "CASE_B" | field uri)
[ "$CA" != "$CB" ] && pass "case-different titles still disambiguate" || fail "case dup" "$CA|$CB"
# same title in DIFFERENT collection → both clean (no collision)
X1=$(put "$VAULT" "ca" "Notes" "X1" | field uri)
X2=$(put "$VAULT" "cb" "Notes" "X2" | field uri)
echo "$X1" | grep -q "/ca/doc/notes.md" && echo "$X2" | grep -q "/cb/doc/notes.md" && pass "same title, diff collection → both clean" || fail "cross-coll" "$X1|$X2"

echo ""; echo "▸ S3. Title edit freezes path"
ED=$(put "$VAULT" "edit" "Draft Title" "ED_BODY" | field uri)
mcp akb_update "{\"uri\":\"$ED\",\"title\":\"Completely New Title\"}" >/dev/null
R=$(geturi "$ED")
[ "$(has "$R" ED_BODY)" = y ] && pass "title edit: path frozen, get-by-old-path works" || fail "edit freeze" "$ED"
echo "$ED" | grep -q "/draft-title.md" && pass "title edit: slug unchanged (draft-title.md)" || fail "edit slug" "$ED"
# two docs can end with same title via edit (non-unique)
E1=$(put "$VAULT" "edit" "Alpha" "E1" | field uri)
E2=$(put "$VAULT" "edit" "Beta" "E2" | field uri)
R2=$(mcp akb_update "{\"uri\":\"$E2\",\"title\":\"Alpha\"}")
[ "$(iserr "$R2")" = False ] && pass "edit title to duplicate is allowed (title non-unique)" || fail "dup title edit" "$R2"

echo ""; echo "▸ S4. Move / rename + alias resolution"
M=$(put "$VAULT" "mv" "Move Me" "MV_BODY" | field uri)
MN=$(mcp akb_move "{\"uri\":\"$M\",\"slug\":\"renamed\"}" | field uri)
[ -n "$MN" ] && [ "$MN" != "$M" ] && pass "rename slug → new uri" || fail "rename" "$MN"
[ "$(has "$(geturi "$MN")" MV_BODY)" = y ] && pass "new uri resolves" || fail "new resolve" "$MN"
[ "$(has "$(geturi "$M")" MV_BODY)" = y ] && pass "OLD uri resolves via alias" || fail "alias resolve" "$M"
# move collection only
MN2=$(mcp akb_move "{\"uri\":\"$MN\",\"collection\":\"archive\"}" | field uri)
echo "$MN2" | grep -q "/archive/" && pass "move collection-only" || fail "move coll" "$MN2"
# all prior URIs resolve (no chain)
[ "$(has "$(geturi "$M")" MV_BODY)" = y ] && [ "$(has "$(geturi "$MN")" MV_BODY)" = y ] && pass "all prior URIs resolve (no chain)" || fail "chain" "old uris"
# move to root collection
MR=$(put "$VAULT" "mvr" "Root Bound" "RB" | field uri)
MRN=$(mcp akb_move "{\"uri\":\"$MR\",\"collection\":\"\"}" | field uri)
echo "$MRN" | grep -qE "/$VAULT/doc/root-bound.md" && pass "move to root collection" || fail "move root" "$MRN"
# same-title collision into a collection → suffixed, both survive
T1=$(put "$VAULT" "tgt" "Twin" "TW_A" | field uri)
T2=$(put "$VAULT" "src" "Twin" "TW_B" | field uri)
T2N=$(mcp akb_move "{\"uri\":\"$T2\",\"collection\":\"tgt\"}" | field uri)
[ "$T2N" != "$T1" ] && [ "$(has "$(geturi "$T1")" TW_A)" = y ] && [ "$(has "$(geturi "$T2N")" TW_B)" = y ] && pass "move same-title into collection → both survive" || fail "move twin" "$T1|$T2N"
# suffix stripped on move to free collection
P1=$(put "$VAULT" "sa" "Sfx" "S1" | field uri); P2=$(put "$VAULT" "sa" "Sfx" "S2" | field uri)
echo "$P2" | grep -qE "/sfx-[0-9a-f]{8}.md" && {
  P2N=$(mcp akb_move "{\"uri\":\"$P2\",\"collection\":\"sb\"}" | field uri)
  echo "$P2N" | grep -q "/sb/doc/sfx.md" && pass "suffix stripped on move to free collection" || fail "strip" "$P2N"
} || fail "strip-setup" "$P2 not suffixed"
# explicit slug onto taken path → reject
mcp akb_put "{\"vault\":\"$VAULT\",\"collection\":\"rej\",\"title\":\"Taken\",\"content\":\"R1\"}" >/dev/null
RS=$(put "$VAULT" "rej" "Mover" "R2" | field uri)
RR=$(mcp akb_move "{\"uri\":\"$RS\",\"slug\":\"taken\"}")
[ "$(iserr "$RR")" = True ] && pass "explicit slug onto taken path rejected" || fail "explicit reject" "$RR"
# error cases (assign first; deep $(...) nesting of quoted JSON is brittle)
ENOARG=$(mcp akb_move "{\"uri\":\"$RS\"}")
[ "$(iserr "$ENOARG")" = True ] && pass "move w/o collection|slug rejected" || fail "noarg" "$ENOARG"
EBAD=$(mcp akb_move "{\"uri\":\"not-a-uri\",\"slug\":\"x\"}")
echo "$EBAD" | grep -q "invalid_uri" && pass "malformed uri → clean invalid_uri error" || fail "baduri" "$EBAD"
EMISS=$(mcp akb_move "{\"uri\":\"akb://$VAULT/doc/nonexistent.md\",\"slug\":\"y\"}")
[ "$(iserr "$EMISS")" = True ] && pass "move missing doc rejected" || fail "missing" "$EMISS"

echo ""; echo "▸ S5. Lifecycle: reuse, delete, ops by old uri"
# reuse vacated path → old uri resolves to NEW doc
RU=$(put "$VAULT" "reuse" "Recycle" "ORIG" | field uri)
mcp akb_move "{\"uri\":\"$RU\",\"slug\":\"recycled\"}" >/dev/null
put "$VAULT" "reuse" "Recycle" "NEWDOC" >/dev/null
RR=$(geturi "$RU")
[ "$(has "$RR" NEWDOC)" = y ] && [ "$(has "$RR" ORIG)" = n ] && pass "reused path: old uri → new doc (exact-first)" || fail "reuse" "$RU"
# update by OLD uri after move
UM=$(put "$VAULT" "ops" "Edit By Old" "OLD1" | field uri)
UMN=$(mcp akb_move "{\"uri\":\"$UM\",\"slug\":\"edit-by-old-2\"}" | field uri)
mcp akb_update "{\"uri\":\"$UM\",\"content\":\"UPDATED_VIA_OLD\"}" >/dev/null
[ "$(has "$(geturi "$UMN")" UPDATED_VIA_OLD)" = y ] && pass "update via OLD uri hits moved doc" || fail "update-old" "$UMN"
# delete by OLD uri, then old uri 404 (alias cleaned)
DM=$(put "$VAULT" "ops" "Del By Old" "DEL1" | field uri)
mcp akb_move "{\"uri\":\"$DM\",\"slug\":\"del-by-old-2\"}" >/dev/null
mcp akb_delete "{\"uri\":\"$DM\"}" >/dev/null
[ "$(iserr "$(mcp akb_get "{\"uri\":\"$DM\"}")")" = True ] && pass "delete via OLD uri + alias cleaned (old 404)" || fail "delete-old" "$DM"

echo ""; echo "▸ S6. Edges survive move"
GA=$(put "$VAULT" "g" "Graph A" "GA" | field uri)
GB=$(put "$VAULT" "g" "Graph B" "GB" | field uri)
mcp akb_link "{\"source\":\"$GA\",\"target\":\"$GB\",\"relation\":\"depends_on\"}" >/dev/null
GBN=$(mcp akb_move "{\"uri\":\"$GB\",\"slug\":\"graph-b-moved\"}" | field uri)
REL=$(mcp akb_relations "{\"uri\":\"$GA\"}")
echo "$REL" | grep -q "graph-b-moved" && pass "edge rewritten to new uri after target move" || fail "edge rewrite" "$REL"

echo ""; echo "▸ S7. Cross-vault isolation"
CV=$(put "$VAULT" "x" "Cross Vault" "CV" | field uri)
mcp akb_move "{\"uri\":\"$CV\",\"slug\":\"cross-moved\"}" >/dev/null
# old path in vault1 must NOT resolve when queried under vault2
CVP=$(echo "$CV" | sed "s#/$VAULT/#/$VAULT2/#")
[ "$(iserr "$(mcp akb_get "{\"uri\":\"$CVP\"}")")" = True ] && pass "alias is vault-scoped (no cross-vault leak)" || fail "cross-vault" "$CVP"

echo ""; echo "▸ S8. Collection doc_count adjusts on move"
# Maintained collections.doc_count, read from the collection row in a full browse
# (content_type=all so collection rows are present). Empty → 0.
ccount() {
  local n
  n=$(mcp akb_browse "{\"vault\":\"$VAULT\",\"depth\":-1}" \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print(next((it.get('doc_count') or 0 for it in d.get('items',[]) if it.get('type')=='collection' and it.get('path')=='$1'),0))" 2>/dev/null)
  echo "${n:-0}"
}
put "$VAULT" "cc-src" "Count Keeper A" "KA" >/dev/null
CC=$(put "$VAULT" "cc-src" "Count Keeper B" "KB" | field uri)
SRC0=$(ccount cc-src); DST0=$(ccount cc-dst)
mcp akb_move "{\"uri\":\"$CC\",\"collection\":\"cc-dst\"}" >/dev/null
SRC1=$(ccount cc-src); DST1=$(ccount cc-dst)
[ "$(( SRC0 - SRC1 ))" -eq 1 ] && [ "$(( DST1 - DST0 ))" -eq 1 ] && pass "doc_count: src -1 / dst +1 on cross-collection move (src $SRC0->$SRC1, dst $DST0->$DST1)" || fail "doc_count cross" "src $SRC0->$SRC1 dst $DST0->$DST1"
# move-to-root: source decrements, no NULL-collection increment crash
RM=$(put "$VAULT" "cc-src" "To Root" "TR" | field uri)
SRC2=$(ccount cc-src)
mcp akb_move "{\"uri\":\"$RM\",\"collection\":\"\"}" >/dev/null
SRC3=$(ccount cc-src)
[ "$(( SRC2 - SRC3 ))" -eq 1 ] && pass "doc_count: move-to-root decrements source ($SRC2->$SRC3)" || fail "doc_count root" "src $SRC2->$SRC3"

# ── Cleanup ──────────────────────────────────────────────────
mcp akb_delete_vault "{\"name\":\"$VAULT\",\"confirm\":\"$VAULT\"}" >/dev/null 2>&1
mcp akb_delete_vault "{\"name\":\"$VAULT2\",\"confirm\":\"$VAULT2\"}" >/dev/null 2>&1

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   Results: $PASS passed, $FAIL failed"
echo "╚══════════════════════════════════════════╝"
if [ "$FAIL" -gt 0 ]; then printf '%s\n' "${ERRORS[@]}"; exit 1; fi
