#!/bin/bash
#
# AKB vault-skill bootstrap E2E
# Covers: doc_type='skill', akb_help router, missing notice, vault create seed,
# author workflow.
#
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
VAULT="skill-e2e-$(date +%s)"
EMPTY_VAULT="skill-e2e-empty-$(date +%s)"
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

mcp() {
  local tool="$1"; local args="$2"
  curl -sk -X POST "$BASE_URL/api/v1/mcp/" \
    -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"$tool\",\"arguments\":$args}}"
}

# ── 1. Create a vault → vault-skill.md should be seeded ─────
echo "▸ 1. Vault create seeds overview/vault-skill.md"

mcp akb_create_vault "{\"name\":\"$VAULT\",\"description\":\"e2e\"}" >/dev/null

GET_RESP=$(mcp akb_get "{\"vault\":\"$VAULT\",\"doc_id\":\"overview/vault-skill.md\"}")

echo "$GET_RESP" | grep -q '"type": *"skill"' \
  && pass "Seeded doc has type=skill" \
  || fail "T1.1" "type is not 'skill'; got: $(echo $GET_RESP | head -c 200)"

echo "$GET_RESP" | grep -q "$VAULT Vault Skill" \
  && pass "Seeded body contains vault name in title" \
  || fail "T1.2" "vault name not substituted in template title"

echo "$GET_RESP" | grep -q "Document types" \
  && pass "Seeded body includes Document types section" \
  || fail "T1.3" "missing Document types section"

# ── 2. akb_help(topic='vault-skill') static topic body ──────
echo "▸ 2. akb_help(topic='vault-skill') without vault arg"

H1=$(mcp akb_help '{"topic":"vault-skill"}')
echo "$H1" | grep -q "Vault skill" \
  && pass "Topic body returned" \
  || fail "T2.1" "topic body missing"

# Should NOT contain a 'Vault skill for <name>' header (that's only for the vault-specific render)
echo "$H1" | grep -q "Vault skill for" \
  && fail "T2.2" "static topic returned vault-specific header" \
  || pass "Static topic has no vault-specific header"

# ── 3. akb_help(topic='vault-skill', vault=<v>) returns body ─
echo "▸ 3. akb_help(topic='vault-skill', vault=<existing>)"

H2=$(mcp akb_help "{\"topic\":\"vault-skill\",\"vault\":\"$VAULT\"}")
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

# ── 4. Missing vault-skill → notice + akb_put template ──────
echo "▸ 4. akb_help(topic='vault-skill', vault=<no-skill>)"

# Create a vault then DELETE its vault-skill.md so it's missing.
mcp akb_create_vault "{\"name\":\"$EMPTY_VAULT\",\"description\":\"e2e\"}" >/dev/null
mcp akb_delete "{\"vault\":\"$EMPTY_VAULT\",\"doc_id\":\"overview/vault-skill.md\"}" >/dev/null

H3=$(mcp akb_help "{\"topic\":\"vault-skill\",\"vault\":\"$EMPTY_VAULT\"}")
echo "$H3" | grep -q "No \`overview/vault-skill.md\` found" \
  && pass "Missing notice rendered" \
  || fail "T4.1" "missing notice not shown"

echo "$H3" | grep -q 'akb_put(' \
  && pass "akb_put template included in missing notice" \
  || fail "T4.2" "akb_put template missing"

echo "$H3" | grep -q '\${{secrets.X}}' \
  && pass "Fallback rules included" \
  || fail "T4.3" "fallback rules missing"

# ── 5. Author workflow: edit vault-skill, re-fetch ──────────
echo "▸ 5. Owner can edit vault-skill, akb_help returns updated body"

NEW_BODY="# Custom Vault Skill\n\nMy custom rules: report only."
mcp akb_update "{\"vault\":\"$VAULT\",\"doc_id\":\"overview/vault-skill.md\",\"content\":\"$NEW_BODY\"}" >/dev/null

H4=$(mcp akb_help "{\"topic\":\"vault-skill\",\"vault\":\"$VAULT\"}")
echo "$H4" | grep -q "My custom rules" \
  && pass "Edited body is returned" \
  || fail "T5.1" "edit did not propagate to akb_help"

GET2=$(mcp akb_get "{\"vault\":\"$VAULT\",\"doc_id\":\"overview/vault-skill.md\"}")
echo "$GET2" | grep -q '"type": *"skill"' \
  && pass "type=skill preserved across edit" \
  || fail "T5.2" "type changed after update"

# ── 6. doc_type='skill' is queryable ────────────────────────
echo "▸ 6. akb_search supports type='skill'"

S1=$(mcp akb_search "{\"vault\":\"$VAULT\",\"query\":\"vault\",\"type\":\"skill\"}")
echo "$S1" | grep -q "overview/vault-skill.md" \
  && pass "type=skill filter accepts and matches" \
  || fail "T6.1" "search with type=skill did not return the skill doc"

# ── Cleanup ──────────────────────────────────────────────────
mcp akb_delete_vault "{\"name\":\"$VAULT\"}" >/dev/null 2>&1
mcp akb_delete_vault "{\"name\":\"$EMPTY_VAULT\"}" >/dev/null 2>&1

# ── Summary ──────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════"
echo "  Passed: $PASS    Failed: $FAIL"
if [ $FAIL -gt 0 ]; then
  echo "  Errors:"
  for e in "${ERRORS[@]}"; do echo "    - $e"; done
  exit 1
fi
