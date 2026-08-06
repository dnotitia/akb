"""Focused contract tests for the repository-owned E2E runtime."""

from __future__ import annotations

import asyncio
import json
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
from e2e_gate_observability import (  # noqa: E402
    EVENT_PREFIX,
    emit_gate_event,
    shell_exit_code,
    signal_from_returncode,
)
import e2e_suite_runner  # noqa: E402
from e2e_suite_runner import (  # noqa: E402
    CURATED_SUITES,
    SuiteResult,
    parse_assertion_summary,
)
from fixture_control import create_app  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = CI_DIR / "dependency-compose.yaml"
BOOTSTRAP = CI_DIR / "ubuntu_e2e_bootstrap.sh"
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
    assert fixture["fixture_discovery"] == {
        "method": "GET",
        "url": "http://127.0.0.1:8889/discover",
    }
    assert fixture["log_observation"] == {
        "method": "GET",
        "url": "http://127.0.0.1:8889/log-observation",
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


def test_scenario_argument_supports_the_lifecycle_fixture():
    config = _parse_args(["serve", "--scenario", "empty"])
    assert config.scenario == "empty"
    lifecycle = _parse_args(["serve", "--scenario", "app-installation-lifecycle"])
    assert lifecycle.scenario == "app-installation-lifecycle"
    with pytest.raises(SystemExit):
        _parse_args(["serve", "--scenario", "project"])


def test_suite_sql_uses_compose_psql_by_default_and_preserves_override(tmp_path, monkeypatch):
    runtime = E2ERuntime(make_config(tmp_path))
    assert runtime._child_environment()["PYTHONFAULTHANDLER"] == "1"
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


def test_runtime_configures_distinct_app_token_signing_secret(tmp_path):
    runtime = E2ERuntime(make_config(tmp_path))
    prepare_private_runtime_root(runtime.config.runtime_root)

    runtime._write_config()

    secret_config = yaml.safe_load(
        (runtime.config.config_dir / "secret.yaml").read_text(encoding="utf-8")
    )
    assert secret_config["app_token_secret"]
    assert secret_config["app_token_secret"] != secret_config["jwt_secret"]


def test_suite_summary_uses_last_complete_line_and_fails_closed():
    output = "Results: 1 passed, 0 failed\nnoise\nResults: 3 passed, 1 failed\n"
    assert parse_assertion_summary(output) == (3, 1, "Results: 3 passed, 1 failed")
    assert parse_assertion_summary("Results: 5 passed") is None
    assert SuiteResult("suite.sh", 0, 0, 0, None, "").gate_failed
    assert SuiteResult("suite.sh", 0, 2, 0, "Results: 2 passed, 0 failed", "").gate_failed is False
    assert SuiteResult("suite.sh", -4, 0, 0, None, "").signal == 4
    assert len(CURATED_SUITES) == 15


def test_gate_events_are_safe_and_shell_signal_aware(capsys):
    emit_gate_event(
        {
            "event": "suite_complete",
            "process": "suite_runner",
            "suite": "test_publications_e2e.sh",
            "returncode": -4,
            "passed": 0,
            "failed": 0,
            "summary": None,
            "signal": 4,
        }
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith(EVENT_PREFIX)
    stderr_event = json.loads(captured.err[len(EVENT_PREFIX) :])
    assert stderr_event["suite"] == "test_publications_e2e.sh"
    assert stderr_event["signal"] == 4
    assert signal_from_returncode(-4) == 4
    assert shell_exit_code(-4) == 132
    assert "external-password-value" not in captured.err


def test_suite_runner_emits_suite_and_gate_events(monkeypatch, capsys):
    monkeypatch.setattr(e2e_suite_runner, "CURATED_SUITES", ("test_fake.sh",))
    monkeypatch.setattr(
        e2e_suite_runner,
        "run_suite",
        lambda *_args, **_kwargs: SuiteResult(
            "test_fake.sh", 0, 2, 0, "Results: 2 passed, 0 failed", ""
        ),
    )
    assert e2e_suite_runner.run_curated(REPO_ROOT) == 0

    events = [
        json.loads(line[len(EVENT_PREFIX) :])
        for line in capsys.readouterr().err.splitlines()
        if line.startswith(EVENT_PREFIX)
    ]
    assert [event["event"] for event in events] == [
        "suite_start",
        "suite_complete",
        "gate_complete",
    ]
    assert events[1]["suite"] == "test_fake.sh"
    assert events[1]["summary"] == "Results: 2 passed, 0 failed"
    assert events[2]["passed"] == 2


@pytest.mark.asyncio
async def test_gate_child_stdout_is_private_and_stderr_is_inherited(tmp_path, capfd):
    checkout = tmp_path / "checkout"
    suite_path = checkout / "backend" / "scripts" / "ci" / "e2e_suite_runner.py"
    suite_path.parent.mkdir(parents=True)
    suite_path.write_text(
        "import sys\n"
        "print('credential-looking suite output')\n"
        "print('AKB_E2E_GATE {\"event\":\"suite_complete\","
        "\"suite\":\"test_fake.sh\",\"returncode\":132}', file=sys.stderr)\n",
        encoding="utf-8",
    )
    config = RuntimeConfig(
        checkout=checkout,
        runtime_root=tmp_path / "runtime",
        mode="gate",
        compose_file=COMPOSE_FILE,
        compose_project="akb-e2e-io-unit",
    )
    prepare_private_runtime_root(config.runtime_root)
    runtime = E2ERuntime(config)

    assert await runtime._run_gate() == 0

    captured = capfd.readouterr()
    gate_log = (config.logs_dir / "gate.log").read_text(encoding="utf-8")
    assert "credential-looking suite output" in gate_log
    assert "credential-looking suite output" not in captured.err
    assert '"event":"suite_complete"' in captured.err
    assert '"returncode":132' in captured.err


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


@pytest.mark.asyncio
async def test_dependency_start_waits_for_compose_health_before_backend_boot(tmp_path, monkeypatch):
    runtime = E2ERuntime(make_config(tmp_path))
    compose_calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    async def fake_compose(*arguments: str, **kwargs: object) -> int:
        compose_calls.append((arguments, kwargs))
        return 0

    async def fake_wait_tcp(*_args: object) -> None:
        return None

    async def fake_wait_http(*_args: object) -> bytes:
        return b""

    monkeypatch.setattr(runtime, "_compose", fake_compose)
    monkeypatch.setattr(runtime, "_wait_tcp", fake_wait_tcp)
    monkeypatch.setattr(runtime, "_wait_http", fake_wait_http)
    monkeypatch.setattr(runtime, "_ensure_minio_bucket", lambda: None)

    await runtime._start_dependencies()

    assert compose_calls == [(("up", "--detach", "--wait"), {})]


class FakeFixtureRuntime:
    def __init__(self, scenario="empty"):
        self.reset_count = 0
        self.scenario = scenario

    def fixture_health(self):
        return {"status": "ready", "scenario": self.scenario, "app_ready": True}

    def fixture_discovery(self):
        return {"status": "ready", "scenario": self.scenario, "fixtures": {}}

    def fixture_log_observation(self):
        return {
            "status": "ready",
            "scenario": self.scenario,
            "redacted": True,
            "redaction_scan": {"private_value_hits": 0, "raw_log_exposed": False},
        }

    async def reset_scenario(self):
        self.reset_count += 1


@pytest.mark.asyncio
async def test_fixture_control_exposes_reset_discovery_and_sanitized_logs():
    runtime = FakeFixtureRuntime()
    app = create_app(runtime)
    paths = {route.path for route in app.routes}
    assert {
        "/health",
        "/reset",
        "/discover",
        "/log-observation",
        "/openapi.json",
    } <= paths
    assert "/stop" not in paths

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture.test"
    ) as client:
        health = await client.get("/health")
        assert health.status_code == 200
        discovery = await client.get("/discover")
        assert discovery.json()["fixtures"] == {}
        log_observation = await client.get("/log-observation")
        assert log_observation.json()["redaction_scan"]["private_value_hits"] == 0
        reset = await client.post("/reset", json={"scenario": "empty"})
        assert reset.status_code == 200
        assert runtime.reset_count == 1
        lifecycle_runtime = FakeFixtureRuntime("app-installation-lifecycle")
        lifecycle_app = create_app(lifecycle_runtime)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=lifecycle_app),
            base_url="http://lifecycle-fixture.test",
        ) as lifecycle_client:
            lifecycle_reset = await lifecycle_client.post(
                "/reset", json={"scenario": "app-installation-lifecycle"}
            )
        assert lifecycle_reset.status_code == 200
        assert lifecycle_runtime.reset_count == 1
        unsupported = await client.post("/reset", json={"scenario": "project"})
        assert unsupported.status_code == 422
        openapi = await client.get("/openapi.json")
        assert openapi.status_code == 200
        assert "/reset" in openapi.json()["paths"]


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
    assert "app-installation-lifecycle" in (CI_DIR / "e2e_runtime.py").read_text()
    assert "uv sync --locked --extra dev --project backend" in workflow
    assert "services:" not in workflow
    for suite in CURATED_SUITES:
        assert suite not in workflow
    assert "akb-e2e-runtime-logs" in workflow


def test_ubuntu_bootstrap_is_bash_safe_and_keeps_descriptor_stdout_clean():
    result = subprocess.run(["bash", "-n", str(BOOTSTRAP)], check=False)
    assert result.returncode == 0
    text = BOOTSTRAP.read_text()
    assert "exec 3>&1 1>&2" in text
    assert "--scenario empty" in text
    assert "app-installation-lifecycle" in text
    assert "uv sync --locked" in text
    assert "exec 1>&3 3>&-" in text
    assert stat.S_IMODE(BOOTSTRAP.stat().st_mode) & stat.S_IXUSR
    assert "CRABBOX_" not in text
    assert "CRABBOX_" not in (CI_DIR / "e2e_runtime.py").read_text()
