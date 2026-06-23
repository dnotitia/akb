#!/bin/bash
#
# AKB E2E: Knowledge-graph relation WRITE surface over HTTP REST.
#
# Covers POST /api/v1/relations (link) and DELETE /api/v1/relations
# (unlink) — the REST twins of MCP akb_link / akb_unlink. The read
# side (GET /relations) is exercised here only to round-trip the edges
# the write routes create. Bootstrap (users / MCP / vault / docs)
# mirrors test_graph_replace_e2e.sh.
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
echo "║   Relations REST (link/unlink) E2E       ║"
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
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"rel-rest-e2e","version":"1.0"}}}' > "$tmpfile" 2>/dev/null
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
# POST /relations with a JSON body; *_code variants return the HTTP status.
rpost()       { curl -sk             -X POST   "$BASE_URL/api/v1/relations" -H "Authorization: Bearer $1" -H 'Content-Type: application/json' -d "$2"; }
rpost_code()  { curl -sk -o /dev/null -w '%{http_code}' -X POST "$BASE_URL/api/v1/relations" -H "Authorization: Bearer $1" -H 'Content-Type: application/json' -d "$2"; }
# GET /relations?uri=  (urlencoded)
rget_rel()    { curl -sk -G "$BASE_URL/api/v1/relations" --data-urlencode "uri=$2" -H "Authorization: Bearer $1"; }
# DELETE /relations?source=&target=[&relation=]  (urlencoded query, no body — akb DELETE convention)
rdel() {
  local pat=$1 src=$2 tgt=$3 rel=${4:-}
  if [ -n "$rel" ]; then
    curl -sk -X DELETE -G "$BASE_URL/api/v1/relations" --data-urlencode "source=$src" --data-urlencode "target=$tgt" --data-urlencode "relation=$rel" -H "Authorization: Bearer $pat"
  else
    curl -sk -X DELETE -G "$BASE_URL/api/v1/relations" --data-urlencode "source=$src" --data-urlencode "target=$tgt" -H "Authorization: Bearer $pat"
  fi
}
rdel_code() {
  local pat=$1 src=$2 tgt=$3 rel=${4:-}
  if [ -n "$rel" ]; then
    curl -sk -o /dev/null -w '%{http_code}' -X DELETE -G "$BASE_URL/api/v1/relations" --data-urlencode "source=$src" --data-urlencode "target=$tgt" --data-urlencode "relation=$rel" -H "Authorization: Bearer $pat"
  else
    curl -sk -o /dev/null -w '%{http_code}' -X DELETE -G "$BASE_URL/api/v1/relations" --data-urlencode "source=$src" --data-urlencode "target=$tgt" -H "Authorization: Bearer $pat"
  fi
}
# count outgoing+undirected relations reported by GET /relations for a uri
rel_count() { rget_rel "$1" "$2" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('relations',[])))" 2>/dev/null; }

USER1="rel-rest-u1-$(date +%s)"     # owner (writer) of V1
USER2="rel-rest-u2-$(date +%s)"     # reader on V1
PAT1=$(setup_user "$USER1")
PAT2=$(setup_user "$USER2")
[ -n "$PAT1" ] && [ -n "$PAT2" ] && pass "2 users created" || { fail "Setup" "user creation failed"; exit 1; }

SID1=$(setup_mcp "$PAT1")
m1() { mc "$PAT1" "$SID1" "$1" "$2" | mr; }

VAULT1="rel-rest-$(date +%s)"
VAULT2="rel-rest2-$(($(date +%s)+1))"
m1 "akb_create_vault" "{\"name\":\"$VAULT1\",\"description\":\"relations rest test\"}" >/dev/null
m1 "akb_create_vault" "{\"name\":\"$VAULT2\",\"description\":\"cross vault\"}" >/dev/null
m1 "akb_grant" "{\"vault\":\"$VAULT1\",\"user\":\"$USER2\",\"role\":\"reader\"}" >/dev/null
pass "2 vaults created, USER2 granted reader on V1"

