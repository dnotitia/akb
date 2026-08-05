"""Focused checks for the Ubuntu E2E runtime bootstrap boundary."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO_ROOT / "backend/scripts/ci/bootstrap_e2e_runtime.sh"


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o755)


def test_bootstrap_prepares_locked_python_314_environment_and_execs_runtime(tmp_path: Path) -> None:
    fake_bin = tmp_path / "fake-bin"
    data_root = tmp_path / "bootstrap-data"
    uv_log = tmp_path / "uv.log"
    python_log = tmp_path / "python.log"
    python_template = tmp_path / "python-template"

    _write_executable(
        fake_bin / "docker",
        "#!/usr/bin/env bash\n"
        "[[ \"$1\" == info || \"$1 $2\" == 'compose version' ]]\n",
    )
    _write_executable(
        python_template,
        "#!/usr/bin/env bash\n"
        "if [[ \"${1:-}\" == -c ]]; then exit 0; fi\n"
        "printf 'docker=%s\\n' \"${AKB_E2E_DOCKER_ARGV:-}\" >\"$AKB_TEST_PYTHON_LOG\"\n"
        "printf 'arg=%s\\n' \"$@\" >>\"$AKB_TEST_PYTHON_LOG\"\n"
        "exit 23\n",
    )
    _write_executable(
        data_root / "bin/uv",
        "#!/usr/bin/env bash\n"
        "if [[ \"${1:-}\" == --version ]]; then echo 'uv 0.12.1 (aarch64-unknown-linux-gnu)'; exit 0; fi\n"
        "printf '%s\\n' \"$*\" >>\"$AKB_TEST_UV_LOG\"\n"
        "if [[ \"${1:-}\" == venv ]]; then\n"
        "  target=\"${!#}\"\n"
        "  mkdir -p \"$target/bin\"\n"
        "  cp \"$AKB_TEST_PYTHON_TEMPLATE\" \"$target/bin/python\"\n"
        "fi\n",
    )

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "AKB_E2E_BOOTSTRAP_ROOT": str(data_root),
            "AKB_TEST_PYTHON_LOG": str(python_log),
            "AKB_TEST_PYTHON_TEMPLATE": str(python_template),
            "AKB_TEST_UV_LOG": str(uv_log),
        }
    )

    result = subprocess.run(
        ["bash", str(BOOTSTRAP), "--manage-postgres", "--scenario", "empty"],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 23
    assert uv_log.read_text().splitlines() == [
        "python install 3.14",
        f"venv --clear --python 3.14 {data_root / 'venv'}",
        "sync --project backend --locked --no-dev --python 3.14",
    ]
    assert python_log.read_text().splitlines() == [
        "docker=docker",
        "arg=backend/scripts/ci/e2e_runtime.py",
        "arg=--manage-postgres",
        "arg=--scenario",
        "arg=empty",
    ]
