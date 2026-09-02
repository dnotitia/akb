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
        item for item in _documents(path) if item.get("kind") == kind and item.get("metadata", {}).get("name") == name
    ]
    assert len(matches) == 1
    return matches[0]


def test_base_has_no_committed_secret_or_fixed_namespace_resource():
    assert not (_K8S / "kustomization.yaml").exists()
    assert not (_K8S / "namespace.yaml").exists()
    assert not (_K8S / "standalone-sso").exists()
    base_dir = _K8S / "base"
    kustomization = yaml.safe_load((base_dir / "kustomization.yaml").read_text(encoding="utf-8"))
    for resource in kustomization["resources"]:
        path = base_dir / resource
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
    assert volumes["secret-config"]["secret"]["secretName"] == "akb-secret"  # pragma: allowlist secret
    local = volumes["local-session-keys"]["secret"]
    assert local["secretName"] == "akb-secret"  # pragma: allowlist secret
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


def test_well_known_ingress_route_is_valid_for_kubernetes_and_stays_on_backend():
    ingress = _one(_K8S / "ingress.yaml", kind="Ingress", name="akb-ingress")
    paths = {item["path"]: item for item in ingress["spec"]["rules"][0]["http"]["paths"]}
    route = paths["/.well-known"]
    assert route["pathType"] == "ImplementationSpecific"
    assert route["backend"]["service"] == {"name": "backend", "port": {"number": 8000}}


def test_vso_adapter_projects_only_contract_keys_and_has_bounded_rollout():
    path = _SECRETS / "vso-vault-compatible.yaml"
    operator = _one(path, kind="ServiceAccount", name="akb-secret-admin")
    assert operator["automountServiceAccountToken"] is False
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
    assert runtime["spec"]["rolloutRestartTargets"] == [{"kind": "Deployment", "name": "backend"}]
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
    assert runtime["spec"]["rolloutRestartTargets"] == [{"kind": "Deployment", "name": "backend"}]

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
        assert item["spec"]["destination"]["annotations"] == {"akb.dnotitia.com/lifecycle": "one-time-first-install"}
        assert "rolloutRestartTargets" not in item["spec"]


@pytest.mark.parametrize("engine", ["openbao", "hashicorp-vault"])
def test_bundled_profiles_separate_ephemeral_development_from_ha_production(engine: str):
    development = yaml.safe_load((_SECRETS / "values" / f"{engine}-development.yaml").read_text(encoding="utf-8"))
    production = yaml.safe_load((_SECRETS / "values" / f"{engine}-production.yaml").read_text(encoding="utf-8"))
    assert development["server"]["dev"]["enabled"] is True
    assert development["server"]["dataStorage"]["enabled"] is False
    assert production["global"]["tlsDisable"] is False
    assert production["server"]["dev"]["enabled"] is False
    assert production["server"]["ha"]["enabled"] is True
    assert production["server"]["ha"]["replicas"] == 3
    assert production["server"]["ha"]["raft"]["enabled"] is True
    assert production["server"]["podManagementPolicy"] == "Parallel"
    assert production["server"]["persistentVolumeClaimRetentionPolicy"] == {
        "whenDeleted": "Retain",
        "whenScaled": "Retain",
    }
    assert "tls_disable = 0" in production["server"]["ha"]["raft"]["config"]
    assert "retry_join" in production["server"]["ha"]["raft"]["config"]
    assert 'leader_api_addr = "RAFT_ADDR"' in production["server"]["ha"]["raft"]["config"]


