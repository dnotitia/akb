"""Input boundaries for the disposable real native OAuth fixture."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "native_oauth_fixture", _ROOT / "deploy/keycloak-dev/native-oauth/fixture.py",
)
assert _SPEC is not None and _SPEC.loader is not None
_FIXTURE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_FIXTURE)


@pytest.mark.parametrize("resource", [
    "http://127.0.0.1:8080/mcp",
    "http://127.0.0.1:8080/prefix/mcp",
])
def test_native_fixture_accepts_exact_loopback_resources(resource):
    assert _FIXTURE.validate_resource(resource) == resource


@pytest.mark.parametrize("resource", [
    "https://production.example.com/mcp",
    "http://127.0.0.1/mcp",
    "http://127.0.0.1:8080/mcp/",
    "http://127.0.0.1:8080/api",
    "http://127.0.0.1:8080/mcp?token=unexpected",
    "http://127.0.0.1:8080/mcp#fragment",
    "http://user@127.0.0.1:8080/mcp",
    "http://@127.0.0.1:8080/mcp",
])
def test_native_fixture_refuses_other_targets_before_creating_resources(resource):
    with pytest.raises(ValueError):
        _FIXTURE.validate_resource(resource)


def test_native_fixture_refuses_runtime_state_inside_checkout(tmp_path):
    checkout = tmp_path / "checkout"
    with pytest.raises(ValueError, match="outside the checkout"):
        _FIXTURE.NativeOAuthFixture(
            checkout=checkout, runtime_root=checkout / "runtime", resource="http://127.0.0.1:8080/mcp",
        )
    assert not checkout.exists()


def test_native_fixture_requires_injected_credentials_before_creating_resources(tmp_path, monkeypatch):
    monkeypatch.delenv("AKB_E2E_USERNAME", raising=False)
    monkeypatch.delenv("AKB_E2E_PASSWORD", raising=False)
    runtime_root = tmp_path / "runtime"
    with pytest.raises(ValueError, match="AKB_E2E_USERNAME and AKB_E2E_PASSWORD"):
        _FIXTURE.NativeOAuthFixture(
            checkout=tmp_path / "checkout", runtime_root=runtime_root, resource="http://127.0.0.1:8080/mcp",
        )
    assert not runtime_root.exists()
