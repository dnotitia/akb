"""Contracts for the source-installable AKB Helm chart."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest
import yaml


_ROOT = Path(__file__).resolve().parents[2]
_CHART = _ROOT / "deploy" / "helm" / "akb"


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


def test_chart_is_dependency_free_and_source_installable():
    chart = yaml.safe_load((_CHART / "Chart.yaml").read_text(encoding="utf-8"))
    assert "dependencies" not in chart
    assert chart["version"] == "0.2.0"
    assert not (_CHART / "Chart.lock").exists()
    assert not list((_CHART / "charts").glob("*.tgz"))

    subprocess.run(
        [_helm(), "lint", str(_CHART)],
        check=True,
        capture_output=True,
        text=True,
        timeout=90,
    )


@pytest.mark.parametrize(
    ("profile", "wants_sso"),
    [("standalone", False), ("standalone-sso", True)],
)
def test_profiles_render_one_coherent_application_stack(
    profile: str,
    wants_sso: bool,
):
    resources = _render(profile)
    assert not _names(resources, "Secret")
    assert not _names(resources, "ClusterRole")
    assert not _names(resources, "ClusterRoleBinding")
    assert {"backend", "frontend"}.issubset(_names(resources, "Deployment"))

    backend = _one(resources, "Deployment", "backend")
    assert _container_env(backend, "backend")["AKB_TOKENIZER_PROCESSES"] == "1"
    assert _container_env(backend, "worker")["AKB_TOKENIZER_PROCESSES"] == "2"

    statefulsets = _names(resources, "StatefulSet")
    assert "postgres" in statefulsets
    assert ("keycloak" in statefulsets) is wants_sso
    assert ("keycloak-postgres" in statefulsets) is wants_sso

    config = _one(resources, "ConfigMap", "akb-app-config")
    app = yaml.safe_load(config["data"]["app.yaml"])
    assert app["auth_mode"] == ("sso" if wants_sso else "local")
    if wants_sso:
        assert app["keycloak_internal_url"] == "http://keycloak:8080"
        assert backend["spec"]["template"]["spec"]["initContainers"][0]["name"] == ("bootstrap-standalone-sso")


def test_pgvector_scale_tuning_is_composable_with_both_profiles():
    tuning = _CHART / "tuning" / "pgvector-million-1024d.yaml"
    for profile in ("standalone", "standalone-sso"):
        resources = _render(profile, "--values", str(tuning))
        config = yaml.safe_load(
            _one(resources, "ConfigMap", "akb-app-config")["data"]["app.yaml"]
        )
        assert config["vector_store_startup_prewarm"] == "search"
        assert config["pgvector_search_timeout_secs"] == 30
        assert config["bm25_hybrid_max_df_ratio"] == 0.8

        postgres = _one(resources, "StatefulSet", "postgres")
        container = postgres["spec"]["template"]["spec"]["containers"][0]
        assert "shared_buffers=16GB" in container["args"]
        assert "shared_preload_libraries=pg_stat_statements,pg_prewarm" in container["args"]
        assert "pg_prewarm.autoprewarm=true" in container["args"]
        assert container["resources"]["limits"]["memory"] == "36Gi"


def test_bounded_pgvector_tuning_restores_only_the_fixed_hot_set():
    tuning = _CHART / "tuning" / "pgvector-million-bounded-1024d.yaml"
    resources = _render("standalone", "--values", str(tuning))
    config = yaml.safe_load(
        _one(resources, "ConfigMap", "akb-app-config")["data"]["app.yaml"]
    )
    assert config["vector_store_startup_prewarm"] == "off"
    assert config["pgvector_search_timeout_secs"] == 90
    assert config["bm25_hybrid_max_df_ratio"] == 0.8

    postgres = _one(resources, "StatefulSet", "postgres")
    container = postgres["spec"]["template"]["spec"]["containers"][0]
    assert "shared_buffers=3GB" in container["args"]
    assert "shared_preload_libraries=pg_stat_statements,pg_prewarm" in container["args"]
    assert "pg_prewarm.autoprewarm=true" in container["args"]
    assert container["resources"]["limits"]["memory"] == "8Gi"
    assert container["readinessProbe"]["initialDelaySeconds"] == 45


def test_bundled_prewarm_requires_database_restart_support():
    result = subprocess.run(
        [
            _helm(),
            "template",
            "akb",
            str(_CHART),
            "--values",
            str(_CHART / "profiles" / "standalone.yaml"),
            "--set-string",
            "app.vectorStore.startupPrewarm=search",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "requires postgres.parameters.autoPrewarm" in result.stderr


def test_custom_pgvector_timeout_rejects_an_unrelated_driver():
    result = subprocess.run(
        [
            _helm(),
            "template",
            "akb",
            str(_CHART),
            "--values",
            str(_CHART / "profiles" / "standalone.yaml"),
            "--set-string",
            "app.vectorStore.driver=qdrant",
            "--set",
            "app.vectorStore.searchTimeoutSeconds=60",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "searchTimeoutSeconds is supported only by the pgvector driver" in result.stderr


def test_chart_consumes_a_configurable_existing_secret():
    resources = _render(
        "standalone",
        "--set-string",
        "secretContract.name=operator-owned-runtime",
    )
    backend = _one(resources, "Deployment", "backend")
    volumes = {item["name"]: item for item in backend["spec"]["template"]["spec"]["volumes"]}
    assert volumes["secret-config"]["secret"]["secretName"] == ("operator-owned-runtime")
    assert volumes["local-session-keys"]["secret"]["secretName"] == ("operator-owned-runtime")

    postgres = _one(resources, "StatefulSet", "postgres")
    password = next(
        item
        for item in postgres["spec"]["template"]["spec"]["containers"][0]["env"]
        if item["name"] == "POSTGRES_PASSWORD"
    )
    assert password["valueFrom"]["secretKeyRef"]["name"] == ("operator-owned-runtime")


def test_profile_validation_rejects_conflicting_sso_switch():
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


def test_removed_profile_is_not_accepted():
    result = subprocess.run(
        [
            _helm(),
            "template",
            "akb",
            str(_CHART),
            "--set-string",
            "profile=unsupported-profile",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "profile" in result.stderr


@pytest.mark.parametrize(
    "removed_key",
    ["secretManager", "secretSync", "openbao", "hashicorpVault"],
)
def test_removed_credential_service_values_fail_explicitly(removed_key: str):
    result = subprocess.run(
        [
            _helm(),
            "template",
            "akb",
            str(_CHART),
            "--values",
            str(_CHART / "profiles" / "standalone.yaml"),
            "--set-string",
            f"{removed_key}.enabled=true",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert f"{removed_key} is no longer supported" in result.stderr


def test_chart_has_no_custom_installer_and_guards_legacy_secret_ownership():
    assert not (_CHART / "install.sh").exists()
    validation = (_CHART / "templates" / "000-validate.yaml").read_text(encoding="utf-8")
    assert 'lookup "v1" "Secret"' in validation
    assert 'eq $owner.kind "VaultStaticSecret"' in validation
    assert "garbage collection cannot delete AKB credentials" in validation


@pytest.mark.parametrize(
    "manifest",
    [
        _ROOT / "deploy" / "k8s" / "postgres.yaml",
        _ROOT / "deploy" / "k8s" / "standalone-sso" / "keycloak-postgres.yaml",
    ],
)
def test_postgres_persistent_volumes_use_a_subdirectory(manifest: Path):
    resources = [item for item in yaml.safe_load_all(manifest.read_text(encoding="utf-8")) if isinstance(item, dict)]
    statefulset = next(item for item in resources if item.get("kind") == "StatefulSet")
    assert _container_env(statefulset, "postgres")["PGDATA"] == ("/var/lib/postgresql/data/pgdata")
