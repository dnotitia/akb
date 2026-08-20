#!/bin/bash
#
# AKB vault-skill bootstrap E2E
# Covers: doc_type='skill', akb_help router, missing-skill fallback, vault
# create seed, author workflow, the reserved-namespace guards, and the
# vault_skill auto-injection payload.
#
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
VAULT="skill-e2e-$(date +%s)"
INJECT_VAULT="skill-e2e-inject-$(date +%s)"
E2E_USER="skill-user-$(date +%s)"
PASS=0
FAIL=0
ERRORS=()

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

echo "╔══════════════════════════════════════════╗"
echo "║   AKB Vault-Skill Bootstrap E2E          ║"
echo "║   Target: $BASE_URL"
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
  -d '{"name":"skill-e2e"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

[ -n "$PAT" ] && pass "PAT acquired" || { fail "PAT" "could not get PAT"; exit 1; }

# ── MCP Session initialization ───────────────────────────────
INIT_RESP=$(curl -sk -i -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{"experimental":{"io.dnotitia.akb/vault-skill-preflight":{"version":1}}},"clientInfo":{"name":"skill-e2e","version":"1.0"}}}' 2>&1)

SID=$(echo "$INIT_RESP" | grep -i "mcp-session-id" | tr -d '\r' | awk '{print $2}')
[ -n "$SID" ] && pass "Session initialized ($SID)" || { fail "init" "no session"; exit 1; }

curl -sk -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $PAT" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null 2>&1

MCP_ID=10
mcp() {
  local tool="$1"; local args="$2"
  MCP_ID=$((MCP_ID+1))
  curl -sk -X POST "$BASE_URL/mcp/" \
    -H "Authorization: Bearer $PAT" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -H "mcp-session-id: $SID" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":$MCP_ID,\"method\":\"tools/call\",\"params\":{\"name\":\"$tool\",\"arguments\":$args}}"
}

mcp_text() {
  python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(d.get('result',{}).get('content',[{}])[0].get('text',''))" 2>/dev/null
}

# Same text, minus the auto-injected `vault_skill` payload. Injection puts a
# COPY of the skill body inside the very tool result these tests grep, so a
# content assertion on an un-stripped response could be satisfied by the
# payload instead of by the thing under test. Use this for content asserts;
# use mcp_text when the payload itself is the subject.
mcp_text_noskill() {
  python3 -c "import sys,json; d=json.loads(sys.stdin.read()); t=d.get('result',{}).get('content',[{}])[0].get('text','') or '{}'; o=json.loads(t); o.pop('vault_skill',None); print(json.dumps(o, ensure_ascii=False))" 2>/dev/null
}

# One field out of the injected payload; empty when nothing was injected.
skill_field() {
  python3 -c "import sys,json; t=json.loads(sys.stdin.read() or '{}'); print((t.get('vault_skill') or {}).get('$1',''))" 2>/dev/null
}

# ── 1. Create a vault → vault-skill.md should be seeded ─────
echo "▸ 1. Vault create seeds overview/vault-skill.md"

mcp akb_create_vault "{\"name\":\"$VAULT\",\"description\":\"e2e\"}" >/dev/null

GET_RESP=$(mcp akb_get "{\"uri\":\"akb://$VAULT/doc/overview/vault-skill.md\"}" | mcp_text_noskill)

echo "$GET_RESP" | grep -q '"type": *"skill"' \
  && pass "Seeded doc has type=skill" \
  || fail "T1.1" "type is not 'skill'; got: $(echo $GET_RESP | head -c 200)"

echo "$GET_RESP" | grep -q "$VAULT Guide" \
  && pass "Seeded body contains vault name in title" \
  || fail "T1.2" "vault name not substituted in template title"

echo "$GET_RESP" | grep -q "Document types" \
  && pass "Seeded body includes Document types section" \
  || fail "T1.3" "missing Document types section"

