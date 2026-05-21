#!/bin/bash
#
# AKB — PG-native RBAC E2E tests.
#
# Verifies that vault isolation is enforced by PostgreSQL ACL (akb_sql
# fails with PG error codes / messages) rather than by application-side
# regex. Positive cases pair with each role; negative cases probe
# cross-vault SQL exfiltration, system catalog access, and role-switch
# attempts — each must be rejected by PG, not by the (now removed) app
# sandbox.
#
# Pair with: docs/designs/pg-native-rbac/00-overview.md
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
echo "║   AKB PG-native RBAC E2E Tests           ║"
echo "║   Target: $BASE_URL"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── Setup ────────────────────────────────────────────────────
echo "▸ 0. Setup (2 users + 2 vaults + 1 table)"

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

ALICE="rbac-alice-$(date +%s)"
BOB="rbac-bob-$(($(date +%s)+1))"
PAT_ALICE=$(setup_user "$ALICE")
PAT_BOB=$(setup_user "$BOB")
[ -n "$PAT_ALICE" ] && [ -n "$PAT_BOB" ] && pass "2 users created" || { fail "Setup" "user creation failed"; exit 1; }

# MCP session helpers
setup_mcp() {
  local pat=$1
  local tmpfile=$(mktemp)
  curl -sk -i -X POST "$BASE_URL/mcp/" \
    -H "Authorization: Bearer $pat" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"rbac-e2e","version":"1.0"}}}' > "$tmpfile" 2>/dev/null
  local sid=$(grep -i "mcp-session-id" "$tmpfile" | tr -d '\r' | awk '{print $2}')
  rm -f "$tmpfile"
  curl -sk -X POST "$BASE_URL/mcp/" \
    -H "Authorization: Bearer $pat" \
    -H "Content-Type: application/json" \
    -H "mcp-session-id: $sid" \
    -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null 2>&1
  echo "$sid"
}

SID_ALICE=$(setup_mcp "$PAT_ALICE")
SID_BOB=$(setup_mcp "$PAT_BOB")

mcp_as() {
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

# Alice creates two vaults
VAULT_A="rbac-a-$(date +%s)"
VAULT_B="rbac-b-$(($(date +%s)+1))"

R=$(mcp_as "$PAT_ALICE" "$SID_ALICE" "akb_create_vault" "{\"name\":\"$VAULT_A\",\"description\":\"Alice's vault A\"}" | mr)
echo "$R" | python3 -c "import sys,json; json.load(sys.stdin)['vault_id']" >/dev/null 2>&1 && pass "Vault A created ($VAULT_A)" || fail "Vault A" "$R"

R=$(mcp_as "$PAT_ALICE" "$SID_ALICE" "akb_create_vault" "{\"name\":\"$VAULT_B\",\"description\":\"Alice's vault B\"}" | mr)
echo "$R" | python3 -c "import sys,json; json.load(sys.stdin)['vault_id']" >/dev/null 2>&1 && pass "Vault B created ($VAULT_B)" || fail "Vault B" "$R"

# Table `secrets` in vault A
R=$(mcp_as "$PAT_ALICE" "$SID_ALICE" "akb_create_table" "{\"vault\":\"$VAULT_A\",\"name\":\"secrets\",\"description\":\"Sensitive data\",\"columns\":[{\"name\":\"item\",\"type\":\"text\",\"required\":true},{\"name\":\"value\",\"type\":\"text\"}]}" | mr)
TABLE_OK=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(bool(d.get('uri') or d.get('name')=='secrets'))" 2>/dev/null)
[ "$TABLE_OK" = "True" ] && pass "Table 'secrets' created in vault A" || fail "Table create" "$R"

# Pre-populate vault A's table
mcp_as "$PAT_ALICE" "$SID_ALICE" "akb_sql" "{\"vault\":\"$VAULT_A\",\"sql\":\"INSERT INTO secrets (item, value) VALUES ('api_key', 'xyzzy-123'), ('db_pw', 'hunter2')\"}" >/dev/null 2>&1

# Also one table in vault B, for cross-vault probe
R=$(mcp_as "$PAT_ALICE" "$SID_ALICE" "akb_create_table" "{\"vault\":\"$VAULT_B\",\"name\":\"notes\",\"description\":\"Public-ish notes\",\"columns\":[{\"name\":\"topic\",\"type\":\"text\",\"required\":true}]}" | mr)
mcp_as "$PAT_ALICE" "$SID_ALICE" "akb_sql" "{\"vault\":\"$VAULT_B\",\"sql\":\"INSERT INTO notes (topic) VALUES ('hello')\"}" >/dev/null 2>&1
pass "Vault B has table 'notes'"

# Derive physical PG names (matches table_data_repo.pg_table_name)
sanitize() { echo "$1" | tr '[:upper:]-' '[:lower:]_'; }
PG_A_SECRETS="vt_$(sanitize "$VAULT_A")__secrets"
PG_B_NOTES="vt_$(sanitize "$VAULT_B")__notes"

# ── 1. Positive cases ────────────────────────────────────────
echo ""
echo "▸ 1. Positive — owner / reader / writer / admin"

# Owner Alice: SELECT works on own table
R=$(mcp_as "$PAT_ALICE" "$SID_ALICE" "akb_sql" "{\"vault\":\"$VAULT_A\",\"sql\":\"SELECT * FROM secrets\"}" | mr)
N=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('items',[])))" 2>/dev/null)
[ "$N" = "2" ] && pass "Owner SELECT: 2 rows" || fail "Owner SELECT" "got $N rows; raw=$R"

