"""Focused contract tests for the repository-owned E2E runtime."""

from __future__ import annotations

import asyncio
import dataclasses
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
    BlockedRuntimeConfig,
    CapabilityProfile,
    CredentialNames,
    E2ERuntime,
    ManagedProcess,
    RuntimeConfig,
    _parse_args,
    prepare_private_runtime_root,
    select_capability_profile,
    terminate_process,
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
    DEFERRED_SUITE_GROUPS,
    DeferredSuiteGroup,
    SuiteResult,
    parse_assertion_summary,
    validate_suite_manifest,
)
from fixture_control import create_app  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = CI_DIR / "dependency-compose.yaml"
BOOTSTRAP = CI_DIR / "ubuntu_e2e_bootstrap.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "e2e.yml"
LOCAL_CANONICAL_RUNNER = REPO_ROOT / "scripts" / "run_canonical_e2e.sh"


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
        "url": "http://127.0.0.1:8889/discover",
    }
    assert set(fixture) == {"origin", "health", "reset", "discovery"}
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


def test_frontend_descriptor_exposes_web_origin_and_backend_proxy_target(tmp_path):
    runtime = E2ERuntime(
        dataclasses.replace(
            make_config(tmp_path),
            frontend_enabled=True,
            frontend_port=3017,
        )
    )

    descriptor = runtime.descriptor()
    assert descriptor["services"]["web"] == {
        "origin": "http://127.0.0.1:3017",
        "health": {
            "method": "GET",
            "url": "http://127.0.0.1:3017",
        },
    }
    evidence = descriptor["evidence"]
    assert evidence["origin"]["frontend"] == "http://127.0.0.1:3017"
    assert evidence["frontend"] == {
        "service": "web",
        "origin": "http://127.0.0.1:3017",
        "health": {
            "method": "GET",
            "url": "http://127.0.0.1:3017",
        },
        "backend_proxy_target": "http://127.0.0.1:8000",
        "browser_input": "AKB_FRONTEND_URL",
    }

    discovery = runtime.fixture_discovery()
    assert discovery["web"] == {
        "service": "web",
        "origin": "http://127.0.0.1:3017",
        "health": {"method": "GET", "path": "/"},
        "backend_proxy_target": "http://127.0.0.1:8000",
    }


def test_frontend_runtime_requires_explicit_flag_and_supports_isolated_port():
    default = _parse_args(["serve"])
    assert default.frontend_enabled is False
    assert default.frontend_port == 3000

    configured = _parse_args(
        ["serve", "--with-frontend", "--frontend-port", "3017"]
    )
    assert configured.frontend_enabled is True
    assert configured.frontend_port == 3017


