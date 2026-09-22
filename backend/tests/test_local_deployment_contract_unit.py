"""Local packages must preserve Kubernetes runtime and configuration contracts."""

import configparser
import importlib.util
from pathlib import Path
import shutil
import subprocess
import uuid

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "deploy/all-in-one"
spec = importlib.util.spec_from_file_location("demo_configure", DEMO / "configure.py")
configure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configure)


def test_process_roles_mounts_and_shutdown_match_kubernetes():
    compose = yaml.safe_load((ROOT / "docker-compose.yaml").read_text())["services"]
    resources = list(yaml.safe_load_all((ROOT / "deploy/k8s/backend.yaml").read_text()))
    pod = next(r for r in resources if r and r["kind"] == "Deployment")["spec"]["template"]["spec"]
    supervisor = configparser.ConfigParser(interpolation=None)
    supervisor.read(DEMO / "supervisord.conf")
    for container in pod["containers"]:
        name = container["name"]
        expected = {v["name"]: v["value"] for v in container["env"]}
        if name == "worker":
            # Keep the runtime role, but allow local YAML to tune corpus memory.
            expected.pop("AKB_TOKENIZER_PROCESSES")
        assert compose[name]["environment"] == expected
        assert compose[name]["stop_grace_period"] == f"{pod['terminationGracePeriodSeconds']}s"
        assert supervisor[f"program:{name}"].getint("stopwaitsecs") >= pod["terminationGracePeriodSeconds"]
        for key, value in expected.items():
            assert f'{key}="{value}"' in supervisor[f"program:{name}"]["environment"]
    assert compose["backend"]["volumes"] == compose["worker"]["volumes"]
    assert compose["worker"]["command"] == ["python", "-m", "app.worker_main"]
    assert compose["worker"]["restart"] == "unless-stopped"


def test_worker_uses_yaml_tokenizer_setting(monkeypatch):
    from app.services import lifecycle

    compose = yaml.safe_load((ROOT / "docker-compose.yaml").read_text())["services"]
    assert "AKB_TOKENIZER_PROCESSES" not in compose["worker"]["environment"]
    supervisor = configparser.ConfigParser(interpolation=None)
    supervisor.read(DEMO / "supervisord.conf")
    assert "AKB_TOKENIZER_PROCESSES" not in supervisor["program:worker"]["environment"]
    monkeypatch.delenv("AKB_TOKENIZER_PROCESSES", raising=False)
    monkeypatch.setattr(lifecycle.settings, "tokenizer_processes", 1)
    pools = []
    monkeypatch.setattr(lifecycle.sparse_encoder, "start_tokenizer_pool", pools.append)
    monkeypatch.setattr(lifecycle.write_lane, "start_commit_pool", lambda: None)
    lifecycle.start_runtime_pools()
    assert pools == [1]


def test_compose_defaults_connect_the_bundled_services():
    app = yaml.safe_load((ROOT / "config/app.yaml.example").read_text())
    secret = yaml.safe_load((ROOT / "config/secret.yaml.example").read_text())
    services = yaml.safe_load((ROOT / "docker-compose.yaml").read_text())["services"]
    assert app["db_host"] in services
    assert secret["db_password"] == services["postgres"]["environment"]["POSTGRES_PASSWORD"]
    assert app["s3_endpoint_url"] == "http://minio:9000"
    # `s3_public_url` is retained and ignored — nothing signs a URL for a
    # browser to follow any more. The example must still ship the key, or a
    # reader copying it would produce a config that an older build rejects;
    # and it must ship it blank, or the example invites configuring a field
    # that does nothing. Pinning it to a MinIO address would pin a contract
    # that no longer exists.
    assert "s3_public_url" in app
    assert app["s3_public_url"] == ""
    assert app["public_base_url"] == "http://localhost:3000"
    assert secret["s3_access_key"] == services["minio"]["environment"]["MINIO_ROOT_USER"]
    assert secret["s3_secret_key"] == services["minio"]["environment"]["MINIO_ROOT_PASSWORD"]


def test_dev_keycloak_uses_the_kubernetes_version():
    compose = yaml.safe_load((ROOT / "docker-compose.keycloak.yaml").read_text())
    resources = list(yaml.safe_load_all((ROOT / "deploy/k8s/standalone-sso/keycloak.yaml").read_text()))
    statefulset = next(r for r in resources if r and r["kind"] == "StatefulSet")
    expected = statefulset["spec"]["template"]["spec"]["containers"][0]["image"]
    assert compose["services"]["keycloak"]["image"] == expected


