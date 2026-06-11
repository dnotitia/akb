#!/usr/bin/env bash
# UserPromptSubmit hook for akb-claude-code.
#
# Fires every time the user submits a prompt. Optionally re-injects AKB
# memory into the prompt — useful for long sessions where the
# SessionStart injection has fallen out of the active context window.
#
# Off by default (no-op) to avoid charging every prompt with a network
# round-trip + AKB call. Enable by setting
#   AKB_CLAUDE_CODE_PER_PROMPT_INJECT=1
# in the environment. Even when enabled, the hook caps total
# additionalContext to ~800 chars to keep latency + token cost bounded.

# shellcheck source=hooks/_lib.sh
. "$(dirname "$0")/_lib.sh"

if [ "${AKB_CLAUDE_CODE_PER_PROMPT_INJECT:-0}" != "1" ]; then
  exit 0
fi

akb_lib_check_env
akb_lib_read_input

# Forward the user's prompt as a free-form query so AKB can prioritise
# topically relevant memories. The Claude Code UserPromptSubmit field is
# `prompt` (not `user_prompt`). NOTE: AKB 0.5.x ignores `query` and orders
# by recency — semantic ranking is a backend follow-up, so this currently
# re-injects the most recently updated memories regardless of the prompt.
PROMPT=$(printf '%s' "$HOOK_INPUT" | jq -r '.prompt // empty')
QSTR=""
if [ -n "$PROMPT" ] && [ "$PROMPT" != "null" ]; then
  encoded=$(printf '%s' "$PROMPT" | jq -sRr @uri)
  QSTR="?query=${encoded}&scopes=preferences,learnings&limit=3"
fi

if response=$(akb_lib_curl GET "/api/v1/agent-sessions/${AKB_SESSION_ID}/context${QSTR}"); then
  context=$(printf '%s' "$response" | jq -r '
    ((.preferences // []) + (.learnings // [])) as $all |
    $all |
    .[0:3] |
    map("- " + (.title // "(untitled)") + ": " + ((.summary // "") | .[0:240])) |
    join("\n")
  ')
  if [ -n "$context" ] && [ "$context" != "null" ]; then
    akb_lib_emit_additional_context $'Relevant memory from AKB:\n'"$context"
  fi
fi

exit 0