@pytest.mark.asyncio
async def test_frontend_start_uses_private_root_and_per_run_backend_target(tmp_path):
    runtime = E2ERuntime(
        dataclasses.replace(
            make_config(tmp_path),
            frontend_enabled=True,
            frontend_port=3017,
        )
    )
    started: list[tuple[str, list[str], str, dict[str, str] | None]] = []
    waited: list[tuple[str, str]] = []

    async def fake_spawn(
        name: str,
        command: list[str],
        log_name: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> None:
        started.append((name, command, log_name, environment))

    async def fake_wait(label: str, url: str, _predicate) -> bytes:
        waited.append((label, url))
        return b""

    runtime._spawn_host_process = fake_spawn  # type: ignore[method-assign]
    runtime._wait_http = fake_wait  # type: ignore[method-assign]

    await runtime._start_frontend()

    assert len(started) == 1
    name, command, log_name, environment = started[0]
    assert name == "frontend"
    assert log_name == "frontend.log"
    assert command == [
        str(REPO_ROOT / "frontend" / "node_modules" / ".bin" / "vite"),
        str(REPO_ROOT / "frontend"),
        "--config",
        str(REPO_ROOT / "frontend" / "vite.config.ts"),
        "--host",
        "127.0.0.1",
        "--port",
        "3017",
        "--strictPort",
    ]
    assert environment == {
        "AKB_FRONTEND_BACKEND_URL": "http://127.0.0.1:8000",
        "AKB_FRONTEND_CACHE_DIR": str(tmp_path / "runtime" / "frontend-cache"),
    }
    assert waited == [("frontend", "http://127.0.0.1:3017")]


@pytest.mark.asyncio
async def test_cleanup_stops_frontend_with_the_owned_runtime_processes(tmp_path):
    runtime = E2ERuntime(
        dataclasses.replace(make_config(tmp_path), frontend_enabled=True)
    )
    runtime._children["frontend"] = ManagedProcess(_LifecycleProcess())
    stopped: list[str] = []

    async def record_stop(name: str) -> None:
        stopped.append(name)

    async def record_compose(*_arguments: str, **_kwargs: object) -> int:
        return 0

    runtime._stop_fixture_control = lambda: asyncio.sleep(0)  # type: ignore[method-assign]
    runtime._stop_named_process = record_stop  # type: ignore[method-assign]
    runtime._compose = record_compose  # type: ignore[method-assign]

    await runtime.cleanup()

    assert stopped == ["stdio", "frontend", "backend", "embed"]


def test_fixture_discovery_declares_auth_and_observability_without_secrets(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("TEST_USERNAME_ENV", "external-user-value")
    monkeypatch.setenv("TEST_PASSWORD_ENV", "external-password-value")
    runtime = E2ERuntime(make_config(tmp_path))
    runtime._fixture_catalog = {
        "status": "ready",
        "scenario": "app-installation-lifecycle",
        "namespace": "fixture-randomized",
        "fixtures": {
            "active": {"installation_id": "installation-randomized"},
        },
    }

    discovery = runtime.fixture_discovery()

    assert discovery["scenario"] == "app-installation-lifecycle"
    assert discovery["namespace"] == "fixture-randomized"
    assert discovery["fixtures"] == {
        "active": {"installation_id": "installation-randomized"},
    }
    assert discovery["access"] == {
        "login": {
            "service": "app",
            "method": "POST",
            "path": "/api/v1/auth/login",
            "fields": ["username", "password"],
        }
    }
    assert discovery["observability"] == {
        "log_observation": {
            "service": "fixture",
            "method": "GET",
            "path": "/log-observation",
        }
    }
    serialized = json.dumps(discovery)
    assert "external-user-value" not in serialized
    assert "external-password-value" not in serialized


def test_installation_discovery_publishes_success_lifecycle_command_bodies(tmp_path):
    runtime = E2ERuntime(
        dataclasses.replace(
            make_config(tmp_path), scenario="app-installation-lifecycle"
        )
    )
    runtime._fixture_catalog = {
        "status": "ready",
        "scenario": "app-installation-lifecycle",
        "commands": {
            "restore_compatible": {
                "app_id": "target-app",
                "vault_id": "restore-vault",
                "release_id": "release-primary",
                "capabilities": ["installation:read", "inventory:read"],
                "mode": "restore",
                "request": {
                    "service": "app",
                    "method": "PUT",
                    "path": "/api/v1/apps/target-app/installations/restore-vault",
                    "body": {
                        "release_id": "release-primary",
                        "capabilities": ["installation:read", "inventory:read"],
                        "mode": "restore",
                    },
                },
            },
            "fresh_empty": {
                "app_id": "target-app",
                "vault_id": "fresh-vault",
                "release_id": "release-next",
                "capabilities": ["installation:read", "inventory:read"],
                "mode": "fresh",
                "request": {
                    "service": "app",
                    "method": "PUT",
                    "path": "/api/v1/apps/target-app/installations/fresh-vault",
                    "body": {
                        "release_id": "release-next",
                        "capabilities": ["installation:read", "inventory:read"],
                        "mode": "fresh",
                    },
                },
            },
        },
    }

    discovery = runtime.fixture_discovery()

    assert discovery["commands"] == runtime._fixture_catalog["commands"]


def test_scenario_argument_supports_the_lifecycle_fixture():
    config = _parse_args(["serve", "--scenario", "empty"])
    assert config.scenario == "empty"
    lifecycle = _parse_args(["serve", "--scenario", "app-installation-lifecycle"])
    assert lifecycle.scenario == "app-installation-lifecycle"
    rollout = _parse_args(["serve", "--scenario", "app-release-rollout"])
    assert rollout.scenario == "app-release-rollout"
    control_plane = _parse_args(["serve", "--scenario", "app-control-plane"])
    assert control_plane.scenario == "app-control-plane"
    with pytest.raises(SystemExit):
        _parse_args(["serve", "--scenario", "project"])


def test_app_control_plane_descriptor_keeps_schema_v2_discovery_contract(tmp_path):
    runtime = E2ERuntime(
        dataclasses.replace(make_config(tmp_path), scenario="app-control-plane")
    )
    runtime._fixture_catalog = {
        "status": "ready",
        "scenario": "app-control-plane",
        "namespace": "fixture-randomized",
        "installations": [{"fixture_id": "target-00", "id": "installation-randomized"}],
        "coordinates": {
            "admin": {
                "registry": {"app_create": {"path": "/api/v1/apps"}},
                "resume": {"method": "POST"},
            },
            "self_app": {
                "resume": {"path": "/api/v1/app/rollouts/{rollout_id}/resume"},
            },
        },
    }
    descriptor = runtime.descriptor()
    discovery = runtime.fixture_discovery()
    assert descriptor["schema_version"] == 2
    assert descriptor["scenario"] == "app-control-plane"
    assert descriptor["services"]["fixture"]["reset"]["body"] == {
        "scenario": "app-control-plane"
    }
    assert discovery["scenario"] == "app-control-plane"
    assert discovery["controls"]["fault_injection"]["path"] == "/control"
    assert discovery["coordinates"]["admin"]["registry"]["app_create"]["path"] == "/api/v1/apps"
    assert discovery["coordinates"]["admin"]["resume"]["method"] == "POST"
    assert discovery["coordinates"]["self_app"]["resume"]["path"] == "/api/v1/app/rollouts/{rollout_id}/resume"


def test_app_control_plane_discovery_exposes_legacy_adoption_target_and_drift_control(tmp_path):
    runtime = E2ERuntime(
        dataclasses.replace(make_config(tmp_path), scenario="app-control-plane")
    )
    runtime._fixture_catalog = {
        "status": "ready",
        "scenario": "app-control-plane",
        "fixtures": {
            "legacy_adoption": {
                "fixture_id": "legacy-adoption",
                "vault_id": "vault-legacy",
                "before": {"row_count": 3},
                "after": {"grant_generation": 0},
            }
        },
    }

    discovery = runtime.fixture_discovery()
    control = discovery["controls"]["fault_injection"]
    assert "legacy_schema_drift" in control["kinds"]
    assert {
        "fixture_id": "legacy-adoption",
        "vault_id": "vault-legacy",
        "target_type": "legacy_adoption",
    } in control["targets"]
    assert discovery["fixtures"]["legacy_adoption"]["before"]["row_count"] == 3
    assert discovery["fixtures"]["legacy_adoption"]["after"]["grant_generation"] == 0


def test_suite_sql_uses_compose_psql_by_default_and_preserves_override(tmp_path, monkeypatch):
    runtime = E2ERuntime(make_config(tmp_path))
    assert runtime._child_environment()["PYTHONFAULTHANDLER"] == "1"
    monkeypatch.delenv("AKB_PG_EXEC", raising=False)
    child_env = runtime._child_environment()
    assert child_env["AKB_PG_EXEC"].endswith("exec -T postgres")
    assert child_env["AKB_E2E_POSTGRES_PORT"] == "15432"
    assert child_env["AKB_E2E_MINIO_PORT"] == "9000"

    monkeypatch.setenv("AKB_PG_EXEC", "custom-pg-command")
    assert runtime._child_environment()["AKB_PG_EXEC"] == "custom-pg-command"


def test_runtime_uses_selected_dependency_ports_consistently(tmp_path):
    runtime = E2ERuntime(
        dataclasses.replace(
            make_config(tmp_path),
            postgres_port=25432,
            minio_port=29000,
        )
    )
    prepare_private_runtime_root(runtime.config.runtime_root)

    runtime._write_config()

    child_env = runtime._child_environment()
    app_config = yaml.safe_load(
        (runtime.config.config_dir / "app.yaml").read_text(encoding="utf-8")
    )
    assert child_env["AKB_E2E_POSTGRES_PORT"] == "25432"
    assert child_env["AKB_E2E_MINIO_PORT"] == "29000"
    assert app_config["db_port"] == 25432
    assert app_config["s3_endpoint_url"] == "http://127.0.0.1:29000"
    assert app_config["s3_public_url"] == "http://127.0.0.1:29000"


def test_runtime_root_is_private_and_separate(tmp_path):
    root = tmp_path / "private-runtime"
    config_dir, logs_dir, state_dir, vault_dir = prepare_private_runtime_root(root)

    assert {config_dir, logs_dir, state_dir, vault_dir} <= set(root.rglob("*"))
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    for directory in (config_dir, logs_dir, state_dir, vault_dir):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_runtime_configures_local_auth_mode_keys_and_distinct_app_token_secret(tmp_path):
    runtime = E2ERuntime(make_config(tmp_path))
    prepare_private_runtime_root(runtime.config.runtime_root)

    runtime._write_config()

    app_config = yaml.safe_load(
        (runtime.config.config_dir / "app.yaml").read_text(encoding="utf-8")
    )
    secret_config = yaml.safe_load(
        (runtime.config.config_dir / "secret.yaml").read_text(encoding="utf-8")
    )
    assert app_config["auth_mode"] == "local"
    assert app_config["jwt_algorithm"] == "RS256"
    assert Path(app_config["local_session_private_key_path"]).is_file()
    assert Path(app_config["local_session_jwks_path"]).is_file()
    assert secret_config["app_token_secret"]
    assert secret_config["app_token_secret"] != secret_config["system_hmac_secret"]


def test_suite_summary_uses_last_complete_line_and_fails_closed():
    output = "Results: 1 passed, 0 failed\nnoise\nResults: 3 passed, 1 failed\n"
    assert parse_assertion_summary(output) == (3, 1, "Results: 3 passed, 1 failed")
    assert parse_assertion_summary("Results: 5 passed") is None
    assert SuiteResult("suite.sh", 0, 0, 0, None, "").gate_failed
    assert SuiteResult("suite.sh", 0, 2, 0, "Results: 2 passed, 0 failed", "").gate_failed is False
    assert SuiteResult("suite.sh", -4, 0, 0, None, "").signal == 4
    assert len(CURATED_SUITES) == 26


def test_shell_e2e_manifest_classifies_every_suite_exactly_once():
    assert validate_suite_manifest(REPO_ROOT) == ()
    discovered = {
        path.name for path in (REPO_ROOT / "backend" / "tests").glob("*_e2e.sh")
    }
    deferred = {
        suite for group in DEFERRED_SUITE_GROUPS for suite in group.suites
    }
    assert discovered == set(CURATED_SUITES) | deferred
    assert set(CURATED_SUITES).isdisjoint(deferred)


def test_shell_e2e_manifest_reports_unclassified_overlap_and_missing(
    tmp_path, monkeypatch
):
    tests_dir = tmp_path / "backend" / "tests"
    tests_dir.mkdir(parents=True)
    for name in ("test_run_e2e.sh", "test_defer_e2e.sh", "test_new_e2e.sh"):
        (tests_dir / name).touch()

    monkeypatch.setattr(
        e2e_suite_runner,
        "CURATED_SUITES",
        ("test_run_e2e.sh", "test_overlap_e2e.sh"),
    )
    monkeypatch.setattr(
        e2e_suite_runner,
        "DEFERRED_SUITE_GROUPS",
        (
            DeferredSuiteGroup(
                reason="fixture",
                suites=("test_defer_e2e.sh", "test_overlap_e2e.sh"),
            ),
        ),
    )

    errors = validate_suite_manifest(tmp_path)
    assert any(
        "unclassified E2E suites" in error and "test_new_e2e.sh" in error
        for error in errors
    )
    assert any(
        "both curated and deferred" in error and "test_overlap_e2e.sh" in error
        for error in errors
    )
    assert any(
        "without a matching suite file" in error and "test_overlap_e2e.sh" in error
        for error in errors
    )


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
    monkeypatch.setattr(e2e_suite_runner, "check_suite_manifest", lambda _repo_root: 0)
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
    mcp_pytest_path = checkout / "backend" / "tests" / "mcp_e2e"
    mcp_pytest_path.mkdir(parents=True)
    (mcp_pytest_path / "conftest.py").write_text(
        "def pytest_addoption(parser):\n"
        "    parser.addoption('--runtime-descriptor')\n",
        encoding="utf-8",
    )
    (mcp_pytest_path / "test_fake.py").write_text(
        "def test_fake():\n"
        "    pass\n",
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
        return {
            "status": "ready",
            "scenario": self.scenario,
            "fixtures": {},
            "access": {
                "login": {
                    "service": "app",
                    "method": "POST",
                    "path": "/api/v1/auth/login",
                    "fields": ["username", "password"],
                }
            },
            "observability": {
                "log_observation": {
                    "service": "fixture",
                    "method": "GET",
                    "path": "/log-observation",
                }
            },
        }

    def fixture_log_observation(self):
        return {
            "status": "ready",
            "scenario": self.scenario,
            "redacted": True,
            "redaction_scan": {"private_value_hits": 0, "raw_log_exposed": False},
        }

    def fixture_control(self, action, target, enabled, kind=None):
        return {
            "status": "accepted",
            "scenario": self.scenario,
            "action": action,
            "enabled": enabled,
            "kind": kind,
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
        "/control",
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
        assert discovery.json()["access"]["login"] == {
            "service": "app",
            "method": "POST",
            "path": "/api/v1/auth/login",
            "fields": ["username", "password"],
        }
        assert discovery.json()["observability"]["log_observation"] == {
            "service": "fixture",
            "method": "GET",
            "path": "/log-observation",
        }
        log_observation = await client.get("/log-observation")
        assert log_observation.json()["redaction_scan"]["private_value_hits"] == 0
        reset = await client.post("/reset", json={"scenario": "empty"})
        assert reset.status_code == 200
        assert runtime.reset_count == 1
        control = await client.post(
            "/control",
            json={"action": "restart", "enabled": True},
        )
        assert control.status_code == 200
        assert control.json()["scenario"] == "empty"
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


@pytest.mark.asyncio
async def test_control_plane_reset_discovery_serializes_success_installation_commands(tmp_path):
    runtime = E2ERuntime(
        RuntimeConfig(
            checkout=REPO_ROOT,
            runtime_root=tmp_path / "runtime",
            mode="serve",
            compose_file=COMPOSE_FILE,
            compose_project="akb-e2e-control-plane-discovery-unit",
            scenario="app-control-plane",
        )
    )
    runtime._prepared = True
    app_id = "11111111-1111-4111-8111-111111111111"
    restore_vault_id = "22222222-2222-4222-8222-222222222222"
    fresh_vault_id = "33333333-3333-4333-8333-333333333333"
    restore_release_id = "44444444-4444-4444-8444-444444444444"
    fresh_release_id = "55555555-5555-4555-8555-555555555555"

    async def reset_scenario() -> None:
        runtime._fixture_catalog = {
            "status": "ready",
            "scenario": "app-control-plane",
        }
        runtime._publish_installation_lifecycle_commands(
            app_id=app_id,
            restore_vault_id=restore_vault_id,
            restore_release_id=restore_release_id,
            fresh_vault_id=fresh_vault_id,
            fresh_release_id=fresh_release_id,
        )

    runtime.reset_scenario = reset_scenario  # type: ignore[method-assign]
    app = create_app(runtime)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control-plane-fixture.test"
    ) as client:
        reset = await client.post("/reset", json={"scenario": "app-control-plane"})
        assert reset.status_code == 200
        discovery = await client.get("/discover")

    commands = discovery.json()["commands"]
    assert commands["restore_compatible"] == {
        "app_id": app_id,
        "vault_id": restore_vault_id,
        "release_id": restore_release_id,
        "capabilities": ["installation:read", "inventory:read"],
        "mode": "restore",
        "request": {
            "service": "app",
            "method": "PUT",
            "path": f"/api/v1/apps/{app_id}/installations/{restore_vault_id}",
            "body": {
                "release_id": restore_release_id,
                "capabilities": ["installation:read", "inventory:read"],
                "mode": "restore",
            },
        },
    }
    assert commands["fresh_empty"]["mode"] == "fresh"
    assert commands["fresh_empty"]["request"]["body"] == {
        "release_id": fresh_release_id,
        "capabilities": ["installation:read", "inventory:read"],
        "mode": "fresh",
    }


class _LifecycleProcess:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.returncode: int | None = None

    async def wait(self) -> int:
        await self.release.wait()
        self.returncode = 0
        return 0


@pytest.mark.asyncio
async def test_serve_ignores_process_exit_after_reset_replaces_the_process(tmp_path):
    runtime = E2ERuntime(make_config(tmp_path))
    old_process = _LifecycleProcess()
    new_process = _LifecycleProcess()
    runtime._children["backend"] = ManagedProcess(old_process)  # type: ignore[arg-type]

    serve_task = asyncio.create_task(runtime._serve_foreground())
    await asyncio.sleep(0)

    runtime._resetting = True
    old_process.release.set()
    runtime._children["backend"] = ManagedProcess(new_process)  # type: ignore[arg-type]
    runtime._resetting = False
    for _ in range(3):
        await asyncio.sleep(0)

    assert not serve_task.done()
    runtime.request_stop()
    assert await asyncio.wait_for(serve_task, timeout=1) == 0
    assert runtime._children["backend"].process is new_process


@pytest.mark.asyncio
async def test_restart_requested_during_reset_is_deferred_until_reset_finishes(tmp_path):
    runtime = E2ERuntime(make_config(tmp_path))
    runtime._lifecycle_generation = 4
    calls: list[str] = []

    async def record_stop(_name: str) -> None:
        calls.append("stop")
        runtime._children.pop("backend", None)

    async def record_start() -> None:
        calls.append("start")
        runtime._children["backend"] = ManagedProcess(_LifecycleProcess())

    runtime._stop_named_process = record_stop  # type: ignore[method-assign]
    runtime._start_backend = record_start  # type: ignore[method-assign]

    async with runtime._reset_lock:
        runtime._resetting = True
        result = await runtime.fixture_control("restart", None, True)
        await asyncio.sleep(0)
        assert calls == []
        runtime._resetting = False

    for _ in range(3):
        await asyncio.sleep(0)

    assert result["status"] == "accepted"
    assert calls == ["stop", "start"]
    assert runtime.app_ready is True


def test_rollout_fixture_discovery_is_generic_and_redacted(tmp_path):
    runtime = E2ERuntime(
        RuntimeConfig(
            checkout=REPO_ROOT,
            runtime_root=tmp_path / "runtime",
            mode="serve",
            compose_file=COMPOSE_FILE,
            compose_project="akb-e2e-rollout-unit",
            scenario="app-release-rollout",
        )
    )
    descriptor = runtime.descriptor()
    assert descriptor["schema_version"] == 2
    assert descriptor["status"] == "ready"
    assert descriptor["scenario"] == "app-release-rollout"
    runtime._fixture_private_values = ("private-credential", "private-marker")
    runtime._fixture_catalog = {
        "status": "ready",
        "scenario": "app-release-rollout",
        "namespace": "fixture-randomized",
        "apps": {"target": {"id": "target-app"}, "foreign": {"id": "foreign-app"}},
        "installations": [{"fixture_id": "target-00", "id": "target-installation"}],
        "coordinates": {
            "admin": {"request": {"method": "POST", "path": "/api/v1/apps/{app_id}/rollouts"}},
            "self_app": {"request": {"method": "POST", "path": "/api/v1/app/rollouts"}},
        },
        "controls": {"fault_injection": {"method": "POST", "path": "/control"}},
        "secret": "private-marker-suffix",  # pragma: allowlist secret
    }
    discovery = runtime.fixture_discovery()
    assert discovery["scenario"] == "app-release-rollout"
    assert len(discovery["installations"]) >= 1
    assert "private-marker" not in json.dumps(discovery)
    # Fixture discovery must never include runtime values, even if test setup
    # accidentally placed one in its private catalog.
    assert "private-credential" not in json.dumps(discovery)
    assert discovery["coordinates"]["admin"]["request"]["method"] == "POST"
    assert discovery["controls"]["fault_injection"]["body"]["kind"] == "missing_owned_table"


def test_rollout_discovery_advertises_same_coordinates_for_each_app(tmp_path):
    runtime = E2ERuntime(
        RuntimeConfig(
            checkout=REPO_ROOT,
            runtime_root=tmp_path / "runtime",
            mode="serve",
            compose_file=COMPOSE_FILE,
            compose_project="akb-e2e-rollout-app-coordinates-unit",
            scenario="app-release-rollout",
        )
    )
    runtime._fixture_catalog = {
        "status": "ready",
        "scenario": "app-release-rollout",
        "apps": {
            "target": {
                "id": "target-app",
                "rollout": {
                    "release_id": "target-release",
                    "manifest_checksum": "a" * 64,
                    "request": {"method": "POST", "path": "/api/v1/apps/target-app/rollouts"},
                    "status": {"method": "GET", "path": "/api/v1/apps/target-app/rollouts/{rollout_id}"},
                },
            },
            "foreign": {
                "id": "foreign-app",
                "rollout": {
                    "release_id": "foreign-release",
                    "manifest_checksum": "b" * 64,
                    "request": {"method": "POST", "path": "/api/v1/apps/foreign-app/rollouts"},
                    "status": {"method": "GET", "path": "/api/v1/apps/foreign-app/rollouts/{rollout_id}"},
                },
            },
        },
    }

    discovery = runtime.fixture_discovery()
    apps = discovery["apps"]
    assert set(apps) == {"target", "foreign"}
    assert {item["rollout"]["request"]["method"] for item in apps.values()} == {"POST"}
    assert {item["rollout"]["status"]["method"] for item in apps.values()} == {"GET"}
    assert {item["rollout"]["release_id"] for item in apps.values()} == {
        "target-release",
        "foreign-release",
    }


def test_rollout_fault_control_discovery_uses_fixture_ids_and_kinds(tmp_path):
    runtime = E2ERuntime(
        RuntimeConfig(
            checkout=REPO_ROOT,
            runtime_root=tmp_path / "runtime",
            mode="serve",
            compose_file=COMPOSE_FILE,
            compose_project="akb-e2e-rollout-control-unit",
            scenario="app-release-rollout",
        )
    )
    runtime._fixture_catalog = {
        "status": "ready",
        "scenario": "app-release-rollout",
        "apps": {"target": {"id": "target-app"}},
        "installations": [
            {"fixture_id": f"target-{index:02d}", "id": f"installation-{index}"}
            for index in range(13)
        ],
    }

    discovery = runtime.fixture_discovery()
    fault = discovery["controls"]["fault_injection"]
    assert fault["kinds"] == ["missing_owned_table"]
    assert fault["body"] == {
        "action": "fault_injection",
        "kind": "missing_owned_table",
        "target": "target-00",
        "enabled": True,
    }
    assert {item["fixture_id"] for item in fault["targets"]} == {
        f"target-{index:02d}" for index in range(13)
    }
    assert runtime._resolve_fault_target("target-00")["id"] == "installation-0"
    assert runtime._resolve_fault_target("target-11")["id"] == "installation-11"
    with pytest.raises(ValueError):
        runtime._resolve_fault_target("target-13")


@pytest.mark.asyncio
async def test_rollout_fault_control_applies_before_ack(tmp_path, monkeypatch):
    runtime = E2ERuntime(
        RuntimeConfig(
            checkout=REPO_ROOT,
            runtime_root=tmp_path / "runtime",
            mode="serve",
            compose_file=COMPOSE_FILE,
            compose_project="akb-e2e-rollout-control-ack-unit",
            scenario="app-release-rollout",
        )
    )
    runtime._fixture_catalog = {
        "status": "ready",
        "scenario": "app-release-rollout",
        "installations": [
            {
                "fixture_id": f"target-{index:02d}",
                "id": f"installation-{index}",
                "vault_id": f"vault-{index}",
            }
            for index in range(13)
        ],
    }
    applied: list[tuple[str, str]] = []

    async def record_application(fixture: dict[str, object], kind: str) -> bool:
        applied.append((str(fixture["fixture_id"]), kind))
        return True

    monkeypatch.setattr(runtime, "_apply_fault", record_application)
    result = await runtime.fixture_control(
        "fault_injection", "target-11", True, "missing_owned_table"
    )

    assert result["status"] == "accepted"
    assert applied == [("target-11", "missing_owned_table")]
    assert runtime._fixture_controls["fault"] == {
        "target": "target-11",
        "kind": "missing_owned_table",
    }


@pytest.mark.asyncio
async def test_rollout_fault_disable_restores_fixture_before_ack(tmp_path, monkeypatch):
    runtime = E2ERuntime(
        RuntimeConfig(
            checkout=REPO_ROOT,
            runtime_root=tmp_path / "runtime",
            mode="serve",
            compose_file=COMPOSE_FILE,
            compose_project="akb-e2e-rollout-control-recovery-unit",
            scenario="app-release-rollout",
        )
    )
    runtime._fixture_catalog = {
        "status": "ready",
        "scenario": "app-release-rollout",
        "installations": [
            {
                "fixture_id": "target-00",
                "id": "installation-0",
                "vault_id": "vault-0",
            }
        ],
    }
    runtime._fixture_controls["fault"] = {
        "target": "target-00",
        "kind": "missing_owned_table",
    }
    restored: list[tuple[str, str]] = []

    async def record_restore(fixture: dict[str, object], kind: str) -> bool:
        restored.append((str(fixture["fixture_id"]), kind))
        return True

    monkeypatch.setattr(runtime, "_restore_fault", record_restore)
    result = await runtime.fixture_control(
        "fault_injection", "target-00", False, "missing_owned_table"
    )

    assert result["status"] == "accepted"
    assert result["enabled"] is False
    assert restored == [("target-00", "missing_owned_table")]
    assert result["observed"]["fault_injection"] is None


def test_compose_and_hosted_workflow_preserve_the_live_topology():
    compose = yaml.safe_load(COMPOSE_FILE.read_text())
    assert set(compose["services"]) == {"postgres", "minio"}
    assert compose["services"]["postgres"]["image"] == "pgvector/pgvector:pg16"
    assert compose["services"]["minio"]["image"] == (
        "minio/minio:RELEASE.2025-09-07T16-13-09Z"
    )
    assert compose["services"]["postgres"]["ports"] == [
        "${AKB_E2E_POSTGRES_PORT:-15432}:5432"
    ]
    assert compose["services"]["minio"]["ports"] == [
        "${AKB_E2E_MINIO_PORT:-9000}:9000"
    ]

    workflow = WORKFLOW.read_text()
    assert "backend/scripts/ci/e2e_runtime.py gate" in workflow
    assert "--scenario empty" in workflow
    assert "app-installation-lifecycle" in (CI_DIR / "e2e_runtime.py").read_text()
    assert "uv sync --locked --extra dev --project backend" in workflow
    assert "services:" not in workflow
    for suite in CURATED_SUITES:
        assert suite not in workflow
    assert "akb-e2e-runtime-logs" in workflow

    local_runner = LOCAL_CANONICAL_RUNNER.read_text()
    assert "backend/scripts/ci/e2e_suite_runner.py" in local_runner
    assert "SUITES=(" not in local_runner


def test_ubuntu_bootstrap_is_bash_safe_and_keeps_descriptor_stdout_clean():
    result = subprocess.run(["bash", "-n", str(BOOTSTRAP)], check=False)
    assert result.returncode == 0
    text = BOOTSTRAP.read_text()
    assert "exec 3>&1 1>&2" in text
    assert "--scenario empty" in text
    assert "app-installation-lifecycle" in text
    assert "--with-frontend" in text
    assert "pnpm install --frozen-lockfile" in text
    assert "node@22.19.0 pnpm@11.21.0" in text
    assert '"${SUDO[@]}" npm install --global --prefix /usr/local node@22.19.0 pnpm@11.21.0' in text
    assert "command -v pnpm" in text
    assert 'apt-get install -y nodejs npm' in text
    assert 'command -v node' in text
    assert 'command -v npm' in text
    assert "Node.js/npm package installation failed" in text
    assert "uv sync --locked" in text
    assert "exec 1>&3 3>&-" in text
    assert stat.S_IMODE(BOOTSTRAP.stat().st_mode) & stat.S_IXUSR


def test_capability_profiles_are_explicit_and_composable():
    assert select_capability_profile("tool-only") == CapabilityProfile(
        "tool-only", frozenset({"http", "pat"})
    )
    assert select_capability_profile("transport-proxy").needs_stdio
    assert select_capability_profile("oidc-resource-server").needs_oidc
    combined = select_capability_profile("tool-only", ("stdio", "oidc"))
    assert combined.name == "transport-oidc"
    assert combined.capabilities == frozenset({"http", "pat", "stdio", "oidc"})


def test_tool_only_does_not_advertise_optional_processes(tmp_path):
    runtime = E2ERuntime(make_config(tmp_path))
    descriptor = runtime.descriptor()
    assert set(descriptor["services"]) == {"app", "fixture"}
    assert "profile" not in descriptor
    discovery = runtime.fixture_discovery()
    assert discovery["runtime"]["selected_capabilities"] == ["http", "pat"]
    assert "stdio" not in discovery["runtime"]
    assert "oidc" not in discovery["runtime"]


def test_transport_profile_exposes_real_proxy_boundary_without_secret(tmp_path):
    runtime = E2ERuntime(
        dataclasses.replace(make_config(tmp_path), profile="transport-proxy")
    )
    runtime._pat_value = "akb_runtime_secret"  # pragma: allowlist secret
    descriptor = runtime.descriptor()
    assert descriptor["services"]["stdio"]["transport"] == "stdio"
    assert descriptor["credentials"]["pat_env"] == "AKB_E2E_PAT"
    serialized = json.dumps(descriptor)
    assert "akb_runtime_secret" not in serialized
    assert "AKB_E2E_PAT" in serialized
    assert descriptor["evidence"]["transport"] == ["http", "stdio"]


@pytest.mark.asyncio
async def test_oidc_profile_serves_jwks_metadata_and_deterministic_variants(tmp_path):
    runtime = E2ERuntime(
        dataclasses.replace(make_config(tmp_path), profile="oidc-resource-server")
    )
    app = create_app(runtime)
    jwks_path = runtime.oidc_fixture.jwks_uri.removeprefix(runtime.config.fixture_origin)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=runtime.config.fixture_origin
    ) as client:
        health = await client.get("/oidc/health")
        metadata = await client.get("/.well-known/openid-configuration")
        jwks = await client.get(jwks_path)
        token = await client.post("/oidc/token", json={"variant": "valid"})
        bad = await client.post("/oidc/token", json={"variant": "wrong_issuer"})
    assert health.status_code == 200
    assert metadata.json()["issuer"] == runtime.oidc_fixture.issuer
    assert jwks.json()["keys"][0]["alg"] == "RS256"
    assert token.status_code == 200 and token.json()["token_type"] == "Bearer"
    assert bad.status_code == 200 and bad.json()["access_token"] != token.json()["access_token"]
    assert metadata.json()["scopes_supported"] == ["akb:vault:read", "akb:vault:write"]
    assert set(runtime.oidc_fixture.discovery()["token_variants"]) == {
        "valid",
        "wrong_issuer",
        "wrong_audience",
        "expired",
        "wrong_algorithm",
        "wrong_key_id",
        "insufficient_scope",
    }
    assert "challenge_cases" in runtime.oidc_fixture.discovery()
    assert "access_token" not in json.dumps(runtime.fixture_discovery())


def test_optional_capabilities_fail_closed_without_silent_skip(tmp_path, monkeypatch):
    runtime = E2ERuntime(
        dataclasses.replace(make_config(tmp_path), profile="keycloak-overlay")
    )
    with pytest.raises(BlockedRuntimeConfig, match="blocked_runtime_config"):
        runtime._validate_profile()

    transport = E2ERuntime(
        dataclasses.replace(make_config(tmp_path), profile="transport-proxy")
    )
    monkeypatch.setattr(
        "e2e_runtime.shutil.which",
        lambda name: None if name == "node" else "/usr/bin/npm",
    )
    with pytest.raises(BlockedRuntimeConfig, match="stdio capability"):
        transport._validate_profile()
