#!/usr/bin/env bash
# SessionStart hook for akb-claude-code.
#
# Fires when a Claude Code session begins — on first startup AND on
# resume / clear / compact (the `source` field disambiguates). The hook
# is idempotent on `session_id` at AKB's end (POST to the same id
# returns the existing collection state rather than 409), so we make no
# attempt to dedupe client-side.
#
# Output to stdout becomes Claude Code's `additionalContext`, so the
# `injected_context` block in AKB's response (preferences + learnings +
# parent recap, if any) is folded into the model's prompt on every
# new turn within the session.
#
# Exit codes:
#   0 — success OR soft failure (hook is non-blocking by contract)
#   1 — broken contract (missing dep / env / stdin) — surfaces in the
#       terminal but does NOT block Claude Code itself

# shellcheck source=hooks/_lib.sh
. "$(dirname "$0")/_lib.sh"

akb_lib_check_env
akb_lib_read_input

SOURCE=$(printf '%s' "$HOOK_INPUT" | jq -r '.source // "startup"')
MODEL=$(printf '%s' "$HOOK_INPUT" | jq -r '.model // empty')
PERMISSION_MODE=$(printf '%s' "$HOOK_INPUT" | jq -r '.permission_mode // empty')

body=$(jq -n \
  --arg agent_id "claude-code" \
  --arg source "$SOURCE" \
  --arg transcript_path "$AKB_TRANSCRIPT" \
  --arg cwd "$AKB_CWD" \
  --arg model "$MODEL" \
  --arg permission_mode "$PERMISSION_MODE" \
  '{
    agent_id: $agent_id,
    source: $source,
    transcript_path: (if $transcript_path == "" then null else $transcript_path end),
    cwd: (if $cwd == "" then null else $cwd end),
    model: (if $model == "" then null else $model end),
    permission_mode: (if $permission_mode == "" then null else $permission_mode end)
  } | with_entries(select(.value != null))')

if response=$(akb_lib_curl POST "/api/v1/agent-sessions/${AKB_SESSION_ID}" "$body"); then
  # Compose the additionalContext block from injected memories. Cap
  # the budget so a very chatty memory vault does not blow past
  # Claude Code's context budget — 5 items per scope, 280 chars each.
  context=$(printf '%s' "$response" | jq -r '
    [
      (.injected_context.preferences // [] | .[0:5] |
        map("- " + (.title // "(untitled)") + ": " + ((.summary // "") | .[0:280]))),
      (.injected_context.learnings // [] | .[0:5] |
        map("- " + (.title // "(untitled)") + ": " + ((.summary // "") | .[0:280])))
    ] as [$prefs, $learns] |
    ($prefs | if length > 0 then ["### Preferences"] + . + [""] else [] end) +
    ($learns | if length > 0 then ["### Learnings"] + . + [""] else [] end) +
    (.injected_context.parent_recap as $r |
      if $r and ($r.summary // "") != "" then
        ["### Last session recap", "- " + ($r.summary | .[0:600]), ""]
      else [] end)
    | join("\n")
  ')
  if [ -n "$context" ] && [ "$context" != "null" ]; then
    preamble="Persistent context loaded from your AKB memory vault. These are notes from prior sessions — let them inform your answers without quoting them verbatim."
    akb_lib_emit_additional_context "$preamble"$'\n\n'"$context"
  fi
fi

exit 0
