"""Project-neutral foreground runtime for the repository's HTTP E2E suites.

The runtime keeps the CI topology deliberately small: it starts the existing
embedding stub and backend against an externally supplied PostgreSQL service.
The process itself is the supervisor.  Its fixture control plane is local-only
and is separate from the product API:

* ``GET /__e2e/health`` reports runtime health.
* ``GET /__e2e/ready`` returns the public ready payload.
* ``POST /__e2e/reset`` empties the external database and Git fixture root,
  then restarts the same backend command.
* ``POST /__e2e/stop`` requests graceful supervisor shutdown.

The database URL is kept in memory only.  It is never written to generated
configuration, the ready artifact, or runtime output.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen


DEFAULT_DATABASE_URL = "postgresql://akb:akb@localhost:15432/akb"  # pragma: allowlist secret
DEFAULT_ORIGIN = "http://localhost:8000"
DEFAULT_FIXTURE_ORIGIN = "http://localhost:8889"
DEFAULT_SCENARIO = "empty"
DEFAULT_S3_BUCKET = "akb-files"
RUNTIME_TMP = Path(tempfile.gettempdir())
DEFAULT_READY_FILE = str(RUNTIME_TMP / "akb-e2e-ready.json")
EMBED_PORT = 8888
BACKEND_PORT = 8000
GIT_FIXTURE_ROOT = RUNTIME_TMP / "akb-vaults"
EMBED_LOG = RUNTIME_TMP / "embed-stub.log"
BACKEND_LOG = RUNTIME_TMP / "backend.log"
REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILE = Path(__file__).resolve().with_name("e2e-postgres.compose.yml")
DEFAULT_DOCKER_ARGV = "docker"
COMPOSE_PROJECT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
SUPPORTED_SCENARIOS = frozenset({DEFAULT_SCENARIO})


@dataclass(frozen=True)
class DatabaseSettings:
    host: str
    port: int
    name: str
    user: str
    password: str = field(repr=False)


@dataclass(frozen=True)
class RuntimeSettings:
    database_url: str = field(repr=False)
    origin: str = DEFAULT_ORIGIN
    fixture_origin: str = DEFAULT_FIXTURE_ORIGIN
    scenario: str = DEFAULT_SCENARIO
    ready_file: Path = field(default_factory=lambda: Path(DEFAULT_READY_FILE))
    run_suites: bool = False
    manage_postgres: bool = False
    compose_project: str = ""
    docker_argv: tuple[str, ...] = ("docker",)
    s3_endpoint: str = ""
    s3_bucket: str = ""
    s3_access_key: str = field(default="", repr=False)
    s3_secret_key: str = field(default="", repr=False)


def parse_database_url(database_url: str) -> DatabaseSettings:
    """Extract only the fields needed by the existing app config."""

    parsed = urlsplit(database_url)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise ValueError("invalid database URL")
    try:
        port = parsed.port or 5432
    except ValueError as exc:
        raise ValueError("invalid database URL") from exc
    name = parsed.path.lstrip("/")
    if not name or not parsed.username:
        raise ValueError("invalid database URL")
    return DatabaseSettings(
        host=parsed.hostname,
        port=port,
        name=unquote(name),
        user=unquote(parsed.username),
        password=unquote(parsed.password or ""),
    )


def _validate_compose_project(project: str) -> str:
    if not COMPOSE_PROJECT_PATTERN.fullmatch(project):
        raise ValueError("invalid compose project")
    return project


def _resolve_compose_project(value: str | None) -> str:
    project = value or f"akb-e2e-{os.getpid()}-{secrets.token_hex(4)}"
    return _validate_compose_project(project)


def _resolve_docker_argv(value: str | None) -> tuple[str, ...]:
    try:
        argv = tuple(shlex.split(value or DEFAULT_DOCKER_ARGV))
    except ValueError as exc:
        raise ValueError("invalid docker argv") from exc
    if not argv:
        raise ValueError("docker argv is empty")
    return argv


def _validate_managed_database(database: DatabaseSettings) -> None:
    if (
        database.host not in {"localhost", "127.0.0.1"}
        or database.name != "akb"
        or database.user != "akb"
        or database.password != "akb"  # pragma: allowlist secret
    ):
        raise ValueError("managed database must use the CI PostgreSQL settings")


def _resolve_s3_settings(environ: Mapping[str, str]) -> tuple[str, str, str, str]:
    endpoint = (environ.get("AKB_E2E_S3_ENDPOINT") or "").strip()
    bucket = (environ.get("AKB_E2E_S3_BUCKET") or "").strip()
    access_key = environ.get("AKB_E2E_S3_ACCESS_KEY") or ""
    secret_key = environ.get("AKB_E2E_S3_SECRET_KEY") or ""
    if not endpoint:
        if any((bucket, access_key, secret_key)):
            raise ValueError("S3 settings require an endpoint")
        return "", "", "", ""
    _validate_origin(endpoint)
    bucket = bucket or DEFAULT_S3_BUCKET
    if not bucket or any(char.isspace() for char in bucket):
        raise ValueError("invalid S3 bucket")
    if not access_key or not secret_key:
        raise ValueError("S3 credentials are incomplete")
    return endpoint, bucket, access_key, secret_key


def _yaml_scalar(value: object) -> str:
    text = str(value)
    if text and all(char.isalnum() or char in "._/-:" for char in text):
        return text
    return json.dumps(text)


def render_app_config(
    database: DatabaseSettings,
    origin: str,
    s3_endpoint: str = "",
    s3_bucket: str = "",
) -> str:
    """Render the same CI app settings that were previously inline in YAML."""

    lines = [
        f"db_host: {_yaml_scalar(database.host)}",
        f"db_port: {database.port}",
        f"db_name: {_yaml_scalar(database.name)}",
        f"db_user: {_yaml_scalar(database.user)}",
        f"public_base_url: {_yaml_scalar(origin)}",
        "git_storage_path: /tmp/akb-vaults",
        "vector_store_driver: pgvector",
        "embed_base_url: http://localhost:8888/v1",
        "embed_model: ci-embed-stub",
        "embed_dimensions: 1536",
        'llm_base_url: ""',
        'llm_model: ""',
        "rerank_enabled: false",
        f"s3_endpoint_url: {_yaml_scalar(s3_endpoint)}" if s3_endpoint else 's3_endpoint_url: ""',
    ]
    if s3_endpoint:
        lines.append(f"s3_bucket: {_yaml_scalar(s3_bucket)}")
    lines.append("")
    return "\n".join(lines)


def render_secret_config(
    database: DatabaseSettings,
    s3_access_key: str = "",
    s3_secret_key: str = "",
) -> str:
    """Render the existing CI-only secrets without the source database URL."""

    lines = [
        f"db_password: {_yaml_scalar(database.password)}  # pragma: allowlist secret",
        "jwt_secret: ci-only-jwt-secret-not-for-prod-use  # pragma: allowlist secret",
        "embed_api_key: ci-stub-no-auth  # pragma: allowlist secret",
    ]
    if s3_access_key and s3_secret_key:
        lines.extend(
            [
                f"s3_access_key: {_yaml_scalar(s3_access_key)}  # pragma: allowlist secret",
                f"s3_secret_key: {_yaml_scalar(s3_secret_key)}  # pragma: allowlist secret",
            ]
        )
    lines.append("")
    return "\n".join(lines)


def _atomic_write(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, path)
        os.chmod(path, mode)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def write_runtime_config(settings: RuntimeSettings, repo_root: Path = REPO_ROOT) -> None:
    database = parse_database_url(settings.database_url)
    config_dir = repo_root / "config"
    _atomic_write(
        config_dir / "app.yaml",
        render_app_config(database, settings.origin, settings.s3_endpoint, settings.s3_bucket),
        0o644,
    )
    _atomic_write(
        config_dir / "secret.yaml",
        render_secret_config(database, settings.s3_access_key, settings.s3_secret_key),
        0o600,
    )


def ready_payload(settings: RuntimeSettings) -> dict[str, object]:
    return {
        "schema_version": 1,
        "status": "ready",
        "origin": settings.origin,
        "fixture_origin": settings.fixture_origin,
        "reset_url": f"{settings.fixture_origin}/__e2e/reset",
        "scenario": settings.scenario,
    }


def reset_payload(settings: RuntimeSettings) -> dict[str, object]:
    return {"ok": True, "scenario": settings.scenario}


def write_ready_file(path: Path, payload: Mapping[str, object]) -> None:
    content = json.dumps(dict(payload), sort_keys=True, separators=(",", ":")) + "\n"
    _atomic_write(path, content, 0o600)


def remove_ready_file(path: Path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def render_database_reset_sql() -> str:
    """Return a schema-neutral reset for the externally provided CI database."""

    return """
