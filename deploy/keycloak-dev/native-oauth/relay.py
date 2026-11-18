"""Forward real loopback requests while recording sanitized acceptance evidence."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from urllib.parse import parse_qs

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
import uvicorn


def safe_path(path: str) -> str:
    return re.sub(r"(/api/v1/files/(?:upload|download|transfer)/)[^/]+$", r"\1[capability]", path)


class ResourceRelay:
    def __init__(self, *, port: int, target: str, prefix: str = ""):
        if re.fullmatch(r"(?:/[A-Za-z0-9_-]+)*", prefix) is None:
            raise ValueError("fixture prefix must contain simple path segments")
        self.origin = f"http://127.0.0.1:{port}"
        self.base_url = self.origin + prefix
        self.prefix = prefix
        self.target = target
        self.events: list[dict] = []
        self.client = httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=50)
        route = Route("/{path:path}", self.forward, methods=["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
        app = Starlette(routes=[route])
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False))
        self.task: asyncio.Task | None = None

    async def start(self):
        self.task = asyncio.create_task(self.server._serve(), name="loopback-relay")
        async with asyncio.timeout(10):
            while not self.server.started:
                if self.task.done():
                    await self.task
                    raise RuntimeError("loopback relay did not start")
                await asyncio.sleep(0.01)

    def observe(self, request: Request, path: str, inner: str, body: bytes) -> dict | None:
        authorization = request.headers.get("authorization")
        event = {
            "method": request.method, "path": safe_path(path), "inner_path": safe_path(inner),
            "authorization_present": authorization is not None,
            "bearer_sha256": hashlib.sha256(authorization.removeprefix("Bearer ").encode()).hexdigest() if authorization else None,
        }

        if inner.rstrip("/") == "/mcp" and body:
            try:
                rpc = json.loads(body)
            except (UnicodeError, ValueError):
                rpc = None
            if isinstance(rpc, dict):
                event["rpc_method"] = rpc.get("method")
                if rpc.get("method") == "tools/call":
                    event["rpc_tool"] = rpc.get("params", {}).get("name")
        if event["inner_path"].endswith("/[capability]"):
            event["request_sha256"] = hashlib.sha256(body).hexdigest()
        return event

    def observe_response(self, event: dict, response: httpx.Response) -> None:
        if event["inner_path"].endswith("/[capability]"):
            event["response_sha256"] = hashlib.sha256(response.content).hexdigest()

    async def forward(self, request: Request):
        path = request.scope["raw_path"].decode("ascii")
        if not self.prefix or path.startswith("/.well-known/"):
            inner = path
        elif path.startswith(self.prefix + "/"):
            inner = path[len(self.prefix):]
        else:
            return Response(status_code=404)
        query = request.scope["query_string"].decode("ascii")
        target = self.target + inner + ("?" + query if query else "")
        body = await request.body()
        event = self.observe(request, path, inner, body)
        if event is not None:
            self.events.append(event)
        headers = [(key, value) for key, value in request.headers.raw if key.lower() not in {
            b"connection", b"transfer-encoding", b"accept-encoding",
        }]
        headers.append((b"accept-encoding", b"identity"))
        try:
            response = await self.client.request(request.method, target, content=body, headers=headers)
        except httpx.HTTPError:
            if event is not None:
                event["status"] = 502
            return Response(status_code=502)
        if event is not None:
            event["status"] = response.status_code
            self.observe_response(event, response)
        result = Response(response.content, status_code=response.status_code)
        result.raw_headers.extend((key, value) for key, value in response.headers.raw if key.lower() not in {
            b"content-length", b"transfer-encoding", b"connection", b"content-encoding",
        })
        return result

    async def close(self):
        self.server.should_exit = True
        try:
            if self.task is not None:
                await asyncio.wait_for(self.task, timeout=15)
        finally:
            await self.client.aclose()


class AuthorityRelay(ResourceRelay):
    def __init__(self, *, port: int, target: str, realm: str):
        super().__init__(port=port, target=target)
        self.realm_path = f"/realms/{realm}"
        self.challenges: dict[str, str] = {}

    def observe(self, request: Request, path: str, inner: str, body: bytes) -> dict | None:
        endpoint = path.removeprefix(self.realm_path)
        if endpoint == "/clients-registrations/openid-connect" and request.method == "POST":
            return {"kind": "dcr", "anonymous": "authorization" not in request.headers}
        if endpoint == "/protocol/openid-connect/auth" and request.method == "GET":
            code_challenge = request.query_params.get("code_challenge")
            client_id = request.query_params.get("client_id")
            if client_id and code_challenge:
                self.challenges[client_id] = code_challenge
            return {"kind": "authorize", "s256": request.query_params.get("code_challenge_method") == "S256"}
        if endpoint == "/protocol/openid-connect/token" and request.method == "POST":
            params = parse_qs(body.decode("ascii"))
            grant = params.get("grant_type", [""])[0]
            if grant not in {"authorization_code", "refresh_token"}:
                return None
            event = {
                "kind": grant,
                "public_client": "authorization" not in request.headers and "client_secret" not in params,
            }
            if grant == "authorization_code":
                verifier = params.get("code_verifier", [""])[0]
                challenge = self.challenges.pop(params.get("client_id", [""])[0], None)
                computed = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                event["verifier_matches"] = bool(verifier and computed == challenge)
            return event
        discovery_paths = {
            f"{self.realm_path}/.well-known/openid-configuration",
            f"/.well-known/openid-configuration{self.realm_path}",
            f"/.well-known/oauth-authorization-server{self.realm_path}",
        }
        if request.method == "GET" and path in discovery_paths:
            return {"kind": "discovery"}
        return None

    def observe_response(self, event: dict, response: httpx.Response) -> None:
        if event["kind"] == "dcr" and response.status_code == 201:
            registration = response.json()
            event["public_client"] = registration.get("token_endpoint_auth_method") == "none" and not registration.get("client_secret")
            client_id = registration.get("client_id")
            if isinstance(client_id, str) and client_id:
                event["client_id"] = client_id
