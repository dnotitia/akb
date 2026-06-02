#!/bin/bash
#
# AKB Agent Sessions REST E2E
#
# Exercises the v0.4.0 lifecycle-plugin surface: auto-provisioning the
# per-user memory vault, bracketing an agent session as a collection,
# capturing snapshots + recap, recalling context, idempotency on
# repeat starts, and ungraceful-end semantics (reason enum + leak).
#
# Mirrors how akb-claude-code / akb-cursor will hit the API from
# inside SessionStart / PreCompact / SessionEnd / UserPromptSubmit
# hooks — no MCP involvement, plain Bearer HTTP.
#
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
TS=$(date +%s)
E2E_USER="agent-sess-${TS}"
PASS=0
FAIL=0
ERRORS=()

pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

py() { python3 -c "$1" 2>/dev/null; }
jq_field() { py "import sys,json; d=json.load(sys.stdin); print(d$1)"; }

H_JSON='Content-Type: application/json'

echo "╔══════════════════════════════════════════════╗"
echo "║   AKB Agent Sessions REST E2E                ║"
echo "║   Target: $BASE_URL"
echo "╚══════════════════════════════════════════════╝"
echo ""

# ── 0. Setup ────────────────────────────────────────────────
echo "▸ 0. Setup user + PAT"

curl -sk -X POST "$BASE_URL/api/v1/auth/register" -H "$H_JSON" \
  -d "{\"username\":\"$E2E_USER\",\"email\":\"$E2E_USER@t.dev\",\"password\":\"test1234\"}" >/dev/null 2>&1

JWT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/login" -H "$H_JSON" \
  -d "{\"username\":\"$E2E_USER\",\"password\":\"test1234\"}" \
  | py 'import sys,json; print(json.load(sys.stdin)["token"])')
PAT=$(curl -sk -X POST "$BASE_URL/api/v1/auth/tokens" \
  -H "Authorization: Bearer $JWT" -H "$H_JSON" \
  -d '{"name":"agent-sess-e2e"}' \
  | py 'import sys,json; print(json.load(sys.stdin)["token"])')

[ -n "$PAT" ] && pass "PAT acquired" || { fail "PAT" "no PAT"; exit 1; }
H_AUTH="Authorization: Bearer $PAT"

SID1="cc-${TS}-aaaa"
SID2="cc-${TS}-bbbb"
SID_CURSOR="cursor-${TS}-xxxx"

# ── 1. First start auto-provisions memory vault ─────────────
echo ""
echo "▸ 1. start_session — auto-provision + first session"

R=$(curl -sk -X POST "$BASE_URL/api/v1/agent-sessions/$SID1" \
  -H "$H_AUTH" -H "$H_JSON" \
  -d '{"agent_id":"claude-code","source":"startup","goal":"e2e validation","cwd":"/tmp/x"}')
COLL_URI=$(echo "$R" | jq_field "['collection_uri']")
MV=$(echo "$R" | jq_field "['memory_vault']")
IS_NEW=$(echo "$R" | jq_field "['is_new']")
[ -n "$COLL_URI" ] && pass "Start returns collection_uri ($COLL_URI)" \
  || fail "Start collection_uri" "resp=$R"
[ -n "$MV" ] && pass "memory_vault returned ($MV)" \
  || fail "memory_vault" "missing"
[ "$IS_NEW" = "True" ] && pass "is_new=True on first call" \
  || fail "is_new" "expected True, got $IS_NEW"

# Verify the vault actually exists via list_vaults
R=$(curl -sk "$BASE_URL/api/v1/vaults" -H "$H_AUTH")
echo "$R" | grep -q "agent-memory-" && pass "memory vault visible in list_vaults" \
  || fail "vault visible" "resp=${R:0:240}"

# Owner-only: no other user grants exist (vault info should show 0 members beyond owner)
R=$(curl -sk "$BASE_URL/api/v1/vaults/$MV/members" -H "$H_AUTH")
MEMBER_COUNT=$(echo "$R" | py "import sys,json; d=json.load(sys.stdin); print(len(d.get('members', [])))")
[ "$MEMBER_COUNT" = "1" ] && pass "memory vault is owner-only (1 member)" \
  || fail "owner-only" "got $MEMBER_COUNT members; resp=${R:0:200}"

