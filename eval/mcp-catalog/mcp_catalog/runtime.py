"""Consumer for the repository-owned schema-v2 runtime descriptor."""

from __future__ import annotations

import json
import asyncio
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from .contracts import StateProbe

ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DEFAULT_HTTP_TIMEOUT_SECONDS = 30.0
# Matches scripts/ci/e2e_runtime.py::DEFAULT_TIMEOUT_SECONDS for reset/recovery.
RESET_TIMEOUT_SECONDS = 180.0


class RuntimeContractError(RuntimeError):
    """Raised when a runtime descriptor or fixture response is not safe to use."""

    def __init__(self, message: str, *, stage: str | None = None) -> None:
        super().__init__(message)
        self.stage = stage


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeContractError(f"{label} must be an object")
    return value


def _non_empty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeContractError(f"{label} is required")
    return value


def _environment_name(value: Any, label: str) -> str:
    name = _non_empty_string(value, label)
    if ENV_NAME_RE.fullmatch(name) is None:
        raise RuntimeContractError(f"{label} is invalid")
    return name


def _origin(value: Any, label: str) -> str:
    candidate = _non_empty_string(value, label)
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        raise RuntimeContractError(f"{label} must be an HTTP(S) origin") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise RuntimeContractError(f"{label} must be an HTTP(S) origin")
    return candidate.rstrip("/")