# ── 2. akb_help(topic='vault-skill') static topic body ──────
echo "▸ 2. akb_help(topic='vault-skill') without vault arg"

H1=$(mcp akb_help '{"topic":"vault-skill"}' | mcp_text)
echo "$H1" | grep -q "Vault skill" \
  && pass "Topic body returned" \
  || fail "T2.1" "topic body missing"

# Should NOT contain a 'Vault skill for <name>' header (that's only for the vault-specific render)
echo "$H1" | grep -q "Vault skill for" \
  && fail "T2.2" "static topic returned vault-specific header" \
  || pass "Static topic has no vault-specific header"

# ── 3. akb_help(topic='vault-skill', vault=<v>) returns body ─
echo "▸ 3. akb_help(topic='vault-skill', vault=<existing>)"

H2=$(mcp akb_help "{\"topic\":\"vault-skill\",\"vault\":\"$VAULT\"}" | mcp_text)
echo "$H2" | grep -q "# Vault skill for $VAULT" \
  && pass "Response header names the vault" \
  || fail "T3.1" "header missing"

echo "$H2" | grep -q "akb-skill-source" \
  && pass "Source-attribution marker present" \
  || fail "T3.2" "source marker missing"

echo "$H2" | grep -q "Source: vault owner" \
  && pass "Source line names the owner channel" \
  || fail "T3.3" "owner attribution missing"

echo "$H2" | grep -q "Document types" \
  && pass "Body content is included verbatim" \
  || fail "T3.4" "vault-skill.md body not embedded"

# ── 3b. Unknown vault is refused, not rendered ──────────────
echo "▸ 3b. akb_help(topic='vault-skill', vault=<absent>)"

# The branch now runs a reader-role check FIRST, so a vault name that does not
# exist is a not_found — it no longer reaches the renderer at all. The
# mirror-vault fallback render (the only remaining way in: a vault the caller
# CAN read that carries no skill doc) is covered in test_help_skill_unit.py,
# since creating a mirror vault needs a reachable external git remote.
ABSENT_VAULT="skill-e2e-absent-$(date +%s)"
H3=$(mcp akb_help "{\"topic\":\"vault-skill\",\"vault\":\"$ABSENT_VAULT\"}" | mcp_text)

echo "$H3" | grep -q '"code": *"not_found"' \
  && pass "Unknown vault refused with not_found" \
  || fail "T3b.1" "expected not_found; got: $(echo $H3 | head -c 200)"

echo "$H3" | grep -q "# Vault skill for $ABSENT_VAULT" \
  && fail "T3b.2" "renderer still ran for a vault the caller has no access to" \
  || pass "No skill body rendered for an unauthorized vault"

# ── 4. Reserved-namespace guards ────────────────────────────
echo "▸ 4. Reservation guards reject writes that break the pair"

# Two-way reservation: nothing but the canonical doc lives under `overview/`,
# and no other document may claim type='skill'. Every rejection is a
# ForbiddenError, which the MCP layer renders as code=permission_denied.
FORBIDDEN='"code": *"permission_denied"'

R4A=$(mcp akb_put "{\"vault\":\"$VAULT\",\"title\":\"Intruder\",\"collection\":\"overview\",\"content\":\"nope\"}" | mcp_text)
echo "$R4A" | grep -q "$FORBIDDEN" \
  && pass "akb_put into overview/ rejected" \
  || fail "T4a" "expected permission_denied; got: $(echo $R4A | head -c 200)"

R4B=$(mcp akb_put "{\"vault\":\"$VAULT\",\"title\":\"Impostor\",\"collection\":\"notes\",\"type\":\"skill\",\"content\":\"nope\"}" | mcp_text)
echo "$R4B" | grep -q "$FORBIDDEN" \
  && pass "akb_put with type=skill elsewhere rejected" \
  || fail "T4b" "expected permission_denied; got: $(echo $R4B | head -c 200)"

