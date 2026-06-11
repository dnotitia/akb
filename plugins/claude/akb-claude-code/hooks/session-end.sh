#!/usr/bin/env bash
# SessionEnd hook for akb-claude-code.
#
# Fires when a Claude Code session terminates. The stdin `reason` field
# distinguishes graceful endings (`completed`, `user_close`) from
# abnormal terminations (`aborted`, `error`, `window_close`); we forward
# whatever the harness reported. AKB's end endpoint writes a
# `recap.md` (type=session) into the session collection inside the
# user's `agent-memory-{username}` vault.
#
# Best-effort only. SessionEnd fires at session teardown, where Claude
# Code may SIGTERM the hook before a slow curl returns (cf.
# anthropics/claude-code#41577). A missed end just leaves the session
# collection without recap.md, which gardener-side TTL sweepers reap
# later — dropping the recap on a slow network is an accepted degradation
# of this non-blocking hook, not an error path to harden against.

# shellcheck source=hooks/_lib.sh
. "$(dirname "$0")/_lib.sh"

akb_lib_check_env
akb_lib_read_input

REASON=$(printf '%s' "$HOOK_INPUT" | jq -r '.reason // "completed"')

# Composition breadcrumbs for `akb-sessions`/`session-ingest`. Putting
# the JSONL transcript path + cwd into the recap's `metrics` dict lands
# them in the rendered recap.md "Metadata" section, where a later
# `/session-ingest` invocation can read them to elaborate the recap
# into structured TIL/decision/idea/task notes inside a work vault.
# See README §"Composition with akb-sessions".
SUMMARY="Claude Code session ended (reason=$REASON). Run \`/session-ingest $AKB_TRANSCRIPT --vault <work-vault>\` to distill this session into structured AKB notes."

body=$(jq -n \
  --arg reason "$REASON" \
  --arg outcome "success" \
  --arg summary "$SUMMARY" \
  --arg transcript_path "$AKB_TRANSCRIPT" \
  --arg cwd "$AKB_CWD" \
  --arg session_id "$AKB_SESSION_ID" \
  '{
    reason: $reason,
    outcome: $outcome,
    summary: $summary,
    metrics: {
      transcript_path: $transcript_path,
      cwd: $cwd,
      claude_session_id: $session_id
    } | with_entries(select(.value != ""))
  }')

akb_lib_curl POST "/api/v1/agent-sessions/${AKB_SESSION_ID}/end" "$body" >/dev/null || true

exit 0
