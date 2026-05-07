"""MCP Streamable HTTP — mounts MCP server as ASGI app at /mcp.

Each authenticated session gets its own transport + server loop.
Agents connect via:
  POST http://localhost:8000/mcp/
  Authorization: Bearer akb_<pat>
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp.server.streamable_http import StreamableHTTPServerTransport

from app.services.auth_service import resolve_token

logger = logging.getLogger("akb.mcp")

# Active transports keyed by session ID
_transports: dict[str, StreamableHTTPServerTransport] = {}
_server_tasks: dict[str, asyncio.Task] = {}
# Authenticated user per session (used by tool handlers)
_session_users: dict[str, object] = {}


def get_session_user(session_id: str):
    """Get authenticated user for a session. Called by tool handlers."""
    return _session_users.get(session_id)


async def _ensure_server_running(session_id: str, transport: StreamableHTTPServerTransport) -> None:
    """Ensure the MCP server loop is running for this transport."""
    if session_id in _server_tasks and not _server_tasks[session_id].done():
        return

    from mcp_server.server import server

    async def run():
        try:
            async with transport.connect() as (read_stream, write_stream):
                await server.run(read_stream, write_stream, server.create_initialization_options())
        except Exception:
            logger.exception("MCP server loop error for session %s", session_id)
        finally:
            _transports.pop(session_id, None)
            _session_users.pop(session_id, None)
            _server_tasks.pop(session_id, None)

    _server_tasks[session_id] = asyncio.create_task(run())
    await asyncio.sleep(0.05)


class MCPApp:
    """ASGI app that handles MCP Streamable HTTP with PAT auth."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return

        request = Request(scope, receive, send)

        # Auth check
        auth_header = request.headers.get("authorization", "")
        if not auth_header:
            response = JSONResponse(
                {"error": "Authorization required. Use: Bearer akb_<your-pat>"},
                status_code=401,
            )
            await response(scope, receive, send)
            return

        user = await resolve_token(auth_header)
        if not user:
            response = JSONResponse({"error": "Invalid or expired token"}, status_code=401)
            await response(scope, receive, send)
            return

        session_id = request.headers.get("mcp-session-id")

        if request.method == "DELETE":
            if session_id and session_id in _transports:
                transport = _transports.pop(session_id)
                _session_users.pop(session_id, None)
                await transport.terminate()
                task = _server_tasks.pop(session_id, None)
                if task:
                    task.cancel()
                logger.info("MCP session terminated: %s", session_id[:8])
            response = JSONResponse({"terminated": True})
            await response(scope, receive, send)
            return

        if request.method == "POST":
            if session_id and session_id in _transports:
                transport = _transports[session_id]
            else:
                session_id = str(uuid.uuid4())
                transport = StreamableHTTPServerTransport(
                    mcp_session_id=session_id,
                    is_json_response_enabled=True,
                )
                _transports[session_id] = transport
                _session_users[session_id] = user
                await _ensure_server_running(session_id, transport)
                logger.info("MCP session started: %s (user: %s)", session_id[:8], user.username)

            # Delegate to transport's ASGI handler
            await transport.handle_request(scope, receive, send)
            return

        if request.method == "GET":
            if not session_id or session_id not in _transports:
                response = JSONResponse({"error": "Invalid session"}, status_code=404)
                await response(scope, receive, send)
                return
            transport = _transports[session_id]
            await transport.handle_request(scope, receive, send)
            return

        response = JSONResponse({"error": "Method not allowed"}, status_code=405)
        await response(scope, receive, send)


mcp_app = MCPApp()
