"""Persistent key material for the ``local-session-rs256-v2`` profile.

The application never invents signing material at startup.  Operators create a
keyset explicitly, persist it as secret material, and point AKB at the private
PEM plus its public JWKS.  A new keyset may retain selected old public JWKs so
ordinary RS256 rotation does not invalidate sessions before their normal TTL.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Sequence

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

LOCAL_SESSION_PROFILE = "local-session-rs256-v2"
LOCAL_SESSION_ALGORITHM = "RS256"
LOCAL_SESSION_KEY_SIZE = 3072
LOCAL_SESSION_PUBLIC_EXPONENT = 65537
LOCAL_SESSION_JOSE_TYPE = "akb-local-session+jwt"

_MAX_KEY_FILE_BYTES = 64 * 1024
_MAX_JWKS_FILE_BYTES = 256 * 1024
_MAX_VERIFICATION_KEYS = 4
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class LocalSessionKeyConfigurationError(RuntimeError):
    """Safe-to-log local signing-key configuration failure."""


@dataclass(frozen=True, slots=True)
class LocalSessionKeySet:
    active_kid: str
    private_key: rsa.RSAPrivateKey
    public_keys: Mapping[str, rsa.RSAPublicKey]
    _public_jwks: Mapping[str, object]

    @property
    def public_jwks(self) -> dict[str, object]:
        """Return a detached public-only JWKS suitable for an HTTP response."""
        keys = self._public_jwks["keys"]
        assert isinstance(keys, tuple)
        return {"keys": [dict(key) for key in keys]}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _encode_number(value: int) -> str:
    length = (value.bit_length() + 7) // 8
    return _b64url(value.to_bytes(length, "big"))


def _decode_number(value: object, *, field: str) -> int:
    if not isinstance(value, str) or not value or not _B64URL_RE.fullmatch(value):
        raise LocalSessionKeyConfigurationError(f"local session JWK {field} is invalid")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except ValueError, UnicodeError:
        raise LocalSessionKeyConfigurationError(f"local session JWK {field} is invalid") from None
    if not decoded or _b64url(decoded) != value:
        raise LocalSessionKeyConfigurationError(f"local session JWK {field} is invalid")
    return int.from_bytes(decoded, "big")


def _thumbprint(*, modulus: str, exponent: str) -> str:
    canonical = json.dumps(
        {"e": exponent, "kty": "RSA", "n": modulus},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return _b64url(hashlib.sha256(canonical).digest())


def _public_jwk(public_key: rsa.RSAPublicKey) -> dict[str, str]:
    numbers = public_key.public_numbers()
    modulus = _encode_number(numbers.n)
    exponent = _encode_number(numbers.e)
    return {
        "kty": "RSA",
        "kid": _thumbprint(modulus=modulus, exponent=exponent),
        "use": "sig",
        "alg": LOCAL_SESSION_ALGORITHM,
        "n": modulus,
        "e": exponent,
    }


def _validate_public_jwk(value: object) -> tuple[dict[str, str], rsa.RSAPublicKey]:
    if not isinstance(value, dict):
        raise LocalSessionKeyConfigurationError("local session JWKS contains a non-object key")
    allowed = {"kty", "kid", "use", "alg", "n", "e"}
    if set(value) != allowed:
        raise LocalSessionKeyConfigurationError("local session JWK fields do not match the fixed profile")
    if value.get("kty") != "RSA" or value.get("use") != "sig" or value.get("alg") != LOCAL_SESSION_ALGORITHM:
        raise LocalSessionKeyConfigurationError("local session JWK does not match the RS256 signing profile")
    kid = value.get("kid")
    if not isinstance(kid, str) or not kid or len(kid) > 128:
        raise LocalSessionKeyConfigurationError("local session JWK kid is invalid")
    modulus_text = value.get("n")
    exponent_text = value.get("e")
    modulus = _decode_number(modulus_text, field="n")
    exponent = _decode_number(exponent_text, field="e")
    if exponent != LOCAL_SESSION_PUBLIC_EXPONENT:
        raise LocalSessionKeyConfigurationError("local session RSA public exponent must be 65537")
    if modulus.bit_length() != LOCAL_SESSION_KEY_SIZE:
        raise LocalSessionKeyConfigurationError("local session RSA keys must be exactly 3072 bits")
    assert isinstance(modulus_text, str)
    assert isinstance(exponent_text, str)
    expected_kid = _thumbprint(modulus=modulus_text, exponent=exponent_text)
    if kid != expected_kid:
        raise LocalSessionKeyConfigurationError("local session JWK kid must be its RFC 7638 thumbprint")
    try:
        public_key = rsa.RSAPublicNumbers(exponent, modulus).public_key()
    except ValueError:
        raise LocalSessionKeyConfigurationError("local session RSA public key is invalid") from None
    canonical = {name: str(value[name]) for name in ("kty", "kid", "use", "alg", "n", "e")}
    return canonical, public_key


def _read_bounded(path: Path, *, maximum: int, private: bool) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(path, flags)
    except OSError:
        raise LocalSessionKeyConfigurationError("local session key file is unavailable") from None
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise LocalSessionKeyConfigurationError("local session key path must be a regular file")
        if file_stat.st_size <= 0 or file_stat.st_size > maximum:
            raise LocalSessionKeyConfigurationError("local session key file size is invalid")
        if private and stat.S_IMODE(file_stat.st_mode) & 0o077:
            raise LocalSessionKeyConfigurationError("local session private key must not be group- or world-readable")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            data = handle.read(maximum + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(data) > maximum:
        raise LocalSessionKeyConfigurationError("local session key file size is invalid")
    return data


def _parse_jwks_bytes(data: bytes) -> list[tuple[dict[str, str], rsa.RSAPublicKey]]:
    try:
        payload = json.loads(data.decode("utf-8"))
    except UnicodeError, json.JSONDecodeError:
        raise LocalSessionKeyConfigurationError("local session JWKS is not valid JSON") from None
    if not isinstance(payload, dict) or set(payload) != {"keys"} or not isinstance(payload["keys"], list):
        raise LocalSessionKeyConfigurationError("local session JWKS must contain only a keys array")
    if not 1 <= len(payload["keys"]) <= _MAX_VERIFICATION_KEYS:
        raise LocalSessionKeyConfigurationError("local session JWKS key count is invalid")
    parsed = [_validate_public_jwk(item) for item in payload["keys"]]
    kids = [item[0]["kid"] for item in parsed]
    if len(set(kids)) != len(kids):
        raise LocalSessionKeyConfigurationError("local session JWKS contains duplicate kid values")
    return parsed


def load_local_session_keyset(
    private_key_path: str | Path,
    jwks_path: str | Path,
) -> LocalSessionKeySet:
    """Load and cross-check one active private key and bounded public keyset."""
    private_bytes = _read_bounded(
        Path(private_key_path),
        maximum=_MAX_KEY_FILE_BYTES,
        private=True,
    )
    try:
        private_key = serialization.load_pem_private_key(private_bytes, password=None)
    except TypeError, ValueError:
        raise LocalSessionKeyConfigurationError("local session private key is not a valid unencrypted PEM") from None
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise LocalSessionKeyConfigurationError("local session private key must be RSA")
    if private_key.key_size != LOCAL_SESSION_KEY_SIZE:
        raise LocalSessionKeyConfigurationError("local session RSA keys must be exactly 3072 bits")
    if private_key.private_numbers().public_numbers.e != LOCAL_SESSION_PUBLIC_EXPONENT:
        raise LocalSessionKeyConfigurationError("local session RSA public exponent must be 65537")

    parsed = _parse_jwks_bytes(_read_bounded(Path(jwks_path), maximum=_MAX_JWKS_FILE_BYTES, private=False))
    active_jwk = _public_jwk(private_key.public_key())
    active_kid = active_jwk["kid"]
    by_kid = {jwk["kid"]: public_key for jwk, public_key in parsed}
    active_public = by_kid.get(active_kid)
    if active_public is None or active_public.public_numbers() != private_key.public_key().public_numbers():
        raise LocalSessionKeyConfigurationError("active local session private key is absent from JWKS")

    canonical_jwks = tuple(MappingProxyType(dict(jwk)) for jwk, _public_key in parsed)
    return LocalSessionKeySet(
        active_kid=active_kid,
        private_key=private_key,
        public_keys=MappingProxyType(by_kid),
        _public_jwks=MappingProxyType({"keys": canonical_jwks}),
    )


def _stat_identity(path: str) -> tuple[int, int, int, int]:
    try:
        value = Path(path).stat()
    except OSError:
        raise LocalSessionKeyConfigurationError("local session key file is unavailable") from None
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


@lru_cache(maxsize=8)
def _cached_keyset(
    private_path: str,
    jwks_path: str,
    _private_identity: tuple[int, int, int, int],
    _jwks_identity: tuple[int, int, int, int],
) -> LocalSessionKeySet:
    return load_local_session_keyset(private_path, jwks_path)


def get_local_session_keyset() -> LocalSessionKeySet:
    from app.config import settings

    private_path = settings.local_session_private_key_path.strip()
    jwks_path = settings.local_session_jwks_path.strip()
    if not private_path or not jwks_path:
        raise LocalSessionKeyConfigurationError("local session key paths are required")
    return _cached_keyset(
        private_path,
        jwks_path,
        _stat_identity(private_path),
        _stat_identity(jwks_path),
    )


def clear_local_session_keyset_cache() -> None:
    _cached_keyset.cache_clear()


def _write_exclusive(path: Path, data: bytes, *, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, mode)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if fd >= 0:
            os.close(fd)


def _retained_jwks(paths: Sequence[str | Path]) -> list[dict[str, str]]:
    retained: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_path in paths:
        parsed = _parse_jwks_bytes(_read_bounded(Path(raw_path), maximum=_MAX_JWKS_FILE_BYTES, private=False))
        for jwk, _public_key in parsed:
            if jwk["kid"] in seen:
                continue
            seen.add(jwk["kid"])
            retained.append(jwk)
    return retained


def generate_local_session_keyset(
    output_dir: str | Path,
    *,
    retain_jwks_paths: Sequence[str | Path] = (),
) -> dict[str, object]:
    """Create a new, non-overwriting RSA-3072 keyset for operator storage."""
    target = Path(output_dir)
    try:
        target.mkdir(mode=0o700, parents=False, exist_ok=False)
    except FileExistsError:
        raise LocalSessionKeyConfigurationError("local session key output already exists") from None
    except OSError:
        raise LocalSessionKeyConfigurationError("unable to create local session key output") from None

    private_path = target / "private.pem"
    jwks_path = target / "jwks.json"
    created: list[Path] = []
    try:
        private_key = rsa.generate_private_key(
            public_exponent=LOCAL_SESSION_PUBLIC_EXPONENT,
            key_size=LOCAL_SESSION_KEY_SIZE,
        )
        active_jwk = _public_jwk(private_key.public_key())
        retained = [key for key in _retained_jwks(retain_jwks_paths) if key["kid"] != active_jwk["kid"]]
        if 1 + len(retained) > _MAX_VERIFICATION_KEYS:
            raise LocalSessionKeyConfigurationError("too many retained local session verification keys")
        private_bytes = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        jwks_bytes = (
            json.dumps(
                {"keys": [active_jwk, *retained]},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("ascii")
            + b"\n"
        )
        _write_exclusive(private_path, private_bytes, mode=0o600)
        created.append(private_path)
        _write_exclusive(jwks_path, jwks_bytes, mode=0o600)
        created.append(jwks_path)
        directory_fd = os.open(target, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception as exc:
        for path in reversed(created):
            try:
                path.unlink()
            except OSError:
                pass
        try:
            target.rmdir()
        except OSError:
            pass
        if isinstance(exc, LocalSessionKeyConfigurationError):
            raise
        raise LocalSessionKeyConfigurationError("unable to generate local session keyset") from None

    return {
        "profile": LOCAL_SESSION_PROFILE,
        "algorithm": LOCAL_SESSION_ALGORITHM,
        "key_size": LOCAL_SESSION_KEY_SIZE,
        "active_kid": active_jwk["kid"],
        "retained_keys": len(retained),
        "private_key_path": str(private_path),
        "jwks_path": str(jwks_path),
    }
