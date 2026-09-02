"""Behavioral contracts for cluster-scoped VSO prerequisite ownership."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess


_ROOT = Path(__file__).resolve().parents[2]
_MANAGER = _ROOT / "deploy" / "cluster" / "ensure-vso.sh"
_UNINSTALLER = _ROOT / "deploy" / "cluster" / "uninstall-vso.sh"


def _deployment(*, version: str = "1.5.1", owned: bool = True, name: str = "vso") -> dict:
    annotations = {}
    if owned:
        annotations = {
            "meta.helm.sh/release-name": "akb-cluster",
            "meta.helm.sh/release-namespace": "vault-secrets-operator",
        }
    return {
        "metadata": {
            "namespace": "vault-secrets-operator",
            "name": name,
            "annotations": annotations,
        },
        "spec": {
            "replicas": 1,
            "template": {
                "spec": {
                    "containers": [
                        {
                            "image": (
                                "hashicorp/vault-secrets-operator:" f"{version}"
                            )
                        }
                    ]
                }
            },
        },
        "status": {"readyReplicas": 1},
    }


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _fake_tools(
    tmp_path: Path,
    *,
    deployments_before: list[dict],
    deployments_after: list[dict] | None = None,
) -> tuple[dict[str, str], Path]:
    state = tmp_path / "installed"
    log = tmp_path / "helm.log"
    before = json.dumps({"items": deployments_before})
    after = json.dumps({"items": deployments_after or deployments_before})
    _write_executable(
        tmp_path / "kubectl",
        f"""#!/usr/bin/env bash
set -eu
if [[ "$1" == "get" && "$2" == "deployments" ]]; then
  if [[ -f {state!s} ]]; then printf '%s\\n' '{after}'; else printf '%s\\n' '{before}'; fi
  exit 0
fi
if [[ "$1" == "get" && "$2" == "crd" ]]; then
  if [[ -f {state!s} || '{before}' != '{{"items": []}}' ]]; then exit 0; fi
  exit 1
fi
exit 64
""",
    )
    _write_executable(
        tmp_path / "helm",
        f"""#!/usr/bin/env bash
set -eu
printf '%s\\n' "$*" >> {log!s}
if [[ "$1" == "upgrade" ]]; then touch {state!s}; fi
""",
    )
    env = dict(os.environ)
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    return env, log


def test_external_is_read_only_for_one_compatible_ready_controller(tmp_path: Path):
    env, log = _fake_tools(
        tmp_path,
        deployments_before=[_deployment(version="1.4.0", owned=False)],
    )
    env["VSO_MODE"] = "external"
    result = subprocess.run(
        ["bash", str(_MANAGER)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    assert result.returncode == 0
    assert "Using cluster VSO" in result.stdout
    assert "Reusing compatible VSO 1.4.0" in result.stderr
    assert not log.exists()


def test_managed_installs_dedicated_release_only_when_vso_is_absent(tmp_path: Path):
    env, log = _fake_tools(
        tmp_path,
        deployments_before=[],
        deployments_after=[_deployment()],
    )
    env["VSO_MODE"] = "managed"
    result = subprocess.run(
        ["bash", str(_MANAGER)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "No VSO installation found" in result.stdout
    commands = log.read_text(encoding="utf-8")
    assert "dependency build" in commands
    assert "upgrade --install akb-cluster" in commands
    assert "--namespace vault-secrets-operator --create-namespace" in commands


def test_multiple_controllers_fail_closed_before_helm_mutation(tmp_path: Path):
    env, log = _fake_tools(
        tmp_path,
        deployments_before=[_deployment(name="vso-a"), _deployment(name="vso-b")],
    )
    env["VSO_MODE"] = "managed"
    result = subprocess.run(
        ["bash", str(_MANAGER)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    assert result.returncode == 1
    assert "Multiple VSO controller Deployments" in result.stderr
    assert not log.exists()


def test_managed_refuses_to_take_over_foreign_release(tmp_path: Path):
    env, log = _fake_tools(
        tmp_path,
        deployments_before=[_deployment(owned=False)],
    )
    env["VSO_MODE"] = "managed"
    result = subprocess.run(
        ["bash", str(_MANAGER)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    assert result.returncode == 1
    assert "not owned by Helm release" in result.stderr
    assert not log.exists()


def test_uninstall_refuses_while_any_vso_consumer_remains(tmp_path: Path):
    helm_log = tmp_path / "helm.log"
    _write_executable(
        tmp_path / "kubectl",
        """#!/usr/bin/env bash
set -eu
printf '%s\n' 'vaultstaticsecret.secrets.hashicorp.com/akb-runtime'
""",
    )
    _write_executable(
        tmp_path / "helm",
        f"""#!/usr/bin/env bash
set -eu
printf '%s\n' "$*" >> {helm_log!s}
""",
    )
    env = dict(os.environ)
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    result = subprocess.run(
        ["bash", str(_UNINSTALLER)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    assert result.returncode == 1
    assert "Refusing to remove VSO" in result.stderr
    assert not helm_log.exists()
