#!/usr/bin/env python3
"""Claude Code hooks for AKB production-service development.

The hook stays conservative: it blocks clearly dangerous local operations,
asks before deploy/publish/push actions, and otherwise injects compact project
context so Claude keeps the PM/docs/AKB loop in view.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any


PROJECT_CONTEXT = """AKB production-service context:
- Read AGENTS.md and CLAUDE.md before changing behavior.
- PM-facing intent lives in docs/prd/{backlog,wip,applied,denied}/<item>/.
- Engineering designs live in docs/design/{proposal,accepted,denied}/<item>/.
- Each item is a folder with README.md, rounds/, and feedback/.
- Backend owns MCP business logic; packages/akb-mcp-client owns local filesystem proxy behavior.
- PostgreSQL + Git are source of truth; vector stores are derived.
- Durable project-management notes go to the akb vault, not akb-test.

Before final response for AKB work, self-check:
- changed files and why they matter;
- tests or validation run, including gaps;
- PRD/design item state and any feedback/rounds added;
- whether durable project notes were mirrored to the akb vault;
- unresolved PM decisions or release risks."""


PROMPT_KEYWORDS = re.compile(
    r"production|prod|deploy|release|prd|design|proposal|accepted|denied|"
    r"mcp|proxy|vault|akb|index|vector|qdrant|pgvector|seahorse|auth|"
    r"publication|security|review|claude|hook|docs|"
    r"프로덕션|배포|릴리스|기획|설계|리뷰|문서|후크|볼트",
    re.IGNORECASE,
)

DANGEROUS_BASH = [
    (re.compile(r"\brm\s+-rf\s+(/|\$HOME|~|\.|\.{2})(\s|$)"), "Refusing broad rm -rf against root/home/current/parent paths."),
    (re.compile(r"\bgit\s+reset\s+--hard\b"), "Refusing git reset --hard. Preserve user and agent worktree changes."),
    # `--` only when it's the literal end-of-options separator (next char is whitespace or EOL),
    # so `git checkout --ours/--theirs` (merge-conflict resolution) is not caught.
    (re.compile(r"\bgit\s+checkout\s+--(?!\w)"), "Refusing git checkout -- because it can discard local edits."),
    (re.compile(r"\bgit\s+clean\s+-[^\n]*[fdx]"), "Refusing git clean with file removal flags."),
    (re.compile(r"\bgit\s+push\b[^\n]*\s+--force(?:-with-lease)?\b"), "Refusing force push."),
    (re.compile(r"\bfind\b[^\n]*\s+-exec\s+[^\n]*\brm\b"), "Refusing find -exec rm. Use an explicit, reviewable removal plan."),
    (re.compile(r"\bkubectl\s+delete\s+namespace\b"), "Refusing namespace deletion from an agent hook."),
    (re.compile(r"\bdrop\s+database\b", re.IGNORECASE), "Refusing DROP DATABASE command."),
]

ASK_BASH = [
    (re.compile(r"\bdeploy/k8s/deploy\.sh\b"), "This deploys AKB. Confirm target cluster, registry, namespace, and E2E plan with the PM."),
    (re.compile(r"\bkubectl\s+(apply|rollout|set|scale|patch|delete)\b"), "This changes Kubernetes state. Confirm environment and rollback plan."),
    (re.compile(r"\bnpm\s+publish\b"), "This publishes the akb-mcp package. Confirm version bump, changelog, and npm target."),
    (re.compile(r"\bgit\s+push\b"), "This pushes repository state. Confirm branch and review status."),
    (re.compile(r"\bsed\s+-i\b"), "This mutates files through Bash and bypasses file-edit hooks. Prefer Edit/MultiEdit or confirm the exact target."),
    (re.compile(r"\b(psql|pg_restore|docker\s+compose\s+down\s+-v|helm\s+uninstall|terraform\s+destroy)\b"), "This can alter service data or infrastructure. Confirm target and rollback plan."),
]

SECRET_PATH_PATTERNS = [
    re.compile(r"(^|/)\.env(\.|$)"),
    re.compile(r"(^|/)config/secret\.ya?ml$"),
    re.compile(r"(^|/)backend/config/"),
    re.compile(r"(^|/)deploy/k8s/(internal|aws-internal|aws-demo-internal)(/|$)"),
    re.compile(r"(^|/)(secrets?|credentials?)(/|\.|$)", re.IGNORECASE),
]

READ_ONLY_AKB_TOOLS = {
    "akb_activity",
    "akb_browse",
    "akb_diff",
    "akb_drill_down",
    "akb_get",
    "akb_get_file",
    "akb_graph",
    "akb_grep",
    "akb_help",
    "akb_history",
    "akb_list_vaults",
    "akb_provenance",
    "akb_publications",
    "akb_recall",
    "akb_relations",
    "akb_search",
    "akb_sql",
    "akb_vault_info",
    "akb_vault_members",
    "akb_whoami",
}

PUBLIC_AKB_TOOLS = {
    "akb_publish",
    "akb_publication_snapshot",
    "akb_set_public",
    "akb_grant",
    "akb_transfer_ownership",
}


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def hook_output(event: str, message: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": message,
        },
        "systemMessage": message,
    }


def deny(message: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
        },
        "systemMessage": message,
    }


def ask(message: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
        },
        "systemMessage": message,
    }


def read_input() -> dict[str, Any]:
    if sys.stdin.isatty():
        return {}
    try:
        return json.load(sys.stdin)
    except json.JSONDecodeError:
        return {}


def normalized_path(raw: str, cwd: str | None = None) -> str:
    if not raw:
        return ""
    path = Path(raw)
    if not path.is_absolute() and cwd:
        path = Path(cwd) / path
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path.absolute()
    return resolved.as_posix()


def repo_relative(path: str, project_dir: str | None) -> str:
    if not path:
        return ""
    if not project_dir:
        return path
    try:
        return Path(path).resolve().relative_to(Path(project_dir).resolve()).as_posix()
    except ValueError:
        return path


def is_sensitive_path(rel_path: str) -> bool:
    if rel_path.endswith(".example"):
        return False
    return any(pattern.search(rel_path) for pattern in SECRET_PATH_PATTERNS)


def command_opens_sensitive_path(command: str, cwd: str, project_dir: str) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    if not tokens:
        return False

    read_commands = {"cat", "sed", "less", "more", "rg", "grep", "awk", "tail", "head"}
    if not any(Path(token).name in read_commands for token in tokens):
        return False

    for token in tokens[1:]:
        if token.startswith("-"):
            continue
        cleaned = token.strip("'\"")
        if not cleaned or any(ch in cleaned for ch in "*?[]{}()|"):
            continue
        rel = repo_relative(normalized_path(cleaned, cwd), project_dir)
        if is_sensitive_path(rel):
            return True
    return False


def akb_tool_name(tool_name: str) -> str:
    return tool_name.split("__")[-1] if tool_name.startswith("mcp__") else tool_name


def is_akb_mcp_tool(tool_name: str) -> bool:
    return tool_name.startswith("mcp__akb__") or tool_name.startswith("mcp__akb_test__")


def is_akb_mutation(tool: str, tool_input: dict[str, Any]) -> bool:
    if tool == "akb_sql":
        sql = str(tool_input.get("sql") or "").lstrip().lower()
        return not sql.startswith("select")
    return tool not in READ_ONLY_AKB_TOOLS


def handle_akb_tool(tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    tool = akb_tool_name(tool_name)
    vault = str(tool_input.get("vault") or "")
    uri = str(tool_input.get("uri") or "")
    allow_test = os.environ.get("AKB_ALLOW_TEST_VAULT") == "1"

    if is_akb_mutation(tool, tool_input):
        if not allow_test and (tool_name.startswith("mcp__akb_test__") or vault == "akb-test" or uri.startswith("akb://akb-test/")):
            return deny("Project-management writes must target the akb vault, not akb-test. Set AKB_ALLOW_TEST_VAULT=1 only for explicit test work.")

    if tool in PUBLIC_AKB_TOOLS:
        return ask(f"{tool} changes AKB visibility or access. Confirm vault, audience, expiration, and rollback.")

    return {}


def file_path_from_tool(tool_name: str, tool_input: dict[str, Any]) -> str:
    if tool_name in {"Read", "Write", "Edit", "MultiEdit"}:
        return str(tool_input.get("file_path") or "")
    return ""


def docs_lifecycle_warning(rel_path: str) -> str | None:
    if rel_path in {"docs/prd/README.md", "docs/design/README.md"}:
        return None

    if rel_path.startswith("docs/prd/"):
        parts = rel_path.split("/")
        if len(parts) < 4 or parts[2] not in {"backlog", "wip", "applied", "denied"}:
            return "PRD edits should use docs/prd/{backlog,wip,applied,denied}/<item>/README.md with rounds/ and feedback/."
        if len(parts) == 4 and parts[-1] != "README.md":
            return "PRD item root files should be README.md; use rounds/ or feedback/ for supporting artifacts."

    if rel_path.startswith("docs/design/"):
        parts = rel_path.split("/")
        if len(parts) < 4 or parts[2] not in {"proposal", "accepted", "denied"}:
            return "Design edits should use docs/design/{proposal,accepted,denied}/<item>/README.md with rounds/ and feedback/."
        if len(parts) == 4 and parts[-1] != "README.md":
            return "Design item root files should be README.md; use rounds/ or feedback/ for supporting artifacts."

    return None


def handle_pre_tool(input_data: dict[str, Any]) -> dict[str, Any]:
    tool_name = str(input_data.get("tool_name") or "")
    tool_input = input_data.get("tool_input") or {}
    cwd = str(input_data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or cwd

    if tool_name == "Bash":
        command = str(tool_input.get("command") or "")
        for pattern, message in DANGEROUS_BASH:
            if pattern.search(command):
                return deny(message)
        if command_opens_sensitive_path(command, cwd, project_dir):
            return deny("Refusing to read likely secret material. Use example config files or ask the PM for a redacted value.")
        for pattern, message in ASK_BASH:
            if pattern.search(command):
                return ask(message)
        return {}

    if is_akb_mcp_tool(tool_name):
        result = handle_akb_tool(tool_name, tool_input)
        if result:
            return result
        return {}

    raw_path = file_path_from_tool(tool_name, tool_input)
    if raw_path:
        abs_path = normalized_path(raw_path, cwd)
        rel_path = repo_relative(abs_path, project_dir)
        if is_sensitive_path(rel_path):
            return deny(f"Refusing access to sensitive path: {rel_path}")
        warning = docs_lifecycle_warning(rel_path)
        if warning and tool_name in {"Write", "Edit", "MultiEdit"}:
            return ask(warning)

    return {}


def handle_user_prompt(input_data: dict[str, Any]) -> dict[str, Any]:
    prompt = str(input_data.get("prompt") or input_data.get("user_prompt") or "")
    if PROMPT_KEYWORDS.search(prompt):
        return hook_output("UserPromptSubmit", PROJECT_CONTEXT)
    return {}


def handle_session_start(_: dict[str, Any]) -> dict[str, Any]:
    return hook_output("SessionStart", PROJECT_CONTEXT)


def handle_stop(_: dict[str, Any]) -> dict[str, Any]:
    # Stop hooks cannot inject `additionalContext` (Claude Code schema only
    # supports it for SessionStart / UserPromptSubmit / PostToolUse /
    # PostToolBatch). The pre-final-response checklist now lives in
    # PROJECT_CONTEXT so Claude sees it at SessionStart instead.
    return {}


def run_self_test() -> int:
    cases = [
        (
            "dangerous reset",
            {"tool_name": "Bash", "tool_input": {"command": "git reset --hard HEAD"}},
            "deny",
        ),
        (
            "checkout discard denied",
            {"tool_name": "Bash", "tool_input": {"command": "git checkout -- file.txt"}},
            "deny",
        ),
        (
            "checkout --ours allowed (merge conflict)",
            {"tool_name": "Bash", "tool_input": {"command": "git checkout --ours CLAUDE.md"}},
            None,
        ),
        (
            "checkout --theirs allowed (merge conflict)",
            {"tool_name": "Bash", "tool_input": {"command": "git checkout --theirs CLAUDE.md"}},
            None,
        ),
        (
            "deploy asks",
            {"tool_name": "Bash", "tool_input": {"command": "bash deploy/k8s/deploy.sh"}},
            "ask",
        ),
        (
            "secret path",
            {"tool_name": "Read", "tool_input": {"file_path": "config/secret.yaml"}, "cwd": os.getcwd()},
            "deny",
        ),
        (
            "docs lifecycle ask",
            {"tool_name": "Write", "tool_input": {"file_path": "docs/design/new.md"}, "cwd": os.getcwd()},
            "ask",
        ),
        (
            "prompt context",
            {"prompt": "review production design"},
            "context",
        ),
        (
            "akb-test mutation denied",
            {"tool_name": "mcp__akb__akb_put", "tool_input": {"vault": "akb-test"}},
            "deny",
        ),
    ]

    failures: list[str] = []
    for name, payload, expected in cases:
        result = handle_user_prompt(payload) if expected == "context" else handle_pre_tool(payload)
        decision = result.get("hookSpecificOutput", {}).get("permissionDecision")
        if expected == "context":
            if "additionalContext" not in result.get("hookSpecificOutput", {}):
                failures.append(f"{name}: expected additionalContext, got {result!r}")
        elif decision != expected:
            failures.append(f"{name}: expected {expected}, got {decision!r}")

    if failures:
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1
    print("akb_pm_hook self-test passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("event", nargs="?", choices=["session-start", "user-prompt", "pre-tool", "stop"])
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return run_self_test()

    input_data = read_input()
    try:
        if args.event == "session-start":
            emit(handle_session_start(input_data))
        elif args.event == "user-prompt":
            emit(handle_user_prompt(input_data))
        elif args.event == "pre-tool":
            emit(handle_pre_tool(input_data))
        elif args.event == "stop":
            emit(handle_stop(input_data))
        else:
            emit({})
    except Exception as exc:  # Hooks should never crash Claude work.
        emit({"systemMessage": f"AKB PM hook failed non-blockingly: {exc}"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
