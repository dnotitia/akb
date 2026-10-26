#!/usr/bin/env python3
"""Render the Codex and Claude Code role configuration from .agents/roles.toml.

    python scripts/agent-roles.py          # write .codex/ and .claude/agents/
    python scripts/agent-roles.py --check  # exit 1 if either view drifted

One role definition, two rendered views. The rendered files are committed
because each agent finds its configuration by file name; the check exists
because a hand edit to a rendered file is a second contract nobody reads.

Neither view names a model or a reasoning effort: each person's own agent
configuration chooses them and every role inherits the session's choice. A
pinned model goes stale and overrides that choice for everyone, so the source
and .claude/settings.json are refused when they carry one.

Stdlib only: tomllib needs Python 3.11+, which scripts/check.sh already
requires for the analyzers.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    sys.exit("agent-roles: needs Python 3.11+ (tomllib); run with the interpreter scripts/check.sh uses")

REPO = Path(__file__).resolve().parent.parent
SOURCE = REPO / ".agents" / "roles.toml"
CLAUDE_SETTINGS = REPO / ".claude" / "settings.json"
GENERATED = "Generated from .agents/roles.toml by scripts/agent-roles.py; edit the source."
READ_ONLY_DISALLOWED = "Edit, Write, NotebookEdit"

# The keys the source may carry. Anything else is refused rather than ignored,
# so a setting nobody renders cannot look like configuration.
DELEGATION_KEYS = {"max_workers", "depth"}
ROLE_KEYS = {"description", "read_only", "instructions"}

# A key naming a model or tuning one: model, model.codex, effort, effortLevel,
# model_reasoning_effort, model_provider, model_verbosity, availableModels, ...
MODEL_KEY = re.compile(r"model|effort|reasoning|verbosity", re.IGNORECASE)
MODEL_FIX = (
    "this repository does not pin a coding-agent model or reasoning effort; "
    "Claude Code and Codex inherit each person's own choice. Remove {keys} from {file}"
)


def fail(message: str) -> None:
    sys.exit(f"agent-roles: {message}")


def model_keys(table: dict, prefix: str = "") -> list[str]:
    """Dotted paths of every key in ``table`` that names or tunes a model."""
    found: list[str] = []
    for key, value in table.items():
        path = f"{prefix}{key}"
        if MODEL_KEY.search(key):
            found.append(path)
        elif isinstance(value, dict):
            found += model_keys(value, f"{path}.")
    return found


def refuse_model_keys(keys: list[str], file: str, then: str) -> None:
    if keys:
        fail(MODEL_FIX.format(keys=", ".join(f"`{k}`" for k in keys), file=file) + f", then run: {then}")


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def toml_multiline(value: str) -> str:
    if '"""' in value:
        fail("instructions must not contain a triple quote")
    return '"""\n' + value.strip("\n").replace("\\", "\\\\") + '\n"""'


def yaml_scalar(value: str) -> str:
    # Plain scalars are what the docs show; quote only when YAML would misread.
    if re.search(r"(^[\s\-?:,\[\]{}#&*!|>'\"%@`])|(:\s)|(\s#)|(\s$)", value):
        return json.dumps(value, ensure_ascii=False)
    return value


def load() -> dict:
    with SOURCE.open("rb") as handle:
        spec = tomllib.load(handle)
    refuse_model_keys(model_keys(spec), ".agents/roles.toml", "python scripts/agent-roles.py")
    unknown = sorted(set(spec) - {"delegation", "roles"})
    if unknown:
        fail(f"unknown top-level table(s) in .agents/roles.toml: {', '.join(unknown)}")
    delegation = spec.get("delegation") or fail("[delegation] is required")
    roles = spec.get("roles") or fail("at least one [roles.<id>] is required")
    if set(delegation) - DELEGATION_KEYS:
        fail(f"[delegation] unknown key(s): {', '.join(sorted(set(delegation) - DELEGATION_KEYS))}")
    for key in sorted(DELEGATION_KEYS):
        if not isinstance(delegation.get(key), int) or delegation[key] < 1:
            fail(f"[delegation] {key} must be a positive integer")
    for role_id, role in roles.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]*", role_id):
            fail(f"role id {role_id!r}: lowercase letters, digits and underscores only")
        if set(role) - ROLE_KEYS:
            fail(f"[roles.{role_id}] unknown key(s): {', '.join(sorted(set(role) - ROLE_KEYS))}")
        if not isinstance(role.get("description"), str) or not role["description"].strip():
            fail(f"[roles.{role_id}] description is required")
        if not isinstance(role.get("instructions"), str) or not role["instructions"].strip():
            fail(f"[roles.{role_id}] instructions is required")
        if not isinstance(role.get("read_only"), bool):
            fail(f"[roles.{role_id}] read_only must be true or false")
    return spec


