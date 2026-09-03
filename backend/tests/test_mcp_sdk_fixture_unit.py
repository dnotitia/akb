"""Focused unit checks for the MCP SDK fixture boundary."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.mcp_e2e.conftest import _prepare_runtime
from tests.mcp_e2e.runtime import RuntimeDescriptor, RuntimeSetupError


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