def _operation(value: Any, origin: str, label: str, method: str) -> str:
    operation = _object(value, label)
    if operation.get("method") != method:
        raise RuntimeContractError(f"{label} must use {method}")
    path = _non_empty_string(operation.get("url"), label)
    candidate = urljoin(f"{origin}/", path)
    try:
        parsed = urlsplit(candidate)
        parsed_origin = urlsplit(origin)
    except ValueError:
        raise RuntimeContractError(f"{label} must stay on its declared origin") from None
    if (
        parsed.scheme != parsed_origin.scheme
        or parsed.netloc != parsed_origin.netloc
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise RuntimeContractError(f"{label} must stay on its declared origin")
    return candidate


@dataclass(frozen=True, slots=True)
class RuntimeDescriptor:
    """The stable subset of the schema-v2 descriptor used by the benchmark."""

    raw: dict[str, Any]
    scenario: str
    app_origin: str
    app_health_url: str
    fixture_origin: str
    fixture_health_url: str
    reset_url: str
    reset_body: dict[str, Any]
    discovery_url: str
    username_env: str
    password_env: str
    pat_env: str | None
    stdio_service: dict[str, Any] | None

    @classmethod
    def from_file(cls, path: Path) -> RuntimeDescriptor:
        try:
            if str(path) == "-":
                raw = json.loads(sys.stdin.read())
            else:
                raw = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise RuntimeContractError(f"cannot read runtime descriptor: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeContractError(f"runtime descriptor is invalid JSON: {exc}") from exc
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: Any) -> RuntimeDescriptor:
        descriptor = _object(raw, "runtime descriptor")
        if descriptor.get("schema_version") != 2 or descriptor.get("status") != "ready":
            raise RuntimeContractError("runtime descriptor must be a ready schema-v2 descriptor")
        scenario = _non_empty_string(descriptor.get("scenario"), "descriptor scenario")
        services = _object(descriptor.get("services"), "descriptor services")
        app = _object(services.get("app"), "app service")
        fixture = _object(services.get("fixture"), "fixture service")
        app_origin = _origin(app.get("origin"), "app origin")
        fixture_origin = _origin(fixture.get("origin"), "fixture origin")
        app_health_url = _operation(app.get("health"), app_origin, "app health", "GET")
        _operation(app.get("discovery"), app_origin, "app discovery", "GET")
        fixture_health_url = _operation(fixture.get("health"), fixture_origin, "fixture health", "GET")
        reset = _object(fixture.get("reset"), "fixture reset")
        reset_url = _operation(reset, fixture_origin, "fixture reset", "POST")
        reset_body = _object(reset.get("body"), "fixture reset body")
        if reset_body.get("scenario") != scenario:
            raise RuntimeContractError("fixture reset scenario does not match descriptor")
        discovery_url = _operation(fixture.get("discovery"), fixture_origin, "fixture discovery", "GET")

        credentials = _object(descriptor.get("credentials"), "descriptor credentials")
        username_env = _environment_name(credentials.get("username_env"), "username environment name")
        password_env = _environment_name(credentials.get("password_env"), "password environment name")
        pat_raw = credentials.get("pat_env")
        pat_env = _environment_name(pat_raw, "PAT environment name") if pat_raw is not None else None

        stdio_raw = services.get("stdio")
        stdio_service: dict[str, Any] | None = None
        if stdio_raw is not None:
            stdio_service = _object(stdio_raw, "stdio service")
            if stdio_service.get("transport") != "stdio":
                raise RuntimeContractError("stdio service must declare transport=stdio")
            _non_empty_string(stdio_service.get("executable"), "stdio executable")
            _non_empty_string(stdio_service.get("consumer_root"), "stdio consumer root")
            environment = _object(stdio_service.get("environment"), "stdio environment")
            mcp_url = _non_empty_string(environment.get("AKB_MCP_URL"), "stdio AKB_MCP_URL")
            if not mcp_url.startswith(("http://", "https://")):
                raise RuntimeContractError("stdio AKB_MCP_URL must be HTTP(S)")
            if "AKB_PAT" in environment:
                _environment_name(environment["AKB_PAT"], "stdio PAT environment name")
            if pat_env is None:
                raise RuntimeContractError("stdio descriptor must expose credentials.pat_env")

        return cls(
            raw=descriptor,
            scenario=scenario,
            app_origin=app_origin,
            app_health_url=app_health_url,
            fixture_origin=fixture_origin,
            fixture_health_url=fixture_health_url,
            reset_url=reset_url,
            reset_body=dict(reset_body),
            discovery_url=discovery_url,
            username_env=username_env,
            password_env=password_env,
            pat_env=pat_env,
            stdio_service=stdio_service,
        )

    @property
    def supports_stdio(self) -> bool:
        return self.stdio_service is not None

    def source_revision_from(self, discovery: dict[str, Any]) -> str:
        candidates: list[Any] = []
        evidence = self.raw.get("evidence")
        if isinstance(evidence, dict):
            candidates.append(evidence.get("source_revision"))
        runtime = discovery.get("runtime")
        if isinstance(runtime, dict):
            candidates.append(runtime.get("source_revision"))
        supplied = [candidate for candidate in candidates if candidate is not None]
        if any(not isinstance(candidate, str) or re.fullmatch(r"[0-9a-f]{40}", candidate) is None for candidate in supplied):
            raise RuntimeContractError("runtime source revision must be a full git revision")
        revisions = {candidate for candidate in supplied if isinstance(candidate, str)}
        if len(revisions) > 1:
            raise RuntimeContractError("runtime descriptor and discovery source revisions differ")
        if revisions:
            return next(iter(revisions))
        raise RuntimeContractError("runtime discovery does not expose an exact source revision")

    def artifact_versions_from(self, discovery: dict[str, Any]) -> dict[str, str]:
        values: dict[str, str] = {}
        candidates: dict[str, set[str]] = {"backend_artifact_version": set(), "proxy_artifact_version": set()}
        for source in (self.raw.get("evidence"), discovery.get("runtime")):
            if not isinstance(source, dict):
                continue
            for key in ("backend_artifact_version", "proxy_artifact_version"):
                value = source.get(key)
                if isinstance(value, str) and value:
                    candidates[key].add(value)
        for key, versions in candidates.items():
            if len(versions) > 1:
                raise RuntimeContractError(f"runtime descriptor and discovery {key} differ")
            if versions:
                values[key] = next(iter(versions))
        if "backend_artifact_version" not in values:
            raise RuntimeContractError("runtime discovery does not expose backend artifact version")
        if self.supports_stdio and "proxy_artifact_version" not in values:
            raise RuntimeContractError("stdio runtime discovery does not expose proxy artifact version")
        return values


@dataclass(frozen=True, slots=True)
class StateObservation:
    available: bool
    status_code: int | None
    payload: Any = None
    error: str | None = None


class RuntimeFixture:
    """Reset and state-observe the existing runtime without owning its lifecycle."""

    def __init__(
        self,
        descriptor: RuntimeDescriptor,
        *,
        timeout: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
        reset_timeout: float = RESET_TIMEOUT_SECONDS,
        readiness_timeout: float = RESET_TIMEOUT_SECONDS,
        readiness_poll_interval: float = 0.25,
    ) -> None:
        if timeout < 0 or reset_timeout < 0 or readiness_timeout < 0 or readiness_poll_interval < 0:
            raise ValueError("runtime timing values must be non-negative")
        self.descriptor = descriptor
        self.client = httpx.AsyncClient(timeout=timeout)
        self.timeout = timeout
        self.reset_timeout = reset_timeout
        self.readiness_timeout = readiness_timeout
        self.readiness_poll_interval = readiness_poll_interval
        self._reset_lock = asyncio.Lock()

    async def close(self) -> None:
        await self.client.aclose()

    async def preflight(self) -> dict[str, Any]:
        await self.wait_until_ready(stage="runtime_readiness")
        discovery = await self.discover()
        source_revision = self.descriptor.source_revision_from(discovery)
        artifact_versions = self.descriptor.artifact_versions_from(discovery)
        return {
            "source_revision": source_revision,
            "artifact_versions": artifact_versions,
            "discovery": discovery,
        }

    async def wait_until_ready(self, *, stage: str = "fixture_readiness") -> None:
        """Wait for the descriptor's app and fixture health conditions."""

        deadline = time.monotonic() + self.readiness_timeout
        while True:
            try:
                remaining = max(0.0, deadline - time.monotonic())
                probe_timeout = min(self.timeout, remaining)
                app_health = await self.client.get(self.descriptor.app_health_url, timeout=probe_timeout)
                fixture_health = await self.client.get(self.descriptor.fixture_health_url, timeout=probe_timeout)
                app_payload = _json_object(app_health, "app readiness")
                fixture_payload = _json_object(fixture_health, "fixture readiness")
                if (
                    app_health.status_code == 200
                    and fixture_health.status_code == 200
                    and app_payload.get("status") == "ready"
                    and fixture_payload.get("status") == "ready"
                    and fixture_payload.get("scenario") == self.descriptor.scenario
                ):
                    return
            except (httpx.HTTPError, RuntimeContractError):
                pass

            if time.monotonic() >= deadline:
                raise RuntimeContractError(
                    f"{stage} did not recover before timeout",
                    stage=stage,
                )
            await asyncio.sleep(self.readiness_poll_interval)

    async def discover(self) -> dict[str, Any]:
        response = await self.client.get(self.descriptor.discovery_url)
        payload = _json_object(response, "fixture discovery")
        return payload

    async def reset(self) -> None:
        async with self._reset_lock:
            try:
                response = await self.client.post(
                    self.descriptor.reset_url,
                    json=self.descriptor.reset_body,
                    timeout=self.reset_timeout,
                )
            except httpx.HTTPError as exc:
                message = "fixture reset request timed out" if isinstance(exc, httpx.TimeoutException) else "fixture reset request failed"
                raise RuntimeContractError(message, stage="fixture_reset") from exc
            if response.status_code != 200:
                raise RuntimeContractError(
                    f"fixture reset returned HTTP {response.status_code}",
                    stage="fixture_reset",
                )
            try:
                payload = _json_object(response, "fixture reset")
            except RuntimeContractError as exc:
                raise RuntimeContractError(str(exc), stage="fixture_reset") from exc
            if payload.get("status") != "ready" or payload.get("scenario") != self.descriptor.scenario:
                raise RuntimeContractError(
                    "fixture reset response is not ready for the declared scenario",
                    stage="fixture_reset",
                )
            await self.wait_until_ready(stage="fixture_readiness")

    async def mint_pat(
        self,
        username: str,
        password: str,
        *,
        scopes: list[str] | None = None,
    ) -> tuple[str, str]:
        """Mint a short-lived benchmark PAT when the runtime keeps its PAT private."""

        try:
            login = await self.client.post(
                f"{self.descriptor.app_origin}/api/v1/auth/login",
                json={"username": username, "password": password},
            )
            if login.status_code != 200:
                raise RuntimeContractError("runtime benchmark login failed", stage="credential_login")
            login_payload = _json_object(login, "runtime benchmark login")
            session_token = login_payload.get("token")
            if not isinstance(session_token, str) or not session_token:
                raise RuntimeContractError("runtime benchmark login returned no session token")
            mint_body: dict[str, Any] = {"name": "mcp-catalog-benchmark"}
            if scopes is not None:
                mint_body["scopes"] = scopes
            response = await self.client.post(
                f"{self.descriptor.app_origin}/api/v1/auth/tokens",
                headers={"Authorization": f"Bearer {session_token}"},
                json=mint_body,
            )
            if response.status_code != 200:
                raise RuntimeContractError("runtime benchmark PAT mint failed", stage="pat_mint")
            payload = _json_object(response, "runtime benchmark PAT mint")
            token = payload.get("token")
            token_id = payload.get("token_id")
        except httpx.HTTPError as exc:
            raise RuntimeContractError("runtime benchmark credential request failed", stage="credential_request") from exc
        if not isinstance(token, str) or not token or not isinstance(token_id, str) or not token_id:
            raise RuntimeContractError("runtime benchmark PAT response is invalid", stage="pat_mint")
        return token, token_id

    async def revoke_pat(self, token: str, token_id: str, *, allow_absent: bool = False) -> None:
        try:
            response = await self.client.delete(
                f"{self.descriptor.app_origin}/api/v1/auth/tokens/{token_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise RuntimeContractError("runtime benchmark PAT cleanup request failed", stage="pat_cleanup") from exc
        if response.status_code in {200, 204}:
            return
        if allow_absent and response.status_code in {401, 404}:
            return
        if response.status_code not in {200, 204}:
            raise RuntimeContractError("runtime benchmark PAT cleanup failed", stage="pat_cleanup")

    async def observe(self, probe: StateProbe, *, token: str | None) -> StateObservation:
        origin = self.descriptor.app_origin if probe.service == "app" else self.descriptor.fixture_origin
        url = urljoin(f"{origin}/", probe.path.lstrip("/"))
        try:
            parsed = urlsplit(url)
            origin_parts = urlsplit(origin)
        except ValueError:
            return StateObservation(False, None, error="state probe URL is invalid")
        if parsed.scheme != origin_parts.scheme or parsed.netloc != origin_parts.netloc:
            return StateObservation(False, None, error="state probe escaped its declared origin")
        headers = {"Authorization": f"Bearer {token}"} if token and probe.service == "app" else {}
        try:
            if probe.method == "GET":
                response = await self.client.get(url, headers=headers)
            else:
                response = await self.client.post(url, headers=headers, json=probe.body or {})
        except httpx.HTTPError:
            return StateObservation(False, None, error="state probe request failed")
        if response.status_code != probe.expected_status:
            return StateObservation(False, response.status_code, error=f"state probe returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            return StateObservation(False, response.status_code, error="state probe returned non-JSON content")
        return StateObservation(True, response.status_code, payload=payload)

    def stdio_command(self, token: str) -> tuple[str, list[str], dict[str, str]]:
        service = self.descriptor.stdio_service
        if service is None:
            raise RuntimeContractError("runtime descriptor does not provide stdio")
        root = Path(_non_empty_string(service.get("consumer_root"), "stdio consumer root"))
        executable = _non_empty_string(service.get("executable"), "stdio executable")
        executable_path = Path(executable)
        candidates = [
            executable_path if executable_path.is_absolute() else root / "node_modules" / ".bin" / executable,
            root / "node_modules" / "akb-mcp" / "bin" / "akb-mcp.mjs",
        ]
        selected = next((candidate for candidate in candidates if candidate.is_file()), None)
        if selected is None:
            raise RuntimeContractError("stdio consumer does not expose the declared executable")
        node = shutil.which("node")
        if node is None:
            raise RuntimeContractError("stdio transport requires node")
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "AKB_MCP_URL": f"{self.descriptor.app_origin}/mcp/",
            "AKB_PAT": token,
        }
        return node, [str(selected)], environment


def _json_object(response: httpx.Response, label: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeContractError(f"{label} returned non-JSON content") from exc
    return _object(payload, label)
