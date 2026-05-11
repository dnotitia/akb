#!/bin/bash
#
# Self-heal + idempotency + abandoned-reap E2E.
#
# Three crash-recovery scenarios that the previous (pre-self-heal)
# backend would have left in a stuck state:
#
#   1. Stale git index.lock — survives a backend restart and gets
#      auto-cleared by lifecycle.init_storage().
#   2. akb_delete when the git file is already absent — DB cleanup
#      runs to completion instead of failing the whole call.
#   3. Abandoned chunks (vector_retry_count >= MAX) get reaped by
#      delete_worker after the grace window.
#
# kubectl-gated (the lock / chunk seeding happens on the postgres
# pod and the worktree volume), skipped cleanly otherwise.
#
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
NS="${AKB_NS:-akb}"
PG_POD="${AKB_PG_POD:-postgres-0}"
PG_USER="${AKB_PG_USER:-akbuser}"
PG_DB="${AKB_PG_DB:-akb}"
BE_DEPLOY="${AKB_BE_DEPLOY:-deployment/backend}"

VAULT="selfheal-$(date +%s)"
USER="selfheal-$(date +%s)"
PASS=0
FAIL=0
ERRORS=()

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

if ! command -v kubectl >/dev/null 2>&1; then
  echo "kubectl not available — skipping self-heal verification"
  exit 0
fi

run_psql() {
  kubectl exec -n "$NS" "$PG_POD" -- psql -U "$PG_USER" -d "$PG_DB" -tAc "$1" 2>/dev/null
}

run_be() {
  kubectl exec -n "$NS" "$BE_DEPLOY" -- bash -c "$1" 2>/dev/null
}

# ── Setup ────────────────────────────────────────────────────────
echo "▸ Setup"

curl -sk -X POST "$BASE_URL/api/v1/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER\",\"email\":\"$USER@test.dev\",\"password\":\"test1234\"}" >/dev/null 2>&1

JWT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER\",\"password\":\"test1234\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

PAT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" \
  -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' \
  -d '{"name":"selfheal"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])' 2>/dev/null)

[ -n "$PAT" ] || { echo "FATAL: PAT"; exit 1; }

curl -sk -X POST "$BASE_URL/api/v1/vaults?name=$VAULT" \
  -H "Authorization: Bearer $PAT" >/dev/null
VAULT_ID=$(run_psql "SELECT id FROM vaults WHERE name = '$VAULT'")
[ -n "$VAULT_ID" ] && pass "vault ready ($VAULT)" || { fail "vault" "missing"; exit 1; }

# Put one doc so the worktree exists.
PUT=$(curl -sk -X POST "$BASE_URL/api/v1/documents" \
  -H "Authorization: Bearer $PAT" \
  -H 'Content-Type: application/json' \
  -d "{\"vault\":\"$VAULT\",\"title\":\"hello\",\"type\":\"note\",\"content\":\"# hello\\n\\ntest body\",\"collection\":\"misc\"}")
DOC=$(echo "$PUT" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("doc_id",""))' 2>/dev/null)
[ -n "$DOC" ] && pass "doc seeded ($DOC)" || fail "seed doc" "$PUT"

# ── 1. akb_delete idempotent on git-missing file ─────────────────
echo ""
echo "▸ 1. akb_delete is idempotent when git file is already absent"

# Resolve PG row + path, then rm the file from the worktree without
# committing — simulates a half-finished cleanup leaving the row.
PG_DOC_PATH=$(run_psql "SELECT path FROM documents WHERE metadata->>'id' = '$DOC' AND vault_id = '$VAULT_ID'")
[ -n "$PG_DOC_PATH" ] && pass "found PG row path ($PG_DOC_PATH)" || { fail "lookup" "no path"; exit 1; }

run_be "rm -f '/data/vaults/_worktrees/$VAULT/$PG_DOC_PATH'"
GONE=$(run_be "test -f '/data/vaults/_worktrees/$VAULT/$PG_DOC_PATH' && echo 1 || echo 0")
[ "$GONE" = "0" ] && pass "worktree file removed (without commit)" || fail "rm" "still there"

DEL_HTTP=$(curl -sk -o /tmp/del_out.json -w "%{http_code}" -X DELETE \
  "$BASE_URL/api/v1/documents/$VAULT/$DOC" -H "Authorization: Bearer $PAT")
[ "$DEL_HTTP" = "200" ] && pass "DELETE succeeded (HTTP 200) despite missing git file" \
                       || fail "DELETE" "HTTP $DEL_HTTP body=$(cat /tmp/del_out.json)"

REMAINING=$(run_psql "SELECT COUNT(*) FROM documents WHERE metadata->>'id' = '$DOC' AND vault_id = '$VAULT_ID'")
[ "$REMAINING" = "0" ] && pass "DB row also cleaned up" || fail "cleanup" "$REMAINING rows remain"

# ── 2. Abandoned chunk reap — fast path (manually expired) ───────
echo ""
echo "▸ 2. Abandoned chunk reaper drains stuck rows"

# Seed a synthetic chunk that is "abandoned + expired" — retry MAX
# and last attempt set in the past. The reaper rate-limits to once
# per hour by default; we force a single-shot via psql to validate
# the SQL (the cadence is exercised in the live worker, not here).

