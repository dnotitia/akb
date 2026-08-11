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
import hashlib
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
Scenario = Literal["empty", "app-installation-lifecycle", "app-release-rollout"]
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
        self._fixture_catalog: dict[str, object] = {
            "status": "starting",
            "scenario": self.config.scenario,
        }
        self._fixture_private_values: tuple[str, ...] = ()
        self._fixture_private_marker = ""
        self._fixture_controls: dict[str, object] = {}

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

    def fixture_discovery(self) -> dict[str, object]:
        """Return fixture coordinates and source-neutral validator entry points."""

        def scrub(value: object) -> object:
            if isinstance(value, dict):
                return {str(key): scrub(item) for key, item in value.items()}
            if isinstance(value, list):
                return [scrub(item) for item in value]
            if isinstance(value, str):
                for private in sorted(
                    (item for item in self._fixture_private_values if item),
                    key=len,
                    reverse=True,
                ):
                    value = value.replace(private, "[redacted]")
            return value

        catalog = scrub(json.loads(json.dumps(self._fixture_catalog)))
        if not isinstance(catalog, dict):
            catalog = {}
        catalog["access"] = {
            "login": {
                "service": "app",
                "method": "POST",
                "path": "/api/v1/auth/login",
                "fields": ["username", "password"],
                "credential_source": "external_env_only",
                "credential_env": {
                    "username": self.config.credentials.username_env,
                    "password": self.config.credentials.password_env,
                },
            }
        }
        catalog["tasks"] = {
            "log_observation": {
                "service": "fixture",
                "method": "GET",
                "path": "/log-observation",
                "result": "sanitized",
            }
        }
        return catalog

    def fixture_log_observation(self) -> dict[str, object]:
        """Return redaction counts without returning runtime log contents."""

        log_text = ""
        if self.config.logs_dir.is_dir():
            for path in self.config.logs_dir.glob("*.log"):
                with contextlib.suppress(OSError):
                    log_text += path.read_text(encoding="utf-8", errors="replace")
        private_hits = sum(value in log_text for value in self._fixture_private_values)
        return {
            "status": "ready" if self._prepared else "starting",
            "scenario": self.config.scenario,
            "redacted": True,
            "redaction_scan": {
                "private_value_hits": private_hits,
                "app_credential_prefix_hits": log_text.count("akb_app_"),
                "app_token_prefix_hits": log_text.count("Bearer eyJ"),
                "raw_log_exposed": False,
            },
            "log_line_count": log_text.count("\n"),
        }

    def fixture_control(
        self,
        action: str,
        target: str | None,
        enabled: bool,
    ) -> dict[str, object]:
        """Apply a bounded, source-neutral test hook and return only outcome state."""
        if self.config.scenario != "app-release-rollout":
            return {"status": "ignored", "scenario": self.config.scenario, "action": action}
        if action == "restart":
            self._fixture_controls["restart_requested"] = bool(enabled)
            if enabled:
                asyncio.create_task(self._restart_backend_for_control(), name="rollout-backend-restart")
        elif action == "worker_observation":
            self._fixture_controls["worker_observation"] = bool(enabled)
        elif action == "failure_injection":
            # The target is a public fixture coordinate (installation ordinal
            # or outcome label), never a SQL selector or private payload.
            if target is not None and len(target) > 128:
                raise ValueError("control target is too long")
            self._fixture_controls["failure_target"] = target if enabled else None
            if enabled and target is not None:
                asyncio.create_task(self._apply_failure_injection(target), name="rollout-failure-injection")
        else:
            return {"status": "ignored", "scenario": self.config.scenario, "action": action}
        return {
            "status": "accepted",
            "scenario": self.config.scenario,
            "action": action,
            "enabled": bool(enabled),
            "observed": {
                "failure_injection": self._fixture_controls.get("failure_target") is not None,
                "worker_observation": bool(self._fixture_controls.get("worker_observation", False)),
                "restart_requested": bool(self._fixture_controls.get("restart_requested", False)),
            },
        }

    async def _apply_failure_injection(self, target: str) -> None:
        """Make one public target fail ownership preflight without raw SQL exposure."""
        try:
            import asyncpg

            app_id = self._fixture_catalog.get("apps", {}).get("target", {}).get("id")
            if not isinstance(app_id, str):
                return
            ordinal = 0 if target.lower() in {"canary", "first"} else int(target)
            connection = await asyncpg.connect(
                host="127.0.0.1", port=15432, user="akb", password="akb", database="akb"
            )
            try:
                installation_id = await connection.fetchval(
                    """SELECT installation_id FROM app_rollout_targets t JOIN app_rollout_jobs j ON j.id=t.job_id WHERE j.app_id=$1 ORDER BY j.created_at DESC, t.ordinal OFFSET $2 LIMIT 1""",
                    uuid.UUID(app_id),
                    ordinal,
                )
                if installation_id is not None:
                    await connection.execute(
                        "UPDATE app_owned_resources SET status='retained' WHERE installation_id=$1 AND status='owned'",
                        installation_id,
                    )
            finally:
                await connection.close()
        except Exception:
            # Controls are best effort and must not leak database/fixture
            # details into the control response or runtime logs.
            return

    async def _restart_backend_for_control(self) -> None:
        try:
            await self._stop_named_process("backend")
            if not self._resetting:
                await self._start_backend()
        except Exception:
            return

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
                        "url": f"{self.config.fixture_origin}/discover",
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
            "app_token_secret": secrets.token_urlsafe(48),
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
        await self._compose("up", "--detach", "--wait")
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

    async def _insert_fixture_user(
        self,
        connection: Any,
        *,
        username: str,
        password_hash: str,
        label: str,
    ) -> uuid.UUID:
        user_id = uuid.uuid4()
        await connection.execute(
            """
            INSERT INTO users (id, username, email, password_hash)
            VALUES ($1, $2, $3, $4)
            """,
            user_id,
            username,
            f"{label}@invalid.akb",
            password_hash,
        )
        return user_id

    async def _insert_fixture_vault(
        self,
        connection: Any,
        *,
        namespace: str,
        label: str,
        owner_id: uuid.UUID,
        grants: list[tuple[uuid.UUID, str]],
        granted_by: uuid.UUID,
    ) -> tuple[uuid.UUID, str]:
        vault_id = uuid.uuid4()
        name = f"{namespace}-vault-{label}"
        await connection.execute(
            """
            INSERT INTO vaults (id, name, git_path, owner_id)
            VALUES ($1, $2, $3, $4)
            """,
            vault_id,
            name,
            str(self.config.vault_dir / f"{name}.git"),
            owner_id,
        )
        for user_id, role in grants:
            await connection.execute(
                """
                INSERT INTO vault_access (vault_id, user_id, role, granted_by)
                VALUES ($1, $2, $3, $4)
                """,
                vault_id,
                user_id,
                role,
                granted_by,
            )
        return vault_id, name

    async def _insert_fixture_release(
        self,
        connection: Any,
        *,
        app_id: uuid.UUID,
        version: str,
        expected_fingerprint: str | None,
    ) -> uuid.UUID:
        manifest: dict[str, object] = {"steps": [{"id": "prepare"}]}
        if expected_fingerprint is not None:
            manifest["expected_schema_fingerprint"] = expected_fingerprint
        encoded = json.dumps(manifest, separators=(",", ":"))
        release_id = uuid.uuid4()
        await connection.execute(
            """
            INSERT INTO app_releases (id, app_id, version, manifest, manifest_checksum)
            VALUES ($1, $2, $3, $4::jsonb, $5)
            """,
            release_id,
            app_id,
            version,
            encoded,
            hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        )
        return release_id

    async def _insert_rollout_release(
        self,
        connection: Any,
        *,
        app_id: uuid.UUID,
        version: str,
        valid: bool = True,
    ) -> tuple[uuid.UUID, str, dict[str, object]]:
        """Insert a v1 release and return its immutable manifest coordinates."""
        def canonical(value: object) -> bytes:
            return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

        if valid:
            steps: list[dict[str, object]] = [
                {
                    "id": "expand_flag",
                    "phase": "expand",
                    "operation": "add_column",
                    "payload": {
                        "table": "rollout_data",
                        "column": {"name": "flag", "type": "text"},
                    },
                },
                {
                    "id": "backfill_flag",
                    "phase": "backfill",
                    "operation": "backfill_column",
                    "payload": {
                        "table": "rollout_data",
                        "column": "flag",
                        "primary_key": "id",
                        "where_null": True,
                        "batch_size": 10,
                        "value": "ready",
                    },
                },
                {
                    "id": "enforce_flag",
                    "phase": "enforce",
                    "operation": "set_not_null",
                    "payload": {"table": "rollout_data", "column": "flag"},
                },
            ]
            for step in steps:
                step["checksum"] = hashlib.sha256(canonical(step)).hexdigest()
            manifest: dict[str, object] = {"manifest_version": 1, "steps": steps}
        else:
            manifest = {
                "manifest_version": 1,
                "steps": [{"id": "contract", "phase": "contract", "operation": "drop_table", "payload": {"table": "rollout_data"}}],
            }
        checksum_payload = {
            "manifest_version": manifest["manifest_version"],
            "steps": [
                {key: value for key, value in step.items() if key != "checksum"}
                for step in manifest["steps"]  # type: ignore[index]
            ],
        }
        checksum = hashlib.sha256(canonical(checksum_payload)).hexdigest()
        manifest["manifest_checksum"] = checksum
        release_id = uuid.uuid4()
        await connection.execute(
            "INSERT INTO app_releases(id, app_id, version, manifest, manifest_checksum) VALUES($1,$2,$3,$4::jsonb,$5)",
            release_id,
            app_id,
            version,
            json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
            checksum,
        )
        return release_id, checksum, manifest

    async def _seed_app_release_rollout(
        self,
        connection: Any,
        *,
        password_hash: str,
        system_admin_id: uuid.UUID,
    ) -> None:
        """Seed 12+ active installations with real tables and bounded rows."""
        namespace = f"rollout-{uuid.uuid4().hex[:12]}"
        owner_id = await self._insert_fixture_user(
            connection,
            username=f"{namespace}-owner",
            password_hash=password_hash,
            label=f"{namespace}-owner",
        )
        target_app_id = uuid.uuid4()
        foreign_app_id = uuid.uuid4()
        await connection.execute(
            "INSERT INTO app_definitions(id,app_key,display_name) VALUES($1,$2,$3),($4,$5,$6)",
            target_app_id,
            f"{namespace}-target",
            "Runtime Rollout Target",
            foreign_app_id,
            f"{namespace}-foreign",
            "Runtime Rollout Foreign",
        )
        old_release, old_checksum, old_manifest = await self._insert_rollout_release(
            connection, app_id=target_app_id, version="1.0.0", valid=True
        )
        next_release, next_checksum, next_manifest = await self._insert_rollout_release(
            connection, app_id=target_app_id, version="2.0.0", valid=True
        )
        unsupported_release, unsupported_checksum, _unsupported_manifest = await self._insert_rollout_release(
            connection, app_id=target_app_id, version="9.0.0", valid=False
        )
        foreign_release, foreign_checksum, _foreign_manifest = await self._insert_rollout_release(
            connection, app_id=foreign_app_id, version="1.0.0", valid=True
        )
        target_grants = [(owner_id, "owner")]
        installations: list[dict[str, object]] = []
        tables: list[dict[str, object]] = []
        for ordinal in range(13):
            vault_id, vault_name = await self._insert_fixture_vault(
                connection,
                namespace=namespace,
                label=f"target-{ordinal:02d}",
                owner_id=owner_id,
                grants=target_grants,
                granted_by=system_admin_id,
            )
            table_name = "rollout_data"
            table_id = uuid.uuid4()
            physical = f"vt_{vault_name.replace('-', '_')}__{table_name}"
            await connection.execute(
                f"CREATE TABLE {physical} (id UUID PRIMARY KEY DEFAULT uuid_generate_v4(), value TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
            )
            await connection.execute(
                "INSERT INTO vault_tables(id,vault_id,name,description,columns,unique_keys,indexes,created_by) VALUES($1,$2,$3,'',$4::jsonb,'[]'::jsonb,'[]'::jsonb,$5)",
                table_id,
                vault_id,
                table_name,
                json.dumps([{"name": "value", "type": "text"}], separators=(",", ":")),
                str(owner_id),
            )
            await connection.executemany(
                f"INSERT INTO {physical}(value) VALUES($1)", [(None,) for _ in range(25)]
            )
            row_ids = [
                str(row["id"])
                for row in await connection.fetch(f"SELECT id FROM {physical} ORDER BY id")
            ]
            installation_id = uuid.uuid4()
            await connection.execute(
                "INSERT INTO vault_app_installations(id,app_id,vault_id,desired_release_id,current_release_id,lifecycle) VALUES($1,$2,$3,$4,$5,'active')",
                installation_id,
                target_app_id,
                vault_id,
                old_release,
                old_release,
            )
            await connection.execute(
                "INSERT INTO installation_grants(installation_id,generation,capabilities,issuer,provenance) VALUES($1,1,$2,'runtime-fixture','{}'::jsonb)",
                installation_id,
                ["installation:read", "inventory:read", "rollout:read", "rollout:request"],
            )
            await connection.execute(
                "INSERT INTO app_owned_resources(installation_id,vault_id,resource_kind,resource_key,status) VALUES($1,$2,'table',$3,'owned')",
                installation_id,
                vault_id,
                table_name,
            )
            await connection.execute(
                "INSERT INTO app_installation_observed_states(installation_id,app_id,vault_id,observed_generation,observed_at,observed_release_id,observed_release_version,observed_grant_generation,checkpoint) VALUES($1,$2,$3,1,NOW(),$4,'1.0.0',1,'{}'::jsonb)",
                installation_id,
                target_app_id,
                vault_id,
                old_release,
            )
            installations.append({"ordinal": ordinal, "id": str(installation_id), "vault_id": str(vault_id), "vault_name": vault_name})
            tables.append({"installation_id": str(installation_id), "vault_id": str(vault_id), "name": table_name, "row_count": 25, "rows": row_ids})

        foreign_vault_id, foreign_vault_name = await self._insert_fixture_vault(
            connection,
            namespace=namespace,
            label="foreign",
            owner_id=owner_id,
            grants=target_grants,
            granted_by=system_admin_id,
        )
        foreign_installation_id = await self._insert_fixture_installation(
            connection,
            app_id=foreign_app_id,
            vault_id=foreign_vault_id,
            desired_release_id=foreign_release,
            current_release_id=foreign_release,
            lifecycle="active",
            capabilities=["installation:read", "inventory:read", "rollout:read", "rollout:request"],
            resources=[],
            observed_release_id=foreign_release,
            observed_release_version="1.0.0",
        )
        random_ids = {
            "app_id": str(uuid.uuid4()),
            "release_id": str(uuid.uuid4()),
            "installation_id": str(uuid.uuid4()),
            "vault_id": str(uuid.uuid4()),
        }
        self._fixture_catalog = {
            "status": "ready",
            "scenario": self.config.scenario,
            "namespace": namespace,
            "actors": {"system_admin": {"id": str(system_admin_id), "role": "system_admin"}, "target_owner": {"id": str(owner_id), "role": "owner"}},
            "apps": {"target": {"id": str(target_app_id)}, "foreign": {"id": str(foreign_app_id)}},
            "releases": {
                "target_current": {"id": str(old_release), "version": "1.0.0", "manifest_checksum": old_checksum},
                "target_next": {"id": str(next_release), "version": "2.0.0", "manifest_checksum": next_checksum},
                "target_unsupported": {"id": str(unsupported_release), "version": "9.0.0", "manifest_checksum": unsupported_checksum},
                "foreign": {"id": str(foreign_release), "version": "1.0.0", "manifest_checksum": foreign_checksum},
            },
            "installations": installations,
            "tables": tables,
            "foreign_ids": {"real": {"app_id": str(foreign_app_id), "release_id": str(foreign_release), "installation_id": str(foreign_installation_id), "vault_id": str(foreign_vault_id)}, "random": random_ids},
            "coordinates": {
                "admin": {
                    "credential": {"service": "app", "method": "POST", "path": f"/api/v1/apps/{target_app_id}/credentials", "body": {"deployment": "validator"}, "result": "credential_value_is_request_scoped"},
                    "exchange": {"service": "app", "method": "POST", "path": "/api/v1/auth/app-token", "body": {"credential": "value_from_previous_response"}},
                    "request": {"service": "app", "method": "POST", "path": f"/api/v1/apps/{target_app_id}/rollouts", "body": {"release_id": str(next_release), "manifest_checksum": next_checksum, "idempotency_key": "uuid-v4"}, "headers": {"Idempotency-Key": "uuid-v4"}},
                    "status": {"service": "app", "method": "GET", "path": f"/api/v1/apps/{target_app_id}/rollouts/{{rollout_id}}"},
                },
                "self_app": {
                    "request": {"service": "app", "method": "POST", "path": "/api/v1/app/rollouts", "body": {"release_id": str(next_release), "manifest_checksum": next_checksum}, "headers": {"Idempotency-Key": "uuid-v4"}},
                    "status": {"service": "app", "method": "GET", "path": "/api/v1/app/rollouts/{rollout_id}"},
                },
                "installation_status": {"service": "app", "method": "GET", "path": f"/api/v1/apps/{target_app_id}/installations/{{vault_id}}"},
            },
            "controls": {
                "failure_injection": {"service": "fixture", "method": "POST", "path": "/control", "body": {"action": "failure_injection", "target": "installation ordinal or outcome label", "enabled": True}, "result": "outcome_only"},
                "worker_observation": {"service": "fixture", "method": "POST", "path": "/control", "body": {"action": "worker_observation", "enabled": True}, "result": "sanitized"},
                "restart": {"service": "fixture", "method": "POST", "path": "/control", "body": {"action": "restart", "enabled": True}, "result": "outcome_only"},
            },
            "polling": {
                "rollout_status": {
                    "method": "GET",
                    "path": f"/api/v1/apps/{target_app_id}/rollouts/{{rollout_id}}",
                    "interval_seconds": 0.5,
                    "timeout_seconds": 180,
                    "terminal_statuses": ["applied", "blocked"],
                },
                "installation_status": {
                    "method": "GET",
                    "path": f"/api/v1/apps/{target_app_id}/installations/{{vault_id}}",
                    "interval_seconds": 0.5,
                    "timeout_seconds": 180,
                    "terminal_lifecycle": ["active", "blocked"],
                },
            },
        }

    async def _insert_fixture_installation(
        self,
        connection: Any,
        *,
        app_id: uuid.UUID,
        vault_id: uuid.UUID,
        desired_release_id: uuid.UUID,
        current_release_id: uuid.UUID | None,
        lifecycle: str,
        capabilities: list[str],
        resources: list[tuple[str, str, str]],
        observed_release_id: uuid.UUID | None = None,
        observed_release_version: str | None = None,
        schema_fingerprint: str | None = None,
        blocked_reason: str | None = None,
    ) -> uuid.UUID:
        installation_id = uuid.uuid4()
        initial_current = (
            current_release_id
            if current_release_id is not None
            else desired_release_id
            if lifecycle == "uninstalled"
            else None
        )
        initial_lifecycle = lifecycle if lifecycle != "uninstalled" else "active"
        await connection.execute(
            """
            INSERT INTO vault_app_installations (
                id, app_id, vault_id, desired_release_id, current_release_id,
                lifecycle, blocked_reason
            ) VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            installation_id,
            app_id,
            vault_id,
            desired_release_id,
            initial_current,
            initial_lifecycle,
            blocked_reason if initial_lifecycle == "blocked" else None,
        )
        await connection.execute(
            """
            INSERT INTO installation_grants (
                installation_id, generation, capabilities, issuer, provenance
            ) VALUES ($1, 1, $2, 'runtime-fixture', $3::jsonb)
            """,
            installation_id,
            sorted(capabilities),
            json.dumps({"source": "runtime_fixture", "mode": "fixture"}),
        )
        if lifecycle == "uninstalled":
            await connection.execute(
                """
                UPDATE installation_grants
                   SET status = 'revoked', revoked_at = NOW()
                 WHERE installation_id = $1
                """,
                installation_id,
            )
            await connection.execute(
                """
                UPDATE vault_app_installations
                   SET desired_release_id = NULL,
                       lifecycle = 'uninstalled',
                       blocked_reason = NULL
                 WHERE id = $1
                """,
                installation_id,
            )
        for resource_kind, resource_key, status in resources:
            await connection.execute(
                """
                INSERT INTO app_owned_resources (
                    installation_id, vault_id, resource_kind, resource_key, status
                ) VALUES ($1, $2, $3, $4, $5)
                """,
                installation_id,
                vault_id,
                resource_kind,
                resource_key,
                status,
            )
        if observed_release_id is not None:
            await connection.execute(
                """
                INSERT INTO app_installation_observed_states (
                    installation_id, app_id, vault_id, observed_generation,
                    observed_at, observed_release_id, observed_release_version,
                    schema_fingerprint, observed_grant_generation,
                    checkpoint, recent_error
                ) VALUES (
                    $1, $2, $3, 1, NOW(), $4, $5, $6, 1, $7::jsonb, $8::jsonb
                )
                """,
                installation_id,
                app_id,
                vault_id,
                observed_release_id,
                observed_release_version,
                schema_fingerprint,
                json.dumps(
                    {
                        "phase": "ready",
                        "private_marker": self._fixture_private_marker,
                        "worker_payload": {"token": "private"},
                    }
                ),
                json.dumps(
                    {
                        "code": "fixture_recent_error",
                        "message": self._fixture_private_marker,
                        "payload": {"private_field": "private"},
                    }
                ),
            )
        return installation_id

    async def _seed_app_installation_lifecycle(
        self,
        connection: Any,
        *,
        password_hash: str,
        system_admin_id: uuid.UUID,
    ) -> None:
        namespace = f"fixture-{uuid.uuid4().hex[:12]}"
        actor_specs = (
            ("target_owner", "target-owner"),
            ("target_admin", "target-admin"),
            ("reader", "reader"),
            ("writer", "writer"),
            ("foreign_admin", "foreign-admin"),
        )
        actors: dict[str, dict[str, object]] = {
            "system_admin": {"id": str(system_admin_id), "role": "system_admin"}
        }
        actor_ids: dict[str, uuid.UUID] = {}
        for role, label in actor_specs:
            username = f"{namespace}-{label}"
            user_id = await self._insert_fixture_user(
                connection,
                username=username,
                password_hash=password_hash,
                label=f"{namespace}-{label}",
            )
            actor_ids[role] = user_id
            vault_role = {
                "target_owner": "owner",
                "target_admin": "admin",
                "reader": "reader",
                "writer": "writer",
                "foreign_admin": "owner",
            }[role]
            vault_scope = "foreign" if role == "foreign_admin" else "target"
            actors[role] = {
                "id": str(user_id),
                "username": username,
                "role": role,
                "vault_role": vault_role,
                "vault_scope": vault_scope,
            }

        target_grants = [
            (actor_ids["target_owner"], "owner"),
            (actor_ids["target_admin"], "admin"),
            (actor_ids["reader"], "reader"),
            (actor_ids["writer"], "writer"),
        ]
        vaults: dict[str, dict[str, str]] = {}
        target_vault_ids: dict[str, uuid.UUID] = {}
        for label in (
            "install",
            "installing",
            "active",
            "blocked",
            "uninstalled",
            "restore-compatible",
            "restore-mismatch",
            "restore-unknown",
            "fresh-retained",
            "fresh-empty",
        ):
            vault_id, vault_name = await self._insert_fixture_vault(
                connection,
                namespace=namespace,
                label=label,
                owner_id=actor_ids["target_owner"],
                grants=target_grants,
                granted_by=system_admin_id,
            )
            target_vault_ids[label] = vault_id
            vaults[label] = {"id": str(vault_id), "name": vault_name}

        foreign_vault_id, foreign_vault_name = await self._insert_fixture_vault(
            connection,
            namespace=namespace,
            label="foreign",
            owner_id=actor_ids["foreign_admin"],
            grants=[(actor_ids["foreign_admin"], "owner")],
            granted_by=system_admin_id,
        )
        vaults["foreign"] = {"id": str(foreign_vault_id), "name": foreign_vault_name}

        target_app_id = uuid.uuid4()
        foreign_app_id = uuid.uuid4()
        await connection.execute(
            """
            INSERT INTO app_definitions (id, app_key, display_name)
            VALUES ($1, $2, $3), ($4, $5, $6)
            """,
            target_app_id,
            f"{namespace}-target-app",
            "Runtime Target App",
            foreign_app_id,
            f"{namespace}-foreign-app",
            "Runtime Foreign App",
        )
        release_a = await self._insert_fixture_release(
            connection,
            app_id=target_app_id,
            version="1.0.0",
            expected_fingerprint="a" * 64,
        )
        release_b = await self._insert_fixture_release(
            connection,
            app_id=target_app_id,
            version="2.0.0",
            expected_fingerprint="c" * 64,
        )
        release_unknown = await self._insert_fixture_release(
            connection,
            app_id=target_app_id,
            version="3.0.0",
            expected_fingerprint=None,
        )
        foreign_release = await self._insert_fixture_release(
            connection,
            app_id=foreign_app_id,
            version="1.0.0",
            expected_fingerprint="a" * 64,
        )

        fixtures: dict[str, dict[str, object]] = {}

        async def add_fixture(
            name: str,
            *,
            vault_label: str,
            release_id: uuid.UUID,
            installation_id: uuid.UUID | None = None,
            requested_release_id: uuid.UUID | None = None,
        ) -> None:
            item: dict[str, object] = {
                "app_id": str(target_app_id),
                "vault_id": str(target_vault_ids[vault_label]),
                "release_id": str(release_id),
            }
            if requested_release_id is not None:
                item["requested_release_id"] = str(requested_release_id)
            if installation_id is not None:
                item["installation_id"] = str(installation_id)
            fixtures[name] = item

        install_vault = target_vault_ids["install"]
        await add_fixture("install", vault_label="install", release_id=release_a)
        await add_fixture("install_conflict", vault_label="install", release_id=release_b)

        installing_id = await self._insert_fixture_installation(
            connection,
            app_id=target_app_id,
            vault_id=target_vault_ids["installing"],
            desired_release_id=release_a,
            current_release_id=None,
            lifecycle="installing",
            capabilities=["installation:read"],
            resources=[],
        )
        await add_fixture(
            "status_installing",
            vault_label="installing",
            release_id=release_a,
            installation_id=installing_id,
        )

        active_id = await self._insert_fixture_installation(
            connection,
            app_id=target_app_id,
            vault_id=target_vault_ids["active"],
            desired_release_id=release_a,
            current_release_id=release_a,
            lifecycle="active",
            capabilities=["installation:read", "inventory:read"],
            resources=[("table", f"{namespace}-active-table", "owned")],
            observed_release_id=release_a,
            observed_release_version="1.0.0",
            schema_fingerprint="a" * 64,
        )
        await add_fixture(
            "status_active",
            vault_label="active",
            release_id=release_a,
            installation_id=active_id,
        )

        blocked_id = await self._insert_fixture_installation(
            connection,
            app_id=target_app_id,
            vault_id=target_vault_ids["blocked"],
            desired_release_id=release_b,
            current_release_id=release_a,
            lifecycle="blocked",
            capabilities=["installation:read"],
            resources=[("table", f"{namespace}-blocked-table", "owned")],
            observed_release_id=release_a,
            observed_release_version="1.0.0",
            schema_fingerprint="a" * 64,
            blocked_reason="fixture_blocked",
        )
        await add_fixture(
            "status_blocked",
            vault_label="blocked",
            release_id=release_b,
            installation_id=blocked_id,
            requested_release_id=release_b,
        )

        uninstalled_id = await self._insert_fixture_installation(
            connection,
            app_id=target_app_id,
            vault_id=target_vault_ids["uninstalled"],
            desired_release_id=release_a,
            current_release_id=release_a,
            lifecycle="uninstalled",
            capabilities=["installation:read"],
            resources=[("table", f"{namespace}-uninstalled-table", "retained")],
        )
        await add_fixture(
            "status_uninstalled",
            vault_label="uninstalled",
            release_id=release_a,
            installation_id=uninstalled_id,
        )

        restore_compatible_id = await self._insert_fixture_installation(
            connection,
            app_id=target_app_id,
            vault_id=target_vault_ids["restore-compatible"],
            desired_release_id=release_a,
            current_release_id=release_a,
            lifecycle="uninstalled",
            capabilities=["installation:read"],
            resources=[("table", f"{namespace}-restore-table", "retained")],
            observed_release_id=release_a,
            observed_release_version="1.0.0",
            schema_fingerprint="a" * 64,
        )
        await add_fixture(
            "restore_compatible",
            vault_label="restore-compatible",
            release_id=release_a,
            installation_id=restore_compatible_id,
        )

        restore_mismatch_id = await self._insert_fixture_installation(
            connection,
            app_id=target_app_id,
            vault_id=target_vault_ids["restore-mismatch"],
            desired_release_id=release_a,
            current_release_id=release_a,
            lifecycle="uninstalled",
            capabilities=["installation:read"],
            resources=[("table", f"{namespace}-mismatch-table", "retained")],
            observed_release_id=release_a,
            observed_release_version="1.0.0",
            schema_fingerprint="b" * 64,
        )
        await add_fixture(
            "restore_mismatch",
            vault_label="restore-mismatch",
            release_id=release_a,
            installation_id=restore_mismatch_id,
        )

        restore_unknown_id = await self._insert_fixture_installation(
            connection,
            app_id=target_app_id,
            vault_id=target_vault_ids["restore-unknown"],
            desired_release_id=release_unknown,
            current_release_id=release_unknown,
            lifecycle="uninstalled",
            capabilities=["installation:read"],
            resources=[("table", f"{namespace}-unknown-table", "retained")],
            observed_release_id=release_unknown,
            observed_release_version="3.0.0",
            schema_fingerprint="a" * 64,
        )
        await add_fixture(
            "restore_unknown",
            vault_label="restore-unknown",
            release_id=release_unknown,
            installation_id=restore_unknown_id,
        )

        fresh_retained_id = await self._insert_fixture_installation(
            connection,
            app_id=target_app_id,
            vault_id=target_vault_ids["fresh-retained"],
            desired_release_id=release_a,
            current_release_id=release_a,
            lifecycle="uninstalled",
            capabilities=["installation:read"],
            resources=[("table", f"{namespace}-fresh-retained-table", "retained")],
        )
        await add_fixture(
            "fresh_retained",
            vault_label="fresh-retained",
            release_id=release_b,
            installation_id=fresh_retained_id,
            requested_release_id=release_b,
        )

        fresh_empty_id = await self._insert_fixture_installation(
            connection,
            app_id=target_app_id,
            vault_id=target_vault_ids["fresh-empty"],
            desired_release_id=release_a,
            current_release_id=release_a,
            lifecycle="uninstalled",
            capabilities=["installation:read"],
            resources=[],
        )
        await add_fixture(
            "fresh_empty",
            vault_label="fresh-empty",
            release_id=release_b,
            installation_id=fresh_empty_id,
            requested_release_id=release_b,
        )

        foreign_installation_id = await self._insert_fixture_installation(
            connection,
            app_id=foreign_app_id,
            vault_id=foreign_vault_id,
            desired_release_id=foreign_release,
            current_release_id=foreign_release,
            lifecycle="active",
            capabilities=["installation:read"],
            resources=[],
        )

        self._fixture_catalog = {
            "status": "ready",
            "scenario": self.config.scenario,
            "namespace": namespace,
            "actors": actors,
            "apps": {
                "target": {"id": str(target_app_id)},
                "foreign": {"id": str(foreign_app_id)},
            },
            "releases": {
                "target_primary": {"id": str(release_a), "version": "1.0.0"},
                "target_next": {"id": str(release_b), "version": "2.0.0"},
                "target_unknown": {"id": str(release_unknown), "version": "3.0.0"},
                "foreign": {"id": str(foreign_release), "version": "1.0.0"},
            },
            "vaults": vaults,
            "fixtures": fixtures,
            "commands": {
                "install": {
                    "app_id": str(target_app_id),
                    "vault_id": str(install_vault),
                    "release_id": str(release_a),
                },
                "conflict_release": {
                    "app_id": str(target_app_id),
                    "vault_id": str(install_vault),
                    "release_id": str(release_b),
                },
            },
            "foreign_installation": {
                "app_id": str(foreign_app_id),
                "vault_id": str(foreign_vault_id),
                "release_id": str(foreign_release),
                "installation_id": str(foreign_installation_id),
            },
        }

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
            self._fixture_private_marker = f"runtime-private-{uuid.uuid4().hex}"
            self._fixture_private_values = (username, password, self._fixture_private_marker)
            system_admin_id = uuid.uuid4()
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
                    system_admin_id,
                    username,
                    f"runtime-{uuid.uuid4().hex}@invalid.akb",
                    password_hash,
                )
                if self.config.scenario == "app-installation-lifecycle":
                    await self._seed_app_installation_lifecycle(
                        connection,
                        password_hash=password_hash,
                        system_admin_id=system_admin_id,
                    )
                elif self.config.scenario == "app-release-rollout":
                    await self._seed_app_release_rollout(
                        connection,
                        password_hash=password_hash,
                        system_admin_id=system_admin_id,
                    )
                else:
                    self._fixture_catalog = {
                        "status": "ready",
                        "scenario": self.config.scenario,
                        "namespace": f"fixture-{uuid.uuid4().hex[:12]}",
                        "actors": {
                            "system_admin": {
                                "id": str(system_admin_id),
                                "role": "system_admin",
                            }
                        },
                        "fixtures": {},
                    }
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

    async def reset_scenario(self) -> None:
        async with self._reset_lock:
            if not self._prepared:
                raise ProvisioningFailure("fixture reset requested before runtime readiness")
            self._resetting = True
            try:
                self._fixture_controls.clear()
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
    parser.add_argument(
        "--scenario",
        choices=("empty", "app-installation-lifecycle", "app-release-rollout"),
        default=SCENARIO,
    )
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
