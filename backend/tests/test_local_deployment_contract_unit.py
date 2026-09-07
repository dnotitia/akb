"""Local packages must preserve Kubernetes runtime and configuration contracts."""

import configparser
import importlib.util
from pathlib import Path
import shutil
import subprocess

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
    assert app["s3_public_url"] == "http://localhost:9000"
    assert app["public_base_url"] == "http://localhost:3000"
    assert secret["s3_access_key"] == services["minio"]["environment"]["MINIO_ROOT_USER"]
    assert secret["s3_secret_key"] == services["minio"]["environment"]["MINIO_ROOT_PASSWORD"]


def test_dev_keycloak_uses_the_kubernetes_version():
    compose = yaml.safe_load((ROOT / "docker-compose.keycloak.yaml").read_text())
    resources = list(yaml.safe_load_all((ROOT / "deploy/k8s/standalone-sso/keycloak.yaml").read_text()))
    statefulset = next(r for r in resources if r and r["kind"] == "StatefulSet")
    expected = statefulset["spec"]["template"]["spec"]["containers"][0]["image"]
    assert compose["services"]["keycloak"]["image"] == expected


def test_local_runtime_defaults_match_kubernetes_except_topology():
    from app.config import Settings

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
    for path in (ROOT / "config/app.yaml.example", DEMO / "app.yaml"):
        actual = Settings.model_validate(yaml.safe_load(path.read_text())).model_dump()
        for key, value in expected.items():
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
