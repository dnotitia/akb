"""Project-neutral fixture control for the repository-owned E2E runtime.

Only the empty scenario is intentionally exposed.  Product-specific setup
belongs in the E2E suites, not in this control plane.
"""

from __future__ import annotations

from typing import Literal, Protocol

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict


class FixtureRuntime(Protocol):
    scenario: str

    def fixture_health(self) -> dict[str, object]: ...

    async def reset_empty(self) -> None: ...


class ResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario: Literal["empty"]


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

    @app.post("/reset")
    async def reset(request: ResetRequest) -> dict[str, str]:
        if request.scenario != runtime.scenario:
            raise HTTPException(
                status_code=422,
                detail="reset scenario does not match the running runtime",
            )
        await runtime.reset_empty()
        return {"status": "ready", "scenario": runtime.scenario}

    return app
