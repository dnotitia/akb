"""Static contracts for the simplified Kubernetes deployment tree."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml


_ROOT = Path(__file__).resolve().parents[2]
_K8S = _ROOT / "deploy" / "k8s"


def _documents(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as source:
        return [item for item in yaml.safe_load_all(source) if isinstance(item, dict)]


def _one(path: Path, *, kind: str, name: str) -> dict:
    matches = [
        item for item in _documents(path) if item.get("kind") == kind and item.get("metadata", {}).get("name") == name
    ]
    assert len(matches) == 1
    return matches[0]


def test_kubernetes_tree_has_only_standalone_and_standalone_sso_entry_points():
    assert (_K8S / "kustomization.yaml").is_file()
    assert (_K8S / "standalone-sso" / "kustomization.yaml").is_file()
    for removed in ("base", "components", "profiles", "secrets"):
        assert not (_K8S / removed).exists()


def test_backend_and_postgres_consume_the_operator_owned_runtime_secret():
    backend = _one(_K8S / "backend.yaml", kind="Deployment", name="backend")
    pod = backend["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    volumes = {item["name"]: item for item in pod["volumes"]}
    assert volumes["secret-config"]["secret"]["secretName"] == "akb-secret"  # pragma: allowlist secret
    assert volumes["local-session-keys"]["secret"]["secretName"] == "akb-secret"  # pragma: allowlist secret

    postgres = _one(_K8S / "postgres.yaml", kind="StatefulSet", name="postgres")
    env = {item["name"]: item for item in postgres["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["POSTGRES_PASSWORD"]["valueFrom"]["secretKeyRef"] == {
        "name": "akb-secret",
        "key": "db_password",
    }


@pytest.mark.parametrize("entry", [_K8S, _K8S / "standalone-sso"])
def test_kustomize_entry_points_render_without_secrets_or_cluster_objects(entry: Path):
    kubectl = shutil.which("kubectl")
    if kubectl is None:
        pytest.skip("kubectl is required for Kustomize render tests")
    result = subprocess.run(
        [
            kubectl,
            "kustomize",
            "--load-restrictor=LoadRestrictionsNone",
            str(entry),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    resources = [item for item in yaml.safe_load_all(result.stdout) if isinstance(item, dict)]
    assert all(item.get("kind") != "Secret" for item in resources)
    assert all(item.get("kind") not in {"ClusterRole", "ClusterRoleBinding"} for item in resources)


def test_deployer_never_generates_or_rewrites_credentials():
    deployer = _K8S / "deploy.sh"
    assert os.access(deployer, os.X_OK)
    text = deployer.read_text(encoding="utf-8")
    assert "AKB_PROFILE must be standalone or standalone-sso" in text
    assert "GENERATE_SECRETS" not in text
    assert "bootstrap_material" not in text
    assert "kubectl create secret" not in text
    assert "VaultStaticSecret" in text


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def test_deployer_rejects_a_legacy_projection_owned_secret(tmp_path: Path):
    _write_executable(
        tmp_path / "kubectl",
        """#!/usr/bin/env bash
set -eu
case "$1" in
  create)
    printf '%s\n' 'apiVersion: v1' 'kind: Namespace' 'metadata:' '  name: akb-test'
    ;;
  apply)
    cat >/dev/null
    ;;
  get)
    if [[ "$*" == *"jsonpath="* ]]; then
      printf '%s\n' 'secrets.hashicorp.com/v1beta1|VaultStaticSecret'
    fi
    ;;
  *) exit 64 ;;
esac
""",
    )
    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "NAMESPACE": "akb-test",
            "AKB_PROFILE": "standalone",
            "SKIP_BUILD": "true",
            "BACKEND_IMAGE": "example/akb-backend:test",
            "FRONTEND_IMAGE": "example/akb-frontend:test",
        }
    )
    result = subprocess.run(
        ["bash", str(_K8S / "deploy.sh")],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    assert result.returncode == 2
    assert "still owned by VaultStaticSecret" in result.stderr


def test_deployer_fails_closed_when_runtime_secret_is_absent(tmp_path: Path):
    _write_executable(
        tmp_path / "kubectl",
        """#!/usr/bin/env bash
set -eu
case "$1" in
  create)
    printf '%s\n' 'apiVersion: v1' 'kind: Namespace' 'metadata:' '  name: akb-test'
    ;;
  apply)
    cat >/dev/null
    ;;
  get) exit 1 ;;
  *) exit 64 ;;
esac
""",
    )
    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "NAMESPACE": "akb-test",
            "AKB_PROFILE": "standalone",
            "SKIP_BUILD": "true",
            "BACKEND_IMAGE": "example/akb-backend:test",
            "FRONTEND_IMAGE": "example/akb-frontend:test",
        }
    )
    result = subprocess.run(
        ["bash", str(_K8S / "deploy.sh")],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    assert result.returncode == 2
    assert "Secret/akb-secret is required" in result.stderr
