#!/usr/bin/env bash
# PreCompact hook for akb-claude-code.
#
# Fires immediately before Claude Code compacts the context. By posting
# a snapshot to AKB we preserve a checkpoint that survives the reset —
# if the user resumes after compaction (which fires SessionStart again
# with source=compact), the next /context recall will see what was
# happening before the compaction. Each call writes a sequential
# `snapshot-NNN.md` document inside the session collection, so the
# trail is durable and git-versioned.

# shellcheck source=hooks/_lib.sh
. "$(dirname "$0")/_lib.sh"

akb_lib_check_env
akb_lib_read_input

# Claude Code passes a `trigger` ("manual" or "auto") on PreCompact;
# AKB's snapshot accepts an open `cause` string and normalises by
# convention.
TRIGGER=$(printf '%s' "$HOOK_INPUT" | jq -r '.trigger // .source // "auto"')

# We do not have a free-text summary at this hook — we record the
# trigger plus the JSONL transcript path so that a later /session-ingest
# (the `akb-sessions` plugin's slash command) can recover this
# checkpoint and distill it. The substantive distillation happens at
# SessionEnd via the transcript-tail path (future enhancement); v0.1
# just preserves the anchor + breadcrumb.
PARTIAL="Pre-compact snapshot (trigger=$TRIGGER). Context about to be reset. Transcript: ${AKB_TRANSCRIPT:-<unknown>}."

body=$(jq -n \
  --arg partial "$PARTIAL" \
  --arg cause "pre_compact" \
  --arg trigger "$TRIGGER" \
  --arg transcript_path "$AKB_TRANSCRIPT" \
  '{
    partial_summary: $partial,
    cause: $cause,
    progress: {
      trigger: $trigger,
      transcript_path: $transcript_path
    } | with_entries(select(.value != ""))
  }')

akb_lib_curl POST "/api/v1/agent-sessions/${AKB_SESSION_ID}/snapshot" "$body" >/dev/null || true

exit 0
