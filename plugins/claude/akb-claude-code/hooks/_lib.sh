#!/usr/bin/env bash
# Shared helpers for every hook script in akb-claude-code.
#
# Sourced (not executed) by `session-start.sh`, `session-end.sh`,
# `pre-compact.sh`, and `user-prompt-submit.sh`. Centralises:
#   - env-var validation (AKB_URL + AKB_PAT)
#   - dependency check (jq, curl)
#   - stdin JSON capture into `$HOOK_INPUT`
#   - canonical field extraction (`hook_event_name`, `session_id`, …)
#   - the one POST helper that talks to AKB, with a soft-fail rule so a
#     network blip never blocks Claude Code itself
#
# Every hook exits 0 — on AKB-side failure, on missing deps, and on
# unconfigured env. Claude Code renders ANY non-zero hook exit as an
# error in the UI, so an observability plugin must never exit non-zero.
# Stderr from the hook reaches the operator's terminal; stdout is treated
# as `additionalContext` by Claude Code (used by `user-prompt-submit.sh`
# and the SessionStart injection).

set -uo pipefail

akb_lib_warn() {
  echo "akb-claude-code: $*" >&2
}

# Missing tooling or unconfigured env is a soft no-op, NOT a broken
# contract. A fresh install that has not set AKB_URL / AKB_PAT yet — or a
# host without jq/curl — stays quiet (one stderr line, exit 0) instead of
# printing a red hook error on every SessionStart / SessionEnd /
# PreCompact. Never exit non-zero from a hook.
akb_lib_check_env() {
  for cmd in jq curl; do
    command -v "$cmd" >/dev/null 2>&1 || {
      akb_lib_warn "missing dependency: $cmd — exiting noop"
      exit 0
    }
  done
  # The akb MCP server is configured with AKB_MCP_URL (…/mcp/). The
  # lifecycle hooks call the REST API on the same host, so when the
  # operator has only set AKB_MCP_URL (the variable the install docs
  # document for the MCP server) we derive AKB_URL from it by stripping a
  # trailing /mcp or /mcp/. This lets the plugin run from the single pair
  # of env vars already in the user's profile (AKB_MCP_URL + AKB_PAT)
  # instead of silently no-opping because a second, separately-documented
  # AKB_URL was never set. An explicit AKB_URL still wins.
  if [ -z "${AKB_URL:-}" ] && [ -n "${AKB_MCP_URL:-}" ]; then
    AKB_URL="${AKB_MCP_URL%/}"
    AKB_URL="${AKB_URL%/mcp}"
    export AKB_URL
  fi
  if [ -z "${AKB_URL:-}" ] || [ -z "${AKB_PAT:-}" ]; then
    akb_lib_warn "set AKB_PAT and AKB_URL (or AKB_MCP_URL) to enable — exiting noop"
    exit 0
  fi
}

akb_lib_read_input() {
  # Claude Code's hook contract: a JSON object on stdin with at least
  #   session_id, transcript_path, cwd, hook_event_name
  # Event-specific fields layer on top (e.g. SessionStart adds `source`,
  # SessionEnd adds `reason`).
  HOOK_INPUT=$(cat || true)
  if [ -z "$HOOK_INPUT" ]; then
    akb_lib_warn "hook stdin empty — exiting noop"
    exit 0
  fi
  AKB_EVENT=$(printf '%s' "$HOOK_INPUT" | jq -r '.hook_event_name // empty')
  AKB_SESSION_ID=$(printf '%s' "$HOOK_INPUT" | jq -r '.session_id // empty')
  AKB_CWD=$(printf '%s' "$HOOK_INPUT" | jq -r '.cwd // empty')
  AKB_TRANSCRIPT=$(printf '%s' "$HOOK_INPUT" | jq -r '.transcript_path // empty')
  if [ -z "$AKB_SESSION_ID" ]; then
    akb_lib_warn "stdin missing session_id — exiting noop"
    exit 0
  fi
}

# akb_lib_curl <METHOD> <PATH> [<BODY_JSON>]
# Prints the response body on stdout when status is 2xx; logs to stderr
# and returns non-zero otherwise. **Always returns 0 to the shell caller**
# unless the caller explicitly checks $? — i.e. exit-on-failure is
# opt-in, not default, so a hook can keep going on partial errors.
akb_lib_curl() {
  local method="$1"
  local path="$2"
  local body="${3:-}"
  local url="${AKB_URL%/}${path}"
  local code response
  response=$(mktemp)
  # Remove the temp file on every return path (2xx, error, guard) so a
  # partial failure never leaks it.
  trap 'rm -f "$response"' RETURN
  # Send the PAT through a curl config on stdin (`-K -`) instead of a
  # `-H` argv flag: a Bearer token on the command line is readable in the
  # process table (`ps`, /proc/<pid>/cmdline) by other local users.
  # `printf` is a bash builtin, so the token never reaches any argv.
  # --max-time stays below the smallest hook timeout (5s) so curl returns
  # before Claude Code SIGTERMs the hook. curl emits "000" on transport
  # failure; `|| true` only stops curl's non-zero exit from propagating.
  if [ -n "$body" ]; then
    code=$(printf 'header = "Authorization: Bearer %s"\n' "$AKB_PAT" \
      | curl -sS -o "$response" -w '%{http_code}' \
        -X "$method" "$url" \
        -H "Content-Type: application/json" \
        --max-time 4 \
        -d "$body" \
        -K - 2>/dev/null) || true
  else
    code=$(printf 'header = "Authorization: Bearer %s"\n' "$AKB_PAT" \
      | curl -sS -o "$response" -w '%{http_code}' \
        -X "$method" "$url" \
        --max-time 4 \
        -K - 2>/dev/null) || true
  fi
  [ -z "$code" ] && code="000"
  if [ "${code:0:1}" = "2" ]; then
    cat "$response"
    return 0
  fi
  akb_lib_warn "$method $path → HTTP $code"
  if [ -s "$response" ]; then
    akb_lib_warn "  body: $(head -c 240 "$response")"
  fi
  return 1
}

# Emit a JSON envelope on stdout that Claude Code reads as
# `hookSpecificOutput.additionalContext`. Used by SessionStart and
# UserPromptSubmit to inject AKB memories into the model's context.
# `hookEventName` must carry the real event name (Claude Code switches on
# it), so pass AKB_EVENT as a jq --arg, not the invalid `VAR=val` token
# form that jq silently treats as a filename.
akb_lib_emit_additional_context() {
  local text="$1"
  [ -z "$text" ] && return 0
  jq -n --arg text "$text" --arg event "$AKB_EVENT" '{
    hookSpecificOutput: {
      hookEventName: $event,
      additionalContext: $text
    }
  }'
}