def test_local_runtime_defaults_match_kubernetes_except_topology(tmp_path):
    from app.config import Settings
    from app.services.revision_install_config import prepare_native_config

    resources = list(yaml.safe_load_all((ROOT / "deploy/k8s/backend.yaml").read_text()))
    config = next(r for r in resources if r and r["kind"] == "ConfigMap")
    expected = yaml.safe_load(config["data"]["app.yaml"])
    # Infrastructure addresses and local key mounts intentionally differ.
    environment_keys = {
        "db_host",
        "db_user",
        "local_session_issuer",
        "local_session_private_key_path",
        "local_session_jwks_path",
        "public_base_url",
        "s3_endpoint_url",
        "redis_url",
    }
    source = ROOT / "config/app.yaml.example"
    assert yaml.safe_load(source.read_text())["document_revision_backend"] == "postgres_native"
    assert expected["document_revision_backend"] == "bare_git"
    prepared = tmp_path / "native.yaml"
    secret = tmp_path / "secret.yaml"
    secret.write_text("{}\n")
    prepare_native_config(
        source=source, secret=secret, output=prepared,
        tenant_id="deployment-test", namespace="deployment-test",
        image_digest="sha256:" + "a" * 64,
    )
    for path in (prepared, DEMO / "app.yaml"):
        actual = Settings.model_validate(yaml.safe_load(path.read_text())).model_dump()
        assert actual["document_revision_backend"] == ("postgres_native" if path == prepared else "bare_git")
        for key, value in expected.items():
            # Base manifest/demo stay Bare Git; the prepared quickstart is Native.
            if key.startswith("document_revision_"):
                continue
            if key not in environment_keys:
                assert actual[key] == value, (path, key)


def test_local_proxies_cover_ingress_routes_and_upload_envelope():
    ingress = yaml.safe_load((ROOT / "deploy/k8s/ingress.yaml").read_text())
    size = ingress["metadata"]["annotations"]["nginx.ingress.kubernetes.io/proxy-body-size"]
    for path in (ROOT / "frontend/nginx.conf", DEMO / "nginx.conf"):
        text = path.read_text()
        assert f"client_max_body_size {size};" in text
        for route in ("api", "mcp", "well-known", "health", "livez", "readyz"):
            assert route in text
        assert "proxy_buffering off;" in text
        assert "try_files $uri =404;" in text
        assert "no-cache, no-store, must-revalidate" in text


def test_frontend_proxies_vault_health_instead_of_spa_html():
    import re

    for path in (ROOT / "frontend/nginx.conf", DEMO / "nginx.conf"):
        config = path.read_text()
        pattern = re.search(r"location ~ (\S+) \{", config).group(1)
        for route in ("/health", "/health/vault/shared", "/health/vault/with%20space"):
            assert re.match(pattern, route), (path, route)
        assert not re.match(pattern, "/healthcare")


def test_demo_state_honors_first_install_input_and_preserves_it(tmp_path):
    state = tmp_path / "state.env"
    token = "akb_test-'quoted $value; not executable"
    configure.initialize_state(state, {"DEMO_PAT": token})
    before = state.read_bytes()
    configure.initialize_state(state, {"DEMO_PAT": "replacement"})
    assert state.read_bytes() == before
    assert state.stat().st_mode & 0o777 == 0o600
    result = subprocess.check_output(["bash", "-c", '. "$1"; printf %s "$DEMO_PAT"', "test", str(state)], text=True)
    assert result == token


@pytest.fixture
def demo_env():
    return {
        "DB_PASSWORD": "test-db",  # pragma: allowlist secret -- synthetic test fixture
        "SYSTEM_HMAC_SECRET": "test-hmac",  # pragma: allowlist secret -- synthetic test fixture
        "S3_ACCESS_KEY": "test-access",
        "S3_SECRET_KEY": 'test-quote"\\\nvalue',
        "EMBED_API_KEY": 'test-key"\\\nvalue',
    }


def test_demo_yaml_preserves_values_and_mounted_overrides(tmp_path, demo_env):
    inputs, output = tmp_path / "inputs", tmp_path / "output"
    inputs.mkdir()
    (inputs / "app.yaml").write_text("embed_model: custom-model\nrerank_enabled: true\nsearch_prefetch: 80\n")
    (inputs / "secret.yaml").write_text("embed_api_key: mounted-test-key\n")
    configure.render(
        DEMO / "app.yaml", inputs, output, demo_env | {"EMBED_MODEL": "env-model", "EMBED_DIMENSIONS": "768"}
    )
    app = yaml.safe_load((output / "app.yaml").read_text())
    private = yaml.safe_load((output / "secret.yaml").read_text())
    assert app["document_revision_backend"] == "bare_git"
    assert app["git_storage_path"] == "/data/vaults"
    assert app["embed_model"] == "custom-model"
    assert app["embed_dimensions"] == 768
    assert app["rerank_enabled"] is True
    assert app["search_prefetch"] == 80
    assert private["embed_api_key"] == "mounted-test-key"  # pragma: allowlist secret -- synthetic fixture
    assert private["s3_secret_key"] == demo_env["S3_SECRET_KEY"]
    assert app["s3_public_url"] == app["public_base_url"]
    assert (output / "secret.yaml").stat().st_mode & 0o777 == 0o600
    # Re-render from immutable defaults, not the preceding generated file.
    (inputs / "app.yaml").unlink()
    configure.render(DEMO / "app.yaml", inputs, output, demo_env)
    app = yaml.safe_load((output / "app.yaml").read_text())
    assert app["document_revision_backend"] == "bare_git"
    assert app["embed_model"] == "text-embedding-3-small"
    assert app["rerank_enabled"] is False


