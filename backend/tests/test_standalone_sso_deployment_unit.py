"""Static safety contracts for the generic standalone SSO Kustomize overlay."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest
import yaml


_ROOT = Path(__file__).resolve().parents[2]
_OVERLAY = _ROOT / "deploy" / "k8s" / "standalone-sso"


def _documents(name: str) -> list[dict]:
    with (_OVERLAY / name).open(encoding="utf-8") as source:
        return [item for item in yaml.safe_load_all(source) if isinstance(item, dict)]


def _one(name: str, *, kind: str, resource_name: str | None) -> dict:
    matches = [
        item
        for item in _documents(name)
        if item.get("kind") == kind and item.get("metadata", {}).get("name") == resource_name
    ]
    assert len(matches) == 1
    return matches[0]


def test_overlay_owns_dedicated_keycloak_and_database_without_committed_secrets():
    kustomization = _one(
        "kustomization.yaml",
        kind="Kustomization",
        resource_name=None,
    )
    assert {
        "akb-postgres.yaml",
        "keycloak-postgres.yaml",
        "keycloak.yaml",
        "keycloak-ingress.yaml",
    }.issubset(set(kustomization["resources"]))

    for path in _OVERLAY.glob("*.yaml"):
        for document in _documents(path.name):
            assert document.get("kind") != "Secret"
            assert "stringData" not in document

    keycloak = _one("keycloak.yaml", kind="StatefulSet", resource_name="keycloak")
    keycloak_postgres = _one(
        "keycloak-postgres.yaml",
        kind="StatefulSet",
        resource_name="keycloak-postgres",
    )
    assert keycloak["spec"]["template"]["spec"]["containers"][0]["image"].count("@sha256:") == 1
    assert keycloak_postgres["spec"]["template"]["spec"]["containers"][0]["image"].count("@sha256:") == 1


def test_keycloak_bootstrap_secret_is_required_for_first_boot_and_not_a_human_admin():
    keycloak = _one("keycloak.yaml", kind="StatefulSet", resource_name="keycloak")
    env = {item["name"]: item for item in keycloak["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["KC_BOOTSTRAP_ADMIN_CLIENT_ID"]["value"] == ("akb-bootstrap-temporary")
    reference = env["KC_BOOTSTRAP_ADMIN_CLIENT_SECRET"]["valueFrom"]["secretKeyRef"]
    assert reference == {
        "name": "akb-keycloak-bootstrap",
        "key": "client-secret",
    }
    assert "KC_BOOTSTRAP_ADMIN_USERNAME" not in env
    assert "KC_BOOTSTRAP_ADMIN_PASSWORD" not in env


def test_akb_database_uses_the_shared_platform_secret_contract():
    postgres = _one("akb-postgres.yaml", kind="StatefulSet", resource_name="postgres")
    container = postgres["spec"]["template"]["spec"]["containers"][0]
    assert "envFrom" not in container
    env = {item["name"]: item for item in container["env"]}
    assert env["POSTGRES_DB"]["value"] == "akb"
    assert env["POSTGRES_USER"]["value"] == "akbuser"
    assert env["POSTGRES_PASSWORD"]["valueFrom"]["secretKeyRef"] == {
        "name": "akb-secret",
        "key": "db_password",
    }


def test_backend_patch_removes_local_key_authority_and_mounts_one_time_inputs_only_in_init():
    deployment = _one(
        "backend-deployment-patch.yaml",
        kind="Deployment",
        resource_name="backend",
    )
    pod = deployment["spec"]["template"]["spec"]
    init = pod["initContainers"][0]
    assert init["command"] == [
        "python",
        "-m",
        "app.cli",
        "bootstrap-standalone-sso",
    ]
    init_mounts = {item["name"] for item in init["volumeMounts"]}
    assert {
        "keycloak-bootstrap",
        "keycloak-upgrade",
        "product-admin-bootstrap",
    }.issubset(init_mounts)
    main_mounts = {item["name"] for item in pod["containers"][0]["volumeMounts"]}
    assert "keycloak-bootstrap" not in main_mounts
    assert "keycloak-upgrade" not in main_mounts
    assert "product-admin-bootstrap" not in main_mounts
    assert pod["containers"][0]["volumeMounts"][0]["$patch"] == "delete"
    worker = next(item for item in pod["containers"] if item["name"] == "worker")
    assert worker["volumeMounts"][0]["name"] == "local-session-keys"
    assert worker["volumeMounts"][0]["$patch"] == "delete"
    volumes = {item["name"]: item for item in pod["volumes"]}
    assert volumes["local-session-keys"]["$patch"] == "delete"
    assert "optional" not in volumes["keycloak-bootstrap"]["secret"]
    assert volumes["keycloak-upgrade"]["secret"]["optional"] is True
    assert "optional" not in volumes["product-admin-bootstrap"]["secret"]
    assert "--upgrade-client-id" in init["args"]
    assert "--upgrade-client-secret-file" in init["args"]


def test_legacy_profile_upgrade_job_is_explicit_opt_in_and_uses_temporary_service_admin():
    kustomization = _one(
        "kustomization.yaml",
        kind="Kustomization",
        resource_name=None,
    )
    assert "legacy-profile-upgrade-job.yaml" not in kustomization["resources"]

    job = _one(
        "legacy-profile-upgrade-job.yaml",
        kind="Job",
        resource_name="akb-keycloak-profile-upgrade-authority",
    )
    container = job["spec"]["template"]["spec"]["containers"][0]
    assert container["image"].count("@sha256:") == 1
    assert container["args"][:2] == ["bootstrap-admin", "service"]
    assert "--client-id" in container["args"]
    assert "akb-bootstrap-upgrade-v2" in container["args"]
    assert "--client-secret:env=AKB_KEYCLOAK_UPGRADE_SECRET" in container["args"]
    secret_refs = [
        item["valueFrom"]["secretKeyRef"] for item in container["env"] if "secretKeyRef" in item.get("valueFrom", {})
    ]
    assert {
        "name": "akb-keycloak-upgrade",
        "key": "client-secret",
    } in secret_refs
    assert job["spec"]["template"]["spec"]["restartPolicy"] == "Never"


def test_sso_runtime_config_has_one_mode_and_three_distinct_confidential_clients():
    patch = _one(
        "backend-config-patch.yaml",
        kind="ConfigMap",
        resource_name="akb-app-config",
    )
    config = yaml.safe_load(patch["data"]["app.yaml"])
    assert config["auth_mode"] == "sso"
    assert config["keycloak_enabled"] is True
    assert config["keycloak_public_client"] is False
    assert "local_session_private_key_path" not in config
    assert "local_session_jwks_path" not in config
    clients = {
        config["keycloak_client_id"],
        config["keycloak_admin_client_id"],
        config["keycloak_management_client_id"],
    }
    assert clients == {"akb-web", "akb-admin", "akb-sso-manager"}
    assert config["keycloak_internal_url"] == "http://keycloak:8080"
    assert config["keycloak_backchannel_logout_uri"] == ("http://backend:8000/api/v1/auth/keycloak/backchannel-logout")
    assert config["keycloak_server_url"].startswith("https://")


def test_kustomize_render_has_no_local_session_mount_when_kubectl_is_available():
    kubectl = shutil.which("kubectl")
    if kubectl is None:
        pytest.skip("kubectl is not installed on this test host")
    result = subprocess.run(
        [
            kubectl,
            "kustomize",
            "--load-restrictor=LoadRestrictionsNone",
            str(_OVERLAY),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    rendered = [item for item in yaml.safe_load_all(result.stdout) if isinstance(item, dict)]
    backend = next(
        item
        for item in rendered
        if item.get("kind") == "Deployment" and item.get("metadata", {}).get("name") == "backend"
    )
    main = next(
        item
        for item in backend["spec"]["template"]["spec"]["containers"]
        if item["name"] == "backend"
    )
    assert {mount["name"] for mount in main["volumeMounts"]} == {
        "app-config",
        "secret-config",
        "vaultdata",
    }
    assert "local-session-keys" not in result.stdout
    assert "auth_mode: sso" in result.stdout
    assert "kind: Secret" not in result.stdout
