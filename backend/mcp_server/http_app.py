"""Authenticated MCP Streamable HTTP application.

The MCP SDK owns the protocol transport and its two protocol eras.  This
module owns only the AKB authentication boundary and the small adapter that
lets the SDK bind stateful legacy sessions to the authenticated AKB principal.
Modern 2026-07-28 requests are handled as stateless exchanges by the SDK;
legacy initialize/session requests retain the SDK's stateful transport.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import AsyncIterator

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import Message, Receive, Scope, Send

from mcp.server.auth.middleware.bearer_auth import (
    AuthenticatedUser as MCPAuthenticatedUser,
)
from mcp.server.auth.provider import AccessToken
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import DEFAULT_MAX_REQUEST_BODY_SIZE
from mcp_types import HEADER_MISMATCH, INVALID_REQUEST, UNSUPPORTED_PROTOCOL_VERSION
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS, MODERN_PROTOCOL_VERSIONS

from app.config import settings
from app.api.bounded_body import read_bounded_body
from app.exceptions import AKBError
from app.services.auth_service import (
    AuthenticatedUser as AKBAuthenticatedUser,
    resolve_mcp_authorization,
)


AKB_USER_SCOPE_KEY = "akb.mcp.user"
_LEGACY_DELETE_BODY = b'{"terminated":true}'


def _www_authenticate_header() -> str:
    """Build the RFC 9728 protected-resource challenge for MCP 401s."""

    base = 'Bearer realm="akb-mcp"'
    if settings.mcp_oauth_enabled and settings.public_base_url:
        meta_url = (
            f"{settings.public_base_url.rstrip('/')}"
            "/.well-known/oauth-protected-resource"
        )
        return f'{base}, resource_metadata="{meta_url}"'
    return base


def _transport_user(user: AKBAuthenticatedUser) -> MCPAuthenticatedUser:
    """Adapt an authenticated AKB user to the SDK session-owner contract.

    The SDK compares the authorization context that created a legacy session
    with every later request. AKB already resolved the real credential, so the
    adapter supplies only a stable user principal to that comparison. The
    placeholder token is never used for authentication and is deliberately not
    the caller's credential material.
    """

    access_token = AccessToken(
        token="akb-session-principal",
        client_id=user.user_id,
        subject=user.user_id,
        scopes=[],
        claims={"iss": "akb"},
    )
    return MCPAuthenticatedUser(access_token)


def _decoded_object(body: bytes) -> dict[str, object] | None:
    """Decode only enough of a POST body to make the era-routing decision."""

    try:
        decoded = json.loads(body)
    except (ValueError, RecursionError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _modern_envelope(body: dict[str, object] | None) -> dict[str, object] | None:
    if body is None:
        return None
    params = body.get("params")
    if not isinstance(params, dict):
        return None
    meta = params.get("_meta")
    if not isinstance(meta, dict) or "io.modelcontextprotocol/protocolVersion" not in meta:
        return None
    return meta


def _request_id(body: dict[str, object] | None) -> int | str | None:
    if body is None:
        return None
    value = body.get("id")
    return value if isinstance(value, (int, str)) and not isinstance(value, bool) else None


def _initialize_protocol_version(body: dict[str, object] | None) -> object | None:
    if body is None or body.get("method") != "initialize":
        return None
    params = body.get("params")
    return params.get("protocolVersion") if isinstance(params, dict) else None


async def _protocol_error(
    scope: Scope,
    receive: Receive,
    send: Send,
    *,
    request_id: int | str | None,
    code: int,
    message: str,
    data: object | None = None,
) -> None:
    """Return a typed JSON-RPC routing error before SDK/session creation."""

    error: dict[str, object] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    await JSONResponse(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": error,
        },
        status_code=400,
    )(scope, receive, send)


def _replay_body(body: bytes) -> Receive:
    """Give the SDK the one body already consumed by the protocol router."""

    delivered = False

    async def receive() -> dict:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        # The SDK's JSON-response path does not need another frame. Returning
        # disconnect is safe if a transport asks after consuming the body and
        # prevents an accidental wait on a caller-owned receive channel.
        return {"type": "http.disconnect"}

    return receive


def _legacy_delete_response_adapter(send: Send) -> Send:
    """Keep the pre-SDK success body for a terminated legacy session.

    The SDK correctly invalidates the transport, but its generic DELETE
    response has no body. AKB's established public endpoint contract returns
    ``{"terminated": true}`` and does not echo a session header; adapt only a
    successful legacy termination while leaving all rejection responses intact.
    """

    status_code: int | None = None

    async def adapted(message: Message) -> None:
        nonlocal status_code
        if message.get("type") == "http.response.start":
            status_code = message.get("status")
            if status_code == 200:
                headers: list[tuple[bytes, bytes]] = []
                has_content_length = False
                for name, value in message.get("headers", []):
                    lower_name = name.lower()
                    if lower_name == b"mcp-session-id":
                        continue
                    if lower_name == b"content-length":
                        value = str(len(_LEGACY_DELETE_BODY)).encode("ascii")
                        has_content_length = True
                    headers.append((name, value))
                if not has_content_length:
                    headers.append((b"content-length", str(len(_LEGACY_DELETE_BODY)).encode("ascii")))
                message = {**message, "headers": headers}
        elif (
            message.get("type") == "http.response.body"
            and status_code == 200
            and not message.get("more_body", False)
        ):
            message = {**message, "body": _LEGACY_DELETE_BODY}
        await send(message)

    return adapted


class MCPApp:
    """ASGI adapter for AKB auth plus the SDK's dual-era HTTP transport."""

    def __init__(self) -> None:
        self._session_manager: StreamableHTTPSessionManager | None = None

    def _manager(self) -> StreamableHTTPSessionManager:
        if self._session_manager is None:
            # Lazy import avoids constructing the heavy MCP business registry
            # when a caller only imports this module for auth metadata helpers.
            from mcp_server.server import server

            self._session_manager = StreamableHTTPSessionManager(
                app=server,
                json_response=True,
                max_request_body_size=DEFAULT_MAX_REQUEST_BODY_SIZE,
            )
        return self._session_manager

    @asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        """Own the SDK manager lifetime from the FastAPI application lifespan."""

        manager = self._manager()
        try:
            async with manager.run():
                yield
        finally:
            # StreamableHTTPSessionManager instances are intentionally one-shot
            # in MCP 2.x. A new manager is cheap and lets the FastAPI lifespan
            # be entered again in tests and process supervisors.
            self._session_manager = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return

        request = Request(scope, receive, send)
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

        # The SDK uses scope["user"] only for legacy session-owner binding.
        # The business dispatcher reads this AKB user so auth, scope checks and
        # audit all share the principal resolved at this boundary.
        scope["user"] = _transport_user(user)
        scope[AKB_USER_SCOPE_KEY] = user

        session_id = request.headers.get("mcp-session-id")
        header_version = request.headers.get("mcp-protocol-version")
        if request.method in {"GET", "DELETE"} and not session_id:
            if header_version not in MODERN_PROTOCOL_VERSIONS:
                await JSONResponse(
                    {"error": "Invalid session"},
                    status_code=404,
                )(scope, receive, send)
                return

        routed_receive = receive
        if request.method == "POST":
            try:
                body = await read_bounded_body(
                    request,
                    max_bytes=DEFAULT_MAX_REQUEST_BODY_SIZE,
                    too_large_message="MCP request body is too large",
                )
            except AKBError as exc:
                await JSONResponse(
                    {"error": exc.message},
                    status_code=exc.status_code,
                )(scope, receive, send)
                return
            decoded = _decoded_object(body)
            modern_meta = _modern_envelope(decoded)
            modern_body = modern_meta is not None
            modern_version = (
                modern_meta.get("io.modelcontextprotocol/protocolVersion")
                if modern_meta is not None
                else None
            )
            request_id = _request_id(decoded)
            initialize_version = _initialize_protocol_version(decoded)

            # The SDK's legacy negotiation primitive intentionally defaults an
            # unknown initialize offer to its latest handshake revision. AKB's
            # public matrix is an explicit allowlist, so reject that offer at
            # the adapter boundary instead of silently downgrading it.
            if (
                decoded is not None
                and decoded.get("method") == "initialize"
                and isinstance(initialize_version, str)
                and initialize_version not in HANDSHAKE_PROTOCOL_VERSIONS
                and header_version not in MODERN_PROTOCOL_VERSIONS
            ):
                await _protocol_error(
                    scope,
                    receive,
                    send,
                    request_id=request_id,
                    code=UNSUPPORTED_PROTOCOL_VERSION,
                    message="Unsupported protocol version",
                    data={
                        "supported": list(HANDSHAKE_PROTOCOL_VERSIONS),
                        "requested": initialize_version,
                    },
                )
                return

            if (
                decoded is not None
                and decoded.get("method") == "initialize"
                and header_version in MODERN_PROTOCOL_VERSIONS
            ):
                await _protocol_error(
                    scope,
                    receive,
                    send,
                    request_id=request_id,
                    code=UNSUPPORTED_PROTOCOL_VERSION,
                    message="The 2026-07-28 protocol uses server/discover instead of initialize",
                    data={
                        "supported": list(MODERN_PROTOCOL_VERSIONS),
                        "requested": initialize_version,
                    },
                )
                return

            # A modern exchange is self-contained: it never carries a legacy
            # session id. Reject before the SDK can look up or create state.
            if header_version in MODERN_PROTOCOL_VERSIONS and session_id:
                await _protocol_error(
                    scope,
                    receive,
                    send,
                    request_id=request_id,
                    code=INVALID_REQUEST,
                    message="2026-07-28 requests must not carry Mcp-Session-Id",
                )
                return

            # Body and carrier must agree before either manager path runs. A
            # modern body without its exact header must not fall into the
            # stateful legacy manager, where it could mint a session first.
            if modern_body and header_version != modern_version:
                await _protocol_error(
                    scope,
                    receive,
                    send,
                    request_id=request_id,
                    code=HEADER_MISMATCH,
                    message="mcp-protocol-version header does not match the request envelope's protocol version",
                )
                return

            routed_receive = _replay_body(body)

        manager_send = send
        if (
            request.method == "DELETE"
            and session_id
            and header_version not in MODERN_PROTOCOL_VERSIONS
        ):
            manager_send = _legacy_delete_response_adapter(send)
        await self._manager().handle_request(scope, routed_receive, manager_send)


mcp_app = MCPApp()