R4C=$(mcp akb_delete "{\"uri\":\"akb://$VAULT/doc/overview/vault-skill.md\"}" | mcp_text)
echo "$R4C" | grep -q "$FORBIDDEN" \
  && pass "akb_delete of the canonical doc rejected" \
  || fail "T4c" "expected permission_denied; got: $(echo $R4C | head -c 200)"

# The guard must refuse before any write — the doc is still there afterwards.
SURVIVED=$(mcp akb_get "{\"uri\":\"akb://$VAULT/doc/overview/vault-skill.md\"}" | mcp_text_noskill)
echo "$SURVIVED" | grep -q '"type": *"skill"' \
  && pass "Canonical doc survives the rejected delete" \
  || fail "T4c.2" "doc gone or unreadable after a rejected delete"

R4D=$(mcp akb_move "{\"uri\":\"akb://$VAULT/doc/overview/vault-skill.md\",\"collection\":\"notes\"}" | mcp_text)
echo "$R4D" | grep -q "$FORBIDDEN" \
  && pass "akb_move of the canonical doc rejected" \
  || fail "T4d" "expected permission_denied; got: $(echo $R4D | head -c 200)"

R4E=$(mcp akb_delete_collection "{\"vault\":\"$VAULT\",\"path\":\"overview\",\"recursive\":true}" | mcp_text)
echo "$R4E" | grep -q "$FORBIDDEN" \
  && pass "akb_delete_collection on overview rejected" \
  || fail "T4e" "expected permission_denied; got: $(echo $R4E | head -c 200)"

# Symmetric with T4e: without a create guard the delete guard would turn any
# stray sub-collection into permanently undeletable litter.
R4F=$(mcp akb_create_collection "{\"vault\":\"$VAULT\",\"path\":\"overview/junk\"}" | mcp_text)
echo "$R4F" | grep -q "$FORBIDDEN" \
  && pass "akb_create_collection under overview rejected" \
  || fail "T4f" "expected permission_denied; got: $(echo $R4F | head -c 200)"

R4G=$(mcp akb_create_collection "{\"vault\":\"$VAULT\",\"path\":\"overview\"}" | mcp_text)
echo "$R4G" | grep -q "$FORBIDDEN" \
  && pass "akb_create_collection on overview itself rejected" \
  || fail "T4g" "expected permission_denied; got: $(echo $R4G | head -c 200)"

# ── 5. Author workflow: edit vault-skill, re-fetch ──────────
echo "▸ 5. Owner can edit vault-skill, akb_help returns updated body"

NEW_BODY="# Custom Vault Skill\n\nMy custom rules: report only."
mcp akb_update "{\"uri\":\"akb://$VAULT/doc/overview/vault-skill.md\",\"content\":\"$NEW_BODY\"}" >/dev/null

H4=$(mcp akb_help "{\"topic\":\"vault-skill\",\"vault\":\"$VAULT\"}" | mcp_text)
echo "$H4" | grep -q "My custom rules" \
  && pass "Edited body is returned" \
  || fail "T5.1" "edit did not propagate to akb_help"

GET2=$(mcp akb_get "{\"uri\":\"akb://$VAULT/doc/overview/vault-skill.md\"}" | mcp_text_noskill)
echo "$GET2" | grep -q '"type": *"skill"' \
  && pass "type=skill preserved across edit" \
  || fail "T5.2" "type changed after update"

# ── 5b. Seeded vault-skill is immediately searchable (no edit required) ────
echo "▸ 5b. Seeded doc is indexed at create time"

# Create a fresh vault (don't reuse $VAULT which was already edited)
SEARCH_VAULT="skill-e2e-search-$(date +%s)"
mcp akb_create_vault "{\"name\":\"$SEARCH_VAULT\",\"description\":\"e2e search\"}" >/dev/null

# Wait a moment for async indexing to complete
sleep 2

