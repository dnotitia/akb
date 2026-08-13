"""Local-session RS256 v2 key lifecycle and verifier contracts."""

from __future__ import annotations

import json
import stat
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import settings


@pytest.fixture
def configured_keyset(tmp_path: Path, monkeypatch) -> dict[str, object]:
    from app.services.local_session_keys import (
        clear_local_session_keyset_cache,
        generate_local_session_keyset,
    )

    output_dir = tmp_path / "local-session-v2"
    report = generate_local_session_keyset(output_dir)
    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)
    monkeypatch.setattr(
        settings,
        "public_base_url",
        "https://akb.example.test",
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "local_session_private_key_path",
        str(output_dir / "private.pem"),
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "local_session_jwks_path",
        str(output_dir / "jwks.json"),
        raising=False,
    )
    monkeypatch.setattr(settings, "local_session_issuer", "", raising=False)
    monkeypatch.setattr(settings, "local_session_audience", "", raising=False)
    clear_local_session_keyset_cache()
    return {"directory": output_dir, "report": report}


def _mint(
    private_key,
    *,
    kid: str,
    issuer: str = "https://akb.example.test",
    audience: str = "https://akb.example.test/api",
    header_typ: str = "akb-local-session+jwt",
    header_extra: dict[str, object] | None = None,
    claim_overrides: dict[str, object] | None = None,
) -> str:
    now = int(datetime.now(timezone.utc).timestamp())
    claims: dict[str, object] = {
        "iss": issuer,
        "aud": audience,
        "sub": str(uuid.uuid4()),
        "username": "alice",
        "iat": now,
        "nbf": now,
        "exp": now + 300,
        "jti": str(uuid.uuid4()),
        "profile": "local-session-rs256-v2",
        "token_use": "session",
    }
    if claim_overrides:
        claims.update(claim_overrides)
    headers: dict[str, object] = {"typ": header_typ, "kid": kid}
    if header_extra:
        headers.update(header_extra)
    return jwt.encode(claims, private_key, algorithm="RS256", headers=headers)


