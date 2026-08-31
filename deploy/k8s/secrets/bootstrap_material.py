"""Generate one AKB Secret Contract v1 payload without logging secret values.

The normal caller pipes stdout directly into a Vault-compatible KV v2 CLI or
``kubectl apply -f -``. Do not redirect the output to a durable file.
"""

from __future__ import annotations

import argparse
import json
import secrets
import tempfile
from pathlib import Path

import yaml

from app.services.local_session_keys import generate_local_session_keyset


def _material() -> dict[str, str]:
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
    return {
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
        "auth_runtime_mode": "local",
    }


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
    return {"apiVersion": "v1", "kind": "List", "items": [akb_secret, redis_secret]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--format", choices=("vault", "kubernetes"), default="vault")
    parser.add_argument("--namespace")
    args = parser.parse_args()
    material = _material()
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
