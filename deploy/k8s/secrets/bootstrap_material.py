"""Generate one AKB Secret Contract v1 payload without logging secret values.

The normal caller pipes stdout directly into a Vault-compatible KV v2 CLI or
``kubectl apply -f -``. Do not redirect the output to a durable file.
"""

from __future__ import annotations

import argparse
import json
from app.deployment_secret_material import generate_secret_contract_material


# Retain the source-tree helper name used by existing deployment contract tests
# and downstream scripts while the implementation lives in the backend image.
_material = generate_secret_contract_material


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
    material = generate_secret_contract_material(args.auth_profile)
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