@pytest.mark.parametrize("filename", ["app.yaml", "secret.yaml"])
def test_demo_rejects_infrastructure_override_without_exposing_value(tmp_path, demo_env, filename):
    (tmp_path / filename).write_text("db_password: do-not-print-this\n")
    with pytest.raises(ValueError, match="db_password is owned") as failure:
        configure.render(DEMO / "app.yaml", tmp_path, tmp_path / "output", demo_env)
    assert "do-not-print-this" not in str(failure.value)


def test_root_compose_creates_managed_volumes_for_fresh_installations():
    import json

    if not shutil.which("docker"):
        pytest.skip("Docker Compose required")

    def render(path):
        return json.loads(
            subprocess.check_output(
                ["docker", "compose", "-p", "akb-contract", "-f", str(ROOT / path), "config", "--format", "json"],
                text=True,
            )
        )

    root = render("docker-compose.yaml")
    assert not (ROOT / "deploy/docker-compose.yaml").exists()
    assert not (ROOT / "deploy/compose/legacy-volumes.yaml").exists()
    assert root["volumes"]["postgres_data"]["name"] == "akb-contract_postgres_data"
    assert root["volumes"]["vault_data"]["name"] == "akb-contract_vault_data"
    for volume in root["volumes"].values():
        assert not volume.get("external", False)


@pytest.mark.parametrize("filename", ["app.yaml", "secret.yaml"])
def test_demo_rejects_native_override_without_explicit_bootstrap(tmp_path, demo_env, filename):
    (tmp_path / filename).write_text("document_revision_backend: postgres_native\n")
    with pytest.raises(ValueError, match="document_revision_backend is owned by the demo bootstrap"):
        configure.render(DEMO / "app.yaml", tmp_path, tmp_path / "output", demo_env)


@pytest.mark.parametrize("filename", ["app.yaml", "secret.yaml"])
def test_demo_preserves_existing_bare_git_alias(tmp_path, demo_env, filename):
    (tmp_path / filename).write_text("document_revision_backend: bare_git_current\n")
    output = tmp_path / "output"
    configure.render(DEMO / "app.yaml", tmp_path, output, demo_env)
    assert yaml.safe_load((output / filename).read_text())["document_revision_backend"] == "bare_git_current"


def test_sso_broker_chain_fixture_writes_a_config_that_loads():
    """The fixture writes its whole app.yaml, so it owns the revision selector.

    It runs the real Settings loader from its own run directory. When the
    selector was omitted it inherited the new Native default and the run died
    in config validation, before Keycloak was ever contacted — a failure that
    reads as an unreachable broker. Load the exact bytes the script writes.
    """
    from app.config import Settings

    script = (ROOT / "deploy/keycloak-dev/broker-chain/run.sh").read_text()
    body = script.split('cat >"$fixture_run_dir/config/app.yaml" <<YAML\n', 1)[1]
    body = body.split("\nYAML\n", 1)[0]
    values = yaml.safe_load(body.replace("$sso_session_epoch", str(uuid.uuid4())))

    assert values["document_revision_backend"] == "bare_git"
    assert Settings.model_validate(values).document_revision_backend == "bare_git"


def test_contributor_setup_recipe_produces_a_loadable_config(tmp_path):
    """CONTRIBUTING's copy-and-pin recipe must actually start.

    `app.yaml.example` is a new-install Native template whose identity fields
    are empty on purpose, so the plain copy the guide used to prescribe fails
    Settings validation. Run the guide's own commands against the real files.
    """
    from app.config import Settings

    guide = (ROOT / "CONTRIBUTING.md").read_text()
    pin = "sed -i 's/^document_revision_backend: .*/document_revision_backend: bare_git/' config/app.yaml"
    assert pin in guide, "CONTRIBUTING no longer pins the selector it tells contributors to pin"

    config = tmp_path / "config"
    config.mkdir()
    shutil.copyfile(ROOT / "config/app.yaml.example", config / "app.yaml")
    subprocess.run(["sed", "-i", pin.split("'")[1], "config/app.yaml"], cwd=tmp_path, check=True)

    loaded = Settings.model_validate(yaml.safe_load((config / "app.yaml").read_text()))
    assert loaded.document_revision_backend == "bare_git"
