#!/bin/bash
#
# Git ↔ PG consistency + migration idempotency
#
# Git is a load-bearing source of truth (bare repo per vault). Divergence
# between Git and PG would create a split-brain. This suite verifies:
# - put/update/delete create the expected git commits
# - commit count in Git matches what we'd expect from PG ops
# - migration 005 is re-runnable without data loss
#
set -uo pipefail

BASE="${AKB_URL:-http://localhost:8000}"
USER_NAME="hybrid-git-$(date +%s)"
VAULT="hybrid-git-$(date +%s)"
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

echo "╔══════════════════════════════════════════╗"
echo "║   Git ↔ PG Consistency + Migration       ║"
echo "║   Target: $BASE"
echo "╚══════════════════════════════════════════╝"
echo ""

echo "▸ Setup"
rcurl -X POST "$BASE/api/v1/auth/register" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER_NAME\",\"email\":\"$USER_NAME@t.dev\",\"password\":\"test1234\"}" >/dev/null
JWT=$(rcurl -X POST "$BASE/api/v1/auth/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER_NAME\",\"password\":\"test1234\"}" | jget "d['token']")
PAT=$(rcurl -X POST "$BASE/api/v1/auth/tokens" -H "Authorization: Bearer $JWT" -H 'Content-Type: application/json' \
  -d '{"name":"g"}' | jget "d['token']")
[ -n "$PAT" ] && pass "PAT" || { fail "PAT" ""; exit 1; }

rcurl -X POST "$BASE/api/v1/vaults?name=$VAULT" -H "Authorization: Bearer $PAT" >/dev/null
pass "vault created"

# Get backend pod for git inspection
POD=$(kubectl get pods -n akb -l app=akb-backend -o jsonpath='{.items[0].metadata.name}')
[ -n "$POD" ] && pass "backend pod: $POD" || { fail "pod" ""; exit 1; }

git_commits() {
  kubectl exec -n akb "$POD" -- git --git-dir="/data/vaults/$VAULT.git" rev-list --count HEAD 2>/dev/null | tr -d ' \n'
}
git_head_msg() {
  kubectl exec -n akb "$POD" -- git --git-dir="/data/vaults/$VAULT.git" log -1 --format="%s" HEAD 2>/dev/null
}
git_commit_exists() {
  kubectl exec -n akb "$POD" -- git --git-dir="/data/vaults/$VAULT.git" cat-file -e "$1" 2>&1
}

# ── G1. Vault init creates git repo with 1 commit ────────────
echo ""
echo "▸ G1. Fresh vault git state"

C0=$(git_commits)
[ "$C0" = "1" ] && pass "vault init → 1 commit" || fail "G1-init" "got $C0"
MSG=$(git_head_msg)
echo "$MSG" | grep -q "init" && pass "HEAD message is init: $MSG" || fail "G1-msg" "$MSG"

# ── G2. Put creates new commit with correct message ──────────
echo ""
echo "▸ G2. put → 1 new commit"

R1=$(rcurl -X POST "$BASE/api/v1/documents" -H "Authorization: Bearer $PAT" -H 'Content-Type: application/json' \
  -d "{\"vault\":\"$VAULT\",\"collection\":\"x\",\"title\":\"GitTrackA\",\"content\":\"git-tracked content A\"}")
COMMIT_A=$(echo "$R1" | jget "d['commit_hash']")
PATH_A=$(echo "$R1" | jget "d['path']")

C1=$(git_commits)
[ "$C1" = "2" ] && pass "after put: 2 commits (was $C0, now $C1)" || fail "G2-count" "got $C1"

# Commit hash returned by API should exist in git
git_commit_exists "$COMMIT_A" >/dev/null 2>&1 && pass "API commit_hash exists in git ($COMMIT_A)" || fail "G2-exists" "commit not in git"

MSG=$(git_head_msg)
echo "$MSG" | grep -q "put" && pass "HEAD message has 'put': $MSG" || fail "G2-msg" "$MSG"

# ── G3. Update creates a new commit ──────────────────────────
echo ""
echo "▸ G3. update → 1 new commit"

DOC_A=$(echo "$R1" | jget "d['doc_id']")
rcurl -X PATCH "$BASE/api/v1/documents/$VAULT/$DOC_A" -H "Authorization: Bearer $PAT" -H 'Content-Type: application/json' \
  -d "{\"content\":\"git-tracked content A version 2\"}" >/dev/null
sleep 1

C2=$(git_commits)
[ "$C2" = "3" ] && pass "after update: 3 commits" || fail "G3-count" "got $C2"

# ── G4. Delete creates a new commit ──────────────────────────
echo ""
echo "▸ G4. delete → 1 new commit"

rcurl -X DELETE "$BASE/api/v1/documents/$VAULT/$DOC_A" -H "Authorization: Bearer $PAT" >/dev/null
sleep 1

C3=$(git_commits)
[ "$C3" = "4" ] && pass "after delete: 4 commits" || fail "G4-count" "got $C3"
MSG=$(git_head_msg)
echo "$MSG" | grep -q "delete" && pass "HEAD message has 'delete'" || fail "G4-msg" "$MSG"

