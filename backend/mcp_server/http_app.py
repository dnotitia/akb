"""Authenticated, stateless MCP Streamable HTTP entrypoint."""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from typing import Any

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.shared.inbound import unsupported_protocol_version_rejection
from mcp_types import ErrorData, INVALID_REQUEST, JSONRPCError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import Receive, Scope, Send

from app.config import settings
from app.services.auth_service import resolve_mcp_authorization
from mcp_server.protocol import MCP_PROTOCOL_VERSION, MCP_SUPPORTED_PROTOCOL_VERSIONS


def _www_authenticate_header() -> str:
    """Build the RFC 9728 §5 ``WWW-Authenticate`` header for 401s."""
    base = 'Bearer realm="akb-mcp"'
    if settings.mcp_oauth_enabled and settings.public_base_url:
        meta_url = f"{settings.public_base_url.rstrip('/')}/.well-known/oauth-protected-resource"
        return f'{base}, resource_metadata="{meta_url}"'
    return base


def _request_id(decoded: Any) -> int | str | None:
    if not isinstance(decoded, dict):
        return None
    value = decoded.get("id")
    return value if isinstance(value, (int, str)) and not isinstance(value, bool) else None


def _requested_version(decoded: Any, header: str | None) -> str:
    if header:
        return header
    if not isinstance(decoded, dict):
        return ""
    params = decoded.get("params")
    if not isinstance(params, dict):
        return ""
    modern_meta = params.get("_meta")
    if isinstance(modern_meta, dict):
        version = modern_meta.get("io.modelcontextprotocol/protocolVersion")
        if isinstance(version, str):
            return version
    legacy_version = params.get("protocolVersion")
    return legacy_version if isinstance(legacy_version, str) else ""


def _jsonrpc_error(
    *,
    request_id: int | str | None,
    code: int,
    message: str,
    data: Any = None,
) -> JSONResponse:
    error = JSONRPCError(
        jsonrpc="2.0",
        id=request_id,
        error=ErrorData(code=code, message=message, data=data),
    )
    return JSONResponse(error.model_dump(mode="json", by_alias=True, exclude_none=False), status_code=400)


async def _reject_non_modern_request(request: Request, *, reason: str | None = None) -> Response:
    """Reject legacy, missing-version, and session-bound requests explicitly."""
    raw = await request.body()
    try:
        decoded = json.loads(raw)
    except (ValueError, RecursionError):
        decoded = None

    if reason is None:
        requested = _requested_version(decoded, request.headers.get("mcp-protocol-version"))
        rejection = unsupported_protocol_version_rejection(
            requested,
            MCP_SUPPORTED_PROTOCOL_VERSIONS,
        )
        assert rejection is not None
        return _jsonrpc_error(
            request_id=_request_id(decoded),
            code=rejection.code,
            message=rejection.message,
            data=rejection.data,
        )

    return _jsonrpc_error(
        request_id=_request_id(decoded),
        code=INVALID_REQUEST,
        message=reason,
    )


class MCPApp:
    """Authenticate each request before handing it to the native SDK manager."""

    def __init__(self) -> None:
        self._manager: StreamableHTTPSessionManager | None = None
        self._exit_stack: AsyncExitStack | None = None

    async def start(self) -> None:
        """Start the SDK's process-scoped manager and server lifespan."""
        if self._manager is not None:
            return

        from mcp_server.server import server

        manager = StreamableHTTPSessionManager(
            app=server,
            json_response=True,
            stateless=True,
        )
        exit_stack = AsyncExitStack()
        try:
            await exit_stack.enter_async_context(manager.run())
        except BaseException:
            await exit_stack.aclose()
            raise
        self._manager = manager
        self._exit_stack = exit_stack

    async def stop(self) -> None:
        """Stop the SDK manager and release all per-request tasks."""
        exit_stack = self._exit_stack
        self._manager = None
        self._exit_stack = None
        if exit_stack is not None:
            await exit_stack.aclose()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return

        request = Request(scope, receive, send)
        response: Response
        auth_header = request.headers.get("authorization", "")
        www_auth = _www_authenticate_header()
        if not auth_header:
            hint = (
                "Authorization required."
                if settings.mcp_oauth_enabled
                else "Authorization required. Use: Bearer akb_<your-pat>"
            )
            response = JSONResponse(
                {"error": hint},
                status_code=401,
                headers={"WWW-Authenticate": www_auth},
            )
            await response(scope, receive, send)
            return

        user = await resolve_mcp_authorization(auth_header)
        if not user:
            response = JSONResponse(
                {"error": "Invalid or expired token"},
                status_code=401,
                headers={"WWW-Authenticate": www_auth},
            )
            await response(scope, receive, send)
            return

        if request.headers.get("mcp-protocol-version") != MCP_PROTOCOL_VERSION:
            response = await _reject_non_modern_request(request)
            await response(scope, receive, send)
            return

        if request.headers.get("mcp-session-id") is not None:
            response = await _reject_non_modern_request(
                request,
                reason="Mcp-Session-Id is not supported by the stateless 2026-07-28 protocol",
            )
            await response(scope, receive, send)
            return

        manager = self._manager
        if manager is None:
            response = Response(status_code=503)
            await response(scope, receive, send)
            return
        await manager.handle_request(scope, receive, send)


mcp_app = MCPApp()
