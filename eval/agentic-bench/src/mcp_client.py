"""AKB MCP Streamable HTTP client wrapper.

Wraps mcp.client.streamable_http for the bench. One session per query.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client


class MCPCallError(RuntimeError):
    pass


@asynccontextmanager
async def mcp_session(url: str, pat: str):
    headers = {"Authorization": f"Bearer {pat}"}
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def list_tool_names(session: ClientSession) -> list[str]:
    resp = await session.list_tools()
    return [t.name for t in resp.tools]


async def list_tools_full(session: ClientSession) -> list[dict[str, Any]]:
    """Return tools in OpenAI function-calling schema."""
    resp = await session.list_tools()
    out = []
    for t in resp.tools:
        out.append({
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description or "",
                "parameters": t.inputSchema or {"type": "object", "properties": {}},
            },
        })
    return out


def _coerce_content(blocks) -> str:
    parts = []
    for b in blocks:
        if hasattr(b, "text") and b.text is not None:
            parts.append(b.text)
        elif hasattr(b, "data"):
            parts.append(f"<binary {len(b.data)} bytes>")
        else:
            parts.append(str(b))
    return "\n".join(parts)


async def call_tool(session: ClientSession, name: str, args: dict[str, Any]) -> tuple[bool, str]:
    """Call tool; return (is_error, content_text)."""
    try:
        result = await session.call_tool(name, args)
    except Exception as e:
        return True, f"<tool-call exception> {type(e).__name__}: {e}"
    text = _coerce_content(result.content) if result.content else ""
    is_error = bool(getattr(result, "isError", False))
    # Truncate huge results so they don't blow LLM context. The cap
    # is sized so that a slim-formatted vault/collection listing
    # (~70 items × ~150 chars) fits without losing the tail of the
    # list — that scenario was the dominant failure in agentic-bench
    # v6/v7 (target vault hidden past the truncate point).
    MAX = 12000
    if len(text) > MAX:
        text = text[:MAX] + f"\n<truncated, original {len(text)} chars — use `query` / `limit` to narrow>"
    return is_error, text
