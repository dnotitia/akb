from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

from mcp_catalog.runtime import RESET_TIMEOUT_SECONDS, RuntimeContractError, RuntimeDescriptor, RuntimeFixture


def descriptor_dict() -> dict:
    return {
        "schema_version": 2,
        "status": "ready",
        "scenario": "empty",
        "services": {
            "app": {
                "origin": "http://127.0.0.1:8000",
                "health": {"method": "GET", "url": "/readyz"},
                "discovery": {"method": "GET", "url": "/openapi.json"},
            },
            "fixture": {
                "origin": "http://127.0.0.1:8889",
                "health": {"method": "GET", "url": "/health"},
                "reset": {"method": "POST", "url": "/reset", "body": {"scenario": "empty"}},
                "discovery": {"method": "GET", "url": "/discover"},
            },
            "stdio": {
                "transport": "stdio",
                "executable": "akb-mcp",
                "consumer_root": "/tmp/consumer",
                "environment": {"AKB_MCP_URL": "http://127.0.0.1:8000/mcp/", "AKB_PAT": "AKB_E2E_PAT"},
            },
        },
        "credentials": {
            "username_env": "AKB_E2E_USERNAME",
            "password" + "_env": "AKB_E2E_PASSWORD",
            "pat_env": "AKB_E2E_PAT",
        },
        "evidence": {
            "source_revision": "a" * 40,
            "backend_artifact_version": "0.0.0",
            "proxy_artifact_version": "0.0.0",
        },
    }


class _Response:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _ResetReadinessClient:
    def __init__(self, descriptor: RuntimeDescriptor, *, recover: bool) -> None:
        self.descriptor = descriptor
        self.recover = recover
        self.get_calls: list[str] = []
        self.post_calls: list[tuple[str, dict]] = []
        self._health_reads = 0

    async def get(self, url: str, **_kwargs: object) -> _Response:
        self.get_calls.append(url)
        self._health_reads += 1
        ready = self.recover and self._health_reads > 2
        return _Response(
            200,
            {
                "status": "ready" if ready else "starting",
                "scenario": self.descriptor.scenario,
            },
        )

    async def post(self, url: str, *, json: dict, **_kwargs: object) -> _Response:
        self.post_calls.append((url, json))
        self._health_reads = 0
        return _Response(200, {"status": "ready", "scenario": self.descriptor.scenario})

    async def aclose(self) -> None:
        return None


class _TimedResetClient:
    def __init__(self, descriptor: RuntimeDescriptor, *, response_delay: float) -> None:
        self.descriptor = descriptor
        self.response_delay = response_delay
        self.reset_timeout: float | None = None
        self.get_calls: list[str] = []

    async def post(self, _url: str, *, json: dict, timeout: float | None = None) -> _Response:
        assert json == self.descriptor.reset_body
        self.reset_timeout = timeout
        if timeout is not None and self.response_delay > timeout:
            raise httpx.ReadTimeout("reset response exceeded request budget")
        return _Response(200, {"status": "ready", "scenario": self.descriptor.scenario})

    async def get(self, url: str, **_kwargs: object) -> _Response:
        self.get_calls.append(url)
        return _Response(200, {"status": "ready", "scenario": self.descriptor.scenario})

    async def aclose(self) -> None:
        return None


class _RevokeResponseClient:
    async def delete(self, _url: str, **_kwargs: object) -> _Response:
        return _Response(401, {})

    async def aclose(self) -> None:
        return None


class _MintClient:
    def __init__(self, descriptor: RuntimeDescriptor) -> None:
        self.descriptor = descriptor
        self.posts: list[tuple[str, dict]] = []

    async def post(self, url: str, *, json: dict, **_kwargs: object) -> _Response:
        self.posts.append((url, json))
        if url.endswith("/auth/login"):
            return _Response(200, {"token": "session-marker"})
        return _Response(200, {"token": "fresh-marker", "token_id": "token-id"})

    async def aclose(self) -> None:
        return None


def test_schema_v2_descriptor_resolves_exact_source_and_artifacts() -> None:
    descriptor = RuntimeDescriptor.from_dict(descriptor_dict())
    discovery = {
        "runtime": {
            "source_revision": "a" * 40,
            "backend_artifact_version": "0.0.0",
            "proxy_artifact_version": "0.0.0",
        }
    }

    assert descriptor.source_revision_from(discovery) == "a" * 40
    assert descriptor.artifact_versions_from(discovery) == {
        "backend_artifact_version": "0.0.0",
        "proxy_artifact_version": "0.0.0",
    }


def test_benchmark_cell_descriptors_are_explicit_and_non_nested() -> None:
    raw = descriptor_dict()
    raw["benchmark_cells"] = {
        "primary:http": descriptor_dict(),
        "primary:stdio": descriptor_dict(),
    }

    descriptor = RuntimeDescriptor.from_dict(raw)

    assert set(descriptor.benchmark_cells) == {"primary:http", "primary:stdio"}
    assert descriptor.cell_for("primary:http") is descriptor.benchmark_cells["primary:http"]
    assert descriptor.cell_for("missing") is descriptor

    nested = descriptor_dict()
    nested["benchmark_cells"] = {"nested": raw}
    with pytest.raises(RuntimeContractError, match="cannot be nested"):
        RuntimeDescriptor.from_dict(nested)