# akb_grep should find the seeded body (chunks indexed)
GREP_RESP=$(mcp akb_grep "{\"vault\":\"$SEARCH_VAULT\",\"pattern\":\"Document types\"}" | mcp_text_noskill)
echo "$GREP_RESP" | grep -q "overview/vault-skill.md" \
  && pass "Seeded doc is grep-findable without prior edit" \
  || fail "T5b.1" "seeded doc not in chunk index"

# Also verify frontmatter is present in git (akb_get returns parsed body, not raw)
GET_FM=$(mcp akb_get "{\"uri\":\"akb://$SEARCH_VAULT/doc/overview/vault-skill.md\"}" | mcp_text_noskill)
echo "$GET_FM" | grep -q '"type": *"skill"' && pass "Seeded doc has frontmatter (type=skill visible)" \
  || fail "T5b.2" "no frontmatter on seeded doc (type missing)"

# Cleanup
mcp akb_delete_vault "{\"vault\":\"$SEARCH_VAULT\"}" >/dev/null 2>&1

# ── 6. doc_type='skill' is queryable ────────────────────────
echo "▸ 6. akb_search supports type='skill'"

# Wait for async chunk + BM25/vector indexing to settle on the edited vault-skill.
sleep 8
S1=$(mcp akb_search "{\"vault\":\"$VAULT\",\"query\":\"Document types\",\"type\":\"skill\"}" | mcp_text_noskill)
echo "$S1" | grep -q "overview/vault-skill.md" \
  && pass "type=skill filter accepts and matches" \
  || fail "T6.1" "search with type=skill did not return the skill doc"

# ── 7. vault_skill auto-injection ───────────────────────────
echo "▸ 7. vault_skill payload rides on the tool response"

# This harness holds ONE MCP session (SID, sent on every call), so the
# per-(session, vault) injection state is observable end to end: first touch
# carries the payload, the next call does not, and an edit re-arms it. A
# harness that re-initialized a session per call could only ever observe
# first_touch. Mirror vaults (never injected) are not covered here — creating
# one needs a reachable external git remote.
mcp akb_create_vault "{\"name\":\"$INJECT_VAULT\",\"description\":\"e2e inject\"}" >/dev/null

# akb_create_vault takes `name`, not `vault`, so tool_usage.vault_of_call
# attributes it to no vault — this browse is the session's first touch.
INJ1=$(mcp akb_browse "{\"vault\":\"$INJECT_VAULT\"}" | mcp_text)
[ "$(echo "$INJ1" | skill_field reason)" = "first_touch" ] \
  && pass "First touch carries vault_skill reason=first_touch" \
  || fail "T7.1" "no first_touch payload on the first call naming the vault"

[ "$(echo "$INJ1" | skill_field vault)" = "$INJECT_VAULT" ] \
  && pass "Payload names the touched vault" \
  || fail "T7.2" "payload vault mismatch"

echo "$INJ1" | skill_field body | grep -q "Document types" \
  && pass "Payload carries the seeded skill body" \
  || fail "T7.3" "payload body is not the vault-skill text"

[ "$(echo "$INJ1" | skill_field truncated)" = "False" ] \
  && pass "Seed body is under the injection ceiling (truncated=false)" \
  || fail "T7.4" "seed template reported as truncated"

INJ2=$(mcp akb_browse "{\"vault\":\"$INJECT_VAULT\"}" | mcp_text)
[ -z "$(echo "$INJ2" | skill_field reason)" ] \
  && pass "Second touch in the same session injects nothing" \
  || fail "T7.5" "payload re-attached without a skill change"

# The writer invalidates the version cache POST-COMMIT, before the dispatch
# chokepoint attaches — so the re-injection lands on the akb_update response
# itself, not on the call after it.
INJ3=$(mcp akb_update "{\"uri\":\"akb://$INJECT_VAULT/doc/overview/vault-skill.md\",\"content\":\"# Rewritten\\n\\nInjection re-arm probe.\"}" | mcp_text)
[ "$(echo "$INJ3" | skill_field reason)" = "updated" ] \
  && pass "Skill edit re-arms injection with reason=updated" \
  || fail "T7.6" "no updated payload after editing the skill body"

