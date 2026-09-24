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


def test_deployer_requires_explicit_legacy_profile_before_any_cluster_access(tmp_path: Path):
    marker = tmp_path / "cluster-access"
    _write_executable(tmp_path / "kubectl", f"#!/bin/sh\ntouch '{marker}'\nexit 99\n")
    env = {**os.environ, "PATH": f"{tmp_path}:/usr/bin:/bin"}
    env.pop("AKB_PROFILE", None)
    result = subprocess.run(
        ["bash", str(_K8S / "deploy.sh")], capture_output=True, text=True,
        env=env, timeout=10, check=False,
    )
    assert result.returncode == 2
    assert "New Native installs" in result.stderr
    assert not marker.exists()


_PINNED_PGVECTOR = "pgvector/pgvector:pg16@sha256:" + "a" * 64
_KEYCLOAK_POSTGRES = "postgres:16-alpine@sha256:" + "b" * 64


def _render_capturing_kubectl(tmp_path: Path) -> Path:
    """A kubectl that passes every precondition and keeps what gets applied."""
    applied = tmp_path / "applied.yaml"
    _write_executable(
        tmp_path / "kubectl",
        f"""#!/usr/bin/env bash
set -eu
case "$1" in
  create) printf '%s\\n' 'apiVersion: v1' 'kind: Namespace' 'metadata:' '  name: akb-test' ;;
  kustomize) printf '%s\\n' 'kind: StatefulSet' '        - image: {_PINNED_PGVECTOR}' \\
      '        - image: {_KEYCLOAK_POSTGRES}' '        - image: akb-backend:latest' ;;
  apply) if [ "$2" = "-f" ] && [ "$3" != "-" ]; then cp "$3" "{applied}"; else cat >/dev/null; fi ;;
  *) exit 0 ;;
esac
""",
    )
    return applied


def _deploy(tmp_path: Path, **extra: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update({"PATH": f"{tmp_path}:/usr/bin:/bin", "NAMESPACE": "akb-test", "AKB_PROFILE": "standalone"})
    env.update(extra)
    return subprocess.run(["bash", str(_K8S / "deploy.sh")], check=False, capture_output=True,
                          text=True, timeout=30, env=env)


def test_deployer_puts_the_postgres_image_it_is_given_into_the_render(tmp_path: Path):
    applied = _render_capturing_kubectl(tmp_path)
    result = _deploy(tmp_path, SKIP_BUILD="true", BACKEND_IMAGE="example/akb-backend:test",
                     FRONTEND_IMAGE="example/akb-frontend:test", POSTGRES_IMAGE="example/akb-postgres:test")
    assert result.returncode == 0, result.stderr
    rendered = applied.read_text()
    assert "image: example/akb-postgres:test" in rendered
    assert "pgvector/pgvector" not in rendered
    # Keycloak's own database is not AKB's and keeps its image.
    assert f"image: {_KEYCLOAK_POSTGRES}" in rendered


def test_deployer_without_a_postgres_image_keeps_the_base_and_says_what_follows(tmp_path: Path):
    applied = _render_capturing_kubectl(tmp_path)
    result = _deploy(tmp_path, SKIP_BUILD="true", BACKEND_IMAGE="example/akb-backend:test",
                     FRONTEND_IMAGE="example/akb-frontend:test")
    assert result.returncode == 0, result.stderr
    assert f"image: {_PINNED_PGVECTOR}" in applied.read_text()
    assert "POSTGRES_IMAGE is unset" in result.stderr and "posting" in result.stderr


def test_deployer_builds_the_extension_image_and_deploys_it(tmp_path: Path):
    """Built from deploy/postgres and tagged by every input of that build.

    The tag covers the Dockerfile and the patch and lockfile it copies, so an
    AKB upgrade that leaves them unchanged does not restart PostgreSQL, and a
    changed patch cannot hide behind a tag the nodes already hold.
    """
    applied = _render_capturing_kubectl(tmp_path)
    builds = tmp_path / "docker.log"
    _write_executable(tmp_path / "docker", f"#!/bin/sh\necho \"$*\" >> '{builds}'\n")
    result = _deploy(tmp_path, REGISTRY="registry.example")
    assert result.returncode == 0, result.stderr
    context = _K8S.parents[0] / "postgres"
    inputs = [context / "Dockerfile", *sorted((context / "vchord_bm25").rglob("*"))]
    assert any(p.suffix == ".patch" for p in inputs) and any(p.name == "Cargo.lock" for p in inputs)
    stream = b"".join(
        str(p.relative_to(context)).encode() + b"\n" + p.read_bytes() for p in inputs if p.is_file()
    )
    checksum = subprocess.run(["cksum"], input=stream, capture_output=True, check=True).stdout.split()[0].decode()
    image = f"registry.example/akb-postgres:pg16-{checksum}"
    postgres_builds = [line for line in builds.read_text().splitlines() if image in line]
    assert len(postgres_builds) == 1, builds.read_text()
    assert Path(postgres_builds[0].split()[-1]).resolve() == (_K8S.parents[0] / "postgres").resolve()
    assert f"image: {image}" in applied.read_text()

