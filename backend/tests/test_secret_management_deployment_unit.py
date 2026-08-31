"""Static contracts for standalone Kubernetes Secret Manager profiles."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import uuid

import pytest
import yaml


_ROOT = Path(__file__).resolve().parents[2]
_K8S = _ROOT / "deploy" / "k8s"
_SECRETS = _K8S / "secrets"


def _documents(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as source:
        return [item for item in yaml.safe_load_all(source) if isinstance(item, dict)]


def _one(path: Path, *, kind: str, name: str) -> dict:
    matches = [
        item
        for item in _documents(path)
        if item.get("kind") == kind and item.get("metadata", {}).get("name") == name
    ]
    assert len(matches) == 1
    return matches[0]


def test_base_has_no_committed_secret_or_fixed_namespace_resource():
    kustomization = yaml.safe_load((_K8S / "kustomization.yaml").read_text(encoding="utf-8"))
    assert "namespace.yaml" not in kustomization["resources"]

    for resource in kustomization["resources"]:
        path = _K8S / resource
        for document in _documents(path):
            assert document.get("kind") != "Secret"
            assert "stringData" not in document
    assert "change-me" not in (_K8S / "postgres.yaml").read_text(encoding="utf-8")


def test_backend_and_postgres_share_platform_compatible_akb_secret_contract():
    config = _one(_K8S / "backend.yaml", kind="ConfigMap", name="akb-app-config")
    app_config = yaml.safe_load(config["data"]["app.yaml"])
    assert app_config["local_session_issuer"] == "https://akb.example.com"
    assert app_config["public_base_url"] == "https://akb.example.com"

    backend = _one(_K8S / "backend.yaml", kind="Deployment", name="backend")
    pod = backend["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    volumes = {item["name"]: item for item in pod["volumes"]}
    assert volumes["secret-config"]["secret"]["secretName"] == "akb-secret"
    local = volumes["local-session-keys"]["secret"]
    assert local["secretName"] == "akb-secret"
    assert {item["key"]: item["path"] for item in local["items"]} == {
        "local-session-private.pem": "private.pem",
        "local-session-jwks.json": "jwks.json",
    }

    postgres = _one(_K8S / "postgres.yaml", kind="StatefulSet", name="postgres")
    env = {item["name"]: item for item in postgres["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["POSTGRES_PASSWORD"]["valueFrom"]["secretKeyRef"] == {
        "name": "akb-secret",
        "key": "db_password",
    }


def test_vso_adapter_projects_only_contract_keys_and_has_bounded_rollout():
    path = _SECRETS / "vso-vault-compatible.yaml"
    runtime = _one(path, kind="VaultStaticSecret", name="akb-runtime")
    assert runtime["spec"]["type"] == "kv-v2"
    assert runtime["spec"]["hmacSecretData"] is True
    assert runtime["spec"]["destination"]["name"] == "akb-secret"
    transformation = runtime["spec"]["destination"]["transformation"]
    assert transformation["excludes"] == [".*"]
    assert set(transformation["templates"]) == {
        "db_password",
        "system_hmac_secret",
        "jwt_secret",
        "local-session-private.pem",
        "local-session-jwks.json",
        "secret.yaml",
        "auth_runtime_contract",
        "auth_runtime_generation",
        "auth_runtime_mode",
    }
    assert runtime["spec"]["rolloutRestartTargets"] == [
        {"kind": "Deployment", "name": "backend"}
    ]
    redis = _one(path, kind="VaultStaticSecret", name="akb-redis")
    assert set(redis["spec"]["destination"]["transformation"]["templates"]) == {"password"}
    assert "rolloutRestartTargets" not in redis["spec"]


def test_sso_vso_adapter_projects_runtime_database_and_one_time_boundaries():
    path = _SECRETS / "vso-vault-compatible-sso.yaml"
    runtime = _one(path, kind="VaultStaticSecret", name="akb-runtime")
    runtime_keys = set(runtime["spec"]["destination"]["transformation"]["templates"])
    assert {
        "keycloak_client_secret",
        "keycloak_admin_client_secret",
        "keycloak_management_client_secret",
        "sso_browser_session_encryption_key",
        "sso_session_epoch",
    }.issubset(runtime_keys)
    assert runtime["spec"]["rolloutRestartTargets"] == [
        {"kind": "Deployment", "name": "backend"}
    ]

    keycloak_db = _one(path, kind="VaultStaticSecret", name="akb-keycloak-database")
    db_templates = keycloak_db["spec"]["destination"]["transformation"]["templates"]
    assert set(db_templates) == {"POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD"}
    assert db_templates["POSTGRES_DB"]["text"] == "keycloak"
    assert db_templates["POSTGRES_USER"]["text"] == "keycloak"
    assert "rolloutRestartTargets" not in keycloak_db["spec"]

    for name, destination, key in (
        ("akb-keycloak-bootstrap", "akb-keycloak-bootstrap", "client-secret"),
        ("akb-product-admin-bootstrap", "akb-product-admin-bootstrap", "password"),
    ):
        item = _one(path, kind="VaultStaticSecret", name=name)
        assert item["spec"]["destination"]["name"] == destination
        assert set(item["spec"]["destination"]["transformation"]["templates"]) == {key}
        assert item["spec"]["destination"]["annotations"] == {
            "akb.dnotitia.com/lifecycle": "one-time-first-install"
        }
        assert "rolloutRestartTargets" not in item["spec"]


@pytest.mark.parametrize("engine", ["openbao", "hashicorp-vault"])
def test_bundled_profiles_separate_ephemeral_development_from_ha_production(engine: str):
    development = yaml.safe_load(
        (_SECRETS / "values" / f"{engine}-development.yaml").read_text(encoding="utf-8")
    )
    production = yaml.safe_load(
        (_SECRETS / "values" / f"{engine}-production.yaml").read_text(encoding="utf-8")
    )
    assert development["server"]["dev"]["enabled"] is True
    assert development["server"]["dataStorage"]["enabled"] is False
    assert production["global"]["tlsDisable"] is False
    assert production["server"]["dev"]["enabled"] is False
    assert production["server"]["ha"]["enabled"] is True
    assert production["server"]["ha"]["replicas"] == 3
    assert production["server"]["ha"]["raft"]["enabled"] is True
    assert production["server"]["persistentVolumeClaimRetentionPolicy"] == {
        "whenDeleted": "Retain",
        "whenScaled": "Retain",
    }
    assert "tls_disable = 0" in production["server"]["ha"]["raft"]["config"]


def test_material_generator_matches_contract_without_legacy_jwt_in_secret_yaml():
    spec = importlib.util.spec_from_file_location(
        "akb_bootstrap_material",
        _SECRETS / "bootstrap_material.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    material = module._material()  # noqa: SLF001 - deployment contract unit test
    assert material["local_session_private_pem"].startswith("-----BEGIN PRIVATE KEY-----")
    jwks = json.loads(material["local_session_jwks_json"])
    assert len(jwks["keys"]) == 1
    assert jwks["keys"][0]["alg"] == "RS256"
    secret_config = yaml.safe_load(material["secret_yaml"])
    assert secret_config["db_password"] == material["db_password"]
    assert secret_config["system_hmac_secret"] == material["system_hmac_secret"]
    assert "jwt_secret" not in secret_config


def test_sso_material_is_independent_and_projects_complete_standalone_contract():
    spec = importlib.util.spec_from_file_location(
        "akb_bootstrap_material_sso",
        _SECRETS / "bootstrap_material.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    material = module._material("sso")  # noqa: SLF001 - deployment contract unit test

    independent = [
        material["keycloak_client_secret"],
        material["keycloak_admin_client_secret"],
        material["keycloak_management_client_secret"],
        material["keycloak_db_password"],
        material["keycloak_bootstrap_client_secret"],
        material["product_admin_bootstrap_password"],
    ]
    assert len(set(independent)) == len(independent)
    assert len(material["sso_browser_session_encryption_key"]) == 43
    assert "=" not in material["sso_browser_session_encryption_key"]
    assert str(uuid.UUID(material["sso_session_epoch"])) == material["sso_session_epoch"]
    assert material["auth_runtime_mode"] == "sso"
    assert material["auth_runtime_contract"] == "sso-keycloak-broker-v3"

    secret_config = yaml.safe_load(material["secret_yaml"])
    for key in (
        "keycloak_client_secret",
        "keycloak_admin_client_secret",
        "keycloak_management_client_secret",
        "sso_browser_session_encryption_key",
        "sso_session_epoch",
    ):
        assert secret_config[key] == material[key]
    assert "keycloak_db_password" not in secret_config
    assert "keycloak_bootstrap_client_secret" not in secret_config
    assert "product_admin_bootstrap_password" not in secret_config

    resources = module._kubernetes_list(material, "akb-sso-test")["items"]  # noqa: SLF001
    by_name = {item["metadata"]["name"]: item for item in resources}
    assert set(by_name) == {
        "akb-secret",
        "redis-credentials",
        "akb-keycloak-db-credentials",
        "akb-keycloak-bootstrap",
        "akb-product-admin-bootstrap",
    }
    assert by_name["akb-keycloak-db-credentials"]["stringData"] == {
        "POSTGRES_DB": "keycloak",
        "POSTGRES_USER": "keycloak",
        "POSTGRES_PASSWORD": material["keycloak_db_password"],
    }


def test_deploy_scripts_are_context_scoped_idempotent_and_fail_closed():
    deploy = (_K8S / "deploy.sh").read_text(encoding="utf-8")
    secret_deploy = (_SECRETS / "deploy.sh").read_text(encoding="utf-8")

    assert 'KUBECTL+=(--context "${KUBE_CONTEXT}")' in deploy
    assert 'HELM+=(--kube-context "${KUBE_CONTEXT}")' in secret_deploy
    assert 'create namespace "${NAMESPACE}"' in deploy
    assert "STORAGE_CLASS is not a valid StorageClass name" in deploy
    assert "path: /spec/volumeClaimTemplates/0/spec/storageClassName" in deploy
    assert "path: /spec/storageClassName" in deploy
    assert "deployment rollout status follows" in deploy.lower()
    assert 'rollout status deployment/backend' in deploy
    assert "Backend not ready yet" not in deploy
    assert 'get values "${SECRET_STORE_RELEASE}" -n "${NAMESPACE}"' in secret_deploy
    assert ".server.dev.devRootToken // empty" in secret_deploy
    assert "--wait --timeout 5m" in secret_deploy
    assert 'AUTH_PROFILE="${AUTH_PROFILE:-local}"' in deploy
    assert 'AUTH_PROFILE="${AUTH_PROFILE:-local}"' in secret_deploy
    assert "vso-vault-compatible-sso.yaml" in secret_deploy
    assert "SSO_AKB_PUBLIC_URL" in deploy
    assert "SSO_KEYCLOAK_PUBLIC_URL" in deploy
    assert 'rollout status statefulset/keycloak' in deploy
    assert 'SECRET_STORE_RELEASE="akb-sm-${NAMESPACE_DIGEST}"' in secret_deploy
    assert 'SECRET_STORE_POD="${STATEFULSET}-0"' in secret_deploy

    chart_install = secret_deploy.split("HELM_ARGS=(", maxsplit=1)[1]
    production_block, development_block = chart_install.split('ROOT_TOKEN=""', maxsplit=1)
    assert "--wait --timeout 5m" not in production_block
    assert "--wait --timeout 5m" in development_block


def test_kustomize_and_pinned_helm_profiles_render_when_tools_are_available():
    kubectl = shutil.which("kubectl")
    helm = shutil.which("helm")
    if kubectl is None or helm is None:
        pytest.skip("kubectl and helm are required for deployment rendering")

    rendered = subprocess.run(
        [kubectl, "kustomize", "--load-restrictor=LoadRestrictionsNone", str(_K8S)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    resources = [item for item in yaml.safe_load_all(rendered) if isinstance(item, dict)]
    assert not any(item.get("kind") in {"Namespace", "Secret"} for item in resources)

    charts = {
        "openbao": ("openbao/openbao", "0.29.3"),
        "hashicorp-vault": ("hashicorp/vault", "0.34.1"),
    }
    for engine, (chart, version) in charts.items():
        subprocess.run(
            [
                helm,
                "template",
                "akb-secret-store",
                chart,
                "--version",
                version,
                "--namespace",
                "akb-secret-test",
                "--values",
                str(_SECRETS / "values" / f"{engine}-development.yaml"),
                "--set-string",
                "server.dev.devRootToken=test-only",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
