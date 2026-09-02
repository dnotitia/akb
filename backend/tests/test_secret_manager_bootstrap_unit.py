"""Focused contracts for the chart-native Secret Manager bootstrap command."""

from __future__ import annotations

import base64
import time

import pytest

from app.secret_manager_bootstrap import (
    BootstrapError,
    Settings,
    _extract_recovery,
    _init_payload,
    _validate_receipt,
    _wait_for_active,
    _write_recovery_secret,
    normalize_pgp_public_key,
)


def _settings(**overrides) -> Settings:
    values = {
        "namespace": "akb-test",
        "engine": "openbao",
        "seal_mode": "plaintext",
        "auth_profile": "local",
        "replicas": 3,
        "key_shares": 5,
        "key_threshold": 3,
        "kv_mount": "kv",
        "kv_path": "akb/runtime",
        "auth_mount": "kubernetes",
        "runtime_role": "akb-runtime-reader",
        "operator_role": "akb-operator-admin",
        "operator_service_account": "akb-secret-admin",
        "recovery_secret": "akb-secret-manager-recovery",  # pragma: allowlist secret
        "input_secret": "akb-secret-manager-bootstrap-input",  # pragma: allowlist secret
        "receipt": "akb-secret-manager-bootstrap",
        "secret_contract": "akb-secret",  # pragma: allowlist secret
        "secret_store_base": "https://store-{index}.internal:8200",  # pragma: allowlist secret
        "ca_path": "/tmp/ca.crt",
        "wait_seconds": 60,
        "pgp_unseal_keys": (),
        "pgp_recovery_keys": (),
        "pgp_root_key": "",
    }
    values.update(overrides)
    return Settings(**values)


def test_plaintext_init_uses_native_shamir_fields_and_extracts_only_native_shares():
    settings = _settings()
    assert _init_payload(settings) == {"secret_shares": 5, "secret_threshold": 3}
    recovery, root_token = _extract_recovery(
        settings,
        {"keys_base64": [f"share-{index}" for index in range(5)], "root_token": "root"},
    )
    assert root_token == "root"
    assert recovery == {
        "format": "vault-compatible-native-init-v1",
        "engine": "openbao",
        "seal_mode": "plaintext",
        "share_type": "unseal_keys_b64",
        "shares": [f"share-{index}" for index in range(5)],
        "shares_encrypted": False,
        "key_shares": 5,
        "key_threshold": 3,
        "root_token_encrypted": False,
    }


def test_auto_seal_uses_recovery_fields_and_preserves_encryption_metadata():
    settings = _settings(
        seal_mode="auto",
        pgp_recovery_keys=("key-a", "key-b", "key-c", "key-d", "key-e"),
        pgp_root_key="root-key",
    )
    assert _init_payload(settings) == {
        "recovery_shares": 5,
        "recovery_threshold": 3,
        "recovery_pgp_keys": ["key-a", "key-b", "key-c", "key-d", "key-e"],
        "root_token_pgp_key": "root-key",
    }
    recovery, bootstrap_token = _extract_recovery(
        settings,
        {
            "recovery_keys_base64": [f"share-{index}" for index in range(5)],
            "root_token": "encrypted-root",
        },
    )
    assert recovery["shares_encrypted"] is True
    assert recovery["root_token_encrypted"] is True
    assert recovery["encrypted_initial_root_token"] == "encrypted-root"
    assert bootstrap_token == ""


def test_ascii_armoured_pgp_key_is_normalized_to_packet_base64():
    packet = base64.b64encode(b"public-key-packet").decode("ascii")
    armoured = "\n".join(
        [
            "-----BEGIN PGP PUBLIC KEY BLOCK-----",
            "Version: test",
            "",
            packet,
            "=abcd",
            "-----END PGP PUBLIC KEY BLOCK-----",
        ]
    )
    assert normalize_pgp_public_key(armoured) == packet


def test_receipt_accepts_completed_v1_but_rejects_profile_reinterpretation():
    settings = _settings()
    data = {
        **settings.receipt_contract(),
        "contract": "akb-secret-manager-bootstrap-v1",
        "root-token-revoked": "true",
    }
    assert _validate_receipt(settings, {"data": data}) == "complete"
    data["auth-profile"] = "sso"
    with pytest.raises(BootstrapError, match="auth-profile"):
        _validate_receipt(settings, {"data": data})


def test_current_receipt_rejects_cluster_or_immutable_bootstrap_changes():
    settings = _settings()
    data = {
        **settings.receipt_contract(),
        "cluster-id": "cluster-a",
        "status": "complete",
        "root-token-revoked": "true",
    }
    with pytest.raises(BootstrapError, match="different Secret Manager cluster"):
        _validate_receipt(settings, {"data": data}, "cluster-b")

    changed = dict(data)
    changed["key-threshold"] = "4"
    with pytest.raises(BootstrapError, match="key-threshold"):
        _validate_receipt(settings, {"data": changed}, "cluster-a")


def test_recovery_handoff_retries_until_read_after_write_succeeds(monkeypatch):
    class Kube:
        def __init__(self):
            self.calls = 0
            self.stored = None

        def upsert(self, _plural, _name, resource):
            self.calls += 1
            if self.calls == 1:
                raise BootstrapError("transient Kubernetes write failure")
            self.stored = {key: base64.b64decode(value).decode("utf-8") for key, value in resource["data"].items()}

        def decoded_secret(self, _name):
            return self.stored

    monkeypatch.setattr("app.secret_manager_bootstrap.time.sleep", lambda _seconds: None)
    kube = Kube()
    settings = _settings()
    recovery = {"shares": ["one", "two", "three", "four", "five"]}
    _write_recovery_secret(kube, settings, recovery, "root", time.monotonic() + 1)
    assert kube.calls == 2
    assert kube.stored["bootstrap-root-token"] == "root"  # pragma: allowlist secret


def test_wait_for_active_skips_transient_standby_member():
    class Response:
        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class Client:
        def __init__(self, payload):
            self.payload = payload

        def request(self, *_args, **_kwargs):
            return Response(self.payload)

    standby = Client({"ha_enabled": True, "is_self": False})
    active = Client({"ha_enabled": True, "is_self": True})
    selected, status = _wait_for_active([standby, active], time.monotonic() + 1)
    assert selected is active
    assert status["is_self"] is True