# After delete, PG no longer has the doc
DOC_IN_PG=$(kubectl exec -n akb statefulset/postgres -- psql -U akbuser -d akb -t -c "
  SELECT COUNT(*) FROM documents WHERE metadata->>'id'='$DOC_A'
" 2>/dev/null | tr -d ' \n')
[ "$DOC_IN_PG" = "0" ] && pass "doc removed from PG after delete" || fail "G4-pg" "still $DOC_IN_PG"

# ── G5. Multiple puts = correct commit count ─────────────────
echo ""
echo "▸ G5. N puts → N new commits"

N=5
for i in $(seq 1 $N); do
  rcurl -X POST "$BASE/api/v1/documents" -H "Authorization: Bearer $PAT" -H 'Content-Type: application/json' \
    -d "{\"vault\":\"$VAULT\",\"collection\":\"x\",\"title\":\"Batch$i\",\"content\":\"batch doc $i\"}" >/dev/null
done
sleep 2

C4=$(git_commits)
EXPECTED=$((C3 + N))
[ "$C4" = "$EXPECTED" ] && pass "$N puts → $C4 commits (was $C3, expected $EXPECTED)" || fail "G5" "got $C4 expected $EXPECTED"

# ── G6. PG document count matches expected ───────────────────
echo ""
echo "▸ G6. PG state matches git narrative"

V_ID=$(kubectl exec -n akb statefulset/postgres -- psql -U akbuser -d akb -t -c "SELECT id FROM vaults WHERE name='$VAULT'" 2>/dev/null | tr -d ' \n')
PG_DOCS=$(kubectl exec -n akb statefulset/postgres -- psql -U akbuser -d akb -t -c "SELECT COUNT(*) FROM documents WHERE vault_id='$V_ID'" 2>/dev/null | tr -d ' \n')
# We've created: GitTrackA (deleted), Batch1..5 → 5 docs remaining
[ "$PG_DOCS" = "5" ] && pass "PG has exactly 5 docs (matches git narrative)" || fail "G6" "got $PG_DOCS"

# ── G7. Migration 005 is re-runnable (idempotent) ────────────
echo ""
echo "▸ G7. Migration 005 idempotency"

# Snapshot schema before
T_BEFORE=$(kubectl exec -n akb statefulset/postgres -- psql -U akbuser -d akb -t -c "
  SELECT COUNT(*) FROM information_schema.columns
   WHERE table_name='chunks' AND column_name LIKE 'qdrant_%'
" 2>/dev/null | tr -d ' \n')
V_BEFORE=$(kubectl exec -n akb statefulset/postgres -- psql -U akbuser -d akb -t -c "
  SELECT COUNT(*) FROM bm25_vocab
" 2>/dev/null | tr -d ' \n')
S_BEFORE=$(kubectl exec -n akb statefulset/postgres -- psql -U akbuser -d akb -t -c "
  SELECT COUNT(*) FROM bm25_stats WHERE id=1
" 2>/dev/null | tr -d ' \n')

# Re-run migration 005
kubectl exec -n akb deploy/backend -- python -m app.db.migrations.005_qdrant_index 2>&1 | tail -3

# Schema should be identical
T_AFTER=$(kubectl exec -n akb statefulset/postgres -- psql -U akbuser -d akb -t -c "
  SELECT COUNT(*) FROM information_schema.columns
   WHERE table_name='chunks' AND column_name LIKE 'qdrant_%'
" 2>/dev/null | tr -d ' \n')
V_AFTER=$(kubectl exec -n akb statefulset/postgres -- psql -U akbuser -d akb -t -c "
  SELECT COUNT(*) FROM bm25_vocab
" 2>/dev/null | tr -d ' \n')
S_AFTER=$(kubectl exec -n akb statefulset/postgres -- psql -U akbuser -d akb -t -c "
  SELECT COUNT(*) FROM bm25_stats WHERE id=1
" 2>/dev/null | tr -d ' \n')

[ "$T_BEFORE" = "$T_AFTER" ] && pass "qdrant_* column count unchanged ($T_AFTER)" || fail "G7-cols" "$T_BEFORE → $T_AFTER"
[ "$V_BEFORE" = "$V_AFTER" ] && pass "bm25_vocab row count preserved ($V_AFTER)" || fail "G7-vocab" "$V_BEFORE → $V_AFTER"
[ "$S_BEFORE" = "$S_AFTER" ] && pass "bm25_stats singleton preserved" || fail "G7-stats" "$S_BEFORE → $S_AFTER"

# Search still works after migration re-run
T=$(rcurl -H "Authorization: Bearer $PAT" "$BASE/api/v1/search?q=batch&vault=$VAULT&limit=5" | jget "d.get('total', 0)")
[ -n "$T" ] && pass "search functional after migration re-run (total=$T)" || fail "G7-search" "no response"

# ── Cleanup ──────────────────────────────────────────────────
echo ""
echo "▸ Cleanup"
SID=$(curl -sk -i --max-time 10 -X POST "$BASE/mcp/" \
  -H "Authorization: Bearer $PAT" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"g","version":"1.0"}}}' 2>/dev/null \
  | grep -i "mcp-session-id" | tr -d '\r' | awk '{print $2}')
if [ -n "$SID" ]; then
  rcurl -X POST "$BASE/mcp/" -H "Authorization: Bearer $PAT" \
    -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
    -H "mcp-session-id: $SID" \
    -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null
  rcurl -X POST "$BASE/mcp/" -H "Authorization: Bearer $PAT" \
    -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
    -H "mcp-session-id: $SID" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"akb_delete_vault\",\"arguments\":{\"vault\":\"$VAULT\"}}}" >/dev/null
fi

# Self-delete test user to avoid accumulating in DB
curl -sk --max-time 15 -X DELETE "$BASE/api/v1/my/account" -H "Authorization: Bearer $JWT" >/dev/null 2>&1
pass "cleanup attempted"

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
