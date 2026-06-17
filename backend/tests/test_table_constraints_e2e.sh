#!/bin/bash
#
# AKB #215 E2E — declarative unique_keys + indexes on the table DDL tools.
# Self-contained: registers a user, creates a vault, and exercises the
# create/alter/sql paths over MCP Streamable HTTP. Covers acceptance
# criteria #1-#6, #10, #11 (unique/index) and the akb_sql DML-only
# boundary + non-admin alter permission gate (#13).
#
#   AKB_URL=http://localhost:18080 bash tests/test_table_constraints_e2e.sh
#
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
SUF="$(date +%s)-$$"
VAULT="uk-e2e-$SUF"
USER="uk-user-$SUF"
USER2="uk-reader-$SUF"
PASS=0
FAIL=0
ERRORS=()
pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

echo "▸ #215 unique_keys + indexes e2e → $BASE_URL"

# ── helpers ──────────────────────────────────────────────────────
register_pat() {  # $1=username → echoes PAT
  local u=$1
  curl -sk -X POST "$BASE_URL/api/v1/auth/register" -H 'Content-Type: application/json' \
    -d "{\"username\":\"$u\",\"email\":\"$u@test.dev\",\"password\":\"test1234\"}" >/dev/null 2>&1
  local jwt
  jwt=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" -H 'Content-Type: application/json' \
    -d "{\"username\":\"$u\",\"password\":\"test1234\"}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])' 2>/dev/null)
  curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" -H "Authorization: Bearer $jwt" \
    -H 'Content-Type: application/json' -d '{"name":"e2e"}' \
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])' 2>/dev/null
}

mcp_session() {  # $1=PAT → echoes SID
  curl -sk -i -X POST "$BASE_URL/mcp/" -H "Authorization: Bearer $1" \
    -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"e2e","version":"1.0"}}}' 2>&1 \
    | grep -i "mcp-session-id" | tr -d '\r' | awk '{print $2}'
}