# ── 2. Idempotency — re-start same session_id with source=resume ──
echo ""
echo "▸ 2. start_session idempotency (Claude Code resume semantics)"

R=$(curl -sk -X POST "$BASE_URL/api/v1/agent-sessions/$SID1" \
  -H "$H_AUTH" -H "$H_JSON" \
  -d '{"agent_id":"claude-code","source":"resume"}')
IS_NEW2=$(echo "$R" | jq_field "['is_new']")
[ "$IS_NEW2" = "False" ] && pass "Resume returns is_new=False" \
  || fail "resume idempotency" "expected False, got $IS_NEW2"
COLL_URI2=$(echo "$R" | jq_field "['collection_uri']")
[ "$COLL_URI" = "$COLL_URI2" ] && pass "Resume returns same collection_uri" \
  || fail "resume collection_uri" "$COLL_URI vs $COLL_URI2"

# Same with source=compact
R=$(curl -sk -X POST "$BASE_URL/api/v1/agent-sessions/$SID1" \
  -H "$H_AUTH" -H "$H_JSON" \
  -d '{"agent_id":"claude-code","source":"compact"}')
IS_NEW3=$(echo "$R" | jq_field "['is_new']")
[ "$IS_NEW3" = "False" ] && pass "Compact source returns is_new=False" \
  || fail "compact idempotency" "got $IS_NEW3"

# ── 3. Snapshot during PreCompact ───────────────────────────
echo ""
echo "▸ 3. snapshot (PreCompact safety net)"

R=$(curl -sk -X POST "$BASE_URL/api/v1/agent-sessions/$SID1/snapshot" \
  -H "$H_AUTH" -H "$H_JSON" \
  -d '{"partial_summary":"halfway through e2e","progress":{"step":3,"of":5},"cause":"pre_compact"}')
SNAP_URI=$(echo "$R" | jq_field "['snapshot_uri']")
SEQ=$(echo "$R" | jq_field "['sequence']")
[ -n "$SNAP_URI" ] && [ "$SEQ" = "1" ] && pass "Snapshot 1 written ($SNAP_URI)" \
  || fail "Snapshot" "uri=$SNAP_URI seq=$SEQ"

# Second snapshot → sequence 2
R=$(curl -sk -X POST "$BASE_URL/api/v1/agent-sessions/$SID1/snapshot" \
  -H "$H_AUTH" -H "$H_JSON" \
  -d '{"partial_summary":"more progress","cause":"manual"}')
SEQ2=$(echo "$R" | jq_field "['sequence']")
[ "$SEQ2" = "2" ] && pass "Snapshot 2 sequence increments" \
  || fail "Snapshot seq" "got $SEQ2"

# ── 4. Recall context (synchronous) ─────────────────────────
echo ""
echo "▸ 4. /context recall (UserPromptSubmit injection)"

# Seed preferences + learnings via akb_put → these should show up in recall
PUT() {
  local title="$1" coll="$2" content="$3"
  curl -sk -X POST "$BASE_URL/api/v1/documents" \
    -H "$H_AUTH" -H "$H_JSON" \
    -d "{\"vault\":\"$MV\",\"collection\":\"$coll\",\"title\":\"$title\",\"content\":\"$content\",\"type\":\"reference\"}"
}
PUT "Dark mode" "preferences" "I prefer dark mode for long sessions" >/dev/null
PUT "Korean comments" "preferences" "Keep code comments in English, conversation in Korean" >/dev/null
PUT "Migration race" "learnings" "Migration 026 second-pass needs UniqueViolation guard" >/dev/null

R=$(curl -sk "$BASE_URL/api/v1/agent-sessions/$SID1/context?scopes=preferences,learnings&limit=10" \
  -H "$H_AUTH")
PREF_COUNT=$(echo "$R" | py "import sys,json; d=json.load(sys.stdin); print(len(d.get('preferences',[])))")
LEARN_COUNT=$(echo "$R" | py "import sys,json; d=json.load(sys.stdin); print(len(d.get('learnings',[])))")
[ "$PREF_COUNT" -ge "2" ] && pass "context returns $PREF_COUNT preferences" \
  || fail "preferences recall" "got $PREF_COUNT"