def test_material_generator_matches_contract_without_legacy_jwt_in_secret_yaml():
    spec = importlib.util.spec_from_file_location(
        "akb_bootstrap_material",
        _SECRETS / "bootstrap_material.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    material = module._material()  # noqa: SLF001 - deployment contract unit test
    assert material["local_session_private_pem"].startswith("-----BEGIN PRIVATE KEY-----")  # pragma: allowlist secret
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
    production_init = (_SECRETS / "initialize-production-bundled.sh").read_text(encoding="utf-8")
    seal_validator = (_SECRETS / "validate-seal-inputs.sh").read_text(encoding="utf-8")
    bootstrap = (_SECRETS / "bootstrap-bundled.sh").read_text(encoding="utf-8")

    assert 'KUBECTL+=(--context "${KUBE_CONTEXT}")' in deploy
    assert 'HELM+=(--kube-context "${KUBE_CONTEXT}")' in secret_deploy
    assert 'create namespace "${NAMESPACE}"' in deploy
    assert "STORAGE_CLASS is not a valid StorageClass name" in deploy
    assert "path: /spec/volumeClaimTemplates/0/spec/storageClassName" in deploy
    assert "path: /spec/storageClassName" in deploy
    assert "deployment rollout status follows" in deploy.lower()
    assert "rollout status deployment/backend" in deploy
    assert "Backend not ready yet" not in deploy
    assert 'get values "${SECRET_STORE_RELEASE}" -n "${NAMESPACE}"' in secret_deploy
    assert ".server.dev.devRootToken // empty" in secret_deploy
    assert "--wait --timeout 5m" in secret_deploy
    assert 'AUTH_PROFILE="${PROFILE_AUTH}"' in deploy
    assert "Choose a deployment profile" in deploy
    assert "legacy_auth_profile" not in deploy
    assert 'AUTH_PROFILE="${AUTH_PROFILE:-local}"' in secret_deploy
    assert "vso-vault-compatible-sso.yaml" in secret_deploy
    assert "SSO_AKB_PUBLIC_URL" in deploy
    assert "SSO_KEYCLOAK_PUBLIC_URL" in deploy
    assert "rollout status statefulset/keycloak" in deploy
    assert 'SECRET_STORE_RELEASE="akb-sm-${NAMESPACE_DIGEST}"' in secret_deploy  # pragma: allowlist secret
    assert 'SECRET_STORE_POD="${STATEFULSET}-0"' in secret_deploy
    assert 'PROFILE_FILE="${PROFILE_DIR}/profile.env"' in deploy
    assert 'source "${PROFILE_FILE}"' in deploy
    assert 'KUSTOMIZE_DIR="${KUSTOMIZE_DIR:-${PROFILE_DIR}}"' in deploy
    assert 'SECRET_SEAL_MODE="${SECRET_SEAL_MODE:-plaintext}"' in deploy
    assert 'SECRET_TOPOLOGY="${SECRET_TOPOLOGY:-production-ha}"' in deploy
    assert 'IMAGE_PLATFORM="${IMAGE_PLATFORM:-linux/amd64}"' in deploy
    assert 'docker buildx build --platform "${IMAGE_PLATFORM}"' in deploy
    assert 'BOOTSTRAP_DOCKER_PLATFORM="${IMAGE_PLATFORM}"' in deploy
    assert '--platform "${BOOTSTRAP_DOCKER_PLATFORM}"' in secret_deploy
    assert '--platform "${BOOTSTRAP_DOCKER_PLATFORM}"' in bootstrap
    assert "validate-seal-inputs.sh" in deploy
    assert ("PGP mode requires SECRET_PGP_KEYS and SECRET_ROOT_TOKEN_PGP_KEY before deployment") in seal_validator
    assert deploy.index("validate-seal-inputs.sh") < deploy.index('if [[ "${SKIP_BUILD}" == "true" ]]')
    assert "initialize-production-bundled.sh" in secret_deploy
    assert "rerun in external mode" not in secret_deploy
    assert "server.ha.replicas=${SECRET_STORE_REPLICAS}" in secret_deploy
    assert "operator init -format=json" in production_init
    assert '-pgp-keys="${remote_pgp_keys}"' in production_init
    assert '-root-token-pgp-key="${remote_root_key}"' in production_init
    assert '-recovery-pgp-keys="${remote_recovery_keys}"' in production_init
    assert "AwaitingKeyHolderUnseal" in production_init
    assert "write -format=json sys/unseal key=-" in production_init
    assert "(.data.sealed == false) or (.sealed == false)" in production_init
    assert 'operator unseal -format=json "${key}"' not in production_init
    assert "Recovery Kit" not in production_init
    assert "Recovery Code" not in production_init
    assert "BEGIN PGP PUBLIC KEY BLOCK" in production_init
    assert 'tee "${PGP_REMOTE_DIR}/${remote_name}"' in production_init
    assert "akb-secret-admin" in production_init
    assert "operator-service-account" in production_init
    assert "SECRET_STORE_CERT_ISSUER_NAME" in secret_deploy
    assert "certificate.yaml" in secret_deploy
    assert "SECRET_STORE_SEAL_CONFIG_SECRET" in secret_deploy
    assert "VSO_MODE" in secret_deploy
    assert "../../cluster/ensure-vso.sh" in secret_deploy
    assert "INSTALL_VSO" not in secret_deploy
    assert "server.extraVolumes[0].type=secret" in secret_deploy
    assert "server.extraArgs=-config=" in secret_deploy
    assert 'path "auth/token/renew-self"' in bootstrap
    assert "role/${OPERATOR_ROLE}" in bootstrap
    assert "token_no_default_policy=true" in bootstrap
    assert '"${TOKEN_ENV}=${ROOT_TOKEN}"' not in bootstrap
    assert "IFS= read -r bootstrap_token" in bootstrap

    chart_install = secret_deploy.split("HELM_ARGS=(", maxsplit=1)[1]
    production_block, development_block = chart_install.split('ROOT_TOKEN=""', maxsplit=1)
    assert "--wait --timeout 5m" not in production_block
    assert "--wait --timeout 5m" in development_block


def test_pgp_profile_rejects_missing_custody_inputs_before_deployment_action():
    result = subprocess.run(
        ["bash", str(_K8S / "profiles" / "standalone-secret-manager" / "deploy.sh")],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env={
            "PATH": "/usr/bin:/bin",
            "SECRET_PROFILE": "production",  # pragma: allowlist secret
            "SECRET_SEAL_MODE": "pgp",  # pragma: allowlist secret
        },
    )
    assert result.returncode == 2
    assert ("PGP mode requires SECRET_PGP_KEYS and SECRET_ROOT_TOKEN_PGP_KEY before deployment") in result.stderr
    assert "Building Docker images" not in result.stdout
    assert "Creating namespace" not in result.stdout


def test_vso_mode_validation_fails_before_cluster_commands():
    result = subprocess.run(
        ["bash", str(_K8S / "profiles" / "standalone-secret-manager" / "deploy.sh")],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env={
            "PATH": "/usr/bin:/bin",
            "VSO_MODE": "surprise",
        },
    )
    assert result.returncode == 2
    assert "VSO_MODE must be managed, external, or disabled" in result.stderr
    assert "Building Docker images" not in result.stdout
    assert "Creating namespace" not in result.stdout


def test_image_platform_validation_fails_before_cluster_commands():
    result = subprocess.run(
        ["bash", str(_K8S / "profiles" / "standalone" / "deploy.sh")],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env={
            "PATH": "/usr/bin:/bin",
            "IMAGE_PLATFORM": "darwin/arm64",
        },
    )
    assert result.returncode == 2
    assert "IMAGE_PLATFORM must be linux/amd64 or linux/arm64" in result.stderr
    assert "Creating namespace" not in result.stdout


def test_cluster_vso_manager_disabled_mode_is_non_mutating():
    manager = _ROOT / "deploy" / "cluster" / "ensure-vso.sh"
    result = subprocess.run(
        ["bash", str(manager)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env={"PATH": "/usr/bin:/bin", "VSO_MODE": "disabled"},
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "VSO prerequisite check disabled"


def test_kubernetes_profiles_are_symmetric_discoverable_entry_points():
    profiles = _K8S / "profiles"
    expected = {
        "standalone": ("local", "manual"),
        "standalone-sso": ("sso", "manual"),
        "standalone-secret-manager": ("local", "bundled"),
        "standalone-sso-secret-manager": ("sso", "bundled"),
    }
    discovered = {path.parent.name for path in profiles.glob("*/profile.env")}
    assert discovered == set(expected)

    for name, (auth, secret_mode) in expected.items():
        profile_dir = profiles / name
        metadata = dict(
            line.split("#", maxsplit=1)[0].strip().split("=", maxsplit=1)
            for line in (profile_dir / "profile.env").read_text(encoding="utf-8").splitlines()
            if line
        )
        assert metadata == {
            "PROFILE_AUTH": auth,
            "PROFILE_SECRET": secret_mode,
        }
        assert (profile_dir / "kustomization.yaml").is_file()
        application = yaml.safe_load((profile_dir / "kustomization.yaml").read_text(encoding="utf-8"))
        assert application["resources"] == ["../../base"]
        if auth == "sso":
            assert application["components"] == ["../../components/sso"]
        else:
            assert "components" not in application
        wrapper = profile_dir / "deploy.sh"
        assert wrapper.stat().st_mode & 0o111
        assert f"AKB_PROFILE={name}" in wrapper.read_text(encoding="utf-8")


def test_all_kubernetes_profile_application_layers_render(tmp_path: Path):
    kubectl = shutil.which("kubectl")
    if kubectl is None:
        pytest.skip("kubectl is required for profile rendering")

    profiles = _K8S / "profiles"
    for name in (
        "standalone",
        "standalone-sso",
        "standalone-secret-manager",
        "standalone-sso-secret-manager",
    ):
        rendered = subprocess.run(
            [
                kubectl,
                "kustomize",
                "--load-restrictor=LoadRestrictionsNone",
                str(profiles / name),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
        resources = [item for item in yaml.safe_load_all(rendered) if isinstance(item, dict)]
        statefulsets = {item["metadata"]["name"] for item in resources if item.get("kind") == "StatefulSet"}
        assert "postgres" in statefulsets
        assert ("keycloak" in statefulsets) is ("-sso" in name)
        assert not any(item.get("kind") == "Secret" for item in resources)

        # deploy.sh wraps the selected profile in a generated namespace overlay
        # through a symlink. Prove that the same composition remains renderable
        # through that exact shape, not only from its source path.
        generated = tmp_path / name
        generated.mkdir()
        (generated / "source").symlink_to(
            profiles / name,
            target_is_directory=True,
        )
        (generated / "kustomization.yaml").write_text(
            "apiVersion: kustomize.config.k8s.io/v1beta1\n"
            "kind: Kustomization\n"
            "namespace: akb-generated-test\n"
            "resources:\n"
            "  - source\n",
            encoding="utf-8",
        )
        subprocess.run(
            [
                kubectl,
                "kustomize",
                "--load-restrictor=LoadRestrictionsNone",
                str(generated),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )


def test_cert_manager_adapter_covers_service_and_all_ha_member_dns_names():
    template = (_SECRETS / "certificate.yaml").read_text(encoding="utf-8")
    rendered = (
        template.replace("__CERT_ISSUER_KIND__", "Issuer")
        .replace("__CERT_ISSUER_NAME__", "test-ca")
        .replace("__SERVICE__", "akb-sm-openbao")
        .replace("__STATEFULSET__", "akb-sm-openbao")
        .replace("__NAMESPACE__", "akb-rehearsal")
        .replace("__CLUSTER_DOMAIN__", "cluster.local")
    )
    certificate = yaml.safe_load(rendered)
    assert certificate["spec"]["secretName"] == "akb-secret-store-tls"  # pragma: allowlist secret
    assert certificate["spec"]["issuerRef"] == {"kind": "Issuer", "name": "test-ca"}
    dns_names = set(certificate["spec"]["dnsNames"])
    assert "akb-sm-openbao.akb-rehearsal.svc" in dns_names
    for index in range(3):
        assert (f"akb-sm-openbao-{index}.akb-sm-openbao-internal.akb-rehearsal.svc.cluster.local") in dns_names


def test_kustomize_and_pinned_helm_profiles_render_when_tools_are_available():
    kubectl = shutil.which("kubectl")
    helm = shutil.which("helm")
    if kubectl is None or helm is None:
        pytest.skip("kubectl and helm are required for deployment rendering")

    rendered = subprocess.run(
        [
            kubectl,
            "kustomize",
            "--load-restrictor=LoadRestrictionsNone",
            str(_K8S / "profiles" / "standalone"),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    resources = [item for item in yaml.safe_load_all(rendered) if isinstance(item, dict)]
    assert not any(item.get("kind") in {"Namespace", "Secret"} for item in resources)

    charts = {
        "openbao": ("openbao", "https://openbao.github.io/openbao-helm", "0.29.3"),
        "hashicorp-vault": ("vault", "https://helm.releases.hashicorp.com", "0.34.1"),
    }
    for engine, (chart, repository, version) in charts.items():
        subprocess.run(
            [
                helm,
                "template",
                "akb-secret-store",
                chart,
                "--repo",
                repository,
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

        production = subprocess.run(
            [
                helm,
                "template",
                "akb-secret-store",
                chart,
                "--repo",
                repository,
                "--version",
                version,
                "--namespace",
                "akb-secret-test",
                "--values",
                str(_SECRETS / "values" / f"{engine}-production.yaml"),
                "--set-string",
                "server.extraEnvironmentVars.RAFT_ADDR=https://akb-secret-store-0.internal:8200",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout
        assert "retry_join" in production
        assert 'leader_api_addr = "RAFT_ADDR"' in production
        assert "https://akb-secret-store-0.internal:8200" in production
        assert "kind: PodDisruptionBudget" in production

        home = "/openbao" if engine == "openbao" else "/vault"
        auto_seal = subprocess.run(
            [
                helm,
                "template",
                "akb-secret-store",
                chart,
                "--repo",
                repository,
                "--version",
                version,
                "--namespace",
                "akb-secret-test",
                "--values",
                str(_SECRETS / "values" / f"{engine}-production.yaml"),
                "--set",
                "server.extraVolumes[0].type=secret",
                "--set",
                "server.extraVolumes[0].name=akb-secret-store-seal",
                "--set-string",
                f"server.extraArgs=-config={home}/userconfig/akb-secret-store-seal/seal.hcl",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout
        assert "secretName: akb-secret-store-seal" in auto_seal
        assert f"-config={home}/userconfig/akb-secret-store-seal/seal.hcl" in auto_seal
