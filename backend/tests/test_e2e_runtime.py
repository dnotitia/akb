"""Focused, database-free checks for the repository-owned E2E runtime."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import signal
import sys
import types
from pathlib import Path

import pytest

from scripts.ci import e2e_runtime


def test_default_settings_keep_the_existing_ci_database_and_origins() -> None:
    settings = e2e_runtime.resolve_settings([], {})

    assert settings.database_url == e2e_runtime.DEFAULT_DATABASE_URL
    assert settings.origin == "http://localhost:8000"
    assert settings.fixture_origin == "http://localhost:8889"
    assert settings.scenario == "empty"


def test_command_arguments_override_runtime_environment(tmp_path: Path) -> None:
    settings = e2e_runtime.resolve_settings(
        ["--scenario", "empty", "--ready-file", str(tmp_path / "ready.json")],
        {
            "AKB_E2E_DATABASE_URL": "postgresql://runner:" + "ci-passphrase@127.0.0.1:15432/e2e",
            "AKB_E2E_ORIGIN": "http://127.0.0.1:18000",
            "AKB_E2E_FIXTURE_ORIGIN": "http://127.0.0.1:18889",
            "AKB_E2E_SCENARIO": "empty",
        },
    )

    assert settings.ready_file == (tmp_path / "ready.json").resolve()
    assert settings.origin == "http://127.0.0.1:18000"
    assert settings.fixture_origin == "http://127.0.0.1:18889"
    assert e2e_runtime.parse_database_url(settings.database_url).password == "ci-passphrase"  # pragma: allowlist secret
    assert settings.run_suites is False


def test_run_suites_flag_is_explicit() -> None:
    settings = e2e_runtime.resolve_settings(["--run-suites"], {})

    assert settings.run_suites is True


def test_managed_settings_resolve_safe_project_and_docker_argv() -> None:
    settings = e2e_runtime.resolve_settings(
        ["--manage-postgres"],
        {
            "AKB_E2E_COMPOSE_PROJECT": "akb-e2e-unit",
            "AKB_E2E_DOCKER_ARGV": "sudo -n docker",
        },
    )

    assert settings.manage_postgres is True
    assert settings.compose_project == "akb-e2e-unit"
    assert settings.docker_argv == ("sudo", "-n", "docker")


def test_managed_settings_reject_unsafe_project() -> None:
    with pytest.raises(ValueError, match="compose project"):
        e2e_runtime.resolve_settings(
            ["--manage-postgres"],
            {"AKB_E2E_COMPOSE_PROJECT": "unsafe/project"},
        )


def test_supported_scenarios_include_app_lifecycle_and_reject_unknown() -> None:
    settings = e2e_runtime.resolve_settings(["--scenario", "app_lifecycle"], {})

    assert settings.scenario == e2e_runtime.APP_LIFECYCLE_SCENARIO
    assert e2e_runtime.APP_LIFECYCLE_SCENARIO in e2e_runtime.SUPPORTED_SCENARIOS
    with pytest.raises(ValueError, match="unsupported"):
        e2e_runtime.resolve_settings(["--scenario", "other"], {})


def test_app_lifecycle_credentials_use_project_neutral_names() -> None:
    assert e2e_runtime.CREDENTIAL_VARIABLES == (
        "AKB_E2E_SYSTEM_ADMIN_TOKEN",
        "AKB_E2E_TARGET_VAULT_ADMIN_TOKEN",
        "AKB_E2E_READER_TOKEN",
        "AKB_E2E_WRITER_TOKEN",
        "AKB_E2E_FOREIGN_VAULT_ADMIN_TOKEN",
        "AKB_E2E_PRIMARY_APP_CREDENTIAL",
        "AKB_E2E_PRIMARY_APP_TOKEN",
        "AKB_E2E_FOREIGN_APP_CREDENTIAL",
    )
    assert all(name.startswith("AKB_E2E_") for name in e2e_runtime.CREDENTIAL_VARIABLES)


def test_empty_scenario_keeps_the_original_ready_and_reset_shapes(tmp_path: Path) -> None:
    settings = e2e_runtime.RuntimeSettings(
        database_url=e2e_runtime.DEFAULT_DATABASE_URL,
        ready_file=tmp_path / "ready.json",
    )

    assert e2e_runtime.ready_payload(settings)["scenario"] == "empty"
    assert set(e2e_runtime.ready_payload(settings)) == {
        "schema_version",
        "status",
        "origin",
        "fixture_origin",
        "reset_url",
        "scenario",
    }
    assert e2e_runtime.reset_payload(settings) == {"ok": True, "scenario": "empty"}


def test_app_lifecycle_ready_and_reset_expose_only_safe_artifact_metadata(tmp_path: Path) -> None:
    settings = e2e_runtime.RuntimeSettings(
        database_url=e2e_runtime.DEFAULT_DATABASE_URL,
        scenario=e2e_runtime.APP_LIFECYCLE_SCENARIO,
        ready_file=tmp_path / "ready.json",
    )

    ready = e2e_runtime.ready_payload(settings)
    reset = e2e_runtime.reset_payload(settings)
    manifest_path, profile_path = e2e_runtime.fixture_artifact_paths(settings)

    assert ready["fixture_manifest"] == str(manifest_path)
    assert ready["credential_profile"] == str(profile_path)
    assert ready["credential_variables"] == list(e2e_runtime.CREDENTIAL_VARIABLES)
    assert reset["fixture_manifest"] == str(manifest_path)
    assert reset["credential_profile"] == str(profile_path)
    assert e2e_runtime.DEFAULT_DATABASE_URL not in json.dumps(ready)
    assert e2e_runtime.DEFAULT_DATABASE_URL not in json.dumps(reset)


def test_fixture_artifacts_rotate_together_and_keep_credentials_private(tmp_path: Path) -> None:
    settings = e2e_runtime.RuntimeSettings(
        database_url=e2e_runtime.DEFAULT_DATABASE_URL,
        scenario=e2e_runtime.APP_LIFECYCLE_SCENARIO,
        ready_file=tmp_path / "ready.json",
    )
    first_value = "fixture-value-one"
    second_value = "fixture-value-two"
    first_manifest = {"schema_version": 1, "namespace": "first", "id": "first-id"}
    second_manifest = {"schema_version": 1, "namespace": "second", "id": "second-id"}
    first_credentials = {
        name: first_value + name[-1] for name in e2e_runtime.CREDENTIAL_VARIABLES
    }
    second_credentials = {
        name: second_value + name[-1] for name in e2e_runtime.CREDENTIAL_VARIABLES
    }

    e2e_runtime.write_fixture_artifacts(settings, first_manifest, first_credentials)
    manifest_path, profile_path = e2e_runtime.fixture_artifact_paths(settings)
    assert json.loads(manifest_path.read_text())["namespace"] == "first"
    assert first_credentials[e2e_runtime.CREDENTIAL_VARIABLES[1]] in profile_path.read_text()
    assert os.stat(manifest_path).st_mode & 0o777 == 0o600
    assert os.stat(profile_path).st_mode & 0o777 == 0o600

    e2e_runtime.write_fixture_artifacts(settings, second_manifest, second_credentials)
    assert json.loads(manifest_path.read_text())["namespace"] == "second"
    assert second_credentials[e2e_runtime.CREDENTIAL_VARIABLES[1]] in profile_path.read_text()
    assert first_value not in profile_path.read_text()
    assert first_value not in manifest_path.read_text()

    e2e_runtime.remove_fixture_artifacts(settings)
    assert not manifest_path.exists()
    assert not profile_path.exists()


def test_seed_scenario_rotates_app_lifecycle_artifacts_without_db_access(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = e2e_runtime.RuntimeSettings(
        database_url=e2e_runtime.DEFAULT_DATABASE_URL,
        scenario=e2e_runtime.APP_LIFECYCLE_SCENARIO,
        ready_file=tmp_path / "ready.json",
    )
    calls: list[tuple[object, ...]] = []

    async def fake_seed(database_url: str, *, origin: str, fixture_origin: str):
        calls.append((database_url, origin, fixture_origin))
        return {"schema_version": 1, "scenario": "app_lifecycle"}, {
            name: "fixture-value-" + name[-1] for name in e2e_runtime.CREDENTIAL_VARIABLES
        }

    monkeypatch.setattr(e2e_runtime, "_seed_app_lifecycle", fake_seed)
    monkeypatch.setattr(
        e2e_runtime,
        "_exchange_fixture_app_token",
        lambda _origin, _credential: "fixture-app-token",
    )
    manifest, credentials = e2e_runtime.seed_scenario(settings)

    assert calls == [(e2e_runtime.DEFAULT_DATABASE_URL, settings.origin, settings.fixture_origin)]
    assert manifest == {"schema_version": 1, "scenario": "app_lifecycle"}
    assert credentials is not None
    assert credentials[e2e_runtime.CREDENTIAL_VARIABLES[6]] == "fixture-app-token"


def test_app_lifecycle_seed_is_transactional_and_manifest_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeTransaction:
        entered = False

        async def __aenter__(self):
            self.entered = True
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    class FakeConnection:
        def __init__(self) -> None:
            self.transaction_context = FakeTransaction()
            self.statements: list[tuple[str, tuple[object, ...]]] = []
            self.closed = False

        def transaction(self) -> FakeTransaction:
            return self.transaction_context

        async def execute(self, statement: str, *args: object) -> None:
            self.statements.append((statement, args))

        async def close(self) -> None:
            self.closed = True

    connection = FakeConnection()

    async def fake_connect(_database_url: str, *, timeout: int):
        assert timeout == 15
        return connection

    monkeypatch.setitem(
        sys.modules,
        "asyncpg",
        types.SimpleNamespace(connect=fake_connect),
    )
    manifest, credentials = asyncio.run(
        e2e_runtime._seed_app_lifecycle(
            e2e_runtime.DEFAULT_DATABASE_URL,
            origin="http://localhost:8000",
            fixture_origin="http://localhost:8889",
        )
    )

    serialized_manifest = json.dumps(manifest)
    assert connection.transaction_context.entered is True
    assert connection.closed is True
    assert len(connection.statements) >= 30
    assert manifest["scenario"] == e2e_runtime.APP_LIFECYCLE_SCENARIO
    assert set(credentials) == (
        set(e2e_runtime.CREDENTIAL_VARIABLES)
        - {e2e_runtime.CREDENTIAL_VARIABLES[6]}
    )
    assert all(
        credentials[name] not in serialized_manifest
        for name in e2e_runtime.CREDENTIAL_VARIABLES
        if name != e2e_runtime.CREDENTIAL_VARIABLES[6]
    )
    for forbidden_field in (
        "credential_hash",
        "token_hash",
        "password_hash",
        "provenance",
        "grant_id",
    ):
        assert forbidden_field not in serialized_manifest
    assert manifest["actors"]["writer"]["token_env"] == e2e_runtime.CREDENTIAL_VARIABLES[3]  # type: ignore[index]
    assert manifest["apps"]["primary"]["token_env"] == e2e_runtime.CREDENTIAL_VARIABLES[6]  # type: ignore[index]
    assert len(manifest["installations"]) >= 7  # type: ignore[index]


def test_child_environment_does_not_forward_fixture_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    marker = "fixture-value-not-forwarded"
    monkeypatch.setenv(e2e_runtime.CREDENTIAL_VARIABLES[1], marker)
    runtime = e2e_runtime.E2ERuntime(
        e2e_runtime.RuntimeSettings(
            database_url=e2e_runtime.DEFAULT_DATABASE_URL,
            scenario=e2e_runtime.APP_LIFECYCLE_SCENARIO,
            ready_file=tmp_path / "ready.json",
        )
    )

    child_environment = runtime._child_environment()

    assert e2e_runtime.CREDENTIAL_VARIABLES[1] not in child_environment
    assert marker not in child_environment.values()


def test_generated_config_contains_fields_not_the_database_url(tmp_path: Path) -> None:
    database_url = "postgresql://runner:" + "ci-passphrase@db.example.test:15432/e2e"
    settings = e2e_runtime.RuntimeSettings(database_url=database_url, ready_file=tmp_path / "ready.json")

    e2e_runtime.write_runtime_config(settings, tmp_path)
    app_config = (tmp_path / "config" / "app.yaml").read_text()
    secret_config = (tmp_path / "config" / "secret.yaml").read_text()

    assert "db_host: db.example.test" in app_config
    assert "db_port: 15432" in app_config
    assert database_url not in app_config
    assert database_url not in secret_config
    assert os.stat(tmp_path / "config" / "secret.yaml").st_mode & 0o777 == 0o600


def test_optional_s3_config_is_explicit_and_secret_file_stays_private(tmp_path: Path) -> None:
    settings = e2e_runtime.resolve_settings(
        [],
        {
            "AKB_E2E_S3_ENDPOINT": "http://localhost:9000",
            "AKB_E2E_S3_ACCESS_KEY": "local-access",
            "AKB_E2E_S3_SECRET_KEY": "local-value",  # pragma: allowlist secret
        },
    )

    assert settings.s3_bucket == e2e_runtime.DEFAULT_S3_BUCKET
    e2e_runtime.write_runtime_config(settings, tmp_path)
    app_config = (tmp_path / "config" / "app.yaml").read_text()
    secret_config = (tmp_path / "config" / "secret.yaml").read_text()

    assert "s3_endpoint_url: http://localhost:9000" in app_config
    assert "s3_bucket: akb-files" in app_config
    assert "s3_access_key: local-access" in secret_config
    assert "s3_secret_key: local-value" in secret_config
    assert os.stat(tmp_path / "config" / "secret.yaml").st_mode & 0o777 == 0o600


def test_optional_s3_config_requires_endpoint_and_both_credentials() -> None:
    with pytest.raises(ValueError, match="endpoint"):
        e2e_runtime.resolve_settings([], {"AKB_E2E_S3_BUCKET": "akb-files"})
    with pytest.raises(ValueError, match="incomplete"):
        e2e_runtime.resolve_settings(
            [],
            {
                "AKB_E2E_S3_ENDPOINT": "http://localhost:9000",
                "AKB_E2E_S3_ACCESS_KEY": "local-access",
            },
        )


def test_ready_file_is_atomic_private_and_has_the_base_schema(tmp_path: Path) -> None:
    ready_file = tmp_path / "nested" / "ready.json"
    settings = e2e_runtime.RuntimeSettings(
        database_url=e2e_runtime.DEFAULT_DATABASE_URL,
        ready_file=ready_file,
    )

    e2e_runtime.write_ready_file(ready_file, e2e_runtime.ready_payload(settings))
    payload = json.loads(ready_file.read_text())

    assert payload == {
        "schema_version": 1,
        "status": "ready",
        "origin": "http://localhost:8000",
        "fixture_origin": "http://localhost:8889",
        "reset_url": "http://localhost:8889/__e2e/reset",
        "scenario": "empty",
    }
    assert os.stat(ready_file).st_mode & 0o777 == 0o600
    assert e2e_runtime.DEFAULT_DATABASE_URL not in ready_file.read_text()


def test_reset_response_matches_the_shared_fixture_contract() -> None:
    settings = e2e_runtime.RuntimeSettings(database_url=e2e_runtime.DEFAULT_DATABASE_URL)

    assert e2e_runtime.reset_payload(settings) == {"ok": True, "scenario": "empty"}


def test_database_reset_is_schema_neutral_and_does_not_embed_a_url() -> None:
    sql = e2e_runtime.render_database_reset_sql()

    assert "TRUNCATE TABLE public.%I RESTART IDENTITY CASCADE" in sql
    assert "DROP SCHEMA IF EXISTS vector_index CASCADE" in sql
    assert e2e_runtime.DEFAULT_DATABASE_URL not in sql


def test_git_fixture_reset_only_empties_the_runtime_root(tmp_path: Path) -> None:
    root = tmp_path / "vaults"
    (root / "nested").mkdir(parents=True)
    (root / "nested" / "fixture").write_text("fixture")
    (root / "file").write_text("fixture")

    e2e_runtime.clear_git_fixture_root(root)

    assert list(root.iterdir()) == []
    assert os.stat(root).st_mode & 0o777 == 0o700


def test_child_commands_preserve_the_existing_uvicorn_topology(tmp_path: Path) -> None:
    runtime = e2e_runtime.E2ERuntime(
        e2e_runtime.RuntimeSettings(
            database_url=e2e_runtime.DEFAULT_DATABASE_URL,
            ready_file=tmp_path / "ready.json",
        )
    )

    assert runtime.embed_command[-4:] == ["--host", "127.0.0.1", "--port", "8888"]
    assert runtime.backend_command[-6:] == [
        "--app-dir",
        "backend",
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
    ]
    assert runtime.backend_command[:3] == [runtime.backend_command[0], "-m", "uvicorn"]


def test_compose_argv_is_project_scoped_and_does_not_contain_database_url(tmp_path: Path) -> None:
    database_url = e2e_runtime.DEFAULT_DATABASE_URL
    runtime = e2e_runtime.E2ERuntime(
        e2e_runtime.RuntimeSettings(
            database_url=database_url,
            ready_file=tmp_path / "ready.json",
            manage_postgres=True,
            compose_project="akb-e2e-unit",
            docker_argv=("sudo", "-n", "docker"),
        ),
        repo_root=tmp_path,
    )

    argv = runtime.compose_argv("up", "--detach")

    assert argv[:7] == [
        "sudo",
        "-n",
        "docker",
        "compose",
        "--project-name",
        "akb-e2e-unit",
        "--file",
    ]
    assert str(e2e_runtime.COMPOSE_FILE) in argv
    assert argv[-2:] == ["up", "--detach"]
    assert database_url not in argv


def test_suite_environment_derives_pg_access_without_url_or_password_in_command(tmp_path: Path) -> None:
    database_url = "postgresql://runner:" + "ci-passphrase@db.example.test:15432/e2e"
    runtime = e2e_runtime.E2ERuntime(
        e2e_runtime.RuntimeSettings(
            database_url=database_url,
            origin="http://localhost:8000",
            ready_file=tmp_path / "ready.json",
            run_suites=True,
        )
    )

    environment = runtime.suite_environment()

    assert environment["AKB_URL"] == "http://localhost:8000"
    assert environment["AKB_PG_EXEC"] == "env PGHOST=db.example.test PGPORT=15432"
    assert environment["AKB_PG_USER"] == "runner"
    assert environment["AKB_PG_DB"] == "e2e"
    assert environment["PGPASSWORD"] == "ci-passphrase"  # pragma: allowlist secret
    assert database_url not in environment["AKB_PG_EXEC"]


def test_managed_suite_environment_derives_compose_exec_for_psql(tmp_path: Path) -> None:
    runtime = e2e_runtime.E2ERuntime(
        e2e_runtime.RuntimeSettings(
            database_url=e2e_runtime.DEFAULT_DATABASE_URL,
            ready_file=tmp_path / "ready.json",
            manage_postgres=True,
            compose_project="akb-e2e-unit",
        ),
        repo_root=tmp_path,
    )

    environment = runtime.suite_environment()
    pg_exec = shlex.split(environment["AKB_PG_EXEC"])

    assert pg_exec == [
        "docker",
        "compose",
        "--project-name",
        "akb-e2e-unit",
        "--file",
        str(e2e_runtime.COMPOSE_FILE),
        "exec",
        "-T",
        "postgres",
    ]
    assert "PGPASSWORD" not in environment
    assert e2e_runtime.DEFAULT_DATABASE_URL not in environment["AKB_PG_EXEC"]


def test_start_failure_cleans_up_partial_compose_project(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime = e2e_runtime.E2ERuntime(
        e2e_runtime.RuntimeSettings(
            database_url=e2e_runtime.DEFAULT_DATABASE_URL,
            ready_file=tmp_path / "ready.json",
            manage_postgres=True,
            compose_project="akb-e2e-unit",
        ),
        repo_root=tmp_path,
    )
    cleanup_calls: list[str] = []

    def failed_up() -> None:
        runtime.compose_started = True
        raise RuntimeError("expected startup failure")

    def fake_down() -> bool:
        cleanup_calls.append("down")
        runtime.compose_started = False
        return True

    monkeypatch.setattr(runtime, "_compose_up", failed_up)
    monkeypatch.setattr(runtime, "_compose_down", fake_down)

    with pytest.raises(RuntimeError, match="expected"):
        runtime.start()

    assert cleanup_calls == ["down"]
    assert runtime.compose_started is False


def test_signal_request_still_runs_project_cleanup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime = e2e_runtime.E2ERuntime(
        e2e_runtime.RuntimeSettings(
            database_url=e2e_runtime.DEFAULT_DATABASE_URL,
            ready_file=tmp_path / "ready.json",
            manage_postgres=True,
            compose_project="akb-e2e-unit",
        ),
        repo_root=tmp_path,
    )
    cleanup_calls: list[str] = []
    runtime.compose_started = True
    monkeypatch.setattr(runtime, "_compose_down", lambda: cleanup_calls.append("down") or True)

    runtime.request_stop()
    assert runtime.stop_event.is_set()
    assert runtime.shutdown() is True
    assert cleanup_calls == ["down"]


def test_run_suites_calls_the_repository_owned_runner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[object, ...]] = []

    class Completed:
        returncode = 7

        def poll(self) -> int:
            return self.returncode

    def fake_popen(*args: object, **kwargs: object) -> Completed:
        calls.append((args, kwargs))
        return Completed()

    monkeypatch.setattr(e2e_runtime.subprocess, "Popen", fake_popen)
    runtime = e2e_runtime.E2ERuntime(
        e2e_runtime.RuntimeSettings(
            database_url=e2e_runtime.DEFAULT_DATABASE_URL,
            ready_file=tmp_path / "ready.json",
            run_suites=True,
        ),
        repo_root=tmp_path,
    )

    assert runtime.run_suites() == 7
    args, options = calls[0]
    command = args[0]
    assert command == ["bash", str(tmp_path / "backend/scripts/ci/run_e2e_suites.sh")]
    assert options["cwd"] == tmp_path
    assert options["start_new_session"] is True


def test_process_teardown_targets_the_child_process_group(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[tuple[object, ...]] = []

    class FakeProcess:
        pid = 123

        def poll(self) -> None:
            return None

        def wait(self, timeout: int) -> None:
            events.append(("wait", timeout))

    monkeypatch.setattr(e2e_runtime.os, "getpgid", lambda _pid: 456)
    monkeypatch.setattr(e2e_runtime.os, "getpgrp", lambda: 1)
    monkeypatch.setattr(e2e_runtime.os, "killpg", lambda *args: events.append(("killpg", *args)))

    e2e_runtime._terminate_process_group(FakeProcess())  # type: ignore[arg-type]

    assert events == [("killpg", 456, signal.SIGTERM), ("wait", 10)]