def test_descriptor_rejects_cross_origin_reset() -> None:
    raw = descriptor_dict()
    raw["services"]["fixture"]["reset"]["url"] = "http://other.invalid/reset"

    with pytest.raises(RuntimeContractError, match="declared origin"):
        RuntimeDescriptor.from_dict(raw)


def test_descriptor_rejects_revision_mismatch() -> None:
    descriptor = RuntimeDescriptor.from_dict(descriptor_dict())

    with pytest.raises(RuntimeContractError, match="source revisions differ"):
        descriptor.source_revision_from({"runtime": {"source_revision": "b" * 40}})


@pytest.mark.asyncio
async def test_stdio_command_uses_consumer_bin_and_child_env(tmp_path: Path) -> None:
    root = tmp_path / "consumer"
    executable = root / "node_modules" / ".bin" / "akb-mcp"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    os.chmod(executable, 0o755)
    raw = descriptor_dict()
    raw["services"]["stdio"]["consumer_root"] = str(root)
    descriptor = RuntimeDescriptor.from_dict(raw)
    fixture = RuntimeFixture(descriptor)
    try:
        command, args, environment = fixture.stdio_command("secret-pat")
    finally:
        await fixture.close()

    assert command.endswith("node") or Path(command).name == "node"
    assert args == [str(executable)]
    assert environment["AKB_PAT"] == "secret-pat"


@pytest.mark.asyncio
async def test_reset_waits_for_declared_app_and_fixture_readiness() -> None:
    descriptor = RuntimeDescriptor.from_dict(descriptor_dict())
    client = _ResetReadinessClient(descriptor, recover=True)
    fixture = RuntimeFixture(descriptor, readiness_timeout=0.2, readiness_poll_interval=0)
    fixture.client = client  # type: ignore[assignment]

    await fixture.reset()

    assert client.post_calls == [(descriptor.reset_url, descriptor.reset_body)]
    assert client.get_calls.count(descriptor.app_health_url) >= 2
    assert client.get_calls.count(descriptor.fixture_health_url) >= 2
    await fixture.close()


@pytest.mark.asyncio
async def test_reset_reports_readiness_stage_when_runtime_does_not_recover() -> None:
    descriptor = RuntimeDescriptor.from_dict(descriptor_dict())
    client = _ResetReadinessClient(descriptor, recover=False)
    fixture = RuntimeFixture(descriptor, readiness_timeout=0, readiness_poll_interval=0)
    fixture.client = client  # type: ignore[assignment]

    with pytest.raises(RuntimeContractError) as raised:
        await fixture.reset()

    assert raised.value.stage == "fixture_readiness"
    assert "readiness" in str(raised.value)
    await fixture.close()


@pytest.mark.asyncio
async def test_reset_uses_repository_reset_budget_not_ordinary_http_timeout() -> None:
    descriptor = RuntimeDescriptor.from_dict(descriptor_dict())
    client = _TimedResetClient(descriptor, response_delay=0.1)
    fixture = RuntimeFixture(descriptor, timeout=0.01, readiness_timeout=0.2, readiness_poll_interval=0)
    fixture.client = client  # type: ignore[assignment]

    await fixture.reset()

    assert client.reset_timeout == RESET_TIMEOUT_SECONDS
    assert client.reset_timeout > 0.01
    assert client.get_calls
    await fixture.close()


@pytest.mark.asyncio
async def test_reset_timeout_over_budget_is_fixture_reset_failure() -> None:
    descriptor = RuntimeDescriptor.from_dict(descriptor_dict())
    client = _TimedResetClient(descriptor, response_delay=0.1)
    fixture = RuntimeFixture(
        descriptor,
        timeout=0.01,
        reset_timeout=0.05,
        readiness_timeout=0.05,
        readiness_poll_interval=0,
    )
    fixture.client = client  # type: ignore[assignment]

    with pytest.raises(RuntimeContractError) as raised:
        await fixture.reset()

    assert raised.value.stage == "fixture_reset"
    assert "timed out" in str(raised.value)
    assert not client.get_calls
    await fixture.close()


@pytest.mark.asyncio
async def test_revoke_accepts_an_already_removed_token_only_when_marked_absent() -> None:
    descriptor = RuntimeDescriptor.from_dict(descriptor_dict())
    fixture = RuntimeFixture(descriptor)
    fixture.client = _RevokeResponseClient()  # type: ignore[assignment]

    await fixture.revoke_pat("stale-token", "stale-id", allow_absent=True)
    with pytest.raises(RuntimeContractError, match="cleanup failed"):
        await fixture.revoke_pat("stale-token", "stale-id")
    await fixture.close()


@pytest.mark.asyncio
async def test_mint_pat_forwards_the_declared_read_scope() -> None:
    descriptor = RuntimeDescriptor.from_dict(descriptor_dict())
    fixture = RuntimeFixture(descriptor)
    client = _MintClient(descriptor)
    fixture.client = client  # type: ignore[assignment]

    token, token_id = await fixture.mint_pat("fixture-user", "fixture-password", scopes=["read"])

    assert (token, token_id) == ("fresh-marker", "token-id")
    assert client.posts[1][1] == {"name": "mcp-catalog-benchmark", "scopes": ["read"]}
    await fixture.close()
