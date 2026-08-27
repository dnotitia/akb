"""Authenticated MCP Streamable HTTP entrypoint.

The pinned MCP SDK owns the transport split: handshake-era requests use a
stateful session and 2026-07-28 requests use a fresh stateless exchange. This
module owns only the AKB authentication boundary and supplies the SDK with a
stable account principal for legacy-session binding.
"""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from typing import Any

from mcp.server.transport_security import DEFAULT_MAX_REQUEST_BODY_SIZE
from mcp.server.auth.middleware.bearer_auth import (
    AuthenticatedUser as MCPAuthenticatedUser,
)
from mcp.server.auth.provider import AccessToken as MCPAccessToken
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import Message, Receive, Scope, Send

from app.config import settings
from app.services.auth_service import AuthenticatedUser, resolve_mcp_authorization
from mcp_server.protocol import (
    MCP_LEGACY_PROTOCOL_VERSIONS,
    MCP_MODERN_PROTOCOL_VERSION,
)


def _www_authenticate_header() -> str:
    """Build the RFC 9728 §5 ``WWW-Authenticate`` header for 401s."""
    base = 'Bearer realm="akb-mcp"'
    if settings.mcp_oauth_enabled and settings.public_base_url:
        meta_url = f"{settings.public_base_url.rstrip('/')}/.well-known/oauth-protected-resource"
        return f'{base}, resource_metadata="{meta_url}"'
    return base


def _sdk_principal(user: AuthenticatedUser) -> MCPAuthenticatedUser:
    """Project an AKB account onto the SDK's legacy-session identity type.

    The SDK compares ``client_id``, issuer, and subject when a stateful
    session is reused. AKB authorizes the account returned by its own auth
    service, so the account UUID is the stable principal across PAT/OIDC
    credentials for that account. The credential value is deliberately not
    copied into this in-memory adapter.
    """
    access_token = MCPAccessToken(
        token="akb-session-principal",
        client_id=f"akb-user:{user.user_id}",
        scopes=[],
        subject=user.user_id,
        claims={"iss": "akb"},
    )
    return MCPAuthenticatedUser(access_token)


def _request_id(decoded: Any) -> int | str | None:
    if not isinstance(decoded, dict):
        return None
    value = decoded.get("id")
    return value if isinstance(value, (int, str)) and not isinstance(value, bool) else None


def _protocol_error(
    *,
    request_id: int | str | None,
    message: str,
    code: int = -32600,
    data: Any = None,
) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return JSONResponse(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": error,
        },
        status_code=400,
    )


async def _read_body_for_routing(request: Request, receive: Receive) -> tuple[bytes, Any]:
    """Read a POST body once and provide the SDK a replayable receive."""
    body = await request.body()
    decoded: Any = None
    try:
        decoded = json.loads(body)
    except (ValueError, RecursionError):
        pass

    sent = False

    async def replay() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return body, (decoded, replay)


