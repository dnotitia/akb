"""Transparent loopback observation for real resource requests."""

from __future__ import annotations

import asyncio
import hashlib
import re

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
        self.client = httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=60)
        app = Starlette(routes=[Route("/{path:path}", self.forward, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])])
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False))
        self.task: asyncio.Task | None = None

    async def start(self):
        self.task = asyncio.create_task(self.server._serve(), name="native-resource-relay")
        async with asyncio.timeout(10):
            while not self.server.started:
                if self.task.done():
                    await self.task
                    raise RuntimeError("resource relay did not start")
                await asyncio.sleep(0.01)

    async def forward(self, request: Request):
        path = request.scope["raw_path"].decode("ascii")
        if path.startswith("/.well-known/"):
            inner = path
        elif not self.prefix:
            inner = path
        elif path.startswith(self.prefix + "/"):
            inner = path[len(self.prefix):]
        else:
            return Response(status_code=404)
        query = request.scope["query_string"].decode("ascii")
        target = self.target + inner + ("?" + query if query else "")
        authorization = request.headers.get("authorization")
        body = await request.body()
        event = {
            "sequence": len(self.events), "method": request.method, "path": safe_path(path),
            "inner_path": safe_path(inner), "authorization_present": authorization is not None,
            "bearer_sha256": hashlib.sha256(authorization.removeprefix("Bearer ").encode()).hexdigest() if authorization else None,
            "request_bytes": len(body), "request_sha256": hashlib.sha256(body).hexdigest(),
        }
        self.events.append(event)
        headers = [(key, value) for key, value in request.headers.raw if key.lower() not in {b"connection", b"transfer-encoding", b"accept-encoding"}]
        headers.append((b"accept-encoding", b"identity"))
        try:
            response = await self.client.request(request.method, target, content=body, headers=headers)
        except httpx.HTTPError:
            event["status"] = 502
            return Response("fixture resource forwarding failed", status_code=502)
        data = response.content
        event.update({"status": response.status_code, "response_bytes": len(data), "response_sha256": hashlib.sha256(data).hexdigest()})
        result = Response(data, status_code=response.status_code)
        result.raw_headers.extend((key, value) for key, value in response.headers.raw if key.lower() not in {b"content-length", b"transfer-encoding", b"connection", b"content-encoding"})
        return result

    async def close(self):
        self.server.should_exit = True
        if self.task is not None:
            await asyncio.wait_for(self.task, timeout=15)
        await self.client.aclose()
