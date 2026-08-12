"""Test-only fixture control for the repository-owned E2E runtime.

The control plane exposes only scenario-neutral health, reset, discovery, and
sanitized log-observation operations.  Product fixtures remain owned by the
runtime implementation and are never returned as raw private state.
"""

from __future__ import annotations

import inspect
from typing import Awaitable, Literal, Protocol

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict


class FixtureRuntime(Protocol):
    @property
    def scenario(self) -> str: ...

    def fixture_health(self) -> dict[str, object]: ...

    def fixture_discovery(self) -> dict[str, object]: ...

    def fixture_log_observation(self) -> dict[str, object]: ...

    async def reset_scenario(self) -> None: ...

    def fixture_control(
        self, action: str, target: str | None, enabled: bool, kind: str | None
    ) -> object | Awaitable[dict[str, object]]: ...


class ResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario: Literal["empty", "app-installation-lifecycle", "app-release-rollout"]


class ControlRequest(BaseModel):
    """Bounded fixture controls for conditions ordinary product APIs cannot induce."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["fault_injection", "restart"]
    target: str | None = None
    kind: str | None = None
    enabled: bool = True


def create_app(runtime: FixtureRuntime) -> FastAPI:
    """Build the test-only control app around a running runtime instance."""

    app = FastAPI(
        title="AKB E2E Fixture Control",
        version="2",
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json",
    )

    @app.get("/health")
    async def health() -> dict[str, object]:
        return runtime.fixture_health()

    @app.get("/discover")
    async def discover() -> dict[str, object]:
        return runtime.fixture_discovery()

    @app.get("/log-observation")
    async def log_observation() -> dict[str, object]:
        return runtime.fixture_log_observation()

    @app.post("/reset")
    async def reset(request: ResetRequest) -> dict[str, str]:
        if request.scenario != runtime.scenario:
            raise HTTPException(
                status_code=422,
                detail="reset scenario does not match the running runtime",
            )
        await runtime.reset_scenario()
        return {"status": "ready", "scenario": runtime.scenario}

    @app.post("/control")
    async def control(request: ControlRequest) -> dict[str, object]:
        handler = getattr(runtime, "fixture_control", None)
        if handler is None:
            raise HTTPException(status_code=404, detail="fixture control is unavailable")
        result = handler(request.action, request.target, request.enabled, request.kind)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, dict):
            raise HTTPException(status_code=500, detail="fixture control returned an invalid result")
        return result

    return app
