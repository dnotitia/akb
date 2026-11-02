"""Seal an operation credential to the public key its requester generated.

HPKE (RFC 9180) base mode, single shot, with the suite
DHKEM(X25519, HKDF-SHA256) / HKDF-SHA256 / ChaCha20-Poly1305, as implemented by
the ``cryptography`` package this backend already depends on. Nothing here is
a new construction: the sealed value is exactly ``Suite.encrypt``'s output, the
32-byte encapsulated key followed by the AEAD ciphertext and tag.

The recipient key is a raw 32-byte X25519 public key in unpadded base64url. The
HPKE ``info`` names this purpose, the request and its digest, so a sealed value
cannot be presented as the credential of a different request.
"""

from __future__ import annotations

import base64
import binascii

from cryptography.hazmat.primitives import hpke
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey

SUITE = hpke.Suite(hpke.KEM.X25519, hpke.KDF.HKDF_SHA256, hpke.AEAD.CHACHA20_POLY1305)
SUITE_NAME = "DHKEM(X25519, HKDF-SHA256)/HKDF-SHA256/ChaCha20-Poly1305"
INFO_PREFIX = "akb.sso.operation-request.credential.v1"
_KEY_BYTES = 32


class SealingKeyError(ValueError):
    """The recipient public key is not a usable X25519 key."""


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _from_b64url(value: str) -> bytes:
    if not isinstance(value, str) or not value.isascii() or "=" in value:
        raise SealingKeyError
    try:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        raise SealingKeyError from None
    # Exactly one spelling per key: a digest over the request then names one key.
    if _b64url(decoded) != value:
        raise SealingKeyError
    return decoded


def info(request_id: str, request_sha256: str) -> bytes:
    return f"{INFO_PREFIX}:{request_id}:{request_sha256}".encode("ascii")


def public_key(value: str) -> X25519PublicKey:
    raw = _from_b64url(value)
    if len(raw) != _KEY_BYTES:
        raise SealingKeyError
    key = X25519PublicKey.from_public_bytes(raw)
    try:
        # A low-order point yields an all-zero shared secret; HPKE refuses it.
        SUITE.encrypt(b"", key, info=INFO_PREFIX.encode("ascii"))
    except ValueError:
        raise SealingKeyError from None
    return key


def seal(plaintext: str, recipient: str, *, request_id: str, request_sha256: str) -> str:
    return _b64url(SUITE.encrypt(plaintext.encode("ascii"), public_key(recipient),
                                 info=info(request_id, request_sha256)))
