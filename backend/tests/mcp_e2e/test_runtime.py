"""Focused regression tests for descriptor and fixture boundaries."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from .runtime import RuntimeSession, RuntimeSetupError


def _descriptor() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "status": "ready",
        "scenario": "empty",
        "services": {
            "app": {
                "origin": "http://127.0.0.1:8000",
                "health": {"method": "GET", "url": "http://127.0.0.1:8000/readyz"},
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
            "username_env": "AKB_TEST_USERNAME",
            "password_env": "AKB_TEST_PASSWORD",  # pragma: allowlist secret
            "pat_env": "AKB_TEST_PAT",
            "login_path": "/api/v1/auth/login",
        },
    }


class _FixtureClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None, dict[str, str] | None]] = []
        self.closed = False

    def request(
        self, method: str, url: str, *, json: dict[str, Any] | None = None, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        self.calls.append((method, url, json, headers))
        if url.endswith("/readyz") or url.endswith("/health"):
            body: dict[str, Any] = {"status": "ready"}
        elif url.endswith("/discover"):
            body = {
                "status": "ready",
                "scenario": "empty",
                "access": {"login": {"path": "/api/v1/auth/login"}},
                "runtime": {"pat": {"mint": {"path": "/api/v1/auth/tokens"}}},
            }
        elif url.endswith("/reset"):
            body = {"status": "ready", "scenario": "empty"}
        elif url.endswith("/auth/login"):
            body = {"token": "session-secret"}
        elif url.endswith("/auth/tokens"):
            body = {"token": "akb_pat-secret"}
        else:
            body = {}
        return httpx.Response(200, json=body, request=httpx.Request(method, url))

    def close(self) -> None:
        self.closed = True


def test_runtime_session_reuses_descriptor_reset_and_redacts_pat_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AKB_TEST_USERNAME", "fixture-user")
    monkeypatch.setenv("AKB_TEST_PASSWORD", "fixture-password")
    monkeypatch.setenv("AKB_TEST_PAT", "stale-pat")
    client = _FixtureClient()
    session = RuntimeSession.from_json(json.dumps(_descriptor()), client=client)  # type: ignore[arg-type]

    session.prepare()

    assert session.pat == "akb_pat-secret"
    assert session.credential_env_names == ("AKB_TEST_USERNAME", "AKB_TEST_PASSWORD", "AKB_TEST_PAT")
    reset = next(call for call in client.calls if call[1].endswith("/reset"))
    assert reset[0] == "POST" and reset[2] == {"scenario": "empty"}
    mint = next(call for call in client.calls if call[1].endswith("/auth/tokens"))
    assert mint[3] == {"Authorization": "Bearer session-secret"}
    session.close()
    assert client.closed


@pytest.mark.parametrize(
    "change",
    [
        lambda value: value["services"]["fixture"]["reset"].update({"method": "GET"}),
        lambda value: value["services"]["fixture"]["reset"].update({"body": {"scenario": "other"}}),
        lambda value: value["services"]["fixture"]["reset"].update({"url": "http://evil.example/reset"}),
    ],
)
def test_runtime_descriptor_rejects_incompatible_fixture_contract(change: Any) -> None:
    value = _descriptor()
    change(value)
    with pytest.raises(RuntimeSetupError):
        RuntimeSession.from_json(json.dumps(value))