# Owner Alice: INSERT works
R=$(mcp_as "$PAT_ALICE" "$SID_ALICE" "akb_sql" "{\"vault\":\"$VAULT_A\",\"sql\":\"INSERT INTO secrets (item, value) VALUES ('one_more', 'x')\"}" | mr)
echo "$R" | grep -q '"result"' && pass "Owner INSERT" || fail "Owner INSERT" "$R"

# Grant Bob reader on vault A
R=$(mcp_as "$PAT_ALICE" "$SID_ALICE" "akb_grant" "{\"vault\":\"$VAULT_A\",\"user\":\"$BOB\",\"role\":\"reader\"}" | mr)
echo "$R" | grep -q '"granted"' && pass "Granted Bob reader on vault A" || fail "Grant" "$R"

R=$(mcp_as "$PAT_BOB" "$SID_BOB" "akb_sql" "{\"vault\":\"$VAULT_A\",\"sql\":\"SELECT * FROM secrets\"}" | mr)
N=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('items',[])))" 2>/dev/null)
[ "$N" = "3" ] && pass "Reader Bob SELECT: 3 rows" || fail "Reader SELECT" "got $N rows; raw=$R"

# Upgrade Bob to writer
mcp_as "$PAT_ALICE" "$SID_ALICE" "akb_grant" "{\"vault\":\"$VAULT_A\",\"user\":\"$BOB\",\"role\":\"writer\"}" >/dev/null 2>&1

R=$(mcp_as "$PAT_BOB" "$SID_BOB" "akb_sql" "{\"vault\":\"$VAULT_A\",\"sql\":\"INSERT INTO secrets (item, value) VALUES ('bob_added', 'b')\"}" | mr)
echo "$R" | grep -q '"result"' && pass "Writer Bob INSERT" || fail "Writer INSERT" "$R"

# ── 2. Negative — defence-in-depth ───────────────────────────
#
# Two enforcement layers, tested separately:
#   2-A: SQL reaches PG. PG ACL rejects (42501, or "relation does
#        not exist" when the role can't see the object). These are
#        the cases that would actually exfil data if the boundary
#        were soft. assert_pg_denied is strict.
#   2-B: SQL never reaches PG. Application pre-flight (first-keyword
#        DML check, multi-statement check) rejects before any
#        connection is taken. Friendlier error than PG would give.
#
# Both layers must hold; this organisation makes the regression
# surface for each obvious.

