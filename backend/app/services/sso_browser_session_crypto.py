"""Versioned authenticated encryption for Keycloak refresh-token custody.

The browser session table persists only an opaque handle hash plus this
AES-256-GCM envelope.  Its associated data binds ciphertext to the exact AKB
session and user, so copying a row's envelope to another row cannot produce a
usable refresh credential.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
from collections.abc import Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_VERSION = "v1"
_AAD_PREFIX = b"akb-sso-browser-session:v1:"
_NONCE_BYTES = 12
_KEY_BYTES = 32
_MAX_REFRESH_TOKEN_LENGTH = 16_384
_MAX_ID_TOKEN_LENGTH = 16_384
_MAX_SCOPE_LENGTH = 2_048
_MAX_CONTEXT_LENGTH = 256
_PROVIDER_ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")


class BrowserSessionKeyError(ValueError):
    """The configured server-side encryption key is unavailable or invalid."""


class BrowserSessionPayloadError(ValueError):
    """A browser-session payload or encrypted envelope is invalid."""


def _decode_base64url(value: str, *, error: type[ValueError]) -> bytes:
    if not isinstance(value, str) or not value or not value.isascii():
        raise error("Invalid browser-session cryptographic material")
    try:
        return base64.b64decode(
            value + "=" * ((4 - len(value) % 4) % 4),
            altchars=b"-_",
            validate=True,
        )
    except ValueError, binascii.Error:
        raise error("Invalid browser-session cryptographic material") from None


def _encode_base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _context_bytes(context: object) -> bytes:
    if (
        not isinstance(context, str)
        or not 1 <= len(context) <= _MAX_CONTEXT_LENGTH
        or not context.isascii()
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in context)
    ):
        raise BrowserSessionPayloadError("Invalid browser-session encryption context")
    return _AAD_PREFIX + context.encode("ascii")


def _payload_bytes(payload: object) -> bytes:
    if not isinstance(payload, Mapping) or set(payload) != {
        "refresh_token",
        "id_token",
        "scope",
        "provider_alias",
    }:
        raise BrowserSessionPayloadError("Invalid browser-session payload")
    refresh_token = payload.get("refresh_token")
    id_token = payload.get("id_token")
    scope = payload.get("scope")
    provider_alias = payload.get("provider_alias")
    if (
        not isinstance(refresh_token, str)
        or not 1 <= len(refresh_token) <= _MAX_REFRESH_TOKEN_LENGTH
        or not isinstance(id_token, str)
        or not 1 <= len(id_token) <= _MAX_ID_TOKEN_LENGTH
        or not isinstance(scope, str)
        or not 1 <= len(scope) <= _MAX_SCOPE_LENGTH
        or not isinstance(provider_alias, str)
        or _PROVIDER_ALIAS_RE.fullmatch(provider_alias) is None
    ):
        raise BrowserSessionPayloadError("Invalid browser-session payload")
    return json.dumps(
        {
            "refresh_token": refresh_token,
            "id_token": id_token,
            "scope": scope,
            "provider_alias": provider_alias,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class BrowserSessionCipher:
    """One exact AES-256-GCM envelope profile."""

    def __init__(self, key: bytes) -> None:
        if len(key) != _KEY_BYTES:
            raise BrowserSessionKeyError("Browser-session encryption key must be 256 bits")
        self._cipher = AESGCM(key)

    @classmethod
    def from_encoded_key(cls, encoded_key: str) -> BrowserSessionCipher:
        key = _decode_base64url(encoded_key, error=BrowserSessionKeyError)
        if len(key) != _KEY_BYTES:
            raise BrowserSessionKeyError("Browser-session encryption key must be 256 bits")
        return cls(key)

    def seal(self, payload: object, *, context: object) -> str:
        plaintext = _payload_bytes(payload)
        aad = _context_bytes(context)
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = self._cipher.encrypt(nonce, plaintext, aad)
        return f"{_VERSION}.{_encode_base64url(nonce)}.{_encode_base64url(ciphertext)}"

    def open(self, envelope: object, *, context: object) -> dict[str, str]:
        aad = _context_bytes(context)
        if not isinstance(envelope, str):
            raise BrowserSessionPayloadError("Invalid browser-session envelope")
        parts = envelope.split(".")
        if len(parts) != 3 or parts[0] != _VERSION:
            raise BrowserSessionPayloadError("Invalid browser-session envelope")
        nonce = _decode_base64url(parts[1], error=BrowserSessionPayloadError)
        ciphertext = _decode_base64url(parts[2], error=BrowserSessionPayloadError)
        if len(nonce) != _NONCE_BYTES or len(ciphertext) < 17:
            raise BrowserSessionPayloadError("Invalid browser-session envelope")
        try:
            plaintext = self._cipher.decrypt(nonce, ciphertext, aad)
            decoded = json.loads(plaintext)
        except InvalidTag, UnicodeDecodeError, json.JSONDecodeError:
            raise BrowserSessionPayloadError("Invalid browser-session envelope") from None
        canonical = _payload_bytes(decoded)
        if canonical != plaintext:
            raise BrowserSessionPayloadError("Invalid browser-session payload")
        return dict(decoded)