echo "$INJ3" | skill_field body | grep -q "Injection re-arm probe" \
  && pass "Re-armed payload carries the new body" \
  || fail "T7.7" "updated payload still holds the old body"

INJ4=$(mcp akb_browse "{\"vault\":\"$INJECT_VAULT\"}" | mcp_text)
[ -z "$(echo "$INJ4" | skill_field reason)" ] \
  && pass "Injection goes quiet again once the new version is delivered" \
  || fail "T7.8" "payload re-attached after the update was already delivered"

# ── 8. Non-member sees no skill, through any channel ────────
echo "▸ 8. Cross-vault disclosure is closed (second user)"

# The single-owner sections above exercise no authorization path at all, so
# this block provisions a second account (same shape as
# test_security_edge_e2e.sh) that is a member of NOTHING.
E2E_USER2="skill-user2-$(date +%s)"
curl -sk -X POST "$BASE_URL/api/v1/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$E2E_USER2\",\"email\":\"$E2E_USER2@test.dev\",\"password\":\"test1234\"}" >/dev/null 2>&1

JWT2=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$E2E_USER2\",\"password\":\"test1234\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

PAT2=$(curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" \
  -H "Authorization: Bearer $JWT2" \
  -H 'Content-Type: application/json' \
  -d '{"name":"skill-e2e-2"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

INIT2=$(curl -sk -i -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $PAT2" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{"experimental":{"io.dnotitia.akb/vault-skill-preflight":{"version":1}}},"clientInfo":{"name":"skill-e2e-2","version":"1.0"}}}' 2>&1)
SID2=$(echo "$INIT2" | grep -i "mcp-session-id" | tr -d '\r' | awk '{print $2}')

curl -sk -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $PAT2" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: $SID2" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null 2>&1

mcp2() {
  local tool="$1"; local args="$2"
  MCP_ID=$((MCP_ID+1))
  curl -sk -X POST "$BASE_URL/mcp/" \
    -H "Authorization: Bearer $PAT2" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -H "mcp-session-id: $SID2" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":$MCP_ID,\"method\":\"tools/call\",\"params\":{\"name\":\"$tool\",\"arguments\":$args}}"
}

[ -n "$PAT2" ] && [ -n "$SID2" ] \
  && pass "Second (non-member) user session ready" \
  || fail "T8.0" "could not provision the second user"

# Marker the non-member must never see, planted by the owner.
MARKER="LEAK-CANARY-$(date +%s)"
mcp akb_update "{\"uri\":\"akb://$INJECT_VAULT/doc/overview/vault-skill.md\",\"content\":\"# Private\\n\\n$MARKER\"}" >/dev/null

# 8.1 The promoted channel itself.
N1=$(mcp2 akb_help "{\"topic\":\"vault-skill\",\"vault\":\"$INJECT_VAULT\"}" | mcp_text)
echo "$N1" | grep -q '"code": *"permission_denied"' \
  && pass "Non-member akb_help(vault-skill) → permission_denied" \
  || fail "T8.1" "expected permission_denied; got: $(echo $N1 | head -c 200)"

echo "$N1" | grep -q "$MARKER" \
  && fail "T8.2" "skill body leaked to a non-member through akb_help" \
  || pass "No skill body in the refusal"

# 8.3 The auto-injection channel. A STATIC topic runs no access check, so the
# injector must stay silent even though the arguments name the vault — this is
# the exact cross-vault disclosure the final review reproduced.
N2=$(mcp2 akb_help "{\"topic\":\"quickstart\",\"vault\":\"$INJECT_VAULT\"}" | mcp_text)
[ -z "$(echo "$N2" | skill_field vault)" ] \
  && pass "Non-member static-topic call carries NO vault_skill key" \
  || fail "T8.3" "injection attached without an access check: $(echo $N2 | head -c 200)"

echo "$N2" | grep -q "$MARKER" \
  && fail "T8.4" "skill body leaked through the injection channel" \
  || pass "No skill body in the static-topic response"

# 8.5 A denied vault call carries nothing either (belt and braces: the error
# path already skips injection, but this is the shape an attacker would probe).
N3=$(mcp2 akb_browse "{\"vault\":\"$INJECT_VAULT\"}" | mcp_text)
echo "$N3" | grep -q '"code": *"permission_denied"' \
  && pass "Non-member akb_browse → permission_denied" \
  || fail "T8.5" "expected permission_denied; got: $(echo $N3 | head -c 200)"

[ -z "$(echo "$N3" | skill_field vault)" ] \
  && pass "Denied vault call carries NO vault_skill key" \
  || fail "T8.6" "injection attached to a denied call"

# 8.7 The gate is a role check, not a blanket deny: a granted reader gets it
# through BOTH channels. Order matters — the injection channel is per-session
# first-touch, and akb_help is itself an access-checked, vault-attributed call,
# so it would consume the first touch if it ran first.
mcp akb_grant "{\"vault\":\"$INJECT_VAULT\",\"user\":\"$E2E_USER2\",\"role\":\"reader\"}" >/dev/null 2>&1

N4=$(mcp2 akb_browse "{\"vault\":\"$INJECT_VAULT\"}" | mcp_text)
[ "$(echo "$N4" | skill_field reason)" = "first_touch" ] \
  && pass "Granted reader's first touch carries the payload" \
  || fail "T8.7" "authorized member lost injection: $(echo $N4 | head -c 200)"

N5=$(mcp2 akb_help "{\"topic\":\"vault-skill\",\"vault\":\"$INJECT_VAULT\"}" | mcp_text_noskill)
echo "$N5" | grep -q "$MARKER" \
  && pass "Granted reader receives the skill through akb_help" \
  || fail "T8.8" "reader denied after grant: $(echo $N5 | head -c 200)"

# 8.9 A fresh writer session must receive the guide BEFORE its first write,
# and the canonical guide itself remains owner-managed.
mcp akb_grant "{\"vault\":\"$INJECT_VAULT\",\"user\":\"$E2E_USER2\",\"role\":\"writer\"}" >/dev/null 2>&1

INIT3=$(curl -sk -i -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $PAT2" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{"experimental":{"io.dnotitia.akb/vault-skill-preflight":{"version":2}}},"clientInfo":{"name":"skill-e2e-writer","version":"1.0"}}}' 2>&1)
SID3=$(echo "$INIT3" | grep -i "mcp-session-id" | tr -d '\r' | awk '{print $2}')
curl -sk -X POST "$BASE_URL/mcp/" \
  -H "Authorization: Bearer $PAT2" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: $SID3" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null 2>&1

mcp3() {
  local tool="$1"; local args="$2"
  MCP_ID=$((MCP_ID+1))
  curl -sk -X POST "$BASE_URL/mcp/" \
    -H "Authorization: Bearer $PAT2" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -H "mcp-session-id: $SID3" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":$MCP_ID,\"method\":\"tools/call\",\"params\":{\"name\":\"$tool\",\"arguments\":$args}}"
}

# Fire a true first-touch cohort at one Streamable-HTTP session. Every request
# lacks acknowledgement and therefore must return the same challenge without
# mutating. This keeps the shared-session arrival cohort within one contract.
PREFLIGHT_DIR=$(mktemp -d)
for i in $(seq 1 12); do
  curl -sk -X POST "$BASE_URL/mcp/" \
    -H "Authorization: Bearer $PAT2" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -H "mcp-session-id: $SID3" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":$((700+i)),\"method\":\"tools/call\",\"params\":{\"name\":\"akb_put\",\"arguments\":{\"vault\":\"$INJECT_VAULT\",\"collection\":\"notes\",\"title\":\"Parallel preflight $i\",\"slug\":\"parallel-preflight-$i\",\"content\":\"body\"}}}" \
    >"$PREFLIGHT_DIR/$i.json" &
done
wait

PREFLIGHT_COUNT=0
for i in $(seq 1 12); do
  BODY=$(mcp_text <"$PREFLIGHT_DIR/$i.json")
  echo "$BODY" | grep -q '"code": *"vault_skill_required"' \
    && PREFLIGHT_COUNT=$((PREFLIGHT_COUNT+1))
done
[ "$PREFLIGHT_COUNT" -eq 12 ] \
  && pass "All 12 parallel first writes stop at acknowledged preflight" \
  || fail "T8.9" "$PREFLIGHT_COUNT/12 writes were preflighted"

W1=$(mcp_text <"$PREFLIGHT_DIR/1.json")
ACK=$(echo "$W1" | skill_field ack_token)
[ -n "$ACK" ] \
  && pass "Strict preflight returns an acknowledgement token" \
  || fail "T8.10" "v2 challenge omitted ack_token"

BEFORE_RETRY=$(mcp akb_browse "{\"vault\":\"$INJECT_VAULT\",\"collection\":\"notes\"}" | mcp_text_noskill)
if echo "$BEFORE_RETRY" | grep -q 'parallel-preflight-'; then
  fail "T8.11" "parallel preflight cohort mutated a document"
else
  pass "Parallel preflight cohort leaves every document absent"
fi

PUT_ARGS="{\"vault\":\"$INJECT_VAULT\",\"collection\":\"notes\",\"title\":\"Parallel preflight 1\",\"slug\":\"parallel-preflight-1\",\"content\":\"body\",\"_vault_skill_ack\":\"$ACK\"}"
W2=$(mcp3 akb_put "$PUT_ARGS" | mcp_text_noskill)
echo "$W2" | grep -q '"action": *"created"' \
  && pass "Acknowledged exact retry performs the document mutation" \
  || fail "T8.12" "acknowledged retry did not create the document: $(echo $W2 | head -c 200)"
rm -rf "$PREFLIGHT_DIR"

W3=$(mcp3 akb_update "{\"uri\":\"akb://$INJECT_VAULT/doc/overview/vault-skill.md\",\"content\":\"writer replacement\"}" | mcp_text)
echo "$W3" | grep -q '"code": *"permission_denied"' \
  && pass "Generic writer cannot alter owner-authored vault instructions" \
  || fail "T8.13" "writer skill update was not denied: $(echo $W3 | head -c 200)"

OWNER_READ=$(mcp akb_get "{\"uri\":\"akb://$INJECT_VAULT/doc/overview/vault-skill.md\"}" | mcp_text_noskill)
echo "$OWNER_READ" | grep -q "$MARKER" \
  && pass "Denied writer update leaves the guide unchanged" \
  || fail "T8.14" "guide changed after the denied writer update"

# ── Cleanup ──────────────────────────────────────────────────
# The reserved-namespace guards do not block vault deletion (the vault delete
# path is not a document/collection delete) — assert it rather than assume it,
# since a regression there would silently leak e2e vaults.
DEL=$(mcp akb_delete_vault "{\"vault\":\"$VAULT\"}" | mcp_text)
echo "$DEL" | grep -q '"deleted": *true' \
  && pass "Vault with a reserved overview/ still deletable" \
  || fail "T8.1" "vault delete blocked: $(echo $DEL | head -c 200)"

mcp akb_delete_vault "{\"vault\":\"$INJECT_VAULT\"}" >/dev/null 2>&1

# ── Summary ──────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════"
echo "  Passed: $PASS    Failed: $FAIL"
if [ $FAIL -gt 0 ]; then
  echo "  Errors:"
  for e in "${ERRORS[@]}"; do echo "    - $e"; done
  exit 1
fi