def test_generator_creates_rsa3072_keyset_without_overwriting(tmp_path: Path) -> None:
    from app.services.local_session_keys import (
        LocalSessionKeyConfigurationError,
        generate_local_session_keyset,
        load_local_session_keyset,
    )

    output_dir = tmp_path / "keys"
    report = generate_local_session_keyset(output_dir)
    private_path = output_dir / "private.pem"
    jwks_path = output_dir / "jwks.json"

    assert report == {
        "profile": "local-session-rs256-v2",
        "algorithm": "RS256",
        "key_size": 3072,
        "active_kid": report["active_kid"],
        "retained_keys": 0,
        "private_key_path": str(private_path),
        "jwks_path": str(jwks_path),
    }
    assert stat.S_IMODE(private_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(jwks_path.stat().st_mode) == 0o600
    assert "private" not in jwks_path.read_text(encoding="utf-8").lower()

    loaded = load_local_session_keyset(private_path, jwks_path)
    assert loaded.private_key.key_size == 3072
    assert loaded.active_kid == report["active_kid"]
    assert loaded.public_jwks == json.loads(jwks_path.read_text(encoding="utf-8"))

    with pytest.raises(LocalSessionKeyConfigurationError, match="already exists"):
        generate_local_session_keyset(output_dir)


def test_loader_rejects_mismatched_or_weak_private_material(tmp_path: Path) -> None:
    from app.services.local_session_keys import (
        LocalSessionKeyConfigurationError,
        generate_local_session_keyset,
        load_local_session_keyset,
    )

    first = tmp_path / "first"
    second = tmp_path / "second"
    generate_local_session_keyset(first)
    generate_local_session_keyset(second)

    with pytest.raises(LocalSessionKeyConfigurationError, match="absent from JWKS"):
        load_local_session_keyset(first / "private.pem", second / "jwks.json")

    weak_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    weak_path = tmp_path / "weak-private.pem"
    weak_path.write_bytes(
        weak_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    weak_path.chmod(0o600)
    with pytest.raises(LocalSessionKeyConfigurationError, match="exactly 3072 bits"):
        load_local_session_keyset(weak_path, first / "jwks.json")


def test_loader_enforces_private_mode_and_supports_kubernetes_secret_symlinks(
    tmp_path: Path,
) -> None:
    from app.services.local_session_keys import (
        LocalSessionKeyConfigurationError,
        generate_local_session_keyset,
        load_local_session_keyset,
    )

    generated = tmp_path / "generated"
    generate_local_session_keyset(generated)
    private_path = generated / "private.pem"
    private_path.chmod(0o640)
    with pytest.raises(LocalSessionKeyConfigurationError, match="group- or world-readable"):
        load_local_session_keyset(private_path, generated / "jwks.json")

    private_path.chmod(0o400)
    mount = tmp_path / "mounted-secret"
    revision = mount / "..2026_08_13"
    revision.mkdir(parents=True)
    mounted_private = revision / "private.pem"
    mounted_jwks = revision / "jwks.json"
    mounted_private.write_bytes(private_path.read_bytes())
    mounted_jwks.write_bytes((generated / "jwks.json").read_bytes())
    mounted_private.chmod(0o400)
    mounted_jwks.chmod(0o400)
    (mount / "..data").symlink_to(revision.name)
    (mount / "private.pem").symlink_to("..data/private.pem")
    (mount / "jwks.json").symlink_to("..data/jwks.json")

    assert (
        load_local_session_keyset(
            mount / "private.pem",
            mount / "jwks.json",
        ).private_key.key_size
        == 3072
    )


def test_create_and_verify_v2_survives_process_cache_reset(configured_keyset) -> None:
    from app.services.auth_service import create_jwt
    from app.services.auth_verifier_profiles import (
        LOCAL_SESSION_RS256_V2,
        verify_local_session_rs256_v2,
    )
    from app.services.local_session_keys import clear_local_session_keyset_cache

    subject = str(uuid.uuid4())
    token = create_jwt(subject, "alice")
    header = jwt.get_unverified_header(token)
    unverified = jwt.decode(token, options={"verify_signature": False})

    assert header == {
        "alg": "RS256",
        "kid": configured_keyset["report"]["active_kid"],
        "typ": "akb-local-session+jwt",
    }
    assert unverified["iss"] == "https://akb.example.test"
    assert unverified["aud"] == "https://akb.example.test/api"
    assert unverified["profile"] == LOCAL_SESSION_RS256_V2
    assert unverified["token_use"] == "session"
    assert uuid.UUID(unverified["jti"])

    clear_local_session_keyset_cache()
    principal = verify_local_session_rs256_v2(token)
    assert principal is not None
    assert principal.profile_id == LOCAL_SESSION_RS256_V2
    assert principal.subject == subject
    assert principal.audience == "https://akb.example.test/api"


def test_cutover_rejects_legacy_hs256_and_strictly_checks_v2_profile(
    configured_keyset,
) -> None:
    from app.services.auth_verifier_profiles import verify_local_session_rs256_v2
    from app.services.local_session_keys import get_local_session_keyset

    keyset = get_local_session_keyset()
    valid = _mint(keyset.private_key, kid=keyset.active_kid)
    assert verify_local_session_rs256_v2(valid) is not None

    now = int(datetime.now(timezone.utc).timestamp())
    legacy = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "username": "alice",
            "iat": now,
            "exp": now + 300,
        },
        "legacy-hs256-secret-must-not-be-used",  # pragma: allowlist secret
        algorithm="HS256",
        headers={"typ": "JWT"},
    )
    assert verify_local_session_rs256_v2(legacy) is None

    rejected = [
        _mint(keyset.private_key, kid="unknown-kid"),
        _mint(keyset.private_key, kid=keyset.active_kid, header_typ="JWT"),
        _mint(
            keyset.private_key,
            kid=keyset.active_kid,
            header_extra={"jku": "https://attacker.example/jwks.json"},
        ),
        _mint(keyset.private_key, kid=keyset.active_kid, issuer="https://other.example"),
        _mint(keyset.private_key, kid=keyset.active_kid, audience="https://other.example/api"),
        _mint(
            keyset.private_key,
            kid=keyset.active_kid,
            claim_overrides={"profile": "local-session-rs256-v3"},
        ),
        _mint(
            keyset.private_key,
            kid=keyset.active_kid,
            claim_overrides={"token_use": "access_token"},
        ),
    ]
    assert all(verify_local_session_rs256_v2(token) is None for token in rejected)


