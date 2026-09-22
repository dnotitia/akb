"""Opt-in fresh-Native deployment ordering and legacy upgrade contracts."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


class _ComposeLoader(yaml.SafeLoader):
    pass


def _sequence(loader, node):
    return loader.construct_sequence(node)


_ComposeLoader.add_constructor("!override", _sequence)
_ComposeLoader.add_constructor("!reset", lambda loader, node: None)


def test_native_compose_gates_api_and_worker_on_same_image_bootstrap():
    base = yaml.safe_load((ROOT / "docker-compose.yaml").read_text())["services"]
    native = yaml.load((ROOT / "docker-compose.native.yaml").read_text(), Loader=_ComposeLoader)["services"]
    bootstrap = native["native-bootstrap"]
    assert bootstrap["command"] == ["python", "-m", "app.cli", "initialize-postgres-native"]
    assert bootstrap["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert native["backend"]["depends_on"]["native-bootstrap"]["condition"] == "service_completed_successfully"
    assert base["worker"]["depends_on"]["backend"]["condition"] == "service_healthy"
    for name in ("backend", "worker", "native-bootstrap"):
        service = native[name]
        assert service["image"] == bootstrap["image"]
        assert service["volumes"] == bootstrap["volumes"]
        assert service["tmpfs"] == ["/data/vaults:ro"]
    assert native["backend"]["build"] is None
    assert native["worker"]["build"] is None


def test_native_kubernetes_bootstraps_before_both_processes_without_git_pvc():
    root = ROOT / "deploy/k8s/native"
    config = yaml.safe_load((root / "kustomization.yaml").read_text())
    assert config["configMapGenerator"][0]["behavior"] == "replace"
    assert config["configMapGenerator"][0]["files"] == ["app.yaml"]
    pod = yaml.safe_load((root / "backend-patch.yaml").read_text())["spec"]["template"]["spec"]
    init = pod["initContainers"][0]
    assert init["command"] == ["python", "-m", "app.cli", "initialize-postgres-native"]
    assert init["image"] == "akb-backend:latest"
    assert {m["name"] for m in init["volumeMounts"]} == {
        "app-config", "secret-config", "local-session-keys", "vaultdata",
    }
    assert all(m["readOnly"] for m in init["volumeMounts"])
    assert {c["name"] for c in pod["containers"]} == {"backend", "worker"}
    assert all(c["volumeMounts"][0]["readOnly"] for c in pod["containers"])
    assert pod["volumes"] == [{"name": "vaultdata", "persistentVolumeClaim": None, "emptyDir": {}}]
    assert yaml.safe_load((root / "remove-git-pvc.yaml").read_text())["$patch"] == "delete"


def test_legacy_kubernetes_renderers_pin_the_existing_backend():
    for file in ("deploy/k8s/backend.yaml", "deploy/k8s/standalone-sso/backend-config-patch.yaml"):
        objects = yaml.safe_load_all((ROOT / file).read_text())
        config = next(o for o in objects if o and o.get("kind") == "ConfigMap")
        assert yaml.safe_load(config["data"]["app.yaml"])["document_revision_backend"] == "bare_git"


def test_native_kustomize_render_preserves_config_and_pins_all_process_images(tmp_path):
    import shutil
    import subprocess

    import pytest

    kubectl = shutil.which("kubectl")
    if kubectl is None:
        pytest.skip("kubectl is required for Kustomize render tests")
    k8s = tmp_path / "k8s"
    native = k8s / "native"
    native.mkdir(parents=True)
    for name in ("postgres.yaml", "backend.yaml", "frontend.yaml", "ingress.yaml"):
        shutil.copyfile(ROOT / "deploy/k8s" / name, k8s / name)
    for name in ("kustomization.yaml", "backend-patch.yaml", "remove-git-pvc.yaml"):
        shutil.copyfile(ROOT / "deploy/k8s/native" / name, native / name)
    config = "auth_mode: local\ndocument_revision_backend: postgres_native\n"
    (native / "app.yaml").write_text(config)
    digest = "sha256:" + "a" * 64
    with (native / "kustomization.yaml").open("a") as stream:
        stream.write(f"\nimages:\n  - name: akb-backend\n    newName: example/akb\n    digest: {digest}\n")
    rendered = subprocess.run(
        [kubectl, "kustomize", "--load-restrictor=LoadRestrictionsNone", str(native)],
        check=True, capture_output=True, text=True, timeout=60,
    )
    objects = list(yaml.safe_load_all(rendered.stdout))
    app = next(o for o in objects if o["kind"] == "ConfigMap" and o["metadata"]["name"] == "akb-app-config")
    assert app["data"]["app.yaml"] == config
    backend = next(o for o in objects if o["kind"] == "Deployment" and o["metadata"]["name"] == "backend")
    pod = backend["spec"]["template"]["spec"]
    for container in pod["initContainers"] + pod["containers"]:
        assert container["image"] == f"example/akb@{digest}"
    assert next(v for v in pod["volumes"] if v["name"] == "vaultdata") == {"name": "vaultdata", "emptyDir": {}}
    assert not any(o["kind"] == "PersistentVolumeClaim" and o["metadata"]["name"] == "akb-vaultdata" for o in objects)


def test_documented_native_install_assets_are_ignored():
    import subprocess

    assets = [
        "config/native/app.yaml", "config/native/secret.yaml",
        "config/native/local-session/private.pem", "deploy/k8s/native/app.yaml",
    ]
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", *assets], cwd=ROOT,
        check=True, capture_output=True, text=True, timeout=10,
    )
    assert result.stdout.splitlines() == assets
