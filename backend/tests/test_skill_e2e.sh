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
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"skill-e2e","version":"1.0"}}}' 2>&1)

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

# ── 3b. No skill → mirror-oriented fallback ─────────────────
echo "▸ 3b. akb_help(topic='vault-skill', vault=<no skill>)"

# The canonical doc can no longer be deleted to fabricate this case (see T4c),
# so the branch is driven with a vault name that does not exist: the handler's
# _fetch maps NotFoundError → None for a missing vault exactly as it does for a
# mirror vault that carries no skill, which is the render path under test.
ABSENT_VAULT="skill-e2e-absent-$(date +%s)"
H3=$(mcp akb_help "{\"topic\":\"vault-skill\",\"vault\":\"$ABSENT_VAULT\"}" | mcp_text)

echo "$H3" | grep -q "# Vault skill for $ABSENT_VAULT" \
  && pass "Fallback still names the vault" \
  || fail "T3b.1" "header missing"

echo "$H3" | grep -q 'This vault has no `overview/vault-skill.md`' \
  && pass "Missing notice rendered" \
  || fail "T3b.2" "missing notice not shown"

echo "$H3" | grep -q 'read-only external' \
  && pass "Notice points at the mirror-vault explanation" \
  || fail "T3b.3" "mirror explanation missing"

# No akb_put recipe: `overview` is reserved now, so telling an agent to author
# the doc itself would hand it a call the server rejects.
echo "$H3" | grep -q 'akb_put(' \
  && fail "T3b.4" "fallback still hands out an akb_put recipe for a reserved path" \
  || pass "No akb_put recipe in the fallback"

echo "$H3" | grep -q '\${{secrets.X}}' \
  && pass "Fallback rules included" \
  || fail "T3b.5" "fallback rules missing"

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