class MCPApp:
    """Authenticate each request before the SDK's mixed-generation manager."""

    def __init__(self) -> None:
        self._manager: StreamableHTTPSessionManager | None = None
        self._exit_stack: AsyncExitStack | None = None
        self._session_revisions: dict[str, str] = {}

    async def start(self) -> None:
        """Start the process-scoped SDK manager and shared server lifespan."""
        if self._manager is not None:
            return

        from mcp_server.server import server

        manager = StreamableHTTPSessionManager(
            app=server,
            json_response=True,
            # The SDK's 2.1 manager routes modern requests to its stateless
            # driver while keeping the stateful driver for handshake-era
            # revisions. One manager therefore serves both generations.
            stateless=False,
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
        """Stop the manager and release legacy session tasks."""
        exit_stack = self._exit_stack
        self._manager = None
        self._exit_stack = None
        self._session_revisions.clear()
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

        # StreamableHTTPSessionManager binds stateful sessions to this
        # principal and returns the same opaque session-not-found response for
        # a different account. The application handler still resolves the
        # real AKB user from the Authorization header on every tool call.
        scope["user"] = _sdk_principal(user)

        manager = self._manager
        if manager is None:
            response = Response(status_code=503)
            await response(scope, receive, send)
            return

        protocol_header = request.headers.get("mcp-protocol-version")
        modern_header = protocol_header == MCP_MODERN_PROTOCOL_VERSION
        if modern_header and request.method != "POST":
            response = _protocol_error(
                request_id=None,
                message="The stateless 2026-07-28 protocol only accepts POST requests",
            )
            await response(scope, receive, send)
            return

        decoded: Any = None
        replay: Receive = receive
        negotiated_revision: str | None = None
        delete_messages: list[Message] | None = [] if request.method == "DELETE" else None
        if request.method == "POST":
            body, routing = await _read_body_for_routing(request, receive)
            decoded, replay = routing
            if len(body) > DEFAULT_MAX_REQUEST_BODY_SIZE:
                response = Response(status_code=413)
                await response(scope, receive, send)
                return

            body_meta: dict[str, Any] | None = None
            if isinstance(decoded, dict):
                params = decoded.get("params")
                if isinstance(params, dict):
                    meta = params.get("_meta")
                    if isinstance(meta, dict):
                        body_meta = meta
            body_modern_version = body_meta.get("io.modelcontextprotocol/protocolVersion") if body_meta else None
            method = decoded.get("method") if isinstance(decoded, dict) else None
            if body_modern_version is not None and (
                protocol_header is None
                or protocol_header in MCP_LEGACY_PROTOCOL_VERSIONS
                or protocol_header != body_modern_version
            ):
                response = _protocol_error(
                    request_id=_request_id(decoded),
                    message="Modern request metadata requires the 2026-07-28 protocol header",
                    code=-32020,
                )
                await response(scope, receive, send)
                return
            if method == "server/discover" and not modern_header:
                response = _protocol_error(
                    request_id=_request_id(decoded),
                    message="server/discover requires the 2026-07-28 stateless protocol",
                    code=-32022,
                    data={
                        "supported": [MCP_MODERN_PROTOCOL_VERSION],
                        "requested": str(body_modern_version or protocol_header or ""),
                    },
                )
                await response(scope, receive, send)
                return
            if method == "initialize" and modern_header:
                params = decoded.get("params") if isinstance(decoded, dict) else None
                requested = params.get("protocolVersion") if isinstance(params, dict) else ""
                response = _protocol_error(
                    request_id=_request_id(decoded),
                    message="initialize is only valid for the legacy initialize/session protocol",
                    code=-32022,
                    data={
                        "supported": list(MCP_LEGACY_PROTOCOL_VERSIONS),
                        "requested": str(requested or ""),
                    },
                )
                await response(scope, receive, send)
                return
            if method == "initialize" and isinstance(decoded, dict):
                params = decoded.get("params")
                requested = params.get("protocolVersion") if isinstance(params, dict) else None
                negotiated_revision = requested or MCP_LEGACY_PROTOCOL_VERSIONS[-1]
                if protocol_header is not None and protocol_header != negotiated_revision:
                    response = _protocol_error(
                        request_id=_request_id(decoded),
                        message="Mcp-Protocol-Version header does not match initialize protocolVersion",
                        code=-32020,
                    )
                    await response(scope, receive, send)
                    return
                if requested is not None and requested not in MCP_LEGACY_PROTOCOL_VERSIONS:
                    response = _protocol_error(
                        request_id=_request_id(decoded),
                        message="Unsupported protocol version",
                        code=-32022,
                        data={
                            "supported": list(MCP_LEGACY_PROTOCOL_VERSIONS),
                            "requested": str(requested),
                        },
                    )
                    await response(scope, receive, send)
                    return
            if modern_header:
                if request.headers.get("mcp-session-id") is not None:
                    response = _protocol_error(
                        request_id=_request_id(decoded),
                        message="Mcp-Session-Id is not supported by the stateless 2026-07-28 protocol",
                    )
                    await response(scope, receive, send)
                    return
                if isinstance(decoded, dict) and decoded.get("method") == "notifications/initialized":
                    response = _protocol_error(
                        request_id=_request_id(decoded),
                        message="notifications/initialized is only valid for the legacy initialize/session protocol",
                    )
                    await response(scope, receive, send)
                    return
            receive = replay

        session_id = request.headers.get("mcp-session-id")
        if session_id is not None and protocol_header in MCP_LEGACY_PROTOCOL_VERSIONS:
            expected_revision = self._session_revisions.get(session_id)
            if expected_revision is not None and protocol_header != expected_revision:
                response = _protocol_error(
                    request_id=_request_id(decoded),
                    message="Mcp-Protocol-Version does not match the negotiated legacy session revision",
                    code=-32020,
                )
                await response(scope, receive, send)
                return

        async def capture_response(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = dict(message.get("headers", []))
                response_session = headers.get(b"mcp-session-id")
                if response_session is not None and negotiated_revision is not None:
                    self._session_revisions[response_session.decode("latin-1")] = negotiated_revision
            if delete_messages is not None:
                delete_messages.append(message)
            else:
                await send(message)

        await manager.handle_request(scope, receive, capture_response)
        if delete_messages is not None:
            response_start = next(
                (message for message in delete_messages if message["type"] == "http.response.start"),
                None,
            )
            status = response_start["status"] if response_start is not None else 500
            if status == 200:
                await JSONResponse({"terminated": True})(scope, receive, send)
            else:
                for message in delete_messages:
                    await send(message)
            if session_id is not None:
                self._session_revisions.pop(session_id, None)


mcp_app = MCPApp()
