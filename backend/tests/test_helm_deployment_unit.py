"""Contracts for the source-installable AKB umbrella Helm chart."""

from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess

import pytest
import yaml


_ROOT = Path(__file__).resolve().parents[2]
_CHART = _ROOT / "deploy" / "helm" / "akb"
_CLUSTER_CHART = _ROOT / "deploy" / "helm" / "akb-cluster"


def _helm() -> str:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is required for chart contract tests")
    return helm


def _render(profile: str, *extra: str) -> list[dict]:
    output = subprocess.run(
        [
            _helm(),
            "template",
            "akb",
            str(_CHART),
            "--namespace",
            "akb-helm-test",
            "--values",
            str(_CHART / "profiles" / f"{profile}.yaml"),
            *extra,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=90,
    ).stdout
    return [item for item in yaml.safe_load_all(output) if isinstance(item, dict)]


def _one(resources: list[dict], kind: str, name: str) -> dict:
    matches = [item for item in resources if item.get("kind") == kind and item.get("metadata", {}).get("name") == name]
    assert len(matches) == 1
    return matches[0]


def _names(resources: list[dict], kind: str) -> set[str]:
    return {item["metadata"]["name"] for item in resources if item.get("kind") == kind and "metadata" in item}


def _container_env(resource: dict, container_name: str) -> dict[str, str]:
    container = next(
        item for item in resource["spec"]["template"]["spec"]["containers"] if item["name"] == container_name
    )
    return {item["name"]: item["value"] for item in container.get("env", []) if "value" in item}


def test_chart_dependencies_are_pinned_and_source_installable():
    chart = yaml.safe_load((_CHART / "Chart.yaml").read_text(encoding="utf-8"))
    dependencies = {
        item.get("alias", item["name"]): (item["version"], item["condition"]) for item in chart["dependencies"]
    }
    assert dependencies == {
        "openbao": ("0.29.3", "openbao.enabled"),
        "hashicorpVault": ("0.34.1", "hashicorpVault.enabled"),
    }
    for archive in (
        "openbao-0.29.3.tgz",
        "vault-0.34.1.tgz",
    ):
        assert (_CHART / "charts" / archive).is_file()
    assert not (_CHART / "charts" / "vault-secrets-operator-1.5.1.tgz").exists()

    subprocess.run(
        [_helm(), "lint", str(_CHART)],
        check=True,
        capture_output=True,
        text=True,
        timeout=90,
    )

    cluster_chart = yaml.safe_load((_CLUSTER_CHART / "Chart.yaml").read_text(encoding="utf-8"))
    assert {
        item.get("alias", item["name"]): (item["version"], item["condition"]) for item in cluster_chart["dependencies"]
    } == {"vso": ("1.5.1", "vso.enabled")}
    assert (_CLUSTER_CHART / "charts" / "vault-secrets-operator-1.5.1.tgz").is_file()
    subprocess.run(
        [_helm(), "lint", str(_CLUSTER_CHART)],
        check=True,
        capture_output=True,
        text=True,
        timeout=90,
    )


@pytest.mark.parametrize(
    ("profile", "wants_sso", "wants_secret_manager"),
    [
        ("standalone", False, False),
        ("standalone-sso", True, False),
        ("standalone-secret-manager", False, True),
        ("standalone-sso-secret-manager", True, True),
    ],
)
def test_profiles_render_one_coherent_stack(profile: str, wants_sso: bool, wants_secret_manager: bool):
    resources = _render(profile)
    assert not _names(resources, "Secret")
    assert {"backend", "frontend"}.issubset(_names(resources, "Deployment"))
    backend = _one(resources, "Deployment", "backend")
    assert _container_env(backend, "backend")["AKB_TOKENIZER_PROCESSES"] == "1"
    assert _container_env(backend, "worker")["AKB_TOKENIZER_PROCESSES"] == "2"
    assert "postgres" in _names(resources, "StatefulSet")
    postgres = _one(resources, "StatefulSet", "postgres")
    assert _container_env(postgres, "postgres")["PGDATA"] == ("/var/lib/postgresql/data/pgdata")

    statefulsets = _names(resources, "StatefulSet")
    assert ("keycloak" in statefulsets) is wants_sso
    assert ("keycloak-postgres" in statefulsets) is wants_sso
    assert ("akb-secret-store" in statefulsets) is wants_secret_manager
    assert ("akb-runtime" in _names(resources, "VaultStaticSecret")) is (wants_secret_manager)
    assert not any("vault-secrets-operator" in name for name in _names(resources, "Deployment"))
    assert not _names(resources, "ClusterRole")

    config = _one(resources, "ConfigMap", "akb-app-config")
    app = yaml.safe_load(config["data"]["app.yaml"])
    assert app["auth_mode"] == ("sso" if wants_sso else "local")
    if wants_sso:
        keycloak_postgres = _one(resources, "StatefulSet", "keycloak-postgres")
        assert _container_env(keycloak_postgres, "postgres")["PGDATA"] == ("/var/lib/postgresql/data/pgdata")
        assert app["keycloak_internal_url"] == "http://keycloak:8080"
        assert backend["spec"]["template"]["spec"]["initContainers"][0]["name"] == ("bootstrap-standalone-sso")


def test_bundled_openbao_renders_production_raft_tls_and_vso_contract():
    resources = _render("standalone-secret-manager")
    server = _one(resources, "StatefulSet", "akb-secret-store")
    assert server["spec"]["replicas"] == 3
    assert server["spec"]["podManagementPolicy"] == "Parallel"
    config = _one(resources, "ConfigMap", "akb-secret-store-config")["data"]["extraconfig-from-values.hcl"]
    assert "retry_join" in config
    assert "tls_disable = 0" in config

    connection = _one(resources, "VaultConnection", "akb-secret-store")
    assert connection["spec"]["address"] == ("https://akb-secret-store.akb-helm-test.svc:8200")
    assert connection["spec"]["caCertSecretRef"] == "akb-secret-store-tls"  # pragma: allowlist secret
    runtime = _one(resources, "VaultStaticSecret", "akb-runtime")
    assert runtime["spec"]["destination"]["name"] == "akb-secret"
    assert runtime["spec"]["rolloutRestartTargets"] == [{"kind": "Deployment", "name": "backend"}]
    binding = _one(
        resources,
        "ClusterRoleBinding",
        "akb-helm-test-akb-secret-store-auth-delegator",
    )
    assert binding["roleRef"]["name"] == "system:auth-delegator"
    assert binding["subjects"] == [
        {
            "kind": "ServiceAccount",
            "name": "akb-secret-store",
            "namespace": "akb-helm-test",
        }
    ]


def test_bundled_secret_manager_cluster_bindings_are_unique_per_instance():
    first = _render("standalone-secret-manager")
    output = subprocess.run(
        [
            _helm(),
            "template",
            "akb",
            str(_CHART),
            "--namespace",
            "other-akb-helm-test",
            "--values",
            str(_CHART / "profiles" / "standalone-secret-manager.yaml"),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=90,
    ).stdout
    second = [item for item in yaml.safe_load_all(output) if isinstance(item, dict)]
    first_bindings = _names(first, "ClusterRoleBinding")
    second_bindings = _names(second, "ClusterRoleBinding")
    assert first_bindings == {"akb-helm-test-akb-secret-store-auth-delegator"}
    assert second_bindings == {"other-akb-helm-test-akb-secret-store-auth-delegator"}
    assert first_bindings.isdisjoint(second_bindings)


def test_hashicorp_override_does_not_render_competing_engine_or_cluster_operator():
    resources = _render(
        "standalone-secret-manager",
        "--set-string",
        "secretManager.engine=hashicorp-vault",
        "--set",
        "secretManager.hashicorpLicenseAcknowledged=true",
        "--set",
        "openbao.enabled=false",
        "--set",
        "hashicorpVault.enabled=true",
    )
    assert "akb-secret-store" in _names(resources, "StatefulSet")
    assert not any(name.startswith("akb-vso") for name in _names(resources, "Deployment"))
    assert not _names(resources, "ClusterRole")
    assert "akb-runtime" in _names(resources, "VaultStaticSecret")


def test_installer_delegates_vso_to_cluster_prerequisite_manager():
    installer = (_CHART / "install.sh").read_text(encoding="utf-8")
    manager = (_ROOT / "deploy" / "cluster" / "ensure-vso.sh").read_text(encoding="utf-8")
    assert "VSO_MODE" in installer
    assert "deploy/cluster/ensure-vso.sh" in installer
    assert "vso.enabled" not in installer
    assert "INSTALL_VSO" not in installer
    assert 'VSO_RELEASE="${VSO_RELEASE:-akb-cluster}"' in manager
    assert 'VSO_NAMESPACE="${VSO_NAMESPACE:-vault-secrets-operator}"' in manager
    assert "Multiple VSO controller Deployments were found" in manager
    assert "outside the supported range >=1.4.0,<1.6.0" in manager
    assert "not owned by Helm release" in manager
    assert 'BOOTSTRAP_DOCKER_PLATFORM="${BOOTSTRAP_DOCKER_PLATFORM:-linux/amd64}"' in installer
    assert '--platform "${BOOTSTRAP_DOCKER_PLATFORM}"' in installer


def test_profile_validation_rejects_conflicting_booleans():
    result = subprocess.run(
        [
            _helm(),
            "template",
            "akb",
            str(_CHART),
            "--values",
            str(_CHART / "profiles" / "standalone.yaml"),
            "--set",
            "sso.enabled=true",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "sso.enabled conflicts with the selected profile" in result.stderr


def test_helm_installer_rejects_pgp_without_keys_before_cluster_commands():
    installer = _CHART / "install.sh"
    assert os.access(installer, os.X_OK)
    result = subprocess.run(
        ["bash", str(installer)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env={
            "PATH": "/usr/bin:/bin",
            "AKB_PROFILE": "standalone-secret-manager",
            "SECRET_PROFILE": "production",  # pragma: allowlist secret
            "SECRET_SEAL_MODE": "pgp",  # pragma: allowlist secret
        },
    )
    assert result.returncode == 2
    assert "PGP mode requires SECRET_PGP_KEYS" in result.stderr
    assert "namespace" not in result.stdout.lower()


def test_single_container_demo_is_not_presented_as_secret_manager_boundary():
    readme = (_ROOT / "deploy" / "all-in-one" / "README.md").read_text(encoding="utf-8")
    assert "single-container demo" in readme
    assert "does not run OpenBao/Vault or Keycloak" in readme


@pytest.mark.parametrize(
    "manifest",
    [
        _ROOT / "deploy" / "k8s" / "postgres.yaml",
        _ROOT / "deploy" / "k8s" / "components" / "sso" / "keycloak-postgres.yaml",
    ],
)
def test_postgres_persistent_volumes_use_a_subdirectory(manifest: Path):
    resources = [item for item in yaml.safe_load_all(manifest.read_text(encoding="utf-8")) if isinstance(item, dict)]
    statefulset = next(item for item in resources if item.get("kind") == "StatefulSet")
    assert _container_env(statefulset, "postgres")["PGDATA"] == ("/var/lib/postgresql/data/pgdata")
