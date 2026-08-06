"""Repository-owned live E2E runtime for a clean Ubuntu 24.04 host.

The runtime owns only the test infrastructure around AKB:

* Compose manages the pinned PostgreSQL/pgvector and MinIO dependencies.
* Uvicorn runs the embedding stub and backend as host processes.
* A small in-process fixture control app exposes health, discovery, and the
  project-neutral ``empty`` reset.

The process deliberately keeps all runtime state outside the checkout.  It
prints one machine-readable descriptor to stdout after readiness; operational
logs and suite output go to stderr/files so a launcher can parse that line.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import faulthandler
import json
import logging
import os
import secrets
import shlex
import shutil
import signal
import socket
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from e2e_gate_observability import (
    emit_gate_event,
    shell_exit_code,
)
from fixture_control import create_app


LOGGER = logging.getLogger("akb.e2e_runtime")
SCHEMA_VERSION = 2
Scenario = Literal["empty"]
SCENARIO: Scenario = "empty"
DEFAULT_USERNAME_ENV = "AKB_E2E_USERNAME"
DEFAULT_PASSWORD_ENV = "AKB_E2E_PASSWORD"
DEFAULT_APP_PORT = 8000
DEFAULT_EMBED_PORT = 8888
DEFAULT_FIXTURE_PORT = 8889
DEFAULT_COMPOSE_PROJECT = "akb-e2e"
DEFAULT_TIMEOUT_SECONDS = 180.0


class ProvisioningFailure(RuntimeError):
    """A dependency, process, or fixture precondition failed."""


@dataclasses.dataclass(frozen=True)
class CredentialNames:
    username_env: str = DEFAULT_USERNAME_ENV
    password_env: str = DEFAULT_PASSWORD_ENV

    def values(self) -> tuple[str, str]:
        username = os.environ.get(self.username_env, "")
        password = os.environ.get(self.password_env, "")
        if not username or not password:
            raise ProvisioningFailure(
                "fixture credential environment variables are required: "
                f"{self.username_env} and {self.password_env}"
            )
        return username, password


@dataclasses.dataclass(frozen=True)
class RuntimeConfig:
    checkout: Path
    runtime_root: Path
    mode: Literal["gate", "serve"]
    compose_file: Path
    compose_project: str
    app_host: str = "127.0.0.1"
    app_port: int = DEFAULT_APP_PORT
    embed_host: str = "127.0.0.1"
    embed_port: int = DEFAULT_EMBED_PORT
    fixture_host: str = "127.0.0.1"
    fixture_port: int = DEFAULT_FIXTURE_PORT
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    credentials: CredentialNames = dataclasses.field(default_factory=CredentialNames)
    scenario: Scenario = SCENARIO

    @property
    def backend_dir(self) -> Path:
        return self.checkout / "backend"

    @property
    def app_origin(self) -> str:
        return f"http://{self.app_host}:{self.app_port}"

    @property
    def fixture_origin(self) -> str:
        return f"http://{self.fixture_host}:{self.fixture_port}"

    @property
    def state_dir(self) -> Path:
        return self.runtime_root / "state"

    @property
    def vault_dir(self) -> Path:
        return self.state_dir / "vaults"

    @property
    def config_dir(self) -> Path:
        return self.runtime_root / "config"

    @property
    def logs_dir(self) -> Path:
        return self.runtime_root / "logs"


@dataclasses.dataclass
class ManagedProcess:
    process: asyncio.subprocess.Process
    process_group: bool = True


def prepare_private_runtime_root(root: Path) -> tuple[Path, Path, Path, Path]:
    """Create the private runtime directories and return their paths."""

    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not root.is_dir():
        raise ProvisioningFailure(f"runtime root is not a directory: {root}")
    os.chmod(root, 0o700)

    config_dir = root / "config"
    logs_dir = root / "logs"
    state_dir = root / "state"
    vault_dir = state_dir / "vaults"
    for directory in (config_dir, logs_dir, state_dir, vault_dir):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
    return config_dir, logs_dir, state_dir, vault_dir


def _write_private_text(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")
    os.chmod(path, 0o600)


def _http_get(url: str, timeout: float = 2.0) -> tuple[int, bytes]:
    request = Request(url, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except HTTPError as exc:
        return exc.code, exc.read()
    except (URLError, OSError):
        return 0, b""


def _tcp_ready(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


async def terminate_process(
    process: asyncio.subprocess.Process,
    *,
    process_group: bool = True,
    grace_seconds: float = 10.0,
) -> None:
    """Terminate a child and its process group, escalating after a grace period."""

    if process.returncode is not None:
        return
    try:
        if process_group:
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        return

    try:
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)
        return
    except asyncio.TimeoutError:
        pass

    try:
        if process_group:
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        return
    await process.wait()


class E2ERuntime:
    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        self._children: dict[str, ManagedProcess] = {}
        self._active_command: asyncio.subprocess.Process | None = None
        self._fixture_server: Any | None = None
        self._fixture_task: asyncio.Task[Any] | None = None
        self._suite_process: ManagedProcess | None = None
        self._stop_event = asyncio.Event()
        self._reset_lock = asyncio.Lock()
        self._resetting = False
        self._cleaned = False
        self._prepared = False

        self._compose_log = self.config.logs_dir / "compose.log"

    @property
    def app_ready(self) -> bool:
        process = self._children.get("backend")
        return process is not None and process.process.returncode is None

    @property
    def scenario(self) -> Scenario:
        return self.config.scenario

    def fixture_health(self) -> dict[str, object]:
        return {
            "status": "ready" if self.app_ready else "starting",
            "scenario": self.config.scenario,
            "app_ready": self.app_ready,
        }

    def descriptor(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "ready",
            "scenario": self.config.scenario,
            "services": {
                "app": {
                    "origin": self.config.app_origin,
                    "health": {
                        "method": "GET",
                        "url": f"{self.config.app_origin}/readyz",
                    },
                    "discovery": {
                        "method": "GET",
                        "url": f"{self.config.app_origin}/openapi.json",
                    },
                },
                "fixture": {
                    "origin": self.config.fixture_origin,
                    "health": {
                        "method": "GET",
                        "url": f"{self.config.fixture_origin}/health",
                    },
                    "reset": {
                        "method": "POST",
                        "url": f"{self.config.fixture_origin}/reset",
                        "content_type": "application/json",
                        "body": {"scenario": self.config.scenario},
                    },
                    "discovery": {
                        "method": "GET",
                        "url": f"{self.config.fixture_origin}/openapi.json",
                    },
                },
            },
            "credentials": {
                "username_env": self.config.credentials.username_env,
                "password_env": self.config.credentials.password_env,
                "login_path": "/api/v1/auth/login",
            },
        }

    def _validate_checkout(self) -> None:
        if not self.config.checkout.is_dir():
            raise ProvisioningFailure(f"checkout does not exist: {self.config.checkout}")
        required = (
            self.config.backend_dir / "uv.lock",
            self.config.backend_dir / "pyproject.toml",
            self.config.backend_dir / "scripts" / "ci" / "embed_stub.py",
            self.config.backend_dir / "scripts" / "ci" / "e2e_suite_runner.py",
        )
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            raise ProvisioningFailure("checkout is missing runtime inputs")
        if self.config.checkout == self.config.runtime_root or self.config.checkout in self.config.runtime_root.parents:
            raise ProvisioningFailure("runtime root must be outside the checkout")

    def _write_config(self) -> None:
        import yaml

        app_config = {
            "db_host": "127.0.0.1",
            "db_port": 15432,
            "db_name": "akb",
            "db_user": "akb",
            "public_base_url": self.config.app_origin,
            "git_storage_path": str(self.config.vault_dir),
            "vector_store_driver": "pgvector",
            "embed_base_url": f"http://{self.config.embed_host}:{self.config.embed_port}/v1",
            "embed_model": "ci-embed-stub",
            "embed_dimensions": 1536,
            "llm_base_url": "",
            "llm_model": "",
            "rerank_enabled": False,
            "s3_endpoint_url": "http://127.0.0.1:9000",
            "s3_public_url": "http://127.0.0.1:9000",
            "s3_bucket": "akb-files",
        }
        secret_config = {
            "db_password": "akb",
            "jwt_secret": secrets.token_urlsafe(48),
            "embed_api_key": "ci-stub-no-auth",
            "s3_access_key": "akb-ci",
            "s3_secret_key": "akb-ci-secret",
        }
        _write_private_text(
            self.config.config_dir / "app.yaml",
            yaml.safe_dump(app_config, sort_keys=False),
        )
        _write_private_text(
            self.config.config_dir / "secret.yaml",
            yaml.safe_dump(secret_config, sort_keys=False),
        )

    def _child_environment(self) -> dict[str, str]:
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONFAULTHANDLER"] = "1"
        env["AKB_URL"] = self.config.app_origin
        if not env.get("AKB_PG_EXEC"):
            env["AKB_PG_EXEC"] = shlex.join(
                [
                    os.environ.get("AKB_DOCKER_BIN", "docker"),
                    "compose",
                    "--project-name",
                    self.config.compose_project,
                    "--file",
                    str(self.config.compose_file),
                    "exec",
                    "-T",
                    "postgres",
                ]
            )
        env.setdefault("AKB_PG_USER", "akb")
        env.setdefault("AKB_PG_DB", "akb")
        return env

    def _compose_command(self, *arguments: str) -> list[str]:
        return [
            os.environ.get("AKB_DOCKER_BIN", "docker"),
            "compose",
            "--project-name",
            self.config.compose_project,
            "--file",
            str(self.config.compose_file),
            *arguments,
        ]

    async def _run_logged_command(
        self,
        command: list[str],
        *,
        log_path: Path,
        check: bool = True,
    ) -> int:
        log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with log_path.open("ab", buffering=0) as handle:
            os.chmod(log_path, 0o600)
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(self.config.runtime_root),
                env=self._child_environment(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=handle,
                stderr=handle,
                start_new_session=True,
            )
            self._active_command = process
            try:
                returncode = await process.wait()
            finally:
                self._active_command = None
        if check and returncode != 0:
            raise ProvisioningFailure(
                f"command failed with exit code {returncode}; see {log_path}"
            )
        return returncode

    async def _compose(self, *arguments: str, check: bool = True) -> int:
        return await self._run_logged_command(
            self._compose_command(*arguments),
            log_path=self._compose_log,
            check=check,
        )

    async def _wait_until(self, label: str, predicate, timeout: float | None = None) -> None:
        deadline = time.monotonic() + (timeout or self.config.timeout_seconds)
        while time.monotonic() < deadline:
            if predicate():
                return
            await asyncio.sleep(1)
        raise ProvisioningFailure(f"{label} did not become ready; see {self.config.logs_dir}")

    async def _wait_tcp(self, label: str, host: str, port: int) -> None:
        await self._wait_until(
            label,
            lambda: _tcp_ready(host, port),
            timeout=self.config.timeout_seconds,
        )

    async def _wait_http(self, label: str, url: str, predicate) -> bytes:
        last_body = b""

        async def probe() -> bool:
            nonlocal last_body
            status, body = await asyncio.to_thread(_http_get, url)
            last_body = body
            return predicate(status, body)

        deadline = time.monotonic() + self.config.timeout_seconds
        while time.monotonic() < deadline:
            if await probe():
                return last_body
            await asyncio.sleep(1)
        raise ProvisioningFailure(f"{label} did not become ready; see {self.config.logs_dir}")

    async def _spawn_host_process(
        self,
        name: str,
        command: list[str],
        log_name: str,
    ) -> None:
        if name in self._children:
            raise ProvisioningFailure(f"process already running: {name}")
        log_path = self.config.logs_dir / log_name
        with log_path.open("ab", buffering=0) as handle:
            os.chmod(log_path, 0o600)
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(self.config.runtime_root),
                env=self._child_environment(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=handle,
                stderr=handle,
                start_new_session=True,
            )
            self._children[name] = ManagedProcess(process)

    async def _stop_named_process(self, name: str) -> None:
        managed = self._children.pop(name, None)
        if managed is None:
            return
        await terminate_process(managed.process, process_group=managed.process_group)

    async def _start_dependencies(self) -> None:
        await self._compose("up", "--detach")
        await self._wait_tcp("PostgreSQL", "127.0.0.1", 15432)
        await self._wait_http(
            "MinIO",
            "http://127.0.0.1:9000/minio/health/live",
            lambda status, _body: status == 200,
        )
        await asyncio.to_thread(self._ensure_minio_bucket)

    def _ensure_minio_bucket(self) -> None:
        try:
            import boto3
            import botocore

            client = boto3.client(
                "s3",
                endpoint_url="http://127.0.0.1:9000",
                aws_access_key_id="akb-ci",
                aws_secret_access_key="akb-ci-secret",
            )
            try:
                client.create_bucket(Bucket="akb-files")
            except botocore.exceptions.ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                if code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                    raise
        except Exception:  # noqa: BLE001 - hide connection/credential details
            raise ProvisioningFailure("MinIO bucket initialization failed") from None

    async def _start_embed_stub(self) -> None:
        await self._spawn_host_process(
            "embed",
            [
                sys.executable,
                "-m",
                "uvicorn",
                "scripts.ci.embed_stub:app",
                "--app-dir",
                str(self.config.backend_dir),
                "--host",
                self.config.embed_host,
                "--port",
                str(self.config.embed_port),
            ],
            "embed-stub.log",
        )
        await self._wait_http(
            "embedding stub",
            f"http://{self.config.embed_host}:{self.config.embed_port}/healthz",
            lambda status, _body: status == 200,
        )

    async def _start_backend(self) -> None:
        await self._spawn_host_process(
            "backend",
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--app-dir",
                str(self.config.backend_dir),
                "--host",
                self.config.app_host,
                "--port",
                str(self.config.app_port),
            ],
            "backend.log",
        )
        await self._wait_http(
            "backend",
            f"{self.config.app_origin}/readyz",
            self._ready_response,
        )

    @staticmethod
    def _ready_response(status: int, body: bytes) -> bool:
        if status != 200:
            return False
        try:
            return json.loads(body).get("status") == "ready"
        except (ValueError, AttributeError):
            return False

    async def _seed_external_credential(self) -> None:
        username, password = self.config.credentials.values()
        if len(username) > 200 or len(password) > 1000:
            raise ProvisioningFailure("fixture credential value is outside the supported size")

        try:
            import asyncpg
            import bcrypt

            password_hash = await asyncio.to_thread(
                lambda: bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode(
                    "ascii"
                )
            )
            connection = await asyncpg.connect(
                host="127.0.0.1",
                port=15432,
                user="akb",
                password="akb",
                database="akb",
            )
            try:
                await connection.execute(
                    """
                    INSERT INTO users
                        (id, username, email, password_hash, is_admin)
                    VALUES ($1, $2, $3, $4, TRUE)
                    """,
                    uuid.uuid4(),
                    username,
                    f"runtime-{uuid.uuid4().hex}@invalid.akb",
                    password_hash,
                )
            finally:
                await connection.close()
        except ProvisioningFailure:
            raise
        except Exception:
            # Do not include asyncpg/bcrypt exception text: it can carry a
            # credential value from a driver error or a constraint message.
            raise ProvisioningFailure("fixture credential initialization failed") from None

    async def _bootstrap_backend_and_seed(self) -> None:
        # The first boot lets AKB create its schema/migrations.  Stopping it
        # before the direct fixture insert makes reset deterministic and keeps
        # the externally supplied credential out of backend logs/argv.
        await self._start_backend()
        await self._stop_named_process("backend")
        await self._seed_external_credential()
        await self._start_backend()

    async def _start_fixture_control(self) -> None:
        import uvicorn

        app = create_app(self)
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=self.config.fixture_host,
                port=self.config.fixture_port,
                log_level="warning",
                access_log=False,
            )
        )
        self._fixture_server = server
        # ``Server.serve()`` installs process-wide SIGINT/SIGTERM handlers.
        # The supervisor owns those signals so it can stop the backend,
        # embedding stub, and Compose resources as one transaction; invoke
        # uvicorn's lifecycle body without its signal-capture wrapper.
        self._fixture_task = asyncio.create_task(server._serve(), name="fixture-control")
        await self._wait_http(
            "fixture control",
            f"{self.config.fixture_origin}/health",
            lambda status, _body: status == 200,
            )

    async def prepare(self) -> None:
        self._validate_checkout()
        prepare_private_runtime_root(self.config.runtime_root)
        self._write_config()
        self.config.logs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        await self._start_dependencies()
        await self._start_fixture_control()
        await self._start_embed_stub()
        await self._bootstrap_backend_and_seed()
        self._prepared = True

    async def reset_empty(self) -> None:
        async with self._reset_lock:
            if not self._prepared:
                raise ProvisioningFailure("fixture reset requested before runtime readiness")
            self._resetting = True
            try:
                await self._stop_named_process("backend")
                await self._stop_named_process("embed")
                await self._compose("down", "--volumes", "--remove-orphans", check=False)
                if self.config.vault_dir.exists():
                    shutil.rmtree(self.config.vault_dir)
                self.config.vault_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.chmod(self.config.vault_dir, 0o700)
                await self._start_dependencies()
                await self._start_embed_stub()
                await self._bootstrap_backend_and_seed()
            finally:
                self._resetting = False

    async def _serve_foreground(self) -> int:
        stop_task = asyncio.create_task(self._stop_event.wait(), name="serve-stop")
        try:
            while not self._stop_event.is_set():
                child_tasks = [
                    asyncio.create_task(child.process.wait(), name=f"serve-{name}")
                    for name, child in self._children.items()
                ]
                if not child_tasks:
                    await asyncio.sleep(0.1)
                    continue
                try:
                    done, _pending = await asyncio.wait(
                        [stop_task, *child_tasks], return_when=asyncio.FIRST_COMPLETED
                    )
                    if stop_task in done or self._stop_event.is_set():
                        return 0
                    if self._resetting:
                        continue
                    LOGGER.error(
                        "E2E runtime child exited unexpectedly; see %s",
                        self.config.logs_dir,
                    )
                    return 1
                finally:
                    for task in child_tasks:
                        if not task.done():
                            task.cancel()
                    for task in child_tasks:
                        with contextlib.suppress(asyncio.CancelledError):
                            await task
        finally:
            stop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stop_task
        return 0

    async def _run_gate(self) -> int:
        suite_path = self.config.backend_dir / "scripts" / "ci" / "e2e_suite_runner.py"
        log_path = self.config.logs_dir / "gate.log"
        emit_gate_event(
            {
                "event": "gate_start",
                "process": "supervisor",
                "suite_runner": str(suite_path),
                "gate_log": str(log_path),
            },
        )

        child_env = self._child_environment()
        with log_path.open("ab", buffering=0) as handle:
            os.chmod(log_path, 0o600)
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(suite_path),
                "--repo-root",
                str(self.config.checkout),
                cwd=str(self.config.runtime_root),
                env=child_env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=handle,
                start_new_session=True,
            )
            self._suite_process = ManagedProcess(process)
            wait_task = asyncio.create_task(process.wait(), name="e2e-suite")
            stop_task = asyncio.create_task(self._stop_event.wait(), name="e2e-stop")
            try:
                done, _pending = await asyncio.wait(
                    (wait_task, stop_task), return_when=asyncio.FIRST_COMPLETED
                )
                if stop_task in done and not wait_task.done():
                    emit_gate_event(
                        {
                            "event": "runner_stop_requested",
                            "process": "supervisor",
                            "child": "suite_runner",
                        },
                    )
                    await terminate_process(process)
                raw_returncode = process.returncode
                if raw_returncode is None:
                    raw_returncode = await wait_task

                return shell_exit_code(raw_returncode)
            finally:
                stop_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stop_task

    async def _stop_fixture_control(self) -> None:
        if self._fixture_server is not None:
            self._fixture_server.should_exit = True
        if self._fixture_task is not None:
            try:
                await asyncio.wait_for(self._fixture_task, timeout=10)
            except asyncio.TimeoutError:
                self._fixture_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._fixture_task
            except Exception:
                # The fixture task is already terminating; cleanup must still
                # continue to child processes and Compose resources.
                pass
            self._fixture_task = None
        self._fixture_server = None

    async def cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        if self._active_command is not None:
            await terminate_process(self._active_command)
            self._active_command = None
        if self._suite_process is not None:
            await terminate_process(self._suite_process.process)
            self._suite_process = None
        await self._stop_fixture_control()
        await self._stop_named_process("backend")
        await self._stop_named_process("embed")
        with contextlib.suppress(Exception):
            await self._compose("down", "--volumes", "--remove-orphans", check=False)

    def request_stop(self) -> None:
        self._stop_event.set()

    async def run(self) -> int:
        try:
            # Validate before any process/resource is created.  The values are
            # intentionally read only for presence and later hashing; they are
            # never copied into a descriptor, command line, or log message.
            self.config.credentials.values()
            await self.prepare()
            print(json.dumps(self.descriptor(), separators=(",", ":"), ensure_ascii=False), flush=True)
            if self.config.mode == "gate":
                return await self._run_gate()
            return await self._serve_foreground()
        except ProvisioningFailure as exc:
            LOGGER.error("E2E runtime provisioning failed: %s", str(exc))
            return 1
        except Exception:
            LOGGER.error("E2E runtime failed; see %s", self.config.logs_dir)
            return 1
        finally:
            await self.cleanup()


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _parse_args(argv: list[str] | None = None) -> RuntimeConfig:
    default_checkout = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("gate", "serve"))
    parser.add_argument("--checkout", type=Path, default=default_checkout)
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument(
        "--compose-file",
        type=Path,
        default=Path(__file__).with_name("dependency-compose.yaml"),
    )
    parser.add_argument("--compose-project", default="")
    parser.add_argument("--app-port", type=int, default=DEFAULT_APP_PORT)
    parser.add_argument("--embed-port", type=int, default=DEFAULT_EMBED_PORT)
    parser.add_argument("--fixture-port", type=int, default=DEFAULT_FIXTURE_PORT)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--username-env", default=DEFAULT_USERNAME_ENV)
    parser.add_argument("--password-env", default=DEFAULT_PASSWORD_ENV)
    parser.add_argument("--scenario", choices=(SCENARIO,), default=SCENARIO)
    args = parser.parse_args(argv)

    checkout = args.checkout.expanduser().resolve()
    runtime_root = (
        args.runtime_root.expanduser().resolve()
        if args.runtime_root is not None
        else Path(tempfile.mkdtemp(prefix="akb-e2e-runtime-"))
    )
    compose_file = args.compose_file.expanduser().resolve()
    project = args.compose_project or f"{DEFAULT_COMPOSE_PROJECT}-{os.getpid()}-{secrets.token_hex(3)}"
    return RuntimeConfig(
        checkout=checkout,
        runtime_root=runtime_root,
        mode=args.mode,
        compose_file=compose_file,
        compose_project=project,
        app_port=args.app_port,
        embed_port=args.embed_port,
        fixture_port=args.fixture_port,
        timeout_seconds=args.timeout_seconds,
        credentials=CredentialNames(args.username_env, args.password_env),
        scenario=args.scenario,
    )


async def _async_main(config: RuntimeConfig) -> int:
    runtime = E2ERuntime(config)
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, runtime.request_stop)
    return await runtime.run()


def main(argv: list[str] | None = None) -> int:
    faulthandler.enable()
    _configure_logging()
    config = _parse_args(argv)
    return asyncio.run(_async_main(config))


if __name__ == "__main__":
    raise SystemExit(main())
