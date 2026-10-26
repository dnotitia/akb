"""HTTP response policy and draining of explicitly read-only requests.

POST SQL can write even when it starts with SELECT/WITH. Auth callbacks,
streams, uploads and writes are intentionally not covered by this middleware.
Database command deadlines still bound those operations.
"""

import asyncio
from contextlib import suppress
import re

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send


_READ_PATH = re.compile(
    r"/api/v1/(?:recent|tables/[^/]+(?:/[^/]+/rows)?|documents/[^/]+/.+)"
)


class NoStorePublicSurfacesMiddleware:
    """Header-only middleware, without a second request/response task group."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        public = any(path == base or path.startswith(base + "/") for base in (
            "/api/v1/public", "/api/v1/publications", "/api/v1/oembed",
        ))
        private = (
            path in {"/api/v1/auth/config", "/api/v1/auth/tokens"}
            or path.startswith(("/api/v1/auth/tokens/", "/api/v1/admin/",
                                "/api/v1/app/installations/", "/api/v1/app/rollouts"))
            or (path.startswith("/api/v1/apps/") and any(part in path for part in (
                "/rollouts", "/installations/", "/legacy-adoptions",
            )))
        )

        async def send_with_policy(message: Message) -> None:
            if message["type"] == "http.response.start" and (public or private):
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "no-store"
                if private:
                    headers["Pragma"] = "no-cache"
            await send(message)

        await self.app(scope, receive, send_with_policy)


class CancelDisconnectedReadMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (scope["type"] != "http" or scope["method"] != "GET"
                or not _READ_PATH.fullmatch(scope["path"])):
            await self.app(scope, receive, send)
            return

        first = await receive()
        if first["type"] == "http.disconnect":
            return
        replayed = False

        async def replay_first() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return first
            return await receive()

        # These routes have no body contract. Preserve unusual GET bodies
        # without buffering them or racing a second reader of their stream.
        if first.get("body") or first.get("more_body"):
            await self.app(scope, replay_first, send)
            return

        disconnected = asyncio.Event()

        async def relay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return first
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def watch_disconnect() -> None:
            # There is exactly one consumer of the underlying receive stream.
            while (await receive())["type"] != "http.disconnect":
                pass
            disconnected.set()

        async def run_request() -> None:
            await self.app(scope, relay, send)

        request_task = asyncio.create_task(run_request())
        watcher = asyncio.create_task(watch_disconnect())
        outer_cancelled = False
        try:
            done, _ = await asyncio.wait((request_task, watcher), return_when=asyncio.FIRST_COMPLETED)
            if request_task in done:
                await request_task
            else:
                await watcher
                request_task.cancel()
                # Await rollback/pool release. Merely cancel() leaves work and
                # exceptions detached from the request lifecycle.
                # wait() does not propagate a shutdown cancellation into the
                # child a second time while its finally cleanup is running.
                await asyncio.wait((request_task,))
                with suppress(asyncio.CancelledError):
                    request_task.result()
        except asyncio.CancelledError:
            outer_cancelled = True
        finally:
            for task in (request_task, watcher):
                if not task.done() and not task.cancelling():
                    task.cancel()
            drain = asyncio.gather(request_task, watcher, return_exceptions=True)
            while not drain.done():
                try:
                    await asyncio.shield(drain)
                except asyncio.CancelledError:
                    outer_cancelled = True
            if outer_cancelled:
                raise asyncio.CancelledError
