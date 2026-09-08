#!/usr/bin/env python3
"""Render the Codex and Claude Code role configuration from .agents/roles.toml.

    python scripts/agent-roles.py          # write .codex/ and .claude/
    python scripts/agent-roles.py --check  # exit 1 if either view drifted

One role definition, two rendered views. The rendered files are committed
because each agent finds its configuration by file name; the check exists
because a hand edit to a rendered file is a second contract nobody reads.
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
GENERATED = "Generated from .agents/roles.toml by scripts/agent-roles.py; edit the source."
EFFORTS = ("low", "medium", "high", "xhigh", "max")
READ_ONLY_DISALLOWED = "Edit, Write, NotebookEdit"


def fail(message: str) -> None:
    sys.exit(f"agent-roles: {message}")


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
    owner = spec.get("owner") or fail("[owner] is required")
    delegation = spec.get("delegation") or fail("[delegation] is required")
    roles = spec.get("roles") or fail("at least one [roles.<id>] is required")
    for section, table in (("owner", owner), *((f"roles.{k}", v) for k, v in roles.items())):
        if table.get("effort") not in EFFORTS:
            fail(f"[{section}] effort must be one of {', '.join(EFFORTS)}")
        model = table.get("model") or {}
        for agent in ("codex", "claude"):
            if not isinstance(model.get(agent), str) or not model[agent]:
                fail(f"[{section}] model.{agent} is required")
    for role_id, role in roles.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]*", role_id):
            fail(f"role id {role_id!r}: lowercase letters, digits and underscores only")
        if not isinstance(role.get("description"), str) or not role["description"].strip():
            fail(f"[roles.{role_id}] description is required")
        if not isinstance(role.get("instructions"), str) or not role["instructions"].strip():
            fail(f"[roles.{role_id}] instructions is required")
        if not isinstance(role.get("read_only"), bool):
            fail(f"[roles.{role_id}] read_only must be true or false")
    for key in ("max_workers", "depth"):
        if not isinstance(delegation.get(key), int) or delegation[key] < 1:
            fail(f"[delegation] {key} must be a positive integer")
    return spec


def render_codex_config(spec: dict) -> str:
    owner, delegation, roles = spec["owner"], spec["delegation"], spec["roles"]
    lines = [
        f"# {GENERATED}",
        "# Coding-agent defaults. Product inference models are configured separately.",
        f"model = {toml_string(owner['model']['codex'])}",
        f"model_reasoning_effort = {toml_string(owner['effort'])}",
    ]
    verbosity = (owner.get("verbosity") or {}).get("codex")
    if verbosity:
        lines.append(f"model_verbosity = {toml_string(verbosity)}")
    lines += [
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
    lines = [
        f"# {GENERATED}",
        f"model = {toml_string(role['model']['codex'])}",
        f"model_reasoning_effort = {toml_string(role['effort'])}",
    ]
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
        f"model: {role['model']['claude']}",
        f"effort: {role['effort']}",
    ]
    if role["read_only"]:
        lines += ["permissionMode: plan", f"disallowedTools: {READ_ONLY_DISALLOWED}"]
    lines += ["---", "", role["instructions"].strip("\n")]
    return "\n".join(lines) + "\n"


def render_claude_settings(spec: dict, existing: str | None) -> str:
    # Only the two keys this file owns are rendered; permissions and anything
    # else in settings.json stay hand-maintained.
    settings = json.loads(existing) if existing else {}
    if not isinstance(settings, dict):
        fail(".claude/settings.json must be a JSON object")
    owner = spec["owner"]
    merged = {"model": owner["model"]["claude"], "effortLevel": owner["effort"]}
    merged.update({k: v for k, v in settings.items() if k not in merged})
    return json.dumps(merged, indent=2, ensure_ascii=False) + "\n"


def render_all(spec: dict) -> dict[Path, str]:
    settings_path = REPO / ".claude" / "settings.json"
    existing = settings_path.read_text() if settings_path.exists() else None
    out: dict[Path, str] = {
        REPO / ".codex" / "config.toml": render_codex_config(spec),
        settings_path: render_claude_settings(spec, existing),
    }
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

    rendered = render_all(load())
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
        print(f"agent-roles: {len(rendered)} rendered files match .agents/roles.toml")
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