MCP_ID=10
mcp() {  # $1=PAT $2=SID $3=tool $4=args-json → echoes result text (content[0].text)
  MCP_ID=$((MCP_ID+1))
  curl -sk -X POST "$BASE_URL/mcp/" -H "Authorization: Bearer $1" \
    -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
    -H "mcp-session-id: $2" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":$MCP_ID,\"method\":\"tools/call\",\"params\":{\"name\":\"$3\",\"arguments\":$4}}" 2>&1 \
    | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(d['result']['content'][0]['text'])" 2>/dev/null
}
field() { python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(d$1)" 2>/dev/null; }
# Strict success assertion: response must be valid JSON object with NO error/code
# envelope (catches malformed/empty/error responses that "no code = success"
# greps would silently pass). #220 review hardening.
assert_ok() { # $1=response $2=label
  echo "$1" | python3 -c "import sys,json
d=json.loads(sys.stdin.read())
assert isinstance(d, dict) and 'code' not in d and 'error' not in d, d" >/dev/null 2>&1 \
    && pass "$2" || fail "$2" "not a clean success: $1"
}

# ── 0. setup ─────────────────────────────────────────────────────
PAT=$(register_pat "$USER")
[ -n "$PAT" ] && pass "PAT acquired" || { fail "setup" "no PAT"; exit 1; }
SID=$(mcp_session "$PAT")
[ -n "$SID" ] && pass "MCP session" || { fail "setup" "no session"; exit 1; }
curl -sk -X POST "$BASE_URL/mcp/" -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null 2>&1
R=$(mcp "$PAT" "$SID" akb_create_vault "{\"name\":\"$VAULT\",\"description\":\"#215 e2e\"}")
echo "$R" | field "['name']" | grep -q "$VAULT" && pass "vault created" || fail "create_vault" "$R"

# ── AC#1: create table with composite unique key + ordered multi-col index
R=$(mcp "$PAT" "$SID" akb_create_table "{\"vault\":\"$VAULT\",\"name\":\"events\",\"columns\":[{\"name\":\"principal_id\",\"type\":\"text\",\"required\":true},{\"name\":\"session_id\",\"type\":\"text\",\"required\":true},{\"name\":\"seq\",\"type\":\"number\",\"required\":true}],\"unique_keys\":[{\"columns\":[\"principal_id\",\"session_id\",\"seq\"]}],\"indexes\":[{\"columns\":[{\"name\":\"principal_id\"},{\"name\":\"session_id\"},{\"name\":\"seq\",\"order\":\"desc\"}]}]}")
UK_NAME=$(echo "$R" | field "['unique_keys'][0]['name']")
IDX_NAME=$(echo "$R" | field "['indexes'][0]['name']")
[ -n "$UK_NAME" ] && pass "AC#1 create returns unique_keys ($UK_NAME)" || fail "AC#1 unique_keys" "$R"
[ -n "$IDX_NAME" ] && pass "AC#1 create returns indexes ($IDX_NAME)" || fail "AC#1 indexes" "$R"

# ── AC#11: vault_info introspection exposes declared guarantees
R=$(mcp "$PAT" "$SID" akb_vault_info "{\"vault\":\"$VAULT\"}")
VI_UK=$(echo "$R" | field "['tables'][0]['unique_keys'][0]['columns']")
echo "$VI_UK" | grep -q "principal_id" && pass "AC#11 vault_info exposes unique_keys" || fail "AC#11 introspection" "$R"
echo "$R" | field "['tables'][0]['indexes'][0]['name']" | grep -q "idx" && pass "AC#11 vault_info exposes indexes" || fail "AC#11 indexes" "$R"

# ── AC#2: duplicate INSERT through akb_sql fails with a STABLE code
R=$(mcp "$PAT" "$SID" akb_sql "{\"vault\":\"$VAULT\",\"sql\":\"INSERT INTO events (principal_id, session_id, seq) VALUES ('p1','s1',1)\"}")
assert_ok "$R" "AC#2 first insert ok"
R=$(mcp "$PAT" "$SID" akb_sql "{\"vault\":\"$VAULT\",\"sql\":\"INSERT INTO events (principal_id, session_id, seq) VALUES ('p1','s1',1)\"}")
CODE=$(echo "$R" | field "['code']")
SQLSTATE=$(echo "$R" | field "['details']['pg_sqlstate']")
{ [ "$CODE" = "unique_violation" ] || [ "$SQLSTATE" = "23505" ]; } \
  && pass "AC#2 duplicate insert → stable unique_violation (code=$CODE sqlstate=$SQLSTATE)" \
  || fail "AC#2 duplicate stable error" "code=$CODE sqlstate=$SQLSTATE resp=$R"

# ── AC#3: alter add/drop unique key
R=$(mcp "$PAT" "$SID" akb_alter_table "{\"uri\":\"akb://$VAULT/table/events\",\"add_unique_keys\":[{\"name\":\"events_principal_key\",\"columns\":[\"principal_id\"]}]}")
echo "$R" | field "['unique_keys']" | grep -q "events_principal_key" && pass "AC#3 alter add_unique_keys" || fail "AC#3 add_unique_keys" "$R"
R=$(mcp "$PAT" "$SID" akb_alter_table "{\"uri\":\"akb://$VAULT/table/events\",\"drop_unique_keys\":[\"events_principal_key\"]}")
echo "$R" | field "['unique_keys']" | grep -q "events_principal_key" && fail "AC#3 drop_unique_keys" "still present: $R" || pass "AC#3 alter drop_unique_keys"

# ── AC#5/#6: alter add/drop index
R=$(mcp "$PAT" "$SID" akb_alter_table "{\"uri\":\"akb://$VAULT/table/events\",\"add_indexes\":[{\"name\":\"events_seq_idx\",\"columns\":[\"seq\"]}]}")
echo "$R" | field "['indexes']" | grep -q "events_seq_idx" && pass "AC#5/6 alter add_indexes" || fail "AC#5/6 add_indexes" "$R"
R=$(mcp "$PAT" "$SID" akb_alter_table "{\"uri\":\"akb://$VAULT/table/events\",\"drop_indexes\":[\"events_seq_idx\"]}")
echo "$R" | field "['indexes']" | grep -q "events_seq_idx" && fail "AC#5/6 drop_indexes" "still present: $R" || pass "AC#5/6 alter drop_indexes"

# ── AC#4/#10: add unique key on duplicate data fails PRE-DDL, schema unchanged
mcp "$PAT" "$SID" akb_create_table "{\"vault\":\"$VAULT\",\"name\":\"dups\",\"columns\":[{\"name\":\"email\",\"type\":\"text\"}]}" >/dev/null
mcp "$PAT" "$SID" akb_sql "{\"vault\":\"$VAULT\",\"sql\":\"INSERT INTO dups (email) VALUES ('a@x.dev')\"}" >/dev/null
mcp "$PAT" "$SID" akb_sql "{\"vault\":\"$VAULT\",\"sql\":\"INSERT INTO dups (email) VALUES ('a@x.dev')\"}" >/dev/null
R=$(mcp "$PAT" "$SID" akb_alter_table "{\"uri\":\"akb://$VAULT/table/dups\",\"add_unique_keys\":[{\"name\":\"dups_email_key\",\"columns\":[\"email\"]}]}")
CODE=$(echo "$R" | field "['code']")
[ "$CODE" = "invalid_argument" ] && pass "AC#4 preflight blocks add on duplicate data (code=$CODE)" || fail "AC#4 preflight" "code=$CODE resp=$R"
# schema+registry unchanged: vault_info shows dups has NO unique_keys
R=$(mcp "$PAT" "$SID" akb_vault_info "{\"vault\":\"$VAULT\"}")
DUPS_UK=$(echo "$R" | python3 -c "import sys,json;d=json.load(sys.stdin);t=[x for x in d['tables'] if x['name']=='dups'][0];print(len(x_uk) if (x_uk:=t.get('unique_keys')) else 0)" 2>/dev/null)
[ "$DUPS_UK" = "0" ] && pass "AC#10 failed alter left registry unchanged (dups.unique_keys empty)" || fail "AC#10 registry unchanged" "dups unique_keys=$DUPS_UK"
# constraint not physically created → a 3rd duplicate insert still succeeds (no UK enforced)
R=$(mcp "$PAT" "$SID" akb_sql "{\"vault\":\"$VAULT\",\"sql\":\"INSERT INTO dups (email) VALUES ('a@x.dev')\"}")
assert_ok "$R" "AC#10 failed alter left physical schema unchanged (dup INSERT still succeeds)"

# ── DML-only boundary: DDL via akb_sql is rejected
R=$(mcp "$PAT" "$SID" akb_sql "{\"vault\":\"$VAULT\",\"sql\":\"CREATE UNIQUE INDEX hack ON events (principal_id)\"}")
CODE=$(echo "$R" | field "['code']")
[ "$CODE" = "method_not_allowed" ] && pass "DDL via akb_sql rejected (code=$CODE)" || fail "akb_sql DDL boundary" "code=$CODE resp=$R"

# ── permission: non-admin (reader) cannot alter_table
PAT2=$(register_pat "$USER2")
SID2=$(mcp_session "$PAT2")
curl -sk -X POST "$BASE_URL/mcp/" -H "Authorization: Bearer $PAT2" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" -H "mcp-session-id: $SID2" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null 2>&1
mcp "$PAT" "$SID" akb_grant "{\"vault\":\"$VAULT\",\"user\":\"$USER2\",\"role\":\"reader\"}" >/dev/null
R=$(mcp "$PAT2" "$SID2" akb_alter_table "{\"uri\":\"akb://$VAULT/table/events\",\"add_indexes\":[{\"columns\":[\"principal_id\"]}]}")
# admin gate fires BEFORE any DDL (check_vault_access). Assert the alter was
# REJECTED (an error envelope, not a success table dict) and the message names
# the role denial. NOTE: the dispatch currently surfaces ForbiddenError under
# code=internal for every admin-gated tool (pre-existing, not #215) — we assert
# the denial, not the specific code.
CODE=$(echo "$R" | field "['code']")
DENIED=$(echo "$R" | python3 -c "import sys,json;d=json.loads(sys.stdin.read());m=(str(d.get('error',''))+str(d.get('code',''))).lower();print(bool(d.get('code')) and any(w in m for w in ('forbid','permission','admin','role','denied','access')))" 2>/dev/null)
[ "$DENIED" = "True" ] && pass "reader cannot alter_table (rejected; code=$CODE)" || fail "permission gate" "not rejected: $R"

# ── #220 review hardening ─────────────────────────────────────────
tbl_meta() { # $1=table $2=field(unique_keys|indexes) → python expr over vault_info
  mcp "$PAT" "$SID" akb_vault_info "{\"vault\":\"$VAULT\"}" | python3 -c "import sys,json;d=json.load(sys.stdin);t=[x for x in d['tables'] if x['name']=='$1'][0];print(json.dumps(t.get('$2',[])))" 2>/dev/null
}

# F3: PG UNIQUE is NULLS DISTINCT — multiple NULL rows must NOT block ADD CONSTRAINT
mcp "$PAT" "$SID" akb_create_table "{\"vault\":\"$VAULT\",\"name\":\"nul\",\"columns\":[{\"name\":\"email\",\"type\":\"text\"}]}" >/dev/null
mcp "$PAT" "$SID" akb_sql "{\"vault\":\"$VAULT\",\"sql\":\"INSERT INTO nul (email) VALUES (NULL)\"}" >/dev/null
mcp "$PAT" "$SID" akb_sql "{\"vault\":\"$VAULT\",\"sql\":\"INSERT INTO nul (email) VALUES (NULL)\"}" >/dev/null
R=$(mcp "$PAT" "$SID" akb_alter_table "{\"uri\":\"akb://$VAULT/table/nul\",\"add_unique_keys\":[{\"name\":\"nul_email_key\",\"columns\":[\"email\"]}]}")
assert_ok "$R" "F3 multiple-NULL rows don't block ADD UNIQUE (NULLS DISTINCT)"
mcp "$PAT" "$SID" akb_sql "{\"vault\":\"$VAULT\",\"sql\":\"INSERT INTO nul (email) VALUES ('a@b')\"}" >/dev/null
R=$(mcp "$PAT" "$SID" akb_sql "{\"vault\":\"$VAULT\",\"sql\":\"INSERT INTO nul (email) VALUES ('a@b')\"}")
[ "$(echo "$R" | field "['code']")" = "unique_violation" ] && pass "F3 non-null duplicate still enforced" || fail "F3 enforcement" "$R"

# F4: duplicate column within one key → clean 422 (not a DDL 500)
R=$(mcp "$PAT" "$SID" akb_create_table "{\"vault\":\"$VAULT\",\"name\":\"dupcol\",\"columns\":[{\"name\":\"a\",\"type\":\"text\"}],\"unique_keys\":[{\"columns\":[\"a\",\"a\"]}]}")
[ "$(echo "$R" | field "['code']")" = "invalid_argument" ] && pass "F4 duplicate column in key → invalid_argument" || fail "F4 dup column" "$R"

# F5: mixed-case column ref on an alter-add over duplicate data → clean 422 (was KeyError 500)
mcp "$PAT" "$SID" akb_create_table "{\"vault\":\"$VAULT\",\"name\":\"mc\",\"columns\":[{\"name\":\"principal\",\"type\":\"text\"}]}" >/dev/null
mcp "$PAT" "$SID" akb_sql "{\"vault\":\"$VAULT\",\"sql\":\"INSERT INTO mc (principal) VALUES ('p')\"}" >/dev/null
mcp "$PAT" "$SID" akb_sql "{\"vault\":\"$VAULT\",\"sql\":\"INSERT INTO mc (principal) VALUES ('p')\"}" >/dev/null
R=$(mcp "$PAT" "$SID" akb_alter_table "{\"uri\":\"akb://$VAULT/table/mc\",\"add_unique_keys\":[{\"name\":\"mc_principal_key\",\"columns\":[\"PRINCIPAL\"]}]}")
[ "$(echo "$R" | field "['code']")" = "invalid_argument" ] && pass "F5 mixed-case preflight → invalid_argument (not 500)" || fail "F5 mixed-case" "$R"
R=$(mcp "$PAT" "$SID" akb_create_table "{\"vault\":\"$VAULT\",\"name\":\"mc2\",\"columns\":[{\"name\":\"principal\",\"type\":\"text\"}],\"unique_keys\":[{\"columns\":[\"PRINCIPAL\"]}]}")
echo "$R" | field "['unique_keys'][0]['columns']" | grep -q "principal" && pass "F5b create stores canonical (lowercase) column name" || fail "F5b canonical" "$R"

# F1: rename a column used by a unique_key → registry metadata follows the rename
mcp "$PAT" "$SID" akb_create_table "{\"vault\":\"$VAULT\",\"name\":\"rn\",\"columns\":[{\"name\":\"old_c\",\"type\":\"text\"}],\"unique_keys\":[{\"name\":\"rn_key\",\"columns\":[\"old_c\"]}]}" >/dev/null
mcp "$PAT" "$SID" akb_alter_table "{\"uri\":\"akb://$VAULT/table/rn\",\"rename_columns\":{\"old_c\":\"new_c\"}}" >/dev/null
RNUK=$(tbl_meta rn unique_keys)
{ echo "$RNUK" | grep -q "new_c" && ! echo "$RNUK" | grep -q "old_c"; } && pass "F1 rename updates UK metadata ($RNUK)" || fail "F1 rename drift" "uk=$RNUK"

# F2: drop a column used by a unique_key → UK pruned from registry
mcp "$PAT" "$SID" akb_create_table "{\"vault\":\"$VAULT\",\"name\":\"dpc\",\"columns\":[{\"name\":\"keep\",\"type\":\"text\"},{\"name\":\"gone\",\"type\":\"text\"}],\"unique_keys\":[{\"name\":\"dpc_key\",\"columns\":[\"gone\"]}]}" >/dev/null
mcp "$PAT" "$SID" akb_alter_table "{\"uri\":\"akb://$VAULT/table/dpc\",\"drop_columns\":[\"gone\"]}" >/dev/null
[ "$(tbl_meta dpc unique_keys)" = "[]" ] && pass "F2 drop-column prunes UK from registry" || fail "F2 drop drift" "uk=$(tbl_meta dpc unique_keys)"

# critic: multi-op alter is all-or-nothing — valid add_indexes + dup add_unique_keys → BOTH rolled back
mcp "$PAT" "$SID" akb_create_table "{\"vault\":\"$VAULT\",\"name\":\"atom\",\"columns\":[{\"name\":\"x\",\"type\":\"text\"}]}" >/dev/null
mcp "$PAT" "$SID" akb_sql "{\"vault\":\"$VAULT\",\"sql\":\"INSERT INTO atom (x) VALUES ('d')\"}" >/dev/null
mcp "$PAT" "$SID" akb_sql "{\"vault\":\"$VAULT\",\"sql\":\"INSERT INTO atom (x) VALUES ('d')\"}" >/dev/null
R=$(mcp "$PAT" "$SID" akb_alter_table "{\"uri\":\"akb://$VAULT/table/atom\",\"add_indexes\":[{\"name\":\"atom_x_idx\",\"columns\":[\"x\"]}],\"add_unique_keys\":[{\"name\":\"atom_x_key\",\"columns\":[\"x\"]}]}")
[ "$(echo "$R" | field "['code']")" = "invalid_argument" ] && pass "multi-op alter rejected on dup data" || fail "multi-op rejected" "$R"
ATBOTH="$(tbl_meta atom indexes)$(tbl_meta atom unique_keys)"
[ "$ATBOTH" = "[][]" ] && pass "multi-op rollback left NEITHER index nor UK (single-TX atomicity)" || fail "multi-op atomicity" "idx+uk=$ATBOTH"

# strengthen drop AC: after drop_unique_keys, a previously-rejected dup INSERT now succeeds (physical-removal proof)
mcp "$PAT" "$SID" akb_alter_table "{\"uri\":\"akb://$VAULT/table/nul\",\"drop_unique_keys\":[\"nul_email_key\"]}" >/dev/null
R=$(mcp "$PAT" "$SID" akb_sql "{\"vault\":\"$VAULT\",\"sql\":\"INSERT INTO nul (email) VALUES ('a@b')\"}")
assert_ok "$R" "AC#3 dropped UK physically gone (dup INSERT now succeeds)"

# ── cleanup: drop the ephemeral vault + ALL its tables ────────────
# Required for idempotent re-runs: several tests use caller-supplied
# constraint/index names, and a UNIQUE constraint's implicit index name is
# SCHEMA-GLOBAL in PostgreSQL — leaving them behind makes a second run collide
# (and matches the AKB convention that tests clean up after themselves).
R=$(mcp "$PAT" "$SID" akb_delete_vault "{\"vault\":\"$VAULT\"}")
assert_ok "$R" "cleanup: ephemeral vault + tables deleted"

echo ""
echo "── #215 e2e: $PASS passed, $FAIL failed ──"
if [ "$FAIL" -gt 0 ]; then printf '%s\n' "${ERRORS[@]}"; exit 1; fi
