#!/usr/bin/env python3
"""Generate the complete MCP tool array without installation configuration."""

from __future__ import annotations

import argparse
from itertools import product
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from mcp_server.tool_catalog import complete_catalog
from mcp_server.tools import TOOLS, tools_for_creation_options


def creation_option_refinements(canonical: list[dict]) -> dict[str, list[dict]]:
    schemas = {
        tool["name"]: {key: tool[key] for key in ("inputSchema", "outputSchema") if key in tool}
        for tool in canonical
    }
    refinements: dict[str, list[dict]] = {}
    for templates, external_git in product((False, True), repeat=2):
        tools = tools_for_creation_options(templates=templates, external_git=external_git)
        for tool in complete_catalog(tools):
            schema = {key: tool[key] for key in ("inputSchema", "outputSchema") if key in tool}
            if schema != schemas[tool["name"]] and schema not in refinements.get(tool["name"], []):
                refinements.setdefault(tool["name"], []).append(schema)
    return refinements


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--refinements-output", required=True, type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    canonical = complete_catalog(TOOLS)
    for output, value in (
        (args.output, canonical),
        (args.refinements_output, creation_option_refinements(canonical)),
    ):
        rendered = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        if args.check:
            if not output.is_file() or output.read_text(encoding="utf-8") != rendered:
                parser.exit(1, f"MCP catalog differs: {output}\n")
        else:
            output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
