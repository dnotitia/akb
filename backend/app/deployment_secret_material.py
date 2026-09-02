"""Generate AKB Secret Contract v1 material for deployment bootstrap tools.

This module deliberately contains no Vault/OpenBao client.  It only creates the
runtime values that a deployment adapter can write to Kubernetes or an external
secret store.
"""

from __future__ import annotations

import base64
import secrets
import tempfile
import uuid
from pathlib import Path

import yaml

from app.services.local_session_keys import generate_local_session_keyset


def _base64url_32_bytes() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")


def generate_secret_contract_material(auth_profile: str = "local") -> dict[str, str]:
    """Return one fresh Secret Contract v1 payload without persisting it."""

    if auth_profile not in {"local", "sso"}:
        raise ValueError("auth_profile must be local or sso")
    database_password = secrets.token_urlsafe(48)
    system_hmac_secret = secrets.token_urlsafe(64)
    redis_password = secrets.token_urlsafe(48)
    legacy_jwt_secret = secrets.token_urlsafe(64)

    with tempfile.TemporaryDirectory(prefix="akb-local-session-") as directory:
        root = Path(directory) / "keyset"
        generate_local_session_keyset(root)
        private_pem = (root / "private.pem").read_text(encoding="utf-8")
        public_jwks = (root / "jwks.json").read_text(encoding="utf-8")

    secret_config = {
        "db_password": database_password,
        "system_hmac_secret": system_hmac_secret,
        "embed_api_key": "",
        "llm_api_key": "",
        "rerank_api_key": "",
        "vector_api_key": "",
        "s3_access_key": "",
        "s3_secret_key": "",
        "redis_password": redis_password,
    }
    material = {
        "db_password": database_password,
        "system_hmac_secret": system_hmac_secret,
        "jwt_secret": legacy_jwt_secret,
        "local_session_private_pem": private_pem,
        "local_session_jwks_json": public_jwks,
        "secret_yaml": yaml.safe_dump(secret_config, sort_keys=True),
        "redis_password": redis_password,
        "auth_runtime_contract": "local-session-rs256-v2",
        "auth_runtime_generation": "1",
        "auth_runtime_mode": auth_profile,
    }
    if auth_profile == "sso":
        sso_material = {
            "keycloak_client_secret": secrets.token_urlsafe(48),
            "keycloak_admin_client_secret": secrets.token_urlsafe(48),
            "keycloak_management_client_secret": secrets.token_urlsafe(48),
            "sso_browser_session_encryption_key": _base64url_32_bytes(),
            "sso_session_epoch": str(uuid.uuid4()),
            "keycloak_db_password": secrets.token_urlsafe(48),
            "keycloak_bootstrap_client_secret": secrets.token_urlsafe(48),
            "product_admin_bootstrap_password": secrets.token_urlsafe(32),
        }
        secret_config.update(
            {
                key: sso_material[key]
                for key in (
                    "keycloak_client_secret",
                    "keycloak_admin_client_secret",
                    "keycloak_management_client_secret",
                    "sso_browser_session_encryption_key",
                    "sso_session_epoch",
                )
            }
        )
        material.update(sso_material)
        material["secret_yaml"] = yaml.safe_dump(secret_config, sort_keys=True)
        material["auth_runtime_contract"] = "sso-keycloak-broker-v3"
    return material