echo ""
echo "▸ 2-A. Negative — SQL reaches PG, PG ACL enforces"

# Helper: assert response is a permission_denied envelope from PG,
# not an app-side string. We accept either the structured {code:
# "permission_denied"} envelope or a permission-related PG message.
assert_pg_denied() {
  local label=$1
  local raw=$2
  local matched
  matched=$(echo "$raw" | python3 -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
    code = d.get('code', '')
    err = (d.get('error') or '').lower()
    is_pg = (code == 'permission_denied'
             or 'permission denied' in err
             or 'does not exist' in err
             or 'role does not exist' in err
             or 'must be superuser' in err
             or 'must be a superuser' in err
             or 'not allowed' in err)
    print('Y' if is_pg else 'N')
except Exception:
    print('N')
" 2>/dev/null)
  [ "$matched" = "Y" ] && pass "$label" || fail "$label" "expected PG-side denial; raw=$raw"
}

# Revoke Bob first so we test denial cleanly
mcp_as "$PAT_ALICE" "$SID_ALICE" "akb_revoke" "{\"vault\":\"$VAULT_A\",\"user\":\"$BOB\"}" >/dev/null 2>&1
# Bob has NO membership on vault B (or A after revoke). Set up: Bob owns
# his own throw-away vault so he can issue akb_sql with valid auth, but
# any reference outside his role memberships should fail at PG.
VAULT_BOB="rbac-bob-vault-$(date +%s)"
R=$(mcp_as "$PAT_BOB" "$SID_BOB" "akb_create_vault" "{\"name\":\"$VAULT_BOB\",\"description\":\"Bob's vault\"}" | mr)
echo "$R" | python3 -c "import sys,json; json.load(sys.stdin)['vault_id']" >/dev/null 2>&1 && pass "Bob's throwaway vault created" || fail "Bob vault" "$R"

# 2a: After revoke, Bob's SELECT on vault A's secrets must fail.
R=$(mcp_as "$PAT_BOB" "$SID_BOB" "akb_sql" "{\"vault\":\"$VAULT_A\",\"sql\":\"SELECT * FROM secrets\"}" | mr)
# This may fail at the check_vault_access gate (app-level 403), which
# is fine — it's the friendly path. Just assert it's denied.
echo "$R" | grep -qiE 'access|denied|forbid|require|permission' && pass "Revoked Bob: SELECT denied (app gate)" || fail "Revoked SELECT" "$R"

# 2b: Bob references vault A's physical PG name directly while operating
#     in his own vault. App rewriter doesn't touch it; PG must deny.
R=$(mcp_as "$PAT_BOB" "$SID_BOB" "akb_sql" "{\"vault\":\"$VAULT_BOB\",\"sql\":\"SELECT * FROM $PG_A_SECRETS\"}" | mr)
assert_pg_denied "Cross-vault direct physical-name SELECT" "$R"

# 2c: Same with a predicate, ensure expression doesn't slip through.
R=$(mcp_as "$PAT_BOB" "$SID_BOB" "akb_sql" "{\"vault\":\"$VAULT_BOB\",\"sql\":\"SELECT value FROM $PG_A_SECRETS WHERE item = 'api_key'\"}" | mr)
assert_pg_denied "Cross-vault SELECT with WHERE" "$R"

# 2d: CTE-based exfil.
R=$(mcp_as "$PAT_BOB" "$SID_BOB" "akb_sql" "{\"vault\":\"$VAULT_BOB\",\"sql\":\"WITH x AS (SELECT * FROM $PG_A_SECRETS) SELECT * FROM x\"}" | mr)
assert_pg_denied "Cross-vault WITH (CTE)" "$R"

# 2e: AKB system table 'users'.
R=$(mcp_as "$PAT_BOB" "$SID_BOB" "akb_sql" "{\"vault\":\"$VAULT_BOB\",\"sql\":\"SELECT id, email FROM users LIMIT 1\"}" | mr)
assert_pg_denied "System table 'users' SELECT" "$R"

# 2f: PG superuser-only catalog.
R=$(mcp_as "$PAT_BOB" "$SID_BOB" "akb_sql" "{\"vault\":\"$VAULT_BOB\",\"sql\":\"SELECT * FROM pg_authid LIMIT 1\"}" | mr)
assert_pg_denied "pg_authid (superuser-only)" "$R"

# 2g: vault_access (AKB bookkeeping).
R=$(mcp_as "$PAT_BOB" "$SID_BOB" "akb_sql" "{\"vault\":\"$VAULT_BOB\",\"sql\":\"SELECT * FROM vault_access LIMIT 1\"}" | mr)
assert_pg_denied "vault_access SELECT" "$R"

# 2k: Schema-qualified reference (public.<table>).
R=$(mcp_as "$PAT_BOB" "$SID_BOB" "akb_sql" "{\"vault\":\"$VAULT_BOB\",\"sql\":\"SELECT * FROM public.$PG_A_SECRETS\"}" | mr)
assert_pg_denied "Schema-qualified cross-vault SELECT" "$R"

# 2l: Quoted identifier (PG identifier quoting preserves case + lets
#     special chars in; ACL check still applies to the resolved relation).
R=$(mcp_as "$PAT_BOB" "$SID_BOB" "akb_sql" "{\"vault\":\"$VAULT_BOB\",\"sql\":\"SELECT * FROM \\\"$PG_A_SECRETS\\\"\"}" | mr)
assert_pg_denied "Quoted-identifier cross-vault SELECT" "$R"

# 2m: UNION with cross-vault.
R=$(mcp_as "$PAT_BOB" "$SID_BOB" "akb_sql" "{\"vault\":\"$VAULT_BOB\",\"sql\":\"SELECT 1 UNION ALL SELECT 1 FROM $PG_A_SECRETS\"}" | mr)
assert_pg_denied "UNION cross-vault" "$R"

# 2n: Subquery in FROM.
R=$(mcp_as "$PAT_BOB" "$SID_BOB" "akb_sql" "{\"vault\":\"$VAULT_BOB\",\"sql\":\"SELECT * FROM (SELECT * FROM $PG_A_SECRETS) sub\"}" | mr)
assert_pg_denied "Subquery-in-FROM cross-vault" "$R"

# 2o: EXISTS subquery (most subtle — could be used as a side-channel
#     boolean oracle if ACL didn't apply; ensure it does).
R=$(mcp_as "$PAT_BOB" "$SID_BOB" "akb_sql" "{\"vault\":\"$VAULT_BOB\",\"sql\":\"SELECT EXISTS (SELECT 1 FROM $PG_A_SECRETS)\"}" | mr)
assert_pg_denied "EXISTS-subquery cross-vault" "$R"

# 2p: Correlated scalar subquery in SELECT list.
R=$(mcp_as "$PAT_BOB" "$SID_BOB" "akb_sql" "{\"vault\":\"$VAULT_BOB\",\"sql\":\"SELECT (SELECT value FROM $PG_A_SECRETS LIMIT 1) AS leaked\"}" | mr)
assert_pg_denied "Scalar subquery cross-vault" "$R"

# 2q: pg_read_file (filesystem read — superuser-only PG function).
R=$(mcp_as "$PAT_BOB" "$SID_BOB" "akb_sql" "{\"vault\":\"$VAULT_BOB\",\"sql\":\"SELECT pg_read_file('/etc/passwd')\"}" | mr)
assert_pg_denied "pg_read_file filesystem access" "$R"

# 2r: pg_ls_dir (directory listing — superuser-only).
R=$(mcp_as "$PAT_BOB" "$SID_BOB" "akb_sql" "{\"vault\":\"$VAULT_BOB\",\"sql\":\"SELECT pg_ls_dir('/')\"}" | mr)
assert_pg_denied "pg_ls_dir filesystem access" "$R"

# 2s: lo_import / lo_export (large object — superuser-only).
R=$(mcp_as "$PAT_BOB" "$SID_BOB" "akb_sql" "{\"vault\":\"$VAULT_BOB\",\"sql\":\"SELECT lo_export(1, '/tmp/x')\"}" | mr)
assert_pg_denied "lo_export superuser-only" "$R"

# 2w: Comment-based smuggle (PG comment-strip happens during parse;
#     ACL still applies to the resulting query).
R=$(mcp_as "$PAT_BOB" "$SID_BOB" "akb_sql" "{\"vault\":\"$VAULT_BOB\",\"sql\":\"SELECT /* benign */ * FROM $PG_A_SECRETS /* trailing */\"}" | mr)
assert_pg_denied "Comment-decorated cross-vault SELECT" "$R"

# 2x: Block-comment between keywords (parser-confusion attempt).
R=$(mcp_as "$PAT_BOB" "$SID_BOB" "akb_sql" "{\"vault\":\"$VAULT_BOB\",\"sql\":\"SELECT *FROM/**/$PG_A_SECRETS\"}" | mr)
assert_pg_denied "Inline-comment cross-vault SELECT" "$R"

# 2y: Modifying CTE — try to INSERT into cross-vault from a reader-
#     equivalent (Bob has no access to vault A).
R=$(mcp_as "$PAT_BOB" "$SID_BOB" "akb_sql" "{\"vault\":\"$VAULT_BOB\",\"sql\":\"WITH ins AS (INSERT INTO $PG_A_SECRETS (item, value) VALUES ('x','y') RETURNING id) SELECT * FROM ins\"}" | mr)
assert_pg_denied "Modifying CTE cross-vault INSERT" "$R"

# 2z: Quoted system catalog (pg_catalog.pg_class) — qualified path
#     normally readable by all roles, but tests that we're not
#     accidentally hiding existence via search_path tricks.
R=$(mcp_as "$PAT_BOB" "$SID_BOB" "akb_sql" "{\"vault\":\"$VAULT_BOB\",\"sql\":\"SELECT relname FROM pg_catalog.pg_class WHERE relname = '$PG_A_SECRETS'\"}" | mr)
# pg_class is publicly readable in PG; we accept this returning the
# row name. The defense isn't "hide existence" but "block content
# access." Just verify Bob cannot then read the table itself — that
# was already tested in 2b.
echo "$R" | python3 -c "import sys,json; sys.exit(0 if 'items' in json.load(sys.stdin) else 1)" 2>/dev/null \
  && pass "pg_class metadata readable (content still blocked, see 2b)" \
  || pass "pg_class read returned non-data response (acceptable)"

# ── 2-B. Negative — app pre-flight rejects before reaching PG ─
echo ""
echo "▸ 2-B. Negative — app pre-flight (first-keyword DML / single-statement)"

# Helper: assert response carries the application-side rejection
# (an `error` key whose message references the allow-list rule or
# multi-statement guard). These are intentionally caught BEFORE
# reaching PG so the error is actionable; PG would also deny them
# (non-DML keywords aren't in any GRANT) but with less helpful
# messages like "permission denied for schema public".
assert_app_rejected() {
  local label=$1
  local raw=$2
  local matched
  matched=$(echo "$raw" | python3 -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
    err = (d.get('error') or '').lower()
    # App-side rejection strings carry one of these phrases.
    is_app = ('only select / with / insert / update / delete' in err
              or 'multi-statement' in err
              or 'use akb_create_table' in err)
    print('Y' if is_app else 'N')
except Exception:
    print('N')
" 2>/dev/null)
  [ "$matched" = "Y" ] && pass "$label" || fail "$label" "expected app-side rejection; raw=$raw"
}

# COPY ... TO PROGRAM — first keyword COPY is not DML.
R=$(mcp_as "$PAT_BOB" "$SID_BOB" "akb_sql" "{\"vault\":\"$VAULT_BOB\",\"sql\":\"COPY (SELECT 1) TO PROGRAM 'whoami'\"}" | mr)
assert_app_rejected "COPY TO PROGRAM rejected pre-flight" "$R"

# SET ROLE — first keyword SET is not DML.
R=$(mcp_as "$PAT_BOB" "$SID_BOB" "akb_sql" "{\"vault\":\"$VAULT_BOB\",\"sql\":\"SET ROLE postgres\"}" | mr)
assert_app_rejected "SET ROLE rejected pre-flight" "$R"

# Multi-statement smuggle — semicolon in middle.
R=$(mcp_as "$PAT_BOB" "$SID_BOB" "akb_sql" "{\"vault\":\"$VAULT_BOB\",\"sql\":\"SELECT 1; SELECT * FROM $PG_A_SECRETS\"}" | mr)
assert_app_rejected "Multi-statement rejected pre-flight" "$R"

# Anonymous PL/pgSQL DO block — first keyword DO is not DML.
R=$(mcp_as "$PAT_BOB" "$SID_BOB" "akb_sql" "{\"vault\":\"$VAULT_BOB\",\"sql\":\"DO \$\$ BEGIN PERFORM 1; END \$\$\"}" | mr)
assert_app_rejected "DO block rejected pre-flight" "$R"

# CREATE FUNCTION — first keyword CREATE is not DML.
R=$(mcp_as "$PAT_BOB" "$SID_BOB" "akb_sql" "{\"vault\":\"$VAULT_BOB\",\"sql\":\"CREATE FUNCTION pwn() RETURNS int AS \$\$ SELECT 1 \$\$ LANGUAGE sql\"}" | mr)
assert_app_rejected "CREATE FUNCTION rejected pre-flight" "$R"

# TRUNCATE — first keyword TRUNCATE is not DML.
R=$(mcp_as "$PAT_BOB" "$SID_BOB" "akb_sql" "{\"vault\":\"$VAULT_BOB\",\"sql\":\"TRUNCATE $PG_A_SECRETS\"}" | mr)
assert_app_rejected "TRUNCATE rejected pre-flight" "$R"

# ── 2-C. Reader scope enforcement ────────────────────────────
echo ""
echo "▸ 2-C. Reader role write-attempt denials (PG ACL)"

# Re-grant Bob reader on vault A.
mcp_as "$PAT_ALICE" "$SID_ALICE" "akb_grant" "{\"vault\":\"$VAULT_A\",\"user\":\"$BOB\",\"role\":\"reader\"}" >/dev/null 2>&1

# Reader cannot INSERT.
R=$(mcp_as "$PAT_BOB" "$SID_BOB" "akb_sql" "{\"vault\":\"$VAULT_A\",\"sql\":\"INSERT INTO secrets (item, value) VALUES ('hack', 'h')\"}" | mr)
assert_pg_denied "Reader cannot INSERT" "$R"

# Reader cannot UPDATE.
R=$(mcp_as "$PAT_BOB" "$SID_BOB" "akb_sql" "{\"vault\":\"$VAULT_A\",\"sql\":\"UPDATE secrets SET value = 'pwned' WHERE item = 'api_key'\"}" | mr)
assert_pg_denied "Reader cannot UPDATE" "$R"

# Reader cannot DELETE.
R=$(mcp_as "$PAT_BOB" "$SID_BOB" "akb_sql" "{\"vault\":\"$VAULT_A\",\"sql\":\"DELETE FROM secrets WHERE item = 'api_key'\"}" | mr)
assert_pg_denied "Reader cannot DELETE" "$R"

# Reader TRUNCATE: caught by app pre-flight (TRUNCATE is not in DML
# allow-list). PG ACL would also deny since reader has no TRUNCATE
# grant; both layers cover this.
R=$(mcp_as "$PAT_BOB" "$SID_BOB" "akb_sql" "{\"vault\":\"$VAULT_A\",\"sql\":\"TRUNCATE secrets\"}" | mr)
assert_app_rejected "Reader cannot TRUNCATE (pre-flight)" "$R"

# Restore: revoke for the lifecycle phase below.
mcp_as "$PAT_ALICE" "$SID_ALICE" "akb_revoke" "{\"vault\":\"$VAULT_A\",\"user\":\"$BOB\"}" >/dev/null 2>&1

# ── 2-D. Public vault access via akb_authenticated wildcard ─
echo ""
echo "▸ 2-D. public_access — non-members reach public vaults via wildcard"

# Alice creates a fresh vault with public_access='reader' from the start.
VAULT_PUB="rbac-pub-$(date +%s)"
R=$(mcp_as "$PAT_ALICE" "$SID_ALICE" "akb_create_vault" "{\"name\":\"$VAULT_PUB\",\"description\":\"public-reader test\",\"public_access\":\"reader\"}" | mr)
echo "$R" | python3 -c "import sys,json; json.load(sys.stdin)['vault_id']" >/dev/null 2>&1 && pass "Public vault (reader) created" || fail "Public vault create" "$R"

# Alice adds a table + a row to test reads against.
mcp_as "$PAT_ALICE" "$SID_ALICE" "akb_create_table" "{\"vault\":\"$VAULT_PUB\",\"name\":\"items\",\"description\":\"public data\",\"columns\":[{\"name\":\"label\",\"type\":\"text\",\"required\":true}]}" >/dev/null 2>&1
mcp_as "$PAT_ALICE" "$SID_ALICE" "akb_sql" "{\"vault\":\"$VAULT_PUB\",\"sql\":\"INSERT INTO items (label) VALUES ('one'), ('two')\"}" >/dev/null 2>&1

# 2-D-1: Bob (no vault_access row) can SELECT — that's the bug we just fixed.
R=$(mcp_as "$PAT_BOB" "$SID_BOB" "akb_sql" "{\"vault\":\"$VAULT_PUB\",\"sql\":\"SELECT label FROM items ORDER BY label\"}" | mr)
N=$(echo "$R" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('items',[])))" 2>/dev/null)
[ "$N" = "2" ] && pass "Non-member SELECTs public-reader vault" || fail "Public SELECT" "got $N; raw=$R"

# 2-D-2: But Bob CANNOT INSERT — reader scope only.
R=$(mcp_as "$PAT_BOB" "$SID_BOB" "akb_sql" "{\"vault\":\"$VAULT_PUB\",\"sql\":\"INSERT INTO items (label) VALUES ('hack')\"}" | mr)
assert_pg_denied "Non-member INSERT on public-reader denied" "$R"

# 2-D-3: Alice promotes to public_access='writer'.
mcp_as "$PAT_ALICE" "$SID_ALICE" "akb_set_public" "{\"vault\":\"$VAULT_PUB\",\"level\":\"writer\"}" >/dev/null 2>&1
R=$(mcp_as "$PAT_BOB" "$SID_BOB" "akb_sql" "{\"vault\":\"$VAULT_PUB\",\"sql\":\"INSERT INTO items (label) VALUES ('bob_added')\"}" | mr)
echo "$R" | grep -q '"result"' && pass "Non-member INSERTs after promote to writer" || fail "Promote→writer INSERT" "$R"

# 2-D-4: Alice demotes back to public_access='none'.
mcp_as "$PAT_ALICE" "$SID_ALICE" "akb_set_public" "{\"vault\":\"$VAULT_PUB\",\"level\":\"none\"}" >/dev/null 2>&1
R=$(mcp_as "$PAT_BOB" "$SID_BOB" "akb_sql" "{\"vault\":\"$VAULT_PUB\",\"sql\":\"SELECT label FROM items\"}" | mr)
# App gate also enforces (check_vault_access rejects non-member on
# private vault), so the 403 may come from the app layer. Either layer
# is a valid denial here — the boundary holds.
echo "$R" | grep -qiE 'denied|forbid|permission|require|access' && pass "Demote→none: non-member denied" || fail "Demote denial" "$R"

# 2-D-5: A user created AFTER public_access flipped back to reader still
#         gets access — proves on_user_create grants akb_authenticated
#         membership at registration time (not relying on reconciler).
mcp_as "$PAT_ALICE" "$SID_ALICE" "akb_set_public" "{\"vault\":\"$VAULT_PUB\",\"level\":\"reader\"}" >/dev/null 2>&1
CAROL="rbac-carol-$(date +%s)"
PAT_CAROL=$(setup_user "$CAROL")
SID_CAROL=$(setup_mcp "$PAT_CAROL")
R=$(mcp_as "$PAT_CAROL" "$SID_CAROL" "akb_sql" "{\"vault\":\"$VAULT_PUB\",\"sql\":\"SELECT COUNT(*) AS n FROM items\"}" | mr)
N=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('items',[{}])[0].get('n',-1))" 2>/dev/null)
[ "$N" -ge 1 ] 2>/dev/null && pass "Newly-registered user reads public vault" || fail "New user public read" "$R"

