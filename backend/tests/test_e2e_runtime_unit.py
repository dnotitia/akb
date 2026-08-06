"""Focused contract tests for the repository-owned Apple VM E2E runtime."""

from __future__ import annotations

import asyncio
import stat
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import yaml

CI_DIR = Path(__file__).resolve().parent.parent / "scripts" / "ci"
sys.path.insert(0, str(CI_DIR))

from e2e_runtime import (  # noqa: E402
    CredentialNames,
    E2ERuntime,
    RuntimeConfig,
    _parse_args,
    terminate_process,
    prepare_private_runtime_root,
)
from e2e_suite_runner import (  # noqa: E402
    CURATED_SUITES,
    SuiteResult,
    parse_assertion_summary,
)
from fixture_control import create_app  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = CI_DIR / "dependency-compose.yaml"
BOOTSTRAP = CI_DIR / "apple_vm_bootstrap.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "e2e.yml"


def make_config(tmp_path: Path, *, mode: str = "serve") -> RuntimeConfig:
    return RuntimeConfig(
        checkout=REPO_ROOT,
        runtime_root=tmp_path / "runtime",
        mode=mode,  # type: ignore[arg-type]
        compose_file=COMPOSE_FILE,
        compose_project="akb-e2e-unit",
        credentials=CredentialNames("TEST_USERNAME_ENV", "TEST_PASSWORD_ENV"),
    )


def test_descriptor_is_schema_v2_and_never_contains_credential_values(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_USERNAME_ENV", "external-user-value")
    monkeypatch.setenv("TEST_PASSWORD_ENV", "external-password-value")
    runtime = E2ERuntime(make_config(tmp_path))

    descriptor = runtime.descriptor()
    assert set(descriptor) == {
        "schema_version",
        "status",
        "scenario",
        "services",
        "credentials",
    }
    assert descriptor["schema_version"] == 2
    assert descriptor["status"] == "ready"
    assert descriptor["scenario"] == "empty"
    assert set(descriptor["services"]) == {"app", "fixture"}
    assert descriptor["services"]["app"] == {
        "origin": "http://127.0.0.1:8000",
        "health": {"method": "GET", "url": "http://127.0.0.1:8000/readyz"},
        "discovery": {
            "method": "GET",
            "url": "http://127.0.0.1:8000/openapi.json",
        },
    }
    fixture = descriptor["services"]["fixture"]
    assert fixture["origin"] == "http://127.0.0.1:8889"
    assert fixture["health"] == {
        "method": "GET",
        "url": "http://127.0.0.1:8889/health",
    }
    assert fixture["discovery"] == {
        "method": "GET",
        "url": "http://127.0.0.1:8889/openapi.json",
    }
    assert fixture["reset"] == {
        "method": "POST",
        "url": "http://127.0.0.1:8889/reset",
        "content_type": "application/json",
        "body": {"scenario": "empty"},
    }
    assert "schema" not in descriptor
    assert "app" not in descriptor
    assert "fixture" not in descriptor
    for service in descriptor["services"].values():
        for operation_name, operation in service.items():
            if operation_name == "origin":
                continue
            assert "path" not in operation
            assert operation["url"].startswith(service["origin"])
    assert set(descriptor["credentials"]) == {
        "username_env",
        "password_env",
        "login_path",
    }
    assert descriptor["credentials"]["username_env"] == "TEST_USERNAME_ENV"
    assert descriptor["credentials"]["password_env"] == "TEST_PASSWORD_ENV"
    serialized = str(descriptor)
    assert "external-user-value" not in serialized
    assert "external-password-value" not in serialized


def test_scenario_argument_is_explicitly_empty_only():
    config = _parse_args(["serve", "--scenario", "empty"])
    assert config.scenario == "empty"
    with pytest.raises(SystemExit):
        _parse_args(["serve", "--scenario", "project"])


def test_suite_sql_uses_compose_psql_by_default_and_preserves_override(tmp_path, monkeypatch):
    runtime = E2ERuntime(make_config(tmp_path))
    monkeypatch.delenv("AKB_PG_EXEC", raising=False)
    child_env = runtime._child_environment()
    assert child_env["AKB_PG_EXEC"].endswith("exec -T postgres")

    monkeypatch.setenv("AKB_PG_EXEC", "custom-pg-command")
    assert runtime._child_environment()["AKB_PG_EXEC"] == "custom-pg-command"


def test_runtime_root_is_private_and_separate(tmp_path):
    root = tmp_path / "private-runtime"
    config_dir, logs_dir, state_dir, vault_dir = prepare_private_runtime_root(root)

    assert {config_dir, logs_dir, state_dir, vault_dir} <= set(root.rglob("*"))
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    for directory in (config_dir, logs_dir, state_dir, vault_dir):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_suite_summary_uses_last_complete_line_and_fails_closed():
    output = "Results: 1 passed, 0 failed\nnoise\nResults: 3 passed, 1 failed\n"
    assert parse_assertion_summary(output) == (3, 1, "Results: 3 passed, 1 failed")
    assert parse_assertion_summary("Results: 5 passed") is None
    assert SuiteResult("suite.sh", 0, 0, 0, None, "").gate_failed
    assert SuiteResult("suite.sh", 0, 2, 0, "Results: 2 passed, 0 failed", "").gate_failed is False
    assert len(CURATED_SUITES) == 15


@pytest.mark.asyncio
async def test_process_termination_cleans_a_process_group():
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(60)",
        start_new_session=True,
    )
    await terminate_process(process, grace_seconds=0.2)
    assert process.returncode is not None


