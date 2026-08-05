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
        "AKB_E2E_FOREIGN_APP_CREDENTIAL",
    )
    assert all(name.startswith("AKB_E2E_") for name in e2e_runtime.CREDENTIAL_VARIABLES)
    assert all(not name.endswith("_APP_TOKEN") for name in e2e_runtime.CREDENTIAL_VARIABLES)


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
    profile_names = {line.split("=", 1)[0] for line in profile_path.read_text().splitlines()}
    assert profile_names == set(e2e_runtime.CREDENTIAL_VARIABLES)
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
    manifest, credentials = e2e_runtime.seed_scenario(settings)

    assert calls == [(e2e_runtime.DEFAULT_DATABASE_URL, settings.origin, settings.fixture_origin)]
    assert manifest == {"schema_version": 1, "scenario": "app_lifecycle"}
    assert credentials is not None
    assert set(credentials) == set(e2e_runtime.CREDENTIAL_VARIABLES)


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
    assert set(credentials) == set(e2e_runtime.CREDENTIAL_VARIABLES)
    assert all(
        credentials[name] not in serialized_manifest
        for name in e2e_runtime.CREDENTIAL_VARIABLES
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
    assert manifest["apps"]["primary"]["credential_env"] == e2e_runtime.CREDENTIAL_VARIABLES[5]  # type: ignore[index]
    assert "token_env" not in manifest["apps"]["primary"]  # type: ignore[operator]
    assert manifest["apps"]["foreign"]["credential_env"] == e2e_runtime.CREDENTIAL_VARIABLES[6]  # type: ignore[index]
    assert manifest["endpoint_tasks"]["app_status"]["credential_exchange_task"] == "credential_exchange"  # type: ignore[index]
    assert manifest["endpoint_tasks"]["foreign_app_status"]["credential_exchange_task"] == "credential_exchange"  # type: ignore[index]
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


def test_runtime_failure_diagnostic_is_phase_and_sqlstate_only() -> None:
    class FakePostgresError(Exception):
        sqlstate = "23514"
        constraint_name = None
        table_name = "installation_grants"
        column_name = None

        def __str__(self) -> str:
            return "postgresql://db.invalid/akb credential-marker"

    diagnostic = e2e_runtime.format_runtime_failure(
        "seed_scenario",
        FakePostgresError("not emitted"),
    )

    assert diagnostic == (
        "e2e runtime failed phase=seed_scenario category=FakePostgresError "
        "source=postgres sqlstate=23514 table=installation_grants"
    )
    assert "credential-marker" not in diagnostic


def test_reset_handler_reports_safe_failure_and_preserves_response_shape(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeResetError(Exception):
        sqlstate = "23514"
        constraint_name = None
        table_name = "installation_grants"
        column_name = None

    class FakeRuntime:
        phase = "database_reset"
        settings = e2e_runtime.RuntimeSettings(
            database_url=e2e_runtime.DEFAULT_DATABASE_URL,
        )

        def reset(self) -> None:
            raise FakeResetError(
                "credential-value-marker token-value-marker password-value-marker "
                "database-url-marker sql-arg-marker"
            )

    handler = object.__new__(e2e_runtime._ControlHandler)
    handler.path = "/__e2e/reset"
    handler.runtime = FakeRuntime()
    responses: list[tuple[int, dict[str, object]]] = []
    handler._json = lambda status, payload: responses.append((status, dict(payload)))

    handler.do_POST()

    assert responses == [(500, {"status": "reset_failed"})]
    diagnostic = capsys.readouterr().err
    assert diagnostic == (
        "e2e runtime failed phase=database_reset category=FakeResetError "
        "source=postgres sqlstate=23514 table=installation_grants\n"
    )
    for marker in (
        "credential-value-marker",
        "token-value-marker",
        "password-value-marker",
        "database-url-marker",
        "sql-arg-marker",
    ):
        assert marker not in diagnostic


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


def test_postgres_probe_opens_and_closes_a_real_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[object, ...]] = []

    class Connection:
        async def close(self) -> None:
            events.append(("close",))

    async def fake_connect(database_url: str, *, timeout: int) -> Connection:
        events.append(("connect", database_url, timeout))
        return Connection()

    monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(connect=fake_connect))

    assert e2e_runtime._postgres_connection_ready(e2e_runtime.DEFAULT_DATABASE_URL) is True
    assert events == [
        ("connect", e2e_runtime.DEFAULT_DATABASE_URL, e2e_runtime.POSTGRES_PROBE_TIMEOUT),
        ("close",),
    ]