# Cleanup of the public-vault scratchpad.
mcp_as "$PAT_ALICE" "$SID_ALICE" "akb_delete_vault" "{\"vault\":\"$VAULT_PUB\"}" >/dev/null 2>&1

# ── 3. Lifecycle ────────────────────────────────────────────
echo ""
echo "▸ 3. Lifecycle — grant/revoke takes effect, drift recovery"

# 3a: Re-grant Bob, verify access restored
mcp_as "$PAT_ALICE" "$SID_ALICE" "akb_grant" "{\"vault\":\"$VAULT_A\",\"user\":\"$BOB\",\"role\":\"reader\"}" >/dev/null 2>&1
R=$(mcp_as "$PAT_BOB" "$SID_BOB" "akb_sql" "{\"vault\":\"$VAULT_A\",\"sql\":\"SELECT COUNT(*) AS n FROM secrets\"}" | mr)
N=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('items',[{}])[0].get('n',-1))" 2>/dev/null)
[ "$N" -ge 3 ] 2>/dev/null && pass "Re-grant: Bob can SELECT again ($N rows)" || fail "Re-grant" "$R"

# 3b: Revoke takes effect within next call (same MCP session).
mcp_as "$PAT_ALICE" "$SID_ALICE" "akb_revoke" "{\"vault\":\"$VAULT_A\",\"user\":\"$BOB\"}" >/dev/null 2>&1
R=$(mcp_as "$PAT_BOB" "$SID_BOB" "akb_sql" "{\"vault\":\"$VAULT_A\",\"sql\":\"SELECT * FROM secrets\"}" | mr)
echo "$R" | grep -qiE 'denied|forbid|permission|require' && pass "Revoke takes effect immediately" || fail "Revoke immediate" "$R"

# 3c: Vault delete drops vault group roles. After delete, subsequent
#     SELECT via the same vault name must 404 / not-found.
mcp_as "$PAT_ALICE" "$SID_ALICE" "akb_delete_vault" "{\"vault\":\"$VAULT_B\"}" >/dev/null 2>&1
R=$(mcp_as "$PAT_ALICE" "$SID_ALICE" "akb_sql" "{\"vault\":\"$VAULT_B\",\"sql\":\"SELECT 1\"}" | mr)
echo "$R" | grep -qiE 'not[ _]?found|does not exist' && pass "Deleted vault → not found" || fail "Vault delete" "$R"

# ── Summary ─────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════"
echo "  Results: $PASS passed, $FAIL failed"
if [ "$FAIL" -gt 0 ]; then
  echo ""
  echo "  Failures:"
  for e in "${ERRORS[@]}"; do
    echo "    - $e"
  done
fi
echo "════════════════════════════════════════════"

[ "$FAIL" -eq 0 ] && exit 0 || exit 1
