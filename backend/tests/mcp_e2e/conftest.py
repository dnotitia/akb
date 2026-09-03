"""Pytest fixtures for the authenticated MCP behavior scenario."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from .runtime import RuntimeContext, RuntimeDescriptor, RuntimeSetupError, redact_error


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--runtime-descriptor",
        default=None,
        help="schema-v2 runtime descriptor path, or '-' to read it from stdin",
    )


def _read_descriptor(source: str, capture_manager: Any = None) -> str:
    if source == "-":
        suspended = False
        try:
            if capture_manager is not None:
                capture_manager.suspend_global_capture(in_=True)
                suspended = True
            with os.fdopen(os.dup(0), "r", encoding="utf-8") as stream:
                return stream.read()
        except OSError:
            raise RuntimeSetupError("runtime descriptor stdin could not be read") from None
        finally:
            if suspended:
                capture_manager.resume_global_capture()
    try:
        return Path(source).read_text(encoding="utf-8")
    except OSError:
        raise RuntimeSetupError("runtime descriptor file could not be read") from None


def _request_json(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    stage: str,
    body: Mapping[str, Any] | None = None,
    authorization: str | None = None,
) -> dict[str, Any]:
    headers = {"Authorization": authorization} if authorization else None
    try:
        response = client.request(method, url, json=body, headers=headers)
    except httpx.HTTPError:
        raise RuntimeSetupError(f"{stage} request failed") from None
    if response.status_code != 200:
        raise RuntimeSetupError(f"{stage} returned HTTP {response.status_code}")
    try:
        value = response.json()
    except ValueError:
        raise RuntimeSetupError(f"{stage} returned invalid JSON") from None
    if not isinstance(value, dict):
        raise RuntimeSetupError(f"{stage} returned an invalid object")
    return value


def _prepare_runtime(descriptor: RuntimeDescriptor, client: httpx.Client) -> RuntimeContext:
    def ready(stage: str) -> None:
        app = _request_json(client, "GET", descriptor.app_health_url, stage=f"{stage} app health")
        fixture = _request_json(
            client, "GET", descriptor.fixture_health_url, stage=f"{stage} fixture health"
        )
        if app.get("status") != "ready" or fixture.get("status") != "ready":
            raise RuntimeSetupError(f"{stage} runtime is not ready")

    def mint_path(discovery: dict[str, Any]) -> str:
        if discovery.get("status") != "ready" or discovery.get("scenario") != descriptor.scenario:
            raise RuntimeSetupError("fixture discovery is not ready for this scenario")
        access = discovery.get("access")
        login = access.get("login") if isinstance(access, dict) else None
        if (
            not isinstance(login, dict)
            or login.get("service") != "app"
            or login.get("method") != "POST"
            or login.get("path") != descriptor.login_path
        ):
            raise RuntimeSetupError("descriptor and discovery login coordinates disagree")
        runtime = discovery.get("runtime")
        if not isinstance(runtime, dict):
            raise RuntimeSetupError("fixture runtime must be an object")
        credential_env = runtime.get("credential_env")
        if (
            not isinstance(credential_env, dict)
            or credential_env.get("username") != descriptor.username_env
            or credential_env.get("password") != descriptor.password_env
        ):
            raise RuntimeSetupError("descriptor and discovery credential names disagree")
        pat = runtime.get("pat")
        mint = pat.get("mint") if isinstance(pat, dict) else None
        if not isinstance(mint, dict) or mint.get("service") != "app" or mint.get("method") != "POST":
            raise RuntimeSetupError("fixture PAT mint coordinates are invalid")
        path = mint.get("path")
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path:
            raise RuntimeSetupError("fixture PAT mint path is invalid")
        return path

    ready("before reset")
    discovery = _request_json(client, "GET", descriptor.discovery_url, stage="fixture discovery")
    mint = mint_path(discovery)
    reset = _request_json(
        client, "POST", descriptor.reset_url, stage="fixture reset", body=descriptor.reset_body
    )
    if reset.get("status") != "ready" or reset.get("scenario") != descriptor.scenario:
        raise RuntimeSetupError("fixture reset did not return the selected ready scenario")
    ready("after reset")
    discovery = _request_json(
        client, "GET", descriptor.discovery_url, stage="fixture discovery after reset"
    )
    mint = mint_path(discovery)

    username = os.environ.get(descriptor.username_env, "")
    password = os.environ.get(descriptor.password_env, "")
    if not username or not password:
        raise RuntimeSetupError("declared credential environment values are missing")
    secrets = (username, password)
    login = _request_json(
        client,
        "POST",
        urljoin(f"{descriptor.app_origin}/", descriptor.login_path.lstrip("/")),
        stage="credential login",
        body={"username": username, "password": password},
    )
    session_token = login.get("token")
    if not isinstance(session_token, str) or not session_token:
        raise RuntimeSetupError("credential login returned no session token")
    secrets = (*secrets, session_token)
    minted = _request_json(
        client,
        "POST",
        urljoin(f"{descriptor.app_origin}/", mint.lstrip("/")),
        stage="credential mint",
        authorization=f"Bearer {session_token}",
        body={"name": "mcp-pytest"},
    )
    pat = minted.get("token")
    if not isinstance(pat, str) or not pat.startswith("akb_"):
        raise RuntimeSetupError("credential mint returned an invalid PAT")
    return RuntimeContext(descriptor=descriptor, pat=pat, secrets=(*secrets, pat))


@pytest.fixture
def runtime_session(request: pytest.FixtureRequest) -> Iterator[RuntimeContext]:
    source = request.config.getoption("--runtime-descriptor")
    if not source:
        pytest.fail("scenario=akb_list_vaults preparation: --runtime-descriptor is required")
    try:
        capture_manager = request.config.pluginmanager.getplugin("capturemanager")
        descriptor = RuntimeDescriptor.from_json(_read_descriptor(source, capture_manager))
    except Exception as exc:
        pytest.fail(f"scenario=akb_list_vaults preparation: {redact_error(exc)}")

    client = httpx.Client(timeout=30.0)
    try:
        try:
            context = _prepare_runtime(descriptor, client)
        except Exception as exc:
            pytest.fail(f"scenario=akb_list_vaults preparation: {redact_error(exc)}")
        yield context
    finally:
        client.close()


@pytest.fixture
async def mcp_client(runtime_session: RuntimeContext) -> AsyncIterator[Client]:
    http_client = httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {runtime_session.pat}"},
        timeout=httpx2.Timeout(30.0, read=300.0),
        follow_redirects=True,
        trust_env=False,
    )
    try:
        client = Client(
            streamable_http_client(runtime_session.descriptor.mcp_url, http_client=http_client),
            mode="auto",
            read_timeout_seconds=30.0,
            cache=None,
        )
    except BaseException:
        await http_client.aclose()
        raise

    close_requested = asyncio.Event()
    ready = asyncio.Event()
    finished = asyncio.Event()
    lifecycle_error: BaseException | None = None

    async def run_client() -> None:
        nonlocal lifecycle_error
        try:
            async with client:
                ready.set()
                await close_requested.wait()
        except BaseException as exc:
            lifecycle_error = exc
            ready.set()
        finally:
            try:
                await http_client.aclose()
            except BaseException as exc:
                if lifecycle_error is None:
                    lifecycle_error = exc
            finally:
                finished.set()

    async def wait_for_finish() -> None:
        cancelled = False
        while not finished.is_set():
            try:
                await asyncio.shield(finished.wait())
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError

    task = asyncio.create_task(run_client(), name="mcp-sdk-client")
    reported_startup_error = False
    try:
        await ready.wait()
        if lifecycle_error is not None:
            reported_startup_error = True
            pytest.fail(
                "scenario=akb_list_vaults SDK connection: "
                + redact_error(lifecycle_error, runtime_session.secrets)
            )
        yield client
    finally:
        close_requested.set()
        await wait_for_finish()
        if lifecycle_error is not None and not reported_startup_error:
            raise lifecycle_error
        await task