def test_rotation_retains_only_explicit_v2_public_keys(
    configured_keyset,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from app.services.auth_service import create_jwt
    from app.services.auth_verifier_profiles import verify_local_session_rs256_v2
    from app.services.local_session_keys import (
        clear_local_session_keyset_cache,
        generate_local_session_keyset,
    )

    old_token = create_jwt(str(uuid.uuid4()), "alice")
    old_kid = jwt.get_unverified_header(old_token)["kid"]
    old_dir = configured_keyset["directory"]

    rotated_dir = tmp_path / "rotated"
    report = generate_local_session_keyset(
        rotated_dir,
        retain_jwks_paths=[old_dir / "jwks.json"],
    )
    monkeypatch.setattr(
        settings,
        "local_session_private_key_path",
        str(rotated_dir / "private.pem"),
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "local_session_jwks_path",
        str(rotated_dir / "jwks.json"),
        raising=False,
    )
    clear_local_session_keyset_cache()

    new_token = create_jwt(str(uuid.uuid4()), "alice")
    new_kid = jwt.get_unverified_header(new_token)["kid"]

    assert old_kid != new_kid == report["active_kid"]
    assert report["retained_keys"] == 1
    assert verify_local_session_rs256_v2(old_token) is not None
    assert verify_local_session_rs256_v2(new_token) is not None


def test_expired_or_non_numeric_v2_dates_are_rejected(configured_keyset) -> None:
    from app.services.auth_verifier_profiles import verify_local_session_rs256_v2
    from app.services.local_session_keys import get_local_session_keyset

    keyset = get_local_session_keyset()
    now = datetime.now(timezone.utc)
    expired = _mint(
        keyset.private_key,
        kid=keyset.active_kid,
        claim_overrides={
            "iat": int((now - timedelta(minutes=10)).timestamp()),
            "nbf": int((now - timedelta(minutes=10)).timestamp()),
            "exp": int((now - timedelta(minutes=5)).timestamp()),
        },
    )
    bad_iat = _mint(
        keyset.private_key,
        kid=keyset.active_kid,
        claim_overrides={"iat": "not-a-number"},
    )
    assert verify_local_session_rs256_v2(expired) is None
    assert verify_local_session_rs256_v2(bad_iat) is None


@pytest.mark.asyncio
async def test_public_jwks_is_local_only_and_contains_no_private_material(
    configured_keyset,
    monkeypatch,
) -> None:
    from fastapi import HTTPException

    from app.api.routes.auth import local_session_jwks

    published = await local_session_jwks()
    assert published == json.loads((configured_keyset["directory"] / "jwks.json").read_text(encoding="utf-8"))
    assert all(set(key) == {"kty", "kid", "use", "alg", "n", "e"} for key in published["keys"])

    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    with pytest.raises(HTTPException) as exc_info:
        await local_session_jwks()
    assert exc_info.value.status_code == 404


def test_cli_generates_keyset_without_rendering_private_material(tmp_path, capsys) -> None:
    from app import cli

    output_dir = tmp_path / "cli-keyset"
    assert cli._generate_local_session_keyset(["--output-dir", str(output_dir)]) == 0

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    private_text = (output_dir / "private.pem").read_text(encoding="ascii")
    assert report["key_size"] == 3072
    assert "PRIVATE KEY" not in captured.out
    assert private_text not in captured.out
    assert captured.err == ""

    assert cli._generate_local_session_keyset(["--output-dir", str(output_dir)]) == 1
    captured = capsys.readouterr()
    assert "already exists" in captured.err
    assert private_text not in captured.err