DO $$
DECLARE
    table_name text;
BEGIN
    FOR table_name IN
        SELECT tablename FROM pg_tables WHERE schemaname = 'public'
    LOOP
        EXECUTE format('TRUNCATE TABLE public.%I RESTART IDENTITY CASCADE', table_name);
    END LOOP;
END
$$;
DROP SCHEMA IF EXISTS vector_index CASCADE;
""".strip()


async def _reset_database(database_url: str) -> None:
    import asyncpg

    connection = await asyncpg.connect(database_url, timeout=10)
    try:
        await connection.execute(render_database_reset_sql())
    finally:
        await connection.close()


def reset_database(database_url: str) -> None:
    asyncio.run(_reset_database(database_url))


def clear_git_fixture_root(root: Path = GIT_FIXTURE_ROOT) -> None:
    """Empty only the runtime-owned fixture directory, preserving the root."""

    root.mkdir(parents=True, exist_ok=True)
    for child in root.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    os.chmod(root, 0o700)


def _validate_origin(value: str, *, allow_ephemeral_port: bool = False) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"localhost", "127.0.0.1"}
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("origin must be a local HTTP origin")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("origin has an invalid port") from exc
    if port is None or (port == 0 and not allow_ephemeral_port):
        raise ValueError("origin must include a port")
    return value.rstrip("/")


def resolve_settings(argv: list[str] | None = None, environ: Mapping[str, str] | None = None) -> RuntimeSettings:
    env = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", default=env.get("AKB_E2E_SCENARIO") or DEFAULT_SCENARIO)
    parser.add_argument("--ready-file", default=env.get("AKB_E2E_READY_FILE") or DEFAULT_READY_FILE)
    parser.add_argument(
        "--run-suites",
        action="store_true",
        help="run the repository-owned HTTP suite after readiness, then exit",
    )
    parser.add_argument(
        "--manage-postgres",
        action="store_true",
        help="own the repository's PostgreSQL-only Compose service for this run",
    )
    args = parser.parse_args(argv)

    scenario = args.scenario or DEFAULT_SCENARIO
    if scenario not in SUPPORTED_SCENARIOS:
        raise ValueError("unsupported E2E scenario")
    database_url = env.get("AKB_E2E_DATABASE_URL") or DEFAULT_DATABASE_URL
    database = parse_database_url(database_url)
    if args.manage_postgres:
        _validate_managed_database(database)
    s3_endpoint, s3_bucket, s3_access_key, s3_secret_key = _resolve_s3_settings(env)
    return RuntimeSettings(
        database_url=database_url,
        origin=_validate_origin(env.get("AKB_E2E_ORIGIN") or DEFAULT_ORIGIN),
        fixture_origin=_validate_origin(env.get("AKB_E2E_FIXTURE_ORIGIN") or DEFAULT_FIXTURE_ORIGIN),
        scenario=scenario,
        ready_file=Path(args.ready_file).expanduser().resolve(),
        run_suites=args.run_suites,
        manage_postgres=args.manage_postgres,
        compose_project=_resolve_compose_project(env.get("AKB_E2E_COMPOSE_PROJECT")),
        docker_argv=_resolve_docker_argv(env.get("AKB_E2E_DOCKER_ARGV")),
        s3_endpoint=s3_endpoint,
        s3_bucket=s3_bucket,
        s3_access_key=s3_access_key,
        s3_secret_key=s3_secret_key,
    )


def _http_get(url: str) -> tuple[int, bytes]:
    request = Request(url, method="GET")
    try:
        # Callers pass only fixed or _validate_origin-checked loopback URLs.
        with urlopen(request, timeout=2) as response:  # nosec B310
            return response.status, response.read(65536)
    except HTTPError as exc:
        return exc.code, b""
    except (OSError, URLError):
        return 0, b""


def _terminate_process_group(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        process_group = os.getpgid(process.pid)
    except ProcessLookupError:
        return
    try:
        if process_group == os.getpgrp():
            process.terminate()
        else:
            os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            if process_group == os.getpgrp():
                process.kill()
            else:
                os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            return
        process.wait(timeout=5)


class E2ERuntime:
    """Own the stub, backend, local fixture control plane, and their cleanup."""

    def __init__(self, settings: RuntimeSettings, repo_root: Path = REPO_ROOT):
        self.settings = settings
        self.repo_root = repo_root
        self.embed_process: subprocess.Popen[bytes] | None = None
        self.backend_process: subprocess.Popen[bytes] | None = None
        self.suite_process: subprocess.Popen[bytes] | None = None
        self.compose_started = False
        self.control_server: ThreadingHTTPServer | None = None
        self.control_thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.reset_lock = threading.Lock()
        self.ready = False
        self.failed = False

    @property
    def backend_command(self) -> list[str]:
        return [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--app-dir",
            "backend",
            "--host",
            "127.0.0.1",
            "--port",
            str(BACKEND_PORT),
        ]

    @property
    def embed_command(self) -> list[str]:
        return [
            sys.executable,
            "-m",
            "uvicorn",
            "scripts.ci.embed_stub:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(EMBED_PORT),
        ]

    def compose_argv(self, *arguments: str) -> list[str]:
        if not self.settings.manage_postgres:
            raise RuntimeError("PostgreSQL Compose is not enabled")
        project = _validate_compose_project(self.settings.compose_project)
        return [
            *self.settings.docker_argv,
            "compose",
            "--project-name",
            project,
            "--file",
            str(COMPOSE_FILE),
            *arguments,
        ]

    def _compose_environment(self) -> dict[str, str]:
        database = parse_database_url(self.settings.database_url)
        _validate_managed_database(database)
        environment = self._child_environment()
        environment["AKB_E2E_POSTGRES_PORT"] = str(database.port)
        return environment

    def _run_compose(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.compose_argv(*arguments),
            cwd=self.repo_root,
            env=self._compose_environment(),
            capture_output=True,
            text=True,
            check=False,
        )

    def _run_docker(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*self.settings.docker_argv, *arguments],
            cwd=self.repo_root,
            env=self._compose_environment(),
            capture_output=True,
            text=True,
            check=False,
        )

    def _wait_for_postgres(self) -> None:
        for _ in range(60):
            if self.stop_event.is_set():
                raise RuntimeError("runtime stopped")
            containers = self._run_compose("ps", "-q", "postgres")
            container_id = containers.stdout.strip()
            if container_id:
                health = self._run_docker(
                    "inspect",
                    "--format",
                    "{{.State.Health.Status}}",
                    container_id,
                )
                if health.stdout.strip() == "healthy":
                    return
                if health.stdout.strip() in {"unhealthy", "exited", "dead"}:
                    break
            self.stop_event.wait(1)
        raise RuntimeError("PostgreSQL Compose service did not become healthy")

    def _compose_up(self) -> None:
        # Mark ownership before the command so partial `up` state is cleaned
        # by the same project-scoped `down` in the failure path.
        self.compose_started = True
        result = self._run_compose("up", "--detach")
        if result.returncode != 0:
            raise RuntimeError("PostgreSQL Compose startup failed")
        self._wait_for_postgres()

    def _compose_down(self) -> bool:
        if not self.compose_started:
            return True
        try:
            result = self._run_compose("down", "--volumes", "--remove-orphans")
        except Exception:
            self.compose_started = False
            return False
        self.compose_started = False
        return result.returncode == 0

    def _child_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        for key in (
            "AKB_E2E_DATABASE_URL",
            "AKB_E2E_S3_ACCESS_KEY",
            "AKB_E2E_S3_SECRET_KEY",
        ):
            environment.pop(key, None)
        return environment

    def _start_process(self, command: list[str], cwd: Path, log_path: Path) -> subprocess.Popen[bytes]:
        with log_path.open("ab") as log:
            return subprocess.Popen(
                command,
                cwd=cwd,
                env=self._child_environment(),
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

    def _start_control_server(self) -> None:
        parsed = urlsplit(self.settings.fixture_origin)
        assert parsed.port is not None
        handler = type("E2EControlHandler", (_ControlHandler,), {"runtime": self})
        self.control_server = ThreadingHTTPServer(("127.0.0.1", parsed.port), handler)
        self.control_server.daemon_threads = True
        self.control_thread = threading.Thread(
            target=self.control_server.serve_forever,
            name="e2e-control",
            daemon=True,
        )
        self.control_thread.start()

    def _wait_for_embed(self) -> None:
        for _ in range(20):
            if self.stop_event.is_set():
                raise RuntimeError("runtime stopped")
            status, _ = _http_get("http://127.0.0.1:8888/healthz")
            if status == 200:
                return
            if self.embed_process is not None and self.embed_process.poll() is not None:
                break
            self.stop_event.wait(1)
        raise RuntimeError("embedding stub did not become ready")

    def _wait_for_backend(self) -> None:
        for _ in range(45):
            if self.stop_event.is_set():
                raise RuntimeError("runtime stopped")
            status, body = _http_get(f"{self.settings.origin}/readyz")
            if status == 200:
                try:
                    if isinstance(json.loads(body), dict) and "status" in json.loads(body):
                        return
                except (TypeError, ValueError):
                    pass
            if self.backend_process is not None and self.backend_process.poll() is not None:
                break
            self.stop_event.wait(2)
        raise RuntimeError("backend did not become ready")

    def _write_ready(self) -> None:
        write_ready_file(self.settings.ready_file, ready_payload(self.settings))
        self.ready = True

    def _start_backend(self) -> None:
        self.backend_process = self._start_process(self.backend_command, self.repo_root, BACKEND_LOG)
        self._wait_for_backend()
        self._write_ready()

    def _stop_backend(self) -> None:
        self.ready = False
        remove_ready_file(self.settings.ready_file)
        _terminate_process_group(self.backend_process)
        self.backend_process = None

    def start(self) -> None:
        try:
            remove_ready_file(self.settings.ready_file)
            if self.settings.manage_postgres:
                self._compose_up()
            clear_git_fixture_root()
            reset_database(self.settings.database_url)
            write_runtime_config(self.settings, self.repo_root)
            self._start_control_server()
            self.embed_process = self._start_process(self.embed_command, self.repo_root / "backend", EMBED_LOG)
            self._wait_for_embed()
            self._start_backend()
        except BaseException:
            self.shutdown()
            raise

    def reset(self) -> None:
        with self.reset_lock:
            self._stop_backend()
            try:
                reset_database(self.settings.database_url)
                clear_git_fixture_root()
                self._start_backend()
            except BaseException:
                self._stop_backend()
                raise

    def request_stop(self) -> None:
        self.stop_event.set()

    def suite_environment(self) -> dict[str, str]:
        database = parse_database_url(self.settings.database_url)
        environment = self._child_environment()
        if self.settings.manage_postgres:
            _validate_managed_database(database)
            pg_exec = shlex.join(self.compose_argv("exec", "-T", "postgres"))
            environment.pop("PGPASSWORD", None)
        else:
            pg_exec = f"env PGHOST={shlex.quote(database.host)} PGPORT={database.port}"
            environment["PGPASSWORD"] = database.password
        environment.update(
            {
                "AKB_URL": self.settings.origin,
                "AKB_PG_EXEC": pg_exec,
                "AKB_PG_USER": database.user,
                "AKB_PG_DB": database.name,
            }
        )
        return environment

    def run_suites(self) -> int:
        self.suite_process = subprocess.Popen(
            ["bash", str(self.repo_root / "backend/scripts/ci/run_e2e_suites.sh")],
            cwd=self.repo_root,
            env=self.suite_environment(),
            start_new_session=True,
        )
        try:
            while self.suite_process.poll() is None:
                if self.stop_event.wait(0.5):
                    _terminate_process_group(self.suite_process)
                    return 130
            return self.suite_process.returncode or 0
        finally:
            self.suite_process = None

    def wait(self) -> None:
        while not self.stop_event.wait(0.5):
            for process in (self.embed_process, self.backend_process):
                if process is not None and process.poll() is not None:
                    self.failed = True
                    self.stop_event.set()
                    break

    def shutdown(self) -> bool:
        cleanup_ok = True
        self.ready = False
        try:
            remove_ready_file(self.settings.ready_file)
            if self.control_server is not None:
                self.control_server.shutdown()
                self.control_server.server_close()
                self.control_server = None
            if self.control_thread is not None:
                self.control_thread.join(timeout=5)
                self.control_thread = None
        except Exception:
            cleanup_ok = False
        for process in (self.suite_process, self.backend_process, self.embed_process):
            try:
                _terminate_process_group(process)
            except Exception:
                cleanup_ok = False
        self.backend_process = None
        self.embed_process = None
        if not self._compose_down():
            cleanup_ok = False
        return cleanup_ok

    def health_response(self) -> tuple[int, dict[str, object]]:
        healthy = self.ready and not self.stop_event.is_set()
        if self.backend_process is not None and self.backend_process.poll() is not None:
            healthy = False
        return (200 if healthy else 503), {"status": "ok" if healthy else "starting"}

    def ready_response(self) -> tuple[int, dict[str, object]]:
        if not self.ready:
            return 503, {"status": "starting"}
        return 200, ready_payload(self.settings)


class _ControlHandler(BaseHTTPRequestHandler):
    runtime: E2ERuntime

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _json(self, status: int, payload: Mapping[str, object]) -> None:
        body = json.dumps(dict(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/__e2e/health":
            status, payload = self.runtime.health_response()
            self._json(status, payload)
            return
        if path == "/__e2e/ready":
            status, payload = self.runtime.ready_response()
            self._json(status, payload)
            return
        self._json(404, {"status": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/__e2e/reset":
            try:
                self.runtime.reset()
            except BaseException:
                self._json(500, {"status": "reset_failed"})
                return
            self._json(200, reset_payload(self.runtime.settings))
            return
        if path == "/__e2e/stop":
            self.runtime.request_stop()
            self._json(202, {"status": "stopping"})
            return
        self._json(404, {"status": "not_found"})


def main(argv: list[str] | None = None) -> int:
    try:
        settings = resolve_settings(argv)
        runtime = E2ERuntime(settings)
    except (ValueError, OSError):
        print("e2e runtime configuration failed", file=sys.stderr)
        return 2

    def handle_signal(_signum: int, _frame: Any) -> None:
        runtime.request_stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    exit_code = 1
    try:
        runtime.start()
        if settings.run_suites:
            exit_code = runtime.run_suites()
        else:
            runtime.wait()
            exit_code = 1 if runtime.failed else 0
    except Exception:
        print("e2e runtime failed", file=sys.stderr)
        exit_code = 1
    finally:
        if not runtime.shutdown() and exit_code == 0:
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