[ "$LEARN_COUNT" -ge "1" ] && pass "context returns $LEARN_COUNT learnings" \
  || fail "learnings recall" "got $LEARN_COUNT"

# ── 5. Second session — different agent, parent reference ──
echo ""
echo "▸ 5. cross-agent + parent_session_id"

R=$(curl -sk -X POST "$BASE_URL/api/v1/agent-sessions/$SID_CURSOR" \
  -H "$H_AUTH" -H "$H_JSON" \
  -d "{\"agent_id\":\"cursor\",\"source\":\"startup\",\"parent_session_id\":\"$SID1\"}")
COLL_C=$(echo "$R" | jq_field "['collection_uri']")
INJ=$(echo "$R" | py "import sys,json; d=json.load(sys.stdin); print('parent_recap' in d.get('injected_context',{}))")
[ -n "$COLL_C" ] && pass "Cursor session created at $COLL_C" \
  || fail "cursor session" "no collection_uri"
# parent has not been ended yet → no recap → parent_recap absent → INJ=False expected
[ "$INJ" = "False" ] && pass "parent_recap absent (parent has no recap yet)" \
  || fail "parent_recap" "expected absent (False), got $INJ"

# ── 6. End session — writes recap.md ───────────────────────
echo ""
echo "▸ 6. end_session — recap.md persisted"

R=$(curl -sk -X POST "$BASE_URL/api/v1/agent-sessions/$SID1/end" \
  -H "$H_AUTH" -H "$H_JSON" \
  -d '{"reason":"completed","summary":"e2e suite passed cleanly","outcome":"success","decisions":["promote vault model"],"next_actions":["ship plugin"],"duration_seconds":42}')
RECAP_URI=$(echo "$R" | jq_field "['recap_uri']")
ENDED_AT=$(echo "$R" | jq_field "['ended_at']")
DUR=$(echo "$R" | jq_field "['duration_seconds']")
[ -n "$RECAP_URI" ] && pass "end_session returns recap_uri ($RECAP_URI)" \
  || fail "recap_uri" "$R"
[ -n "$ENDED_AT" ] && pass "ended_at returned" || fail "ended_at" "$R"
[ "$DUR" = "42" ] && pass "duration_seconds preserved" || fail "duration" "got $DUR"

# Recap doc should be browseable in the memory vault
R=$(curl -sk "$BASE_URL/api/v1/browse/$MV?collection=sessions/$(date -u +%Y-%m-%d)/claude-code/cc-${TS}-aaaa&depth=0" \
  -H "$H_AUTH")
echo "$R" | grep -q "recap.md" && pass "recap.md visible via browse" \
  || fail "recap browse" "resp=${R:0:240}"

# ── 7. Status + cross-session recall now sees parent recap ──
echo ""
echo "▸ 7. session_status + parent_recap injection"

R=$(curl -sk "$BASE_URL/api/v1/agent-sessions/$SID1" -H "$H_AUTH")
ENDED=$(echo "$R" | jq_field "['ended']")
[ "$ENDED" = "True" ] && pass "Status reports ended=True" \
  || fail "status ended" "got $ENDED"

# Restart cursor session with parent → should now inject parent recap
R=$(curl -sk -X POST "$BASE_URL/api/v1/agent-sessions/${SID_CURSOR}-v2" \
  -H "$H_AUTH" -H "$H_JSON" \
  -d "{\"agent_id\":\"cursor\",\"source\":\"startup\",\"parent_session_id\":\"$SID1\"}")
INJ2=$(echo "$R" | py "import sys,json; d=json.load(sys.stdin); print('parent_recap' in d.get('injected_context',{}))")
[ "$INJ2" = "True" ] && pass "parent_recap appears now that parent has ended" \
  || fail "parent_recap (after end)" "got $INJ2"

# ── 8. Ungraceful end (window_close) ───────────────────────
echo ""
echo "▸ 8. ungraceful end — reason=window_close"