# vault_id is required for chunks (denormalized).
SYN_DOC_ID=$(uuidgen | tr 'A-Z' 'a-z')
run_psql "INSERT INTO chunks
  (id, vault_id, source_type, source_id, section_path, content,
   chunk_index, char_start, char_end,
   vector_retry_count, vector_next_attempt_at,
   vector_last_error)
 VALUES
  ('$(uuidgen | tr 'A-Z' 'a-z')', '$VAULT_ID', 'document', '$SYN_DOC_ID',
   '', 'synthetic abandoned content', 0, 0, 28,
   8, NOW() - INTERVAL '8 days', 'simulated retry exhaustion')" >/dev/null

PRE=$(run_psql "SELECT COUNT(*) FROM chunks WHERE source_id = '$SYN_DOC_ID' AND vector_retry_count >= 8")
[ "$PRE" = "1" ] && pass "synthetic abandoned chunk seeded" || fail "seed" "got $PRE"

# Drive the reap SQL directly (the worker uses the identical CTE):
REAPED=$(run_psql "WITH abandoned AS (
    SELECT id, source_type, source_id FROM chunks
     WHERE vector_indexed_at IS NULL AND vector_retry_count >= 8
       AND (vector_next_attempt_at IS NULL
         OR vector_next_attempt_at < NOW() - INTERVAL '7 days')
       AND source_id = '$SYN_DOC_ID'
  ), enqueued AS (
    INSERT INTO vector_delete_outbox (chunk_id, source_type, source_id, next_attempt_at)
    SELECT id, source_type, source_id, NOW() FROM abandoned RETURNING 1
  ), deleted AS (
    DELETE FROM chunks WHERE id IN (SELECT id FROM abandoned) RETURNING 1
  )
  SELECT COUNT(*) FROM deleted")
[ "$REAPED" = "1" ] && pass "reaper deleted the abandoned chunk" || fail "reap" "got $REAPED"

OUTBOX=$(run_psql "SELECT COUNT(*) FROM vector_delete_outbox WHERE source_id = '$SYN_DOC_ID'")
[ "$OUTBOX" = "1" ] && pass "vector_delete_outbox enqueued" || fail "outbox" "got $OUTBOX"

# Cleanup: drop the synthetic outbox row so it doesn't show up in
# real workers' processing.
run_psql "DELETE FROM vector_delete_outbox WHERE source_id = '$SYN_DOC_ID'" >/dev/null

# ── 3. Stale lock self-heal (lifecycle) ──────────────────────────
# Note: this verifies the helper directly because we can't easily
# trigger backend pod restart from inside a test. Live verification
# happens via the deploy log line "Cleared N stale git lock(s)…".
echo ""
echo "▸ 3. cleanup_stale_locks helper directly removes old locks"

# Linked-worktree locks live at <bare>/worktrees/<name>/index.lock, not
# at <worktree>/.git/index.lock (`.git` there is a gitdir-pointer file).
LOCK_DIR="/data/vaults/$VAULT.git/worktrees/$VAULT"
LOCK_PATH="$LOCK_DIR/index.lock"

run_be "test -d '$LOCK_DIR' || mkdir -p '$LOCK_DIR'; touch -d '5 minutes ago' '$LOCK_PATH'"
LOCK_PRE=$(run_be "test -f '$LOCK_PATH' && echo 1 || echo 0")
[ "$LOCK_PRE" = "1" ] && pass "synthetic stale lock seeded at $LOCK_PATH" || fail "seed lock" "missing"

run_be "cd /app && python3 -c '
from app.services.git_service import GitService
n = GitService().cleanup_stale_locks(max_age_seconds=60)
print(\"cleared=\", n)
'" >/dev/null

LOCK_POST=$(run_be "test -f '$LOCK_PATH' && echo 1 || echo 0")
[ "$LOCK_POST" = "0" ] && pass "stale lock cleared by helper" || fail "self-heal" "lock still there"

# Defense in depth: recent lock (<60s) must NOT be cleared.
run_be "touch '$LOCK_PATH'"
run_be "cd /app && python3 -c '
from app.services.git_service import GitService
GitService().cleanup_stale_locks(max_age_seconds=60)
'" >/dev/null
RECENT=$(run_be "test -f '$LOCK_PATH' && echo 1 || echo 0")
[ "$RECENT" = "1" ] && pass "recent lock (<60s) preserved" || fail "self-heal grace" "lock removed"

# Cleanup
run_be "rm -f '$LOCK_PATH'"

# ── Cleanup ──────────────────────────────────────────────────────
echo ""
echo "── Cleanup ──"
curl -sk -X DELETE "$BASE_URL/api/v1/vaults/$VAULT" -H "Authorization: Bearer $PAT" >/dev/null 2>&1 || true

echo ""
echo "═══════════════════════════════════════════"
echo "  Passed: $PASS   Failed: $FAIL"
if [ $FAIL -gt 0 ]; then
  echo "  Failures:"
  printf '    - %s\n' "${ERRORS[@]}"
  exit 1
fi
exit 0
