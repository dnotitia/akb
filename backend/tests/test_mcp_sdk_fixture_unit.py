"""Focused unit checks for the MCP SDK fixture boundary."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import subprocess
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp import types as mcp_types

import tests.mcp_e2e.conftest as mcp_conftest
from tests.mcp_e2e.conftest import _prepare_runtime, mcp_client as mcp_client_fixture
from tests.mcp_e2e.runtime import RuntimeContext, RuntimeDescriptor, RuntimeSetupError
from tests.mcp_e2e.test_list_vaults_e2e import (
    _assert_connection,
    _assert_list_vaults_shape,
    _assert_tool_catalog,
    _list_vaults_payload,
)


def _descriptor() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "status": "ready",
        "scenario": "empty",
        "services": {
            "app": {
                "origin": "http://127.0.0.1:8000",
                "health": {"method": "GET", "url": "http://127.0.0.1:8000/readyz"},
                "discovery": {"method": "GET", "url": "http://127.0.0.1:8000/openapi.json"},
            },
            "fixture": {
                "origin": "http://127.0.0.1:8889",
                "health": {"method": "GET", "url": "http://127.0.0.1:8889/health"},
                "reset": {
                    "method": "POST",
                    "url": "http://127.0.0.1:8889/reset",
                    "body": {"scenario": "empty"},
                },
                "discovery": {"method": "GET", "url": "http://127.0.0.1:8889/discover"},
            },
        },
        "credentials": {
            "username_env": "AKB_TEST_USER_ENV",
            "password_env": "AKB_TEST_PASS_ENV",  # pragma: allowlist secret
            "login_path": "/api/v1/auth/login",
        },
    }


def _discovery() -> dict[str, Any]:
    return {
        "status": "ready",
        "scenario": "empty",
        "access": {
            "login": {
                "service": "app",
                "method": "POST",
                "path": "/api/v1/auth/login",
            }
        },
        "runtime": {
            "origin": {"backend": "http://127.0.0.1:8000", "fixture": "http://127.0.0.1:8889"},
            "transport": ["http"],
            "tool_cases": {"read": "akb_list_vaults"},
            "credential_env": {
                "username": "AKB_TEST_USER_ENV",
                "password": "AKB_TEST_PASS_ENV",  # pragma: allowlist secret
            },
            "pat": {
                "mint": {
                    "service": "app",
                    "method": "POST",
                    "path": "/api/v1/auth/tokens",
                }
            },
        },
    }


class _FixtureClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None, dict[str, str] | None]] = []
        self.closed = False

    def request(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        self.calls.append((method, url, json, headers))
        if url.endswith("/readyz") or url.endswith("/health"):
            body: dict[str, Any] = {"status": "ready"}
        elif url.endswith("/discover"):
            body = _discovery()
        elif url.endswith("/reset"):
            body = {"status": "ready", "scenario": "empty"}
        elif url.endswith("/auth/login"):
            body = {"token": "session-value"}
        elif url.endswith("/auth/tokens"):
            body = {"token": "akb_test_pat"}
        else:
            body = {}
        return httpx.Response(200, json=body, request=httpx.Request(method, url))

    def close(self) -> None:
        self.closed = True


def test_runtime_descriptor_accepts_schema_v2_and_derives_mcp_endpoint() -> None:
    descriptor = RuntimeDescriptor.from_json(json.dumps(_descriptor()))

    assert descriptor.scenario == "empty"
    assert descriptor.mcp_url == "http://127.0.0.1:8000/mcp/"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update({"schema_version": 1}),
        lambda value: value["services"]["fixture"]["reset"].update({"method": "GET"}),
        lambda value: value["services"]["fixture"]["reset"].update(
            {"url": "http://evil.example/reset"}
        ),
        lambda value: value["credentials"].update({"username_env": "not-valid"}),
    ],
)
def test_runtime_descriptor_rejects_incompatible_contract(mutate: Any) -> None:
    value = copy.deepcopy(_descriptor())
    mutate(value)

    with pytest.raises(RuntimeSetupError):
        RuntimeDescriptor.from_json(json.dumps(value))


def test_runtime_setup_reuses_reset_discovery_and_mints_in_memory_pat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AKB_TEST_USER_ENV", "fixture-user")
    monkeypatch.setenv("AKB_TEST_PASS_ENV", "fixture-pass")
    client = _FixtureClient()
    descriptor = RuntimeDescriptor.from_json(json.dumps(_descriptor()))
    context = _prepare_runtime(descriptor, client)  # type: ignore[arg-type]

    assert context.pat == "akb_test_pat"
    assert context.descriptor.username_env == "AKB_TEST_USER_ENV"
    assert context.descriptor.password_env == "AKB_TEST_PASS_ENV"  # pragma: allowlist secret
    reset = next(call for call in client.calls if call[1].endswith("/reset"))
    assert reset[0] == "POST" and reset[2] == {"scenario": "empty"}
    mint = next(call for call in client.calls if call[1].endswith("/auth/tokens"))
    assert mint[3] == {"Authorization": "Bearer session-value"}

    client.close()
    assert client.closed


def test_descriptor_stdin_read_is_compatible_with_pytest_capture(tmp_path: Path) -> None:
    probe = tmp_path / "test_descriptor_pipe.py"
    probe.write_text(
        "import json\n"
        "from mcp_e2e.conftest import _read_descriptor\n"
        "\n"
        "def test_pipe(request):\n"
        "    manager = request.config.pluginmanager.getplugin('capturemanager')\n"
        "    assert json.loads(_read_descriptor('-', manager)) == {'ok': True}\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    tests_root = Path(__file__).resolve().parent
    env["PYTHONPATH"] = f"{tests_root}{os.pathsep}{env.get('PYTHONPATH', '')}"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(probe), "-q"],
        cwd=tmp_path,
        env=env,
        input='{"ok":true}\n',
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def _call_result(text: str, *, is_error: bool = False) -> mcp_types.CallToolResult:
    return mcp_types.CallToolResult(
        content=[mcp_types.TextContent(text=text)],
        is_error=is_error,
    )


def _assertion_failure(callback: Any) -> str:
    with pytest.raises(pytest.fail.Exception) as captured:
        callback()
    return str(captured.value)


def test_typed_sdk_response_assertions_accept_the_public_list_vaults_contract() -> None:
    tools = mcp_types.ListToolsResult(
        tools=[mcp_types.Tool(name="akb_list_vaults", input_schema={"type": "object"})]
    )
    _assert_tool_catalog(tools)
    public = _list_vaults_payload(
        _call_result(json.dumps({"vaults": [], "total": 0, "returned": 0}))
    )
    _assert_list_vaults_shape(public)


@pytest.mark.parametrize(
    ("text", "is_error", "detail"),
    [
        ("{}", True, "tool returned an error"),
        ("not-json", False, "tool returned invalid public JSON"),
        ("[]", False, "public result is not an object"),
        (json.dumps({"vaults": {}, "total": 0, "returned": 0}), False, "vaults is not an array"),
        (json.dumps({"vaults": [], "total": True, "returned": 0}), False, "total and returned must be integers"),
        (json.dumps({"vaults": [], "total": 0, "returned": False}), False, "total and returned must be integers"),
        (json.dumps({"vaults": [], "total": 1, "returned": 1}), False, "returned does not match vaults length"),
        (json.dumps({"vaults": [{}], "total": 0, "returned": 1}), False, "total is smaller than returned"),
    ],
)
def test_list_vaults_response_failures_are_pytest_failures(
    text: str,
    is_error: bool,
    detail: str,
) -> None:
    result = _call_result(text, is_error=is_error)
    message = _assertion_failure(
        lambda: _assert_list_vaults_shape(_list_vaults_payload(result))
    )

    assert "scenario=akb_list_vaults operation=tools/call akb_list_vaults" in message
    assert detail in message
    assert "fixture-pass" not in message


def test_tool_catalog_and_connection_failures_identify_their_operations() -> None:
    catalog_message = _assertion_failure(
        lambda: _assert_tool_catalog(mcp_types.ListToolsResult(tools=[]))
    )
    protocol_message = _assertion_failure(
        lambda: _assert_connection(
            "unsupported", mcp_types.Implementation(name="akb", version="")
        )
    )
    server_message = _assertion_failure(
        lambda: _assert_connection(
            "2025-06-18", mcp_types.Implementation(name="other", version="")
        )
    )

    assert "operation=tools/list" in catalog_message
    assert "operation=connect" in protocol_message
    assert "operation=connect" in server_message


def test_preparation_failure_is_an_actual_pytest_failure(tmp_path: Path) -> None:
    descriptor = copy.deepcopy(_descriptor())
    descriptor["services"]["app"]["origin"] = "http://127.0.0.1:1"
    descriptor["services"]["app"]["health"]["url"] = "http://127.0.0.1:1/readyz"
    descriptor["services"]["app"]["discovery"]["url"] = "http://127.0.0.1:1/openapi.json"
    descriptor_path = tmp_path / "descriptor.json"
    descriptor_path.write_text(json.dumps(descriptor), encoding="utf-8")
    env = os.environ.copy()
    env["AKB_TEST_USER_ENV"] = "fixture-user"
    env["AKB_TEST_PASS_ENV"] = "fixture-pass"
    repo_root = Path(__file__).resolve().parents[2]
    mcp_root = repo_root / "backend" / "tests" / "mcp_e2e"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(mcp_root / "test_list_vaults_e2e.py"),
            "--confcutdir",
            str(mcp_root),
            "--runtime-descriptor",
            str(descriptor_path),
            "-q",
            "--tb=short",
        ],
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    output = result.stdout + result.stderr

    assert result.returncode != 0
    assert "scenario=akb_list_vaults preparation:" in output
    assert "fixture-pass" not in output


def _runtime_context() -> RuntimeContext:
    return RuntimeContext(
        descriptor=RuntimeDescriptor.from_json(json.dumps(_descriptor())),
        pat="akb_test_pat",
        secrets=("fixture-user", "fixture-pass", "akb_test_pat"),
    )


class _RecordingHttpClient:
    def __init__(self, **_kwargs: Any) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _RecordingSdkClient:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.entered_task: asyncio.Task[Any] | None = None
        self.exited_task: asyncio.Task[Any] | None = None

    async def __aenter__(self) -> _RecordingSdkClient:
        self.entered_task = asyncio.current_task()
        return self

    async def __aexit__(self, *_args: Any) -> None:
        self.exited_task = asyncio.current_task()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["normal", "failure", "cancel"])
async def test_mcp_fixture_owns_sdk_enter_exit_in_one_task_and_reopens(
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    http_clients: list[_RecordingHttpClient] = []
    sdk_clients: list[_RecordingSdkClient] = []

    def make_http_client(**kwargs: Any) -> _RecordingHttpClient:
        client = _RecordingHttpClient(**kwargs)
        http_clients.append(client)
        return client

    def make_sdk_client(*args: Any, **kwargs: Any) -> _RecordingSdkClient:
        client = _RecordingSdkClient(*args, **kwargs)
        sdk_clients.append(client)
        return client

    monkeypatch.setattr(mcp_conftest.httpx2, "AsyncClient", make_http_client)
    monkeypatch.setattr(mcp_conftest, "streamable_http_client", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(mcp_conftest, "Client", make_sdk_client)

    async def finish(generator: Any) -> None:
        with suppress(StopAsyncIteration):
            await generator.__anext__()

    async def run_once() -> None:
        generator = mcp_client_fixture.__wrapped__(_runtime_context())
        await asyncio.create_task(generator.__anext__())
        try:
            if outcome == "failure":
                raise RuntimeError("test body failed")
            if outcome == "cancel":
                raise asyncio.CancelledError
        finally:
            await asyncio.create_task(finish(generator))

    async def reopen_once() -> None:
        generator = mcp_client_fixture.__wrapped__(_runtime_context())
        await asyncio.create_task(generator.__anext__())
        await asyncio.create_task(finish(generator))

    if outcome == "failure":
        with pytest.raises(RuntimeError, match="test body failed"):
            await run_once()
    elif outcome == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await run_once()
    else:
        await run_once()

    await reopen_once()
    assert len(sdk_clients) == 2
    assert len(http_clients) == 2
    for sdk_client in sdk_clients:
        assert sdk_client.entered_task is sdk_client.exited_task
    assert all(client.closed for client in http_clients)


@pytest.mark.asyncio
async def test_mcp_fixture_reports_connection_failure_and_redacts_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http_client = _RecordingHttpClient()

    class FailingSdkClient(_RecordingSdkClient):
        async def __aenter__(self) -> _RecordingSdkClient:
            self.entered_task = asyncio.current_task()
            raise RuntimeError("Bearer sdk-secret")

    monkeypatch.setattr(mcp_conftest.httpx2, "AsyncClient", lambda **_kwargs: http_client)
    monkeypatch.setattr(mcp_conftest, "streamable_http_client", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(mcp_conftest, "Client", FailingSdkClient)
    generator = mcp_client_fixture.__wrapped__(_runtime_context())

    with pytest.raises(pytest.fail.Exception) as captured:
        await generator.__anext__()

    assert "scenario=akb_list_vaults SDK connection" in str(captured.value)
    assert "sdk-secret" not in str(captured.value)
    assert http_client.closed
