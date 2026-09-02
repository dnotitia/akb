"""Generate one AKB Secret Contract v1 payload without logging secret values.

The normal caller pipes stdout directly into a Vault-compatible KV v2 CLI or
``kubectl apply -f -``. Do not redirect the output to a durable file.
"""

from __future__ import annotations

import argparse
import base64
import json
import secrets
import tempfile
import uuid
from pathlib import Path

import yaml

from app.services.local_session_keys import generate_local_session_keyset


def _base64url_32_bytes() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")


def _material(auth_profile: str = "local") -> dict[str, str]:
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
        # Kept as a top-level compatibility projection, matching the latest
        # akb-platform-owned Secret. It is deliberately absent from secret.yaml.
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
        # The three client credentials, session boundary, and encryption key
        # are durable AKB runtime inputs. Keycloak DB and first-install values
        # stay outside secret.yaml and are projected into narrowly scoped
        # Kubernetes Secrets by the SSO adapter.
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


def _kubernetes_list(material: dict[str, str], namespace: str) -> dict:
    akb_secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": "akb-secret",
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/part-of": "akb",
                "app.kubernetes.io/managed-by": "akb-secret-contract",
            },
            "annotations": {
                "akb.dnotitia.com/secret-contract": "v1",
            },
        },
        "type": "Opaque",
        "stringData": {
            "db_password": material["db_password"],
            "system_hmac_secret": material["system_hmac_secret"],
            "jwt_secret": material["jwt_secret"],
            "local-session-private.pem": material["local_session_private_pem"],
            "local-session-jwks.json": material["local_session_jwks_json"],
            "secret.yaml": material["secret_yaml"],
            "auth_runtime_contract": material["auth_runtime_contract"],
            "auth_runtime_generation": material["auth_runtime_generation"],
            "auth_runtime_mode": material["auth_runtime_mode"],
        },
    }
    if material["auth_runtime_mode"] == "sso":
        akb_secret["stringData"].update(
            {
                "keycloak_client_secret": material["keycloak_client_secret"],
                "keycloak_admin_client_secret": material["keycloak_admin_client_secret"],
                "keycloak_management_client_secret": material[
                    "keycloak_management_client_secret"
                ],
                "sso_browser_session_encryption_key": material[
                    "sso_browser_session_encryption_key"
                ],
                "sso_session_epoch": material["sso_session_epoch"],
            }
        )
    redis_secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": "redis-credentials",
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/part-of": "akb",
                "app.kubernetes.io/managed-by": "akb-secret-contract",
            },
        },
        "type": "Opaque",
        "stringData": {"password": material["redis_password"]},
    }
    items = [akb_secret, redis_secret]
    if material["auth_runtime_mode"] == "sso":
        common_labels = {
            "app.kubernetes.io/part-of": "akb",
            "app.kubernetes.io/managed-by": "akb-secret-contract",
        }
        items.extend(
            [
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {
                        "name": "akb-keycloak-db-credentials",
                        "namespace": namespace,
                        "labels": common_labels,
                    },
                    "type": "Opaque",
                    "stringData": {
                        "POSTGRES_DB": "keycloak",
                        "POSTGRES_USER": "keycloak",
                        "POSTGRES_PASSWORD": material["keycloak_db_password"],
                    },
                },
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {
                        "name": "akb-keycloak-bootstrap",
                        "namespace": namespace,
                        "labels": common_labels,
                        "annotations": {
                            "akb.dnotitia.com/lifecycle": "one-time-first-install"
                        },
                    },
                    "type": "Opaque",
                    "stringData": {
                        "client-secret": material["keycloak_bootstrap_client_secret"]
                    },
                },
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {
                        "name": "akb-product-admin-bootstrap",
                        "namespace": namespace,
                        "labels": common_labels,
                        "annotations": {
                            "akb.dnotitia.com/lifecycle": "one-time-first-install"
                        },
                    },
                    "type": "Opaque",
                    "stringData": {
                        "password": material["product_admin_bootstrap_password"]
                    },
                },
            ]
        )
    return {"apiVersion": "v1", "kind": "List", "items": items}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--format", choices=("vault", "kubernetes"), default="vault")
    parser.add_argument("--auth-profile", choices=("local", "sso"), default="local")
    parser.add_argument("--namespace")
    args = parser.parse_args()
    material = _material(args.auth_profile)
    if args.format == "kubernetes":
        if not args.namespace:
            parser.error("--namespace is required for kubernetes output")
        payload: object = _kubernetes_list(material, args.namespace)
    else:
        payload = material
    print(json.dumps(payload, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