def check_claude_settings() -> None:
    # settings.json is hand-maintained (permissions); it must not pin a model
    # either, or the rendered agents would inherit that pin instead.
    if not CLAUDE_SETTINGS.exists():
        return
    settings = json.loads(CLAUDE_SETTINGS.read_text())
    if not isinstance(settings, dict):
        fail(".claude/settings.json must be a JSON object")
    keys = [k for k in settings if MODEL_KEY.search(k)]
    env = settings.get("env")
    if isinstance(env, dict):
        keys += [f"env.{k}" for k in env if MODEL_KEY.search(k)]
    refuse_model_keys(keys, ".claude/settings.json", "python scripts/agent-roles.py --check")


def render_codex_config(spec: dict) -> str:
    delegation, roles = spec["delegation"], spec["roles"]
    lines = [
        f"# {GENERATED}",
        "# No model or reasoning effort is set: each person's own Codex configuration",
        "# chooses them. Product inference models are configured separately.",
        "",
        "[agents]",
        f"max_concurrent_threads_per_session = {delegation['max_workers']}",
        f"max_depth = {delegation['depth']}",
    ]
    for role_id, role in roles.items():
        lines += [
            "",
            f"[agents.{role_id}]",
            f"description = {toml_string(role['description'])}",
            f"config_file = {toml_string(f'agents/{role_id}.toml')}",
        ]
    return "\n".join(lines) + "\n"


def render_codex_role(role: dict) -> str:
    lines = [f"# {GENERATED}"]
    if role["read_only"]:
        lines.append('sandbox_mode = "read-only"')
    lines.append(f"developer_instructions = {toml_multiline(role['instructions'])}")
    return "\n".join(lines) + "\n"


def claude_name(role_id: str) -> str:
    return role_id.replace("_", "-")


def render_claude_role(role_id: str, role: dict) -> str:
    lines = [
        "---",
        f"# {GENERATED}",
        f"name: {claude_name(role_id)}",
        f"description: {yaml_scalar(role['description'])}",
    ]
    if role["read_only"]:
        lines += ["permissionMode: plan", f"disallowedTools: {READ_ONLY_DISALLOWED}"]
    lines += ["---", "", role["instructions"].strip("\n")]
    return "\n".join(lines) + "\n"


def render_all(spec: dict) -> dict[Path, str]:
    out: dict[Path, str] = {REPO / ".codex" / "config.toml": render_codex_config(spec)}
    for role_id, role in spec["roles"].items():
        out[REPO / ".codex" / "agents" / f"{role_id}.toml"] = render_codex_role(role)
        out[REPO / ".claude" / "agents" / f"{claude_name(role_id)}.md"] = render_claude_role(role_id, role)
    return out


def strays(rendered: dict[Path, str]) -> list[Path]:
    found: list[Path] = []
    for directory, suffix in ((REPO / ".codex" / "agents", ".toml"), (REPO / ".claude" / "agents", ".md")):
        if directory.is_dir():
            found += [p for p in sorted(directory.iterdir()) if p.suffix == suffix and p not in rendered]
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="report drift instead of writing")
    args = parser.parse_args()

    spec = load()
    check_claude_settings()
    rendered = render_all(spec)
    stray = strays(rendered)
    if args.check:
        drift: list[str] = []
        for path, content in rendered.items():
            rel = path.relative_to(REPO)
            current = path.read_text() if path.exists() else ""
            if current != content:
                drift.append(str(rel))
                sys.stdout.writelines(difflib.unified_diff(
                    current.splitlines(True), content.splitlines(True),
                    fromfile=f"{rel} (on disk)", tofile=f"{rel} (rendered)",
                ))
        for path in stray:
            drift.append(f"{path.relative_to(REPO)} (not defined in .agents/roles.toml)")
        if drift:
            print("agent-roles: rendered configuration drifted from .agents/roles.toml:", file=sys.stderr)
            for item in drift:
                print(f"  {item}", file=sys.stderr)
            print("  run: python scripts/agent-roles.py", file=sys.stderr)
            return 1
        print(f"agent-roles: {len(rendered)} rendered files match .agents/roles.toml; no model or effort pinned")
        return 0

    for path, content in rendered.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_text() != content:
            path.write_text(content)
            print(f"wrote {path.relative_to(REPO)}")
    for path in stray:
        print(f"stray (not in .agents/roles.toml, left in place): {path.relative_to(REPO)}", file=sys.stderr)
    return 1 if stray else 0


if __name__ == "__main__":
    sys.exit(main())