def test_wait_for_postgres_waits_for_published_dsn_after_container_health(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = e2e_runtime.E2ERuntime(
        e2e_runtime.RuntimeSettings(
            database_url=e2e_runtime.DEFAULT_DATABASE_URL,
            ready_file=tmp_path / "ready.json",
            manage_postgres=True,
            compose_project="akb-e2e-unit",
        )
    )
    probe_results = iter([False, True])
    waits: list[int] = []
    compose_calls: list[tuple[str, ...]] = []
    docker_calls: list[tuple[str, ...]] = []

    class Completed:
        def __init__(self, stdout: str) -> None:
            self.stdout = stdout

    monkeypatch.setattr(
        runtime,
        "_run_compose",
        lambda *args: compose_calls.append(args) or Completed("postgres-container\n"),
    )
    monkeypatch.setattr(
        runtime,
        "_run_docker",
        lambda *args: docker_calls.append(args) or Completed("healthy\n"),
    )
    monkeypatch.setattr(
        e2e_runtime,
        "_postgres_connection_ready",
        lambda database_url: next(probe_results),
    )
    monkeypatch.setattr(runtime.stop_event, "wait", lambda seconds: waits.append(seconds))

    runtime._wait_for_postgres()

    assert len(compose_calls) == 2
    assert len(docker_calls) == 2
    assert waits == [e2e_runtime.POSTGRES_READY_INTERVAL]


def test_wait_for_postgres_has_bounded_timeout_when_published_dsn_stays_unready(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = e2e_runtime.E2ERuntime(
        e2e_runtime.RuntimeSettings(
            database_url=e2e_runtime.DEFAULT_DATABASE_URL,
            ready_file=tmp_path / "ready.json",
            manage_postgres=True,
            compose_project="akb-e2e-unit",
        )
    )

    class Completed:
        stdout = "healthy\n"

    monkeypatch.setattr(e2e_runtime, "POSTGRES_READY_ATTEMPTS", 2)
    monkeypatch.setattr(runtime, "_run_compose", lambda *_args: Completed())
    monkeypatch.setattr(runtime, "_run_docker", lambda *_args: Completed())
    monkeypatch.setattr(e2e_runtime, "_postgres_connection_ready", lambda _url: False)
    monkeypatch.setattr(runtime.stop_event, "wait", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="PostgreSQL Compose service") as error:
        runtime._wait_for_postgres()

    assert e2e_runtime.DEFAULT_DATABASE_URL not in str(error.value)


def test_wait_for_postgres_stops_before_connecting_when_requested(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = e2e_runtime.E2ERuntime(
        e2e_runtime.RuntimeSettings(
            database_url=e2e_runtime.DEFAULT_DATABASE_URL,
            ready_file=tmp_path / "ready.json",
            manage_postgres=True,
            compose_project="akb-e2e-unit",
        )
    )
    runtime.request_stop()
    monkeypatch.setattr(
        e2e_runtime,
        "_postgres_connection_ready",
        lambda _url: pytest.fail("connection probe must not run after stop"),
    )

    with pytest.raises(RuntimeError, match="runtime stopped"):
        runtime._wait_for_postgres()


def test_backend_readiness_requires_the_ready_api_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = e2e_runtime.E2ERuntime(
        e2e_runtime.RuntimeSettings(
            database_url=e2e_runtime.DEFAULT_DATABASE_URL,
            ready_file=tmp_path / "ready.json",
        )
    )
    responses = iter(
        [
            (200, b'{"status":"not_ready"}'),
            (200, b'{"status":"ready"}'),
        ]
    )
    requests: list[str] = []

    class AliveProcess:
        def poll(self) -> None:
            return None

    def fake_get(url: str) -> tuple[int, bytes]:
        requests.append(url)
        return next(responses)

    runtime.backend_process = AliveProcess()  # type: ignore[assignment]
    monkeypatch.setattr(e2e_runtime, "_http_get", fake_get)
    monkeypatch.setattr(runtime.stop_event, "wait", lambda _seconds: False)

    runtime._wait_for_backend()

    assert requests == [f"{runtime.settings.origin}/readyz"] * 2


def test_start_publishes_ready_only_after_fixture_reset_and_api_health(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ready_file = tmp_path / "ready.json"
    runtime = e2e_runtime.E2ERuntime(
        e2e_runtime.RuntimeSettings(
            database_url=e2e_runtime.DEFAULT_DATABASE_URL,
            ready_file=ready_file,
        ),
        repo_root=tmp_path,
    )
    events: list[str] = []

    class AliveProcess:
        def poll(self) -> None:
            return None

    def fixture_reset() -> None:
        assert not ready_file.exists()
        events.append("fixture_reset")

    def api_health() -> None:
        assert not ready_file.exists()
        events.append("api_health")

    real_write_ready_file = e2e_runtime.write_ready_file

    def publish_ready(path: Path, payload: dict[str, object]) -> None:
        assert events == ["git_reset", "fixture_reset", "api_health", "fixtures_written"]
        real_write_ready_file(path, payload)
        events.append("ready_published")

    monkeypatch.setattr(e2e_runtime, "clear_git_fixture_root", lambda: events.append("git_reset"))
    monkeypatch.setattr(e2e_runtime, "reset_database", lambda _url: fixture_reset())
    monkeypatch.setattr(e2e_runtime, "write_runtime_config", lambda *_args: None)
    monkeypatch.setattr(runtime, "_start_control_server", lambda: None)
    monkeypatch.setattr(runtime, "_start_process", lambda *_args: AliveProcess())
    monkeypatch.setattr(runtime, "_wait_for_embed", lambda: None)
    monkeypatch.setattr(runtime, "_wait_for_backend", api_health)
    monkeypatch.setattr(e2e_runtime, "seed_scenario", lambda _settings: (None, None))
    monkeypatch.setattr(
        e2e_runtime,
        "write_fixture_artifacts",
        lambda *_args: events.append("fixtures_written"),
    )
    monkeypatch.setattr(e2e_runtime, "write_ready_file", publish_ready)

    runtime.start()

    assert events[-1] == "ready_published"
    assert json.loads(ready_file.read_text())["status"] == "ready"


def test_fixture_reset_failure_never_leaves_a_ready_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ready_file = tmp_path / "ready.json"
    ready_file.write_text('{"status":"stale"}')
    runtime = e2e_runtime.E2ERuntime(
        e2e_runtime.RuntimeSettings(
            database_url=e2e_runtime.DEFAULT_DATABASE_URL,
            ready_file=ready_file,
        )
    )

    monkeypatch.setattr(e2e_runtime, "clear_git_fixture_root", lambda: None)
    monkeypatch.setattr(
        e2e_runtime,
        "reset_database",
        lambda _url: (_ for _ in ()).throw(RuntimeError("reset failed")),
    )
    monkeypatch.setattr(runtime, "shutdown", lambda: True)

    with pytest.raises(RuntimeError, match="reset failed"):
        runtime.start()

    assert not ready_file.exists()
    assert runtime.ready is False


def test_backend_exit_invalidates_ready_and_marks_the_supervisor_failed(tmp_path: Path) -> None:
    runtime = e2e_runtime.E2ERuntime(
        e2e_runtime.RuntimeSettings(
            database_url=e2e_runtime.DEFAULT_DATABASE_URL,
            ready_file=tmp_path / "ready.json",
        )
    )

    class Process:
        def __init__(self, returncode: int | None) -> None:
            self.returncode = returncode

        def poll(self) -> int | None:
            return self.returncode

    runtime.embed_process = Process(None)  # type: ignore[assignment]
    runtime.backend_process = Process(1)  # type: ignore[assignment]
    runtime._write_ready()

    assert runtime._dependencies_alive() is False
    assert runtime.failed is True
    assert runtime.phase == "backend_runtime"
    assert runtime.stop_event.is_set()
    assert not runtime.settings.ready_file.exists()


def test_managed_postgres_exit_invalidates_ready_and_marks_the_supervisor_failed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = e2e_runtime.E2ERuntime(
        e2e_runtime.RuntimeSettings(
            database_url=e2e_runtime.DEFAULT_DATABASE_URL,
            ready_file=tmp_path / "ready.json",
            manage_postgres=True,
            compose_project="akb-e2e-unit",
        )
    )

    class AliveProcess:
        def poll(self) -> None:
            return None

    class NoContainers:
        stdout = ""

    runtime.embed_process = AliveProcess()  # type: ignore[assignment]
    runtime.backend_process = AliveProcess()  # type: ignore[assignment]
    runtime._write_ready()
    monkeypatch.setattr(runtime, "_run_compose", lambda *_args: NoContainers())

    assert runtime._dependencies_alive() is False
    assert runtime.failed is True
    assert runtime.phase == "postgres_runtime"
    assert runtime.stop_event.is_set()
    assert not runtime.settings.ready_file.exists()


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


def test_run_suites_returns_nonzero_when_a_dependency_dies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Process:
        def __init__(self, returncode: int | None) -> None:
            self.returncode = returncode

        def poll(self) -> int | None:
            return self.returncode

    suite_process = Process(None)
    monkeypatch.setattr(e2e_runtime.subprocess, "Popen", lambda *_args, **_kwargs: suite_process)
    terminated: list[object] = []
    monkeypatch.setattr(
        e2e_runtime,
        "_terminate_process_group",
        lambda process: terminated.append(process),
    )
    runtime = e2e_runtime.E2ERuntime(
        e2e_runtime.RuntimeSettings(
            database_url=e2e_runtime.DEFAULT_DATABASE_URL,
            ready_file=tmp_path / "ready.json",
            run_suites=True,
        ),
        repo_root=tmp_path,
    )
    runtime.embed_process = Process(None)  # type: ignore[assignment]
    runtime.backend_process = Process(1)  # type: ignore[assignment]
    runtime._write_ready()

    assert runtime.run_suites() == 1
    assert runtime.failed is True
    assert terminated == [suite_process]
    assert not runtime.settings.ready_file.exists()


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