UG_SID="cc-${TS}-window-close"
curl -sk -X POST "$BASE_URL/api/v1/agent-sessions/$UG_SID" \
  -H "$H_AUTH" -H "$H_JSON" \
  -d '{"agent_id":"claude-code","source":"startup"}' >/dev/null

R=$(curl -sk -X POST "$BASE_URL/api/v1/agent-sessions/$UG_SID/end" \
  -H "$H_AUTH" -H "$H_JSON" \
  -d '{"reason":"window_close","summary":"user closed terminal","outcome":"partial"}')
REASON=$(echo "$R" | jq_field "['reason']")
OUT=$(echo "$R" | jq_field "['outcome']")
[ "$REASON" = "window_close" ] && [ "$OUT" = "partial" ] \
  && pass "Ungraceful end tagged reason=window_close outcome=partial" \
  || fail "ungraceful end" "reason=$REASON outcome=$OUT"

# ── 9. List sessions ───────────────────────────────────────
echo ""
echo "▸ 9. list sessions"

R=$(curl -sk "$BASE_URL/api/v1/agent-sessions?limit=20" -H "$H_AUTH")
TOTAL=$(echo "$R" | jq_field "['total']")
RET=$(echo "$R" | jq_field "['returned']")
[ "$TOTAL" -ge "3" ] && pass "list_sessions: total=$TOTAL returned=$RET" \
  || fail "list_sessions" "total=$TOTAL ret=$RET resp=${R:0:240}"

# Filter by agent
R=$(curl -sk "$BASE_URL/api/v1/agent-sessions?agent_id=cursor" -H "$H_AUTH")
T_CURSOR=$(echo "$R" | jq_field "['total']")
[ "$T_CURSOR" -ge "2" ] && pass "list_sessions agent_id=cursor: $T_CURSOR" \
  || fail "list_sessions filter" "got $T_CURSOR"

# ── 10. Validation errors ──────────────────────────────────
echo ""
echo "▸ 10. schema rejects"

# Bad source
CODE=$(curl -sk -o /dev/null -w '%{http_code}' -X POST \
  "$BASE_URL/api/v1/agent-sessions/bad-src" \
  -H "$H_AUTH" -H "$H_JSON" \
  -d '{"agent_id":"claude-code","source":"not-a-valid-source"}')
[ "$CODE" = "422" ] && pass "bad source → 422" || fail "bad source" "got $CODE"

# Bad reason
curl -sk -X POST "$BASE_URL/api/v1/agent-sessions/reason-check" \
  -H "$H_AUTH" -H "$H_JSON" \
  -d '{"agent_id":"claude-code","source":"startup"}' >/dev/null
CODE=$(curl -sk -o /dev/null -w '%{http_code}' -X POST \
  "$BASE_URL/api/v1/agent-sessions/reason-check/end" \
  -H "$H_AUTH" -H "$H_JSON" \
  -d '{"reason":"not-a-real-reason","summary":"x"}')
[ "$CODE" = "422" ] && pass "bad reason → 422" || fail "bad reason" "got $CODE"

# End on unknown session
CODE=$(curl -sk -o /dev/null -w '%{http_code}' -X POST \
  "$BASE_URL/api/v1/agent-sessions/unknown-${TS}/end" \
  -H "$H_AUTH" -H "$H_JSON" \
  -d '{"reason":"completed"}')
[ "$CODE" = "404" ] && pass "end_session on unknown id → 404" \
  || fail "unknown end" "got $CODE"

# ── 11. Auth ───────────────────────────────────────────────
echo ""
echo "▸ 11. auth"

CODE=$(curl -sk -o /dev/null -w '%{http_code}' -X POST \
  "$BASE_URL/api/v1/agent-sessions/anon-${TS}" \
  -H "$H_JSON" -d '{"agent_id":"claude-code"}')
[ "$CODE" = "401" ] || [ "$CODE" = "403" ] \
  && pass "no auth → $CODE" || fail "no auth" "got $CODE"

# ── Summary ────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════"
echo "  Passed: $PASS"
echo "  Failed: $FAIL"
echo "════════════════════════════════════════════════"
if [ "$FAIL" -ne 0 ]; then
  for e in "${ERRORS[@]}"; do echo "  — $e"; done
  exit 1
fi
exit 0
