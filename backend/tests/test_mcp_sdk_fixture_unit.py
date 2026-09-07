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

import pytest

import tests.mcp_e2e.conftest as mcp_conftest
from tests.mcp_e2e.conftest import mcp_client as mcp_client_fixture
from tests.mcp_e2e.runtime import RuntimeContext, RuntimeDescriptor


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


def test_runtime_descriptor_stdin_is_reused_across_function_scoped_sessions(
    tmp_path: Path,
) -> None:
    probe = tmp_path / "test_descriptor_reuse.py"
    probe.write_text(
        "import pytest\n"
        "import tests.mcp_e2e.conftest as mcp_conftest\n"
        "from tests.mcp_e2e.runtime import RuntimeContext\n"
        "\n"
        "pytest_plugins = ('tests.mcp_e2e.conftest',)\n"
        "\n"
        "def _fake_prepare(descriptor, client):\n"
        "    return RuntimeContext(descriptor=descriptor, pat='akb_test_pat', secrets=('fixture-user', 'fixture-pass', 'akb_test_pat'))\n"
        "\n"
        "@pytest.fixture(autouse=True)\n"
        "def patch_prepare(monkeypatch):\n"
        "    monkeypatch.setattr(mcp_conftest, '_prepare_runtime', _fake_prepare)\n"
        "\n"
        "def test_first(runtime_session):\n"
        "    assert runtime_session.descriptor.scenario == 'empty'\n"
        "\n"
        "def test_second(runtime_session):\n"
        "    assert runtime_session.descriptor.scenario == 'empty'\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    tests_root = Path(__file__).resolve().parent
    env["PYTHONPATH"] = f"{tests_root}{os.pathsep}{env.get('PYTHONPATH', '')}"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "tests.mcp_e2e.conftest",
            str(probe),
            "--runtime-descriptor",
            "-",
            "-q",
            "--tb=short",
        ],
        cwd=tmp_path,
        env=env,
        input=json.dumps(_descriptor()),
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout


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


def test_secondary_user_preparation_uses_descriptor_auth_coordinates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
            self.status_code = status_code
            self._payload = payload

        def json(self) -> dict[str, Any]:
            return self._payload

    class RecordingClient:
        requests: list[tuple[str, str, dict[str, Any] | None, dict[str, str] | None]] = []

        def __init__(self, **_kwargs: Any) -> None:
            return None

        def __enter__(self) -> RecordingClient:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def post(self, url: str, *, json: dict[str, Any]) -> Response:
            self.requests.append(("POST", url, json, None))
            return Response(201, {"user_id": "secondary"})

        def request(
            self,
            method: str,
            url: str,
            *,
            json: dict[str, Any] | None = None,
            headers: dict[str, str] | None = None,
        ) -> Response:
            self.requests.append((method, url, json, headers))
            if url.endswith("/login"):
                return Response(200, {"token": "jwt-secondary"})
            return Response(200, {"token": "akb_secondary_pat"})

    RecordingClient.requests = []
    monkeypatch.setattr(mcp_conftest.httpx, "Client", RecordingClient)

    username, pat, secret_values = mcp_conftest._prepare_secondary_user(_runtime_context())

    assert username.startswith("mcp-pytest-u2-")
    assert pat == "akb_secondary_pat"
    assert username in secret_values
    assert "jwt-secondary" in secret_values
    assert any(url.endswith("/api/v1/auth/register") for _, url, _, _ in RecordingClient.requests)
    assert any(url.endswith("/api/v1/auth/login") for _, url, _, _ in RecordingClient.requests)
    assert any(url.endswith("/api/v1/auth/tokens") for _, url, _, _ in RecordingClient.requests)
