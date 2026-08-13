"""Authenticated encryption boundary for server-custodied SSO sessions."""

from __future__ import annotations

import base64

import pytest

from app.services.sso_browser_session_crypto import (
    BrowserSessionCipher,
    BrowserSessionKeyError,
    BrowserSessionPayloadError,
)


def _key(byte: int) -> str:
    return base64.urlsafe_b64encode(bytes([byte]) * 32).decode("ascii").rstrip("=")


def test_v1_cipher_round_trips_without_putting_plaintext_in_the_envelope():
    cipher = BrowserSessionCipher.from_encoded_key(_key(7))
    payload = {
        "refresh_token": "keycloak-refresh-secret",
        "id_token": "keycloak-id-token",
        "scope": "openid profile email",
    }

    envelope = cipher.seal(payload, context="session:session-1:user-1")

    assert envelope.startswith("v1.")
    assert "keycloak-refresh-secret" not in envelope
    assert cipher.open(envelope, context="session:session-1:user-1") == payload


@pytest.mark.parametrize(
    "encoded",
    [
        "",
        "not-base64!",
        base64.urlsafe_b64encode(b"short").decode("ascii"),
        base64.urlsafe_b64encode(b"x" * 31).decode("ascii"),
        base64.urlsafe_b64encode(b"x" * 33).decode("ascii"),
    ],
)
def test_cipher_rejects_every_key_that_is_not_exactly_256_bits(encoded):
    with pytest.raises(BrowserSessionKeyError) as captured:
        BrowserSessionCipher.from_encoded_key(encoded)

    if encoded:
        assert encoded not in str(captured.value)


def test_cipher_binds_ciphertext_to_its_exact_session_context():
    cipher = BrowserSessionCipher.from_encoded_key(_key(9))
    envelope = cipher.seal(
        {
            "refresh_token": "refresh-1",
            "id_token": "id-token-1",
            "scope": "openid",
        },
        context="session:one:user-1",
    )

    with pytest.raises(BrowserSessionPayloadError):
        cipher.open(envelope, context="session:two:user-1")


def test_cipher_rejects_wrong_key_tampering_and_noncanonical_payloads_without_echo():
    first = BrowserSessionCipher.from_encoded_key(_key(11))
    second = BrowserSessionCipher.from_encoded_key(_key(12))
    envelope = first.seal(
        {
            "refresh_token": "never-echo-this",
            "id_token": "never-echo-id-token",
            "scope": "openid",
        },
        context="session:one:user-1",
    )
    tampered = f"{envelope[:-1]}{'A' if envelope[-1] != 'A' else 'B'}"

    for candidate, cipher in ((envelope, second), (tampered, first), ("v2.abc", first)):
        with pytest.raises(BrowserSessionPayloadError) as captured:
            cipher.open(candidate, context="session:one:user-1")
        assert "never-echo-this" not in str(captured.value)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"refresh_token": ""},
        {"refresh_token": 42},
        {"refresh_token": "refresh", "id_token": "id", "scope": []},
        {"refresh_token": "refresh", "id_token": "", "scope": "openid"},
        {
            "refresh_token": "refresh",
            "id_token": "id",
            "scope": "openid",
            "extra": True,
        },
    ],
)
def test_cipher_accepts_only_the_bounded_v1_refresh_payload(payload):
    cipher = BrowserSessionCipher.from_encoded_key(_key(13))

    with pytest.raises(BrowserSessionPayloadError):
        cipher.seal(payload, context="session:one:user-1")
