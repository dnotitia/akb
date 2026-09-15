"""Fixture cleanup must fail visibly, with no legacy or cascade fallback."""
import importlib.util
import io
import json
from pathlib import Path

import pytest


@pytest.fixture
def helper():
    path = Path(__file__).parent / "helpers" / "cleanup_hybrid_account.py"
    spec = importlib.util.spec_from_file_location("cleanup_hybrid_account", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def responses(helper, monkeypatch, payloads):
    requests = []
    pending = iter(payloads)

    def open_request(request, **kwargs):
        requests.append(request)
        return io.BytesIO(json.dumps(next(pending)).encode())

    monkeypatch.setattr(helper, "urlopen", open_request)
    return requests


def test_unsupported_cleanup_never_deletes_vault_or_account(helper, monkeypatch):
    requests = responses(helper, monkeypatch, [
        {"token": "fixture-token", "user": {"id": "fixture-id"}},
        {"user_id": "fixture-id", "username": "hybrid-test-123", "deletion": {"supported": False}},
    ])
    with pytest.raises(RuntimeError, match="account cleanup unsupported"):
        helper.cleanup("http://localhost", "hybrid-test-123", "fixture-password")
    assert [r.get_method() for r in requests] == ["POST", "GET"]


def test_identity_mismatch_never_deletes(helper, monkeypatch):
    requests = responses(helper, monkeypatch, [
        {"token": "fixture-token", "user": {"id": "fixture-id"}},
        {"user_id": "other-id", "username": "hybrid-test-123"},
    ])
    with pytest.raises(RuntimeError, match="identity mismatch"):
        helper.cleanup("http://localhost", "hybrid-test-123", "fixture-password")
    assert len(requests) == 2


def test_owned_vaults_are_deleted_before_identity_bound_account(helper, monkeypatch):
    requests = responses(helper, monkeypatch, [
        {"token": "fixture-token", "user": {"id": "fixture-id"}},
        {"user_id": "fixture-id", "username": "hybrid-test-123", "deletion": {"supported": True}},
        {"user_id": "fixture-id", "owned_vaults": [{"name": "test-vault"}]},
        {}, {}, {"user_id": "fixture-id", "owned_vaults": []},
        {"deleted": True, "user_id": "fixture-id"},
    ])
    helper.cleanup("http://localhost", "hybrid-test-123", "fixture-password")
    assert [(r.get_method(), r.selector) for r in requests[3:]] == [
        ("POST", "/api/v1/vaults/test-vault/archive"),
        ("DELETE", "/api/v1/vaults/test-vault"),
        ("GET", "/api/v1/my/account/deletion-blockers?limit=100"),
        ("POST", "/api/v1/my/account/deletion"),
    ]
    assert json.loads(requests[-1].data) == {
        "expected_user_id": "fixture-id", "confirm_username": "hybrid-test-123",
        "current_password": "fixture-password",  # pragma: allowlist secret
    }


def test_cleanup_refuses_non_fixture_account(helper):
    with pytest.raises(ValueError, match="fixture username"):
        helper.cleanup("http://localhost", "real-account", "unused")