# Three docs in V1, one in V2. Fetch canonical URIs from akb_get responses.
geturi() { echo "$1" | python3 -c "import sys,json; print(json.load(sys.stdin).get('uri',''))" 2>/dev/null; }
URI_A=$(geturi "$(m1 "akb_put" "{\"vault\":\"$VAULT1\",\"collection\":\"specs\",\"title\":\"Issue Doc\",\"content\":\"# Issue\"}")")
URI_B=$(geturi "$(m1 "akb_put" "{\"vault\":\"$VAULT1\",\"collection\":\"specs\",\"title\":\"Target Doc\",\"content\":\"# Target\"}")")
URI_C=$(geturi "$(m1 "akb_put" "{\"vault\":\"$VAULT1\",\"collection\":\"specs\",\"title\":\"Third Doc\",\"content\":\"# Third\"}")")
URI_V2=$(geturi "$(m1 "akb_put" "{\"vault\":\"$VAULT2\",\"collection\":\"specs\",\"title\":\"Other Vault Doc\",\"content\":\"# Other\"}")")
[ -n "$URI_A" ] && [ -n "$URI_B" ] && [ -n "$URI_C" ] && [ -n "$URI_V2" ] \
  && pass "4 docs created ($URI_A …)" || { fail "Docs" "missing URIs"; exit 1; }

MISSING="akb://$VAULT1/doc/specs/does-not-exist"
# A coll URI: parseable, carries an identifier, same vault — so it clears
# _shared_link_vault + the writer check and reaches the service. link/unlink
# must both reject it (collections are navigation aids, never edge endpoints).
COLL_URI="akb://$VAULT1/coll/specs"

# ── 1. POST /relations — happy path + round-trip ─────────────
echo ""
echo "▸ 1. POST /relations (link, writer)"

CODE=$(rpost_code "$PAT1" "{\"source\":\"$URI_A\",\"target\":\"$URI_B\",\"relation\":\"references\"}")
[ "$CODE" = "200" ] && pass "link A→B references → 200" || fail "POST link" "got $CODE"

LINKED=$(rpost "$PAT1" "{\"source\":\"$URI_A\",\"target\":\"$URI_B\",\"relation\":\"references\"}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('linked'))" 2>/dev/null)
[ "$LINKED" = "True" ] && pass "response body {linked:true} (idempotent upsert)" || fail "link body" "linked=$LINKED"

C=$(rel_count "$PAT1" "$URI_A")
[ "$C" -ge 1 ] 2>/dev/null && pass "GET /relations round-trips the edge ($C)" || fail "round-trip" "count=$C"

# Upsert must not create a duplicate edge.
[ "$C" = "1" ] && pass "upsert: still exactly 1 edge after 2 POSTs" || fail "upsert dedupe" "count=$C"

# ── 2. POST validation / authorization matrix ────────────────
echo ""
echo "▸ 2. POST validation & authz"

CODE=$(rpost_code "$PAT1" "{\"source\":\"$URI_A\",\"target\":\"$URI_B\",\"relation\":\"bogus_rel\"}")
[ "$CODE" = "422" ] && pass "invalid relation enum → 422" || fail "bad relation" "got $CODE"

CODE=$(rpost_code "$PAT1" "{\"source\":\"$URI_A\",\"relation\":\"references\"}")
[ "$CODE" = "422" ] && pass "missing required field (target) → 422" || fail "missing field" "got $CODE"

CODE=$(rpost_code "$PAT1" "{\"source\":\"$URI_A\",\"target\":\"$URI_A\",\"relation\":\"references\"}")
[ "$CODE" = "400" ] && pass "self-link → 400" || fail "self-link" "got $CODE"

CODE=$(rpost_code "$PAT1" "{\"source\":\"$URI_A\",\"target\":\"$URI_V2\",\"relation\":\"references\"}")
[ "$CODE" = "400" ] && pass "cross-vault pair → 400" || fail "cross-vault" "got $CODE"

CODE=$(rpost_code "$PAT1" "{\"source\":\"not-a-uri\",\"target\":\"$URI_B\",\"relation\":\"references\"}")
[ "$CODE" = "400" ] && pass "malformed source URI → 400" || fail "bad uri" "got $CODE"

CODE=$(rpost_code "$PAT1" "{\"source\":\"$COLL_URI\",\"target\":\"$URI_B\",\"relation\":\"references\"}")
[ "$CODE" = "400" ] && pass "link from coll URI → 400 (not a linkable kind)" || fail "POST coll reject" "got $CODE"

CODE=$(rpost_code "$PAT1" "{\"source\":\"$URI_A\",\"target\":\"$MISSING\",\"relation\":\"references\"}")
[ "$CODE" = "404" ] && pass "nonexistent target resource → 404" || fail "missing target" "got $CODE"

CODE=$(rpost_code "$PAT2" "{\"source\":\"$URI_A\",\"target\":\"$URI_C\",\"relation\":\"references\"}")
[ "$CODE" = "403" ] && pass "reader cannot link → 403" || fail "reader gate" "got $CODE"

CODE=$(curl -sk -o /dev/null -w '%{http_code}' -X POST "$BASE_URL/api/v1/relations" -H 'Content-Type: application/json' -d "{\"source\":\"$URI_A\",\"target\":\"$URI_B\",\"relation\":\"references\"}")
[ "$CODE" = "401" ] && pass "unauthenticated → 401" || fail "no auth" "got $CODE"

# ── 3. DELETE /relations — unlink + idempotency ──────────────
echo ""
echo "▸ 3. DELETE /relations (unlink, writer)"

CODE=$(rdel_code "$PAT1" "$URI_A" "$URI_B" "references")
[ "$CODE" = "200" ] && pass "unlink A→B references → 200" || fail "DELETE unlink" "got $CODE"

C=$(rel_count "$PAT1" "$URI_A")
[ "$C" = "0" ] && pass "edge removed (GET /relations now 0)" || fail "unlink verify" "count=$C"

REMOVED=$(rdel "$PAT1" "$URI_A" "$URI_B" "references" | python3 -c "import sys,json; print(json.load(sys.stdin).get('unlinked'))" 2>/dev/null)
[ "$REMOVED" = "0" ] && pass "unlink idempotent: re-delete → unlinked:0, 200" || fail "unlink idempotent" "unlinked=$REMOVED"

CODE=$(rdel_code "$PAT2" "$URI_A" "$URI_B" "references")
[ "$CODE" = "403" ] && pass "reader cannot unlink → 403" || fail "reader unlink gate" "got $CODE"

# Regression (PR #168 review): a coll URI must be REJECTED (400), not
# silently 200 + {"unlinked":0}. canonicalize_resource_uri returns None for
# coll, which previously fell back to the raw URI → zero-row DELETE → a
# false success. POST rejects the same URI 400; unlink must match it.
CODE=$(rdel_code "$PAT1" "$COLL_URI" "$URI_B" "references")
[ "$CODE" = "400" ] && pass "unlink from coll URI → 400 (matches POST, no false success)" || fail "DELETE coll reject" "got $CODE"

# DELETE relation is typed RelationType | None → a non-vocab value is
# rejected by request validation (422), mirroring POST's bad-enum 422.
CODE=$(rdel_code "$PAT1" "$URI_A" "$URI_B" "bogus_rel")
[ "$CODE" = "422" ] && pass "unlink with bad relation enum → 422" || fail "DELETE bad relation" "got $CODE"

# relation omitted → remove ALL edges between the two
rpost "$PAT1" "{\"source\":\"$URI_A\",\"target\":\"$URI_B\",\"relation\":\"references\"}" >/dev/null
rpost "$PAT1" "{\"source\":\"$URI_A\",\"target\":\"$URI_B\",\"relation\":\"related_to\"}" >/dev/null
BEFORE=$(rel_count "$PAT1" "$URI_A")
rdel "$PAT1" "$URI_A" "$URI_B" >/dev/null   # no relation → wipe both
AFTER=$(rel_count "$PAT1" "$URI_A")
[ "$BEFORE" = "2" ] && [ "$AFTER" = "0" ] && pass "unlink w/o relation removes all edges (2→0)" || fail "unlink-all" "before=$BEFORE after=$AFTER"

# ── 4. Canonicalization parity (link/unlink resolve URIs identically) ─
# link_resources canonicalizes on write; unlink_resources must too, or a
# non-canonical-but-parseable spelling (here: slash-suffixed) creates an
# edge that the matching DELETE cannot remove. Asserts the regression is
# fixed at the shared-service layer — same guarantee MCP akb_unlink gets.
echo ""
echo "▸ 4. Non-canonical URI link/unlink parity"

CODE=$(rpost_code "$PAT1" "{\"source\":\"$URI_A/\",\"target\":\"$URI_B/\",\"relation\":\"references\"}")
[ "$CODE" = "200" ] && pass "link via slash-suffixed URIs → 200" || fail "noncanon link" "got $CODE"

C=$(rel_count "$PAT1" "$URI_A")
[ "$C" -ge 1 ] 2>/dev/null && pass "edge stored under canonical URI (visible from canonical A)" || fail "noncanon stored" "count=$C"

rdel "$PAT1" "$URI_A/" "$URI_B/" "references" >/dev/null
C=$(rel_count "$PAT1" "$URI_A")
[ "$C" = "0" ] && pass "unlink via slash-suffixed URIs removes the canonical edge" || fail "noncanon unlink" "still $C — unlink did not canonicalize"

# ── 5. GET /graph/overview + /graph/health (read surface) ────
# Degree-ranked overview with honest totals, and the hubs/orphans health
# audit. Reader-gated; vault-scoped. Builds a tiny known graph in V1:
#   A → B, A → C  (so A is a degree-2 hub), and a 4th doc D left ORPHAN.
echo ""
echo "▸ 5. GET /graph/overview + /graph/health"

URI_D=$(geturi "$(m1 "akb_put" "{\"vault\":\"$VAULT1\",\"collection\":\"specs\",\"title\":\"Orphan Doc\",\"content\":\"# Orphan\"}")")
rpost "$PAT1" "{\"source\":\"$URI_A\",\"target\":\"$URI_B\",\"relation\":\"depends_on\"}" >/dev/null
rpost "$PAT1" "{\"source\":\"$URI_A\",\"target\":\"$URI_C\",\"relation\":\"depends_on\"}" >/dev/null

gov()      { curl -sk             -G "$BASE_URL/api/v1/graph/overview" --data-urlencode "vault=$2" --data-urlencode "top_k=${3:-200}" -H "Authorization: Bearer $1"; }
gov_code() { curl -sk -o /dev/null -w '%{http_code}' -G "$BASE_URL/api/v1/graph/overview" --data-urlencode "vault=$2" -H "Authorization: Bearer $1"; }
ghealth()  { curl -sk             -G "$BASE_URL/api/v1/graph/health"   --data-urlencode "vault=$2" --data-urlencode "hub_threshold=${3:-2}" -H "Authorization: Bearer $1"; }

# overview: totals are honest, top node is the degree-2 hub A, edges carry kind.
OVCHECK=$(gov "$PAT1" "$VAULT1" 200 | python3 -c "
import sys,json
d=json.load(sys.stdin)
top=max(d['nodes'], key=lambda n: n.get('degree',0)) if d['nodes'] else {}
kinds=sorted({e.get('kind') for e in d['edges']})
ok = (d['edges_total']==2 and d['nodes_total']==3 and d['truncated'] is False
      and top.get('degree')==2 and top.get('uri')=='$URI_A' and kinds==['explicit'])
print('OK' if ok else 'BAD '+json.dumps({'edges_total':d['edges_total'],'nodes_total':d['nodes_total'],'trunc':d['truncated'],'top':top.get('uri'),'deg':top.get('degree'),'kinds':kinds}))
" 2>&1)
[ "$OVCHECK" = "OK" ] && pass "overview: totals(3/2) + degree-ranked hub A + edge kind" || fail "overview" "$OVCHECK"

# overview surfaces unlinked resources as degree-0 isolated nodes (so a vault
# with orphans isn't a blank canvas). Orphan doc D must appear with degree 0,
# and orphans_returned must count it. nodes_total stays the CONNECTED total (3).
ORPHCHECK=$(gov "$PAT1" "$VAULT1" 200 | python3 -c "
import sys,json
d=json.load(sys.stdin)
by_uri={n['uri']:n for n in d['nodes']}
dn=by_uri.get('$URI_D')
ok = (d.get('orphans_returned',0) >= 1 and dn is not None and dn.get('degree')==0 and d['nodes_total']==3)
print('OK' if ok else 'BAD orphans_returned='+str(d.get('orphans_returned'))+' D='+json.dumps(dn))
" 2>&1)
[ "$ORPHCHECK" = "OK" ] && pass "overview: unlinked doc D surfaced as degree-0 orphan node" || fail "overview orphans" "$ORPHCHECK"

# overview truncation: top_k=1 keeps only the single highest-degree node.
TRUNC=$(gov "$PAT1" "$VAULT1" 1 | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['returned']==1 and d['truncated'] is True and d['nodes_total']==3)" 2>/dev/null)
[ "$TRUNC" = "True" ] && pass "overview: top_k=1 → returned 1 of 3, truncated true" || fail "overview trunc" "got $TRUNC"

# health: A is a hub (deg>=2); the unlinked doc D is reported as an orphan.
HCHECK=$(ghealth "$PAT1" "$VAULT1" 2 | python3 -c "
import sys,json
d=json.load(sys.stdin)
hubs=d.get('hubs',[]); orph=d.get('orphans',{})
hub_ok = any(h.get('uri')=='$URI_A' and h.get('degree')==2 for h in hubs)
orph_ok = orph.get('count',0)>=1 and any('Orphan Doc' in (o.get('name') or '') for o in orph.get('sample',[]))
print('OK' if (hub_ok and orph_ok) else 'BAD '+json.dumps({'hubs':[(h.get('uri'),h.get('degree')) for h in hubs],'orphans':orph}))
" 2>&1)
[ "$HCHECK" = "OK" ] && pass "health: hub A(deg2) + orphan D reported" || fail "health" "$HCHECK"

# Identity-based orphan matching (regression guard for the false-orphan-duplicate
# bug): a collection-scoped TABLE linked via a NON-CANONICAL (mis-cased
# collection) URI must NOT re-appear as a phantom orphan duplicate of its
# connected self; an unlinked table must surface as a degree-0 'table' orphan.
# Also pins the contract len(nodes) == returned + orphans_returned. (Placed last
# in §5 — linking a table changes the connected count the checks above assert.)
curl -sk -X POST "$BASE_URL/api/v1/tables/$VAULT1" -H "Authorization: Bearer $PAT1" -H 'Content-Type: application/json' \
  -d '{"name":"linked_tbl","columns":[{"name":"k","type":"text"}],"collection":"specs"}' >/dev/null
curl -sk -X POST "$BASE_URL/api/v1/tables/$VAULT1" -H "Authorization: Bearer $PAT1" -H 'Content-Type: application/json' \
  -d '{"name":"lonely_tbl","columns":[{"name":"k","type":"text"}],"collection":"specs"}' >/dev/null
# link doc A -> table via a MIS-CASED collection segment (canonical is 'specs')
rpost "$PAT1" "{\"source\":\"$URI_A\",\"target\":\"akb://$VAULT1/coll/SPECS/table/linked_tbl\",\"relation\":\"references\"}" >/dev/null
TBLCHECK=$(gov "$PAT1" "$VAULT1" 200 | python3 -c "
import sys,json,collections
d=json.load(sys.stdin)
dups=[u for u,c in collections.Counter(n['uri'] for n in d['nodes']).items() if c>1]
linked=[n for n in d['nodes'] if n['uri'].endswith('/table/linked_tbl')]
lonely=[n for n in d['nodes'] if n['uri'].endswith('/table/lonely_tbl')]
ok = (not dups
      and len(linked)==1 and (linked[0].get('degree') or 0)>=1
      and len(lonely)==1 and lonely[0].get('degree')==0 and lonely[0].get('resource_type')=='table'
      and len(d['nodes'])==d['returned']+d['orphans_returned'])
print('OK' if ok else 'BAD dups='+str(dups)+' linked='+json.dumps(linked)+' lonely='+json.dumps(lonely))
" 2>&1)
[ "$TBLCHECK" = "OK" ] && pass "overview: non-canonical-linked table not duplicated; unlinked table is degree-0 orphan" || fail "overview table orphan" "$TBLCHECK"

# Reader-gating + cross-vault isolation.
CODE=$(gov_code "$PAT2" "$VAULT1"); [ "$CODE" = "200" ] && pass "overview: reader (USER2) on V1 → 200" || fail "overview reader" "got $CODE"
CODE=$(gov_code "$PAT2" "$VAULT2"); [ "$CODE" = "403" ] && pass "overview: no-access vault (V2) → 403" || fail "overview isolation" "got $CODE"
CODE=$(curl -sk -o /dev/null -w '%{http_code}' -G "$BASE_URL/api/v1/graph/overview" --data-urlencode "vault=$VAULT1")
[ "$CODE" = "401" ] && pass "overview: unauthenticated → 401" || fail "overview no-auth" "got $CODE"

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