class FakeFixtureRuntime:
    def __init__(self):
        self.reset_count = 0
        self.scenario = "empty"

    def fixture_health(self):
        return {"status": "ready", "scenario": "empty", "app_ready": True}

    async def reset_empty(self):
        self.reset_count += 1


@pytest.mark.asyncio
async def test_fixture_control_exposes_only_empty_reset_and_discovery():
    runtime = FakeFixtureRuntime()
    app = create_app(runtime)
    paths = {route.path for route in app.routes}
    assert {"/health", "/reset", "/openapi.json"} <= paths
    assert "/stop" not in paths

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture.test"
    ) as client:
        health = await client.get("/health")
        assert health.status_code == 200
        reset = await client.post("/reset", json={"scenario": "empty"})
        assert reset.status_code == 200
        assert runtime.reset_count == 1
        unsupported = await client.post("/reset", json={"scenario": "project"})
        assert unsupported.status_code == 422
        discovery = await client.get("/openapi.json")
        assert discovery.status_code == 200
        assert "/reset" in discovery.json()["paths"]


def test_compose_and_hosted_workflow_preserve_the_live_topology():
    compose = yaml.safe_load(COMPOSE_FILE.read_text())
    assert set(compose["services"]) == {"postgres", "minio"}
    assert compose["services"]["postgres"]["image"] == "pgvector/pgvector:pg16"
    assert compose["services"]["minio"]["image"] == (
        "minio/minio:RELEASE.2025-09-07T16-13-09Z"
    )
    assert compose["services"]["postgres"]["ports"] == ["15432:5432"]
    assert compose["services"]["minio"]["ports"] == ["9000:9000"]

    workflow = WORKFLOW.read_text()
    assert "backend/scripts/ci/e2e_runtime.py gate" in workflow
    assert "--scenario empty" in workflow
    assert "uv sync --locked --extra dev --project backend" in workflow
    assert "services:" not in workflow
    for suite in CURATED_SUITES:
        assert suite not in workflow
    assert "akb-e2e-runtime-logs" in workflow


def test_bootstrap_is_bash_safe_and_keeps_descriptor_stdout_clean():
    result = subprocess.run(["bash", "-n", str(BOOTSTRAP)], check=False)
    assert result.returncode == 0
    text = BOOTSTRAP.read_text()
    assert "exec 3>&1 1>&2" in text
    assert "--scenario empty" in text
    assert "uv sync --locked" in text
    assert "exec 1>&3 3>&-" in text
    assert stat.S_IMODE(BOOTSTRAP.stat().st_mode) & stat.S_IXUSR
    assert "CRABBOX_" not in text
    assert "CRABBOX_" not in (CI_DIR / "e2e_runtime.py").read_text()
