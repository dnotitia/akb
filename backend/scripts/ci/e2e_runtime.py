"""Repository-owned live E2E runtime for a clean Ubuntu 24.04 host.

The runtime owns only the test infrastructure around AKB:

* Compose manages the pinned PostgreSQL/pgvector and MinIO dependencies.
* Uvicorn runs the embedding stub and backend as host processes.
* A small in-process fixture control app exposes health, discovery, and the
  bounded scenario reset.
* Optional profiles add the clean stdio proxy consumer and/or a lightweight
  OIDC Resource Server fixture without changing the schema-v2 descriptor.

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
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Literal, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from e2e_gate_observability import (
    emit_gate_event,
    shell_exit_code,
)
from fixture_control import create_app
from oidc_fixture import OIDCFixture


LOGGER = logging.getLogger("akb.e2e_runtime")
SCHEMA_VERSION = 2
PROTOCOL_REVISION = "2026-07-28"
LEGACY_PROTOCOL_REVISIONS = (
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    "2025-11-25",
)
Scenario = Literal[
    "empty",
    "app-installation-lifecycle",
    "app-release-rollout",
    "app-control-plane",
]
SCENARIO: Scenario = "empty"
DEFAULT_USERNAME_ENV = "AKB_E2E_USERNAME"
DEFAULT_PASSWORD_ENV = "AKB_E2E_PASSWORD"
DEFAULT_APP_PORT = 8000
DEFAULT_EMBED_PORT = 8888
DEFAULT_FIXTURE_PORT = 8889
DEFAULT_COMPOSE_PROJECT = "akb-e2e"
DEFAULT_TIMEOUT_SECONDS = 180.0
DEFAULT_PROFILE = "tool-only"


@dataclasses.dataclass(frozen=True)
class CapabilityProfile:
    """One explicit, fail-closed runtime capability selection."""

    name: str
    capabilities: frozenset[str]

    @property
    def needs_stdio(self) -> bool:
        return "stdio" in self.capabilities

    @property
    def needs_oidc(self) -> bool:
        return "oidc" in self.capabilities

    @property
    def needs_keycloak_overlay(self) -> bool:
        return "keycloak" in self.capabilities


_PROFILE_CAPABILITIES: dict[str, frozenset[str]] = {
    "tool-only": frozenset({"http", "pat"}),
    "transport-proxy": frozenset({"http", "pat", "stdio"}),
    "oidc-resource-server": frozenset({"http", "pat", "oidc"}),
    "transport-oidc": frozenset({"http", "pat", "stdio", "oidc"}),
    # The common runtime never starts Keycloak.  Selecting this profile is an
    # explicit request for the specialist overlay and therefore fails closed
    # unless the orchestrator supplies that separate runtime.
    "keycloak-overlay": frozenset({"http", "pat", "keycloak"}),
}
_CAPABILITY_ALIASES = {
    "http": "http",
    "pat": "pat",
    "stdio": "stdio",
    "oidc": "oidc",
    "keycloak": "keycloak",
}


def _canonical_capabilities(values: Sequence[str]) -> frozenset[str]:
    selected: set[str] = set()
    for raw in values:
        for token in re.split(r"[,+]", raw.strip().lower().replace("_", "-").replace("/", "-")):
            if not token:
                continue
            capability = _CAPABILITY_ALIASES.get(token)
            if capability is None:
                raise ValueError(f"unknown runtime capability: {raw}")
            selected.add(capability)
    # Every supported MCP profile is rooted in the same HTTP/PAT fixture.  A
    # caller selecting only a transport capability still gets the complete
    # base rather than an accidental unauthenticated or HTTP-less runtime.
    selected.update({"http", "pat"})
    return frozenset(selected)


def select_capability_profile(
    profile: str = DEFAULT_PROFILE,
    capabilities: Sequence[str] = (),
) -> CapabilityProfile:
    """Resolve one profile name plus optional capability additions."""

    normalized = profile.strip().lower()

    if normalized not in _PROFILE_CAPABILITIES:
        raise ValueError(f"unknown runtime capability profile: {profile}")
    selected = set(_PROFILE_CAPABILITIES[normalized])
    if capabilities:
        selected.update(_canonical_capabilities(capabilities))
    resolved = frozenset(selected)
    canonical_name = next(
        (name for name, values in _PROFILE_CAPABILITIES.items() if values == resolved),
        "custom-" + "-".join(sorted(resolved)),
    )
    return CapabilityProfile(canonical_name, resolved)


class ProvisioningFailure(RuntimeError):
    """A dependency, process, or fixture precondition failed."""


class BlockedRuntimeConfig(ProvisioningFailure):
    """A requested capability cannot be provided by this runtime."""

    code = "blocked_runtime_config"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")


class ProductAssertionFailure(RuntimeError):
    """A live profile assertion failed after provisioning succeeded."""

    code = "product_assertion_failed"


@dataclasses.dataclass(frozen=True)
class CredentialNames:
    username_env: str = DEFAULT_USERNAME_ENV
    password_env: str = DEFAULT_PASSWORD_ENV
    pat_env: str = "AKB_E2E_PAT"

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
    profile: str = DEFAULT_PROFILE
    capabilities: tuple[str, ...] = ()

    @property
    def capability_profile(self) -> CapabilityProfile:
        return select_capability_profile(self.profile, self.capabilities)

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

    @property
    def proxy_package_dir(self) -> Path:
        return self.checkout / "packages" / "akb-mcp-client"

    @property
    def proxy_consumer_dir(self) -> Path:
        return self.runtime_root / "node-consumer"


@dataclasses.dataclass
class ManagedProcess:
    process: asyncio.subprocess.Process
    process_group: bool = True
    stdin: asyncio.StreamWriter | None = None
    stdout: asyncio.StreamReader | None = None


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


def _http_json(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, object] | None = None,
    authorization: str | None = None,
    timeout: float = 10.0,
) -> tuple[int, bytes]:
    """Small JSON client used for runtime-owned credential provisioning."""

    payload = json.dumps(body, separators=(",", ":")).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if authorization:
        headers["Authorization"] = authorization
    request = Request(url, data=payload, headers=headers, method=method)
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
        self.profile = config.capability_profile
        self._children: dict[str, ManagedProcess] = {}
        self._active_command: asyncio.subprocess.Process | None = None
        self._fixture_server: Any | None = None
        self._fixture_task: asyncio.Task[Any] | None = None
        self._suite_process: ManagedProcess | None = None
        self._stop_event = asyncio.Event()
        self._reset_lock = asyncio.Lock()
        self._resetting = False
        self._lifecycle_generation = 0
        self._cleaned = False
        self._prepared = False
        self._fixture_catalog: dict[str, object] = {
            "status": "starting",
            "scenario": self.config.scenario,
        }
        self._fixture_private_values: tuple[str, ...] = ()
        self._fixture_private_marker = ""
        self._fixture_controls: dict[str, object] = {}
        self._pat_value = ""
        self._candidate_revision: str | None = None
        self._proxy_version: str | None = None
        self._stdio_initialize_observed = False
        self._stdio_discover_observed = False
        self._stdio_tools_list_observed = False
        self._stdio_modern_tools_list_observed = False
        self._stdio_read_call_observed = False
        self._stdio_modern_read_call_observed = False
        self._stdio_next_id = 2
        self.oidc_fixture: OIDCFixture | None = (
            OIDCFixture(
                origin=config.fixture_origin,
                realm="runtime",
                audience=f"{config.app_origin}/mcp",
            )
            if self.profile.needs_oidc
            else None
        )

        self._compose_log = self.config.logs_dir / "compose.log"

    @property
    def app_ready(self) -> bool:
        process = self._children.get("backend")
        return process is not None and process.process.returncode is None

    @property
    def scenario(self) -> Scenario:
        return self.config.scenario

    @property
    def selected_capabilities(self) -> tuple[str, ...]:
        return tuple(sorted(self.profile.capabilities))

    def _source_revision(self) -> str:
        if self._candidate_revision is not None:
            return self._candidate_revision
        try:
            completed = subprocess.run(
                ["git", "-C", str(self.config.checkout), "rev-parse", "HEAD"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
            )
        except OSError:
            completed = None
        revision = completed.stdout.strip() if completed and completed.returncode == 0 else "unknown"
        self._candidate_revision = revision
        return revision

    def _artifact_version(self, package_dir: Path, *, default: str = "unknown") -> str:
        package_file = package_dir / "package.json"
        try:
            parsed = json.loads(package_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return default
        version = parsed.get("version")
        return version if isinstance(version, str) and version else default

    def _backend_version(self) -> str:
        try:
            text = (self.config.backend_dir / "pyproject.toml").read_text(encoding="utf-8")
        except OSError:
            return "unknown"
        match = re.search(r"(?m)^version\s*=\s*[\"']([^\"']+)[\"']", text)
        return match.group(1) if match else "unknown"

    def runtime_evidence(self) -> dict[str, object]:
        """Return source-bound, credential-free evidence coordinates."""

        fixture_namespace = self._fixture_catalog.get("namespace")
        fixture: dict[str, object] = {
            "scenario": self.config.scenario,
            "reset": {
                "method": "POST",
                "url": f"{self.config.fixture_origin}/reset",
                "body": {"scenario": self.config.scenario},
            },
        }
        if isinstance(fixture_namespace, str):
            fixture["namespace"] = fixture_namespace
        evidence: dict[str, object] = {
            "source_revision": self._source_revision(),
            "backend_artifact_version": self._backend_version(),
            "protocol_revision": PROTOCOL_REVISION,
            "protocol_matrix": {
                "modern": PROTOCOL_REVISION,
                "legacy": list(LEGACY_PROTOCOL_REVISIONS),
            },
            "transport": ["http", "stdio"] if self.profile.needs_stdio else ["http"],
            "selected_capabilities": list(self.selected_capabilities),
            "tool_cases": {
                "read": "akb_list_vaults",
                "write": "akb_put",
                "destructive": "akb_delete",
                "comparison": "same candidate, fixture, origin and credential",
            },
            "origin": {
                "backend": self.config.app_origin,
                "fixture": self.config.fixture_origin,
            },
            "credential_env": {
                "username": self.config.credentials.username_env,
                "password": self.config.credentials.password_env,
                "pat": self.config.credentials.pat_env,
            },
            "pat": {
                "credential_env": self.config.credentials.pat_env,
                "mint": {
                    "service": "app",
                    "method": "POST",
                    "path": "/api/v1/auth/tokens",
                    "body_fields": ["name", "scopes", "vault_scope"],
                    "auth": "login_session",
                },
                "cases": {
                    "valid": {"authorization": "credential_env"},
                    "unauthenticated": {"authorization": "omitted"},
                    "read_only": {"scope": "akb:vault:read"},
                    "write": {"scope": "akb:vault:write"},
                    "destructive": {"scope": "akb:vault:admin"},
                },
            },
            "fixture": fixture,
            "failure_stages": ["provisioning", "product_assertion"],
        }
        if self.profile.needs_stdio:
            if self._proxy_version is None:
                self._proxy_version = self._artifact_version(self.config.proxy_package_dir)
            evidence["proxy_artifact_version"] = self._proxy_version
            evidence["stdio"] = {
                "package": "akb-mcp",
                "executable": "akb-mcp",
                "consumer_root": str(self.config.proxy_consumer_dir),
                "environment": {
                    "AKB_MCP_URL": f"{self.config.app_origin}/mcp/",
                    "AKB_PAT": self.config.credentials.pat_env,
                },
                "initialize_observed": self._stdio_initialize_observed,
                "discover_observed": self._stdio_discover_observed,
                "tools_list_observed": getattr(self, "_stdio_tools_list_observed", False),
                "modern_tools_list_observed": getattr(self, "_stdio_modern_tools_list_observed", False),
                "read_call_observed": getattr(self, "_stdio_read_call_observed", False),
                "modern_read_call_observed": getattr(self, "_stdio_modern_read_call_observed", False),
            }
        if self.oidc_fixture is not None:
            oidc_evidence = self.oidc_fixture.discovery()
            oidc_evidence["resource_metadata"] = {
                "method": "GET",
                "url": f"{self.config.app_origin}/.well-known/oauth-protected-resource",
            }
            oidc_evidence["challenge"] = {
                "method": "POST",
                "url": f"{self.config.app_origin}/mcp/",
                "authorization": "omitted",
            }
            evidence["oidc"] = oidc_evidence
        return evidence

    def fixture_health(self) -> dict[str, object]:
        return {
            "status": "ready" if self.app_ready else "starting",
            "scenario": self.config.scenario,
            "app_ready": self.app_ready,
        }

    def fixture_discovery(self) -> dict[str, object]:
        """Return sanitized product coordinates and bounded fixture controls."""

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
            }
        }
        catalog["observability"] = {
            "log_observation": {
                "service": "fixture",
                "method": "GET",
                "path": "/log-observation",
            }
        }
        catalog["runtime"] = self.runtime_evidence()
        if self.oidc_fixture is not None:
            catalog["oidc"] = self.oidc_fixture.discovery()
        if self.config.scenario in {"app-release-rollout", "app-control-plane"}:
            installations = catalog.get("installations", [])
            targets: list[dict[str, str]] = []
            if isinstance(installations, list):
                for item in installations:
                    if not isinstance(item, dict):
                        continue
                    fixture_id = item.get("fixture_id") or item.get("label") or item.get("id")
                    installation_id = item.get("id")
                    if isinstance(fixture_id, str) and isinstance(installation_id, str):
                        targets.append({"fixture_id": fixture_id, "installation_id": installation_id})
            kinds = ["missing_owned_table"]
            fixtures = catalog.get("fixtures", {})
            legacy_target = None
            if self.config.scenario == "app-control-plane" and isinstance(fixtures, dict):
                candidate = fixtures.get("legacy_adoption")
                if isinstance(candidate, dict):
                    fixture_id = candidate.get("fixture_id", "legacy-adoption")
                    vault_id = candidate.get("vault_id")
                    if isinstance(fixture_id, str) and isinstance(vault_id, str):
                        kinds.append("legacy_schema_drift")
                        legacy_target = {
                            "fixture_id": fixture_id,
                            "vault_id": vault_id,
                            "target_type": "legacy_adoption",
                        }
            if legacy_target is not None:
                targets.append(legacy_target)
            catalog["controls"] = {
                "fault_injection": {
                    "service": "fixture",
                    "method": "POST",
                    "path": "/control",
                    "body": {
                        "action": "fault_injection",
                        "kind": kinds[0],
                        "target": targets[0]["fixture_id"] if targets else None,
                        "enabled": True,
                    },
                    "kinds": kinds,
                    "targets": targets,
                },
                "restart": {
                    "service": "fixture",
                    "method": "POST",
                    "path": "/control",
                    "body": {"action": "restart", "enabled": True},
                },
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

    def _resolve_fault_target(self, target: str) -> dict[str, object]:
        if not isinstance(target, str) or not target.strip():
            raise ValueError("unsupported fault target")
        value = target.strip()
        installations = self._fixture_catalog.get("installations", [])
        if isinstance(installations, list):
            for item in installations:
                if not isinstance(item, dict):
                    continue
                fixture_id = item.get("fixture_id") or item.get("label") or item.get("id")
                if value == fixture_id or value == item.get("id"):
                    return item
        fixtures = self._fixture_catalog.get("fixtures", {})
        if isinstance(fixtures, dict):
            for name, item in fixtures.items():
                if not isinstance(item, dict):
                    continue
                fixture_id = item.get("fixture_id") or name
                if value == fixture_id:
                    return item
        raise ValueError("unsupported fault target")

    async def fixture_control(
        self,
        action: str,
        target: str | None,
        enabled: bool,
        kind: str | None = None,
    ) -> dict[str, object]:
        """Apply a bounded fixture control and return only sanitized state."""
        if action == "restart":
            self._fixture_controls["restart_requested"] = bool(enabled)
            if enabled:
                asyncio.create_task(
                    self._restart_backend(self._lifecycle_generation),
                    name="backend-restart",
                )
        elif action == "fault_injection":
            if self.config.scenario not in {"app-release-rollout", "app-control-plane"}:
                return {
                    "status": "rejected",
                    "scenario": self.config.scenario,
                    "action": action,
                    "reason": "unsupported_scenario",
                }
            if target is not None and len(target) > 128:
                raise ValueError("control target is too long")
            if enabled:
                try:
                    fixture = self._resolve_fault_target(target or "")
                except ValueError:
                    return {
                        "status": "rejected",
                        "scenario": self.config.scenario,
                        "action": action,
                        "enabled": False,
                        "reason": "unsupported_target",
                    }
                selected_kind = kind or "missing_owned_table"
                allowed_kinds = {"missing_owned_table"}
                if self.config.scenario == "app-control-plane":
                    allowed_kinds.add("legacy_schema_drift")
                if selected_kind not in allowed_kinds:
                    return {
                        "status": "rejected",
                        "scenario": self.config.scenario,
                        "action": action,
                        "enabled": False,
                        "reason": "unsupported_kind",
                    }
                if not await self._apply_fault(fixture, selected_kind):
                    return {
                        "status": "rejected",
                        "scenario": self.config.scenario,
                        "action": action,
                        "enabled": False,
                        "reason": "fault_unavailable",
                    }
                self._fixture_controls["fault"] = {
                    "target": fixture.get("fixture_id") or fixture.get("label") or fixture.get("id"),
                    "kind": selected_kind,
                }
            else:
                active_fault = self._fixture_controls.get("fault")
                if isinstance(active_fault, dict):
                    fault_target = target or active_fault.get("target")
                    fault_kind = kind or active_fault.get("kind")
                    if not isinstance(fault_target, str) or not isinstance(fault_kind, str):
                        return {
                            "status": "rejected",
                            "scenario": self.config.scenario,
                            "action": action,
                            "enabled": False,
                            "reason": "fault_unavailable",
                        }
                    try:
                        fixture = self._resolve_fault_target(fault_target)
                    except ValueError:
                        return {
                            "status": "rejected",
                            "scenario": self.config.scenario,
                            "action": action,
                            "enabled": False,
                            "reason": "unsupported_target",
                        }
                    if not await self._restore_fault(fixture, fault_kind):
                        return {
                            "status": "rejected",
                            "scenario": self.config.scenario,
                            "action": action,
                            "enabled": False,
                            "reason": "fault_unavailable",
                        }
                self._fixture_controls["fault"] = None
        else:
            return {"status": "ignored", "scenario": self.config.scenario, "action": action}
        return {
            "status": "accepted",
            "scenario": self.config.scenario,
            "action": action,
            "enabled": bool(enabled),
            "observed": {
                "fault_injection": self._fixture_controls.get("fault"),
                "restart_requested": bool(self._fixture_controls.get("restart_requested", False)),
            },
        }

    async def _apply_fault(self, fixture: dict[str, object], kind: str) -> bool:
        """Seed one bounded schema fault for a fixture installation.

        The target table is removed while ownership remains registered.  The
        public rollout request therefore still passes its ownership preflight,
        while the worker deterministically records ``step_failed`` before any
        migration mutation.  The SQL identifier remains private to the fixture.
        """
        if kind == "legacy_schema_drift":
            try:
                import asyncpg

                vault_id = fixture.get("vault_id")
                table_name = fixture.get("table_name")
                baseline_columns = fixture.get("baseline_columns")
                if (
                    not isinstance(vault_id, str)
                    or not isinstance(table_name, str)
                    or not isinstance(baseline_columns, list)
                ):
                    return False
                connection = await asyncpg.connect(
                    host="127.0.0.1", port=15432, user="akb", password="akb", database="akb"
                )
                try:
                    current = await connection.fetchval(
                        "SELECT columns FROM vault_tables WHERE vault_id=$1 AND name=$2",
                        uuid.UUID(vault_id),
                        table_name,
                    )
                    if current is None:
                        return False
                    columns = current
                    if isinstance(columns, str):
                        columns = json.loads(columns)
                    if not isinstance(columns, list):
                        return False
                    if not any(
                        isinstance(column, dict) and column.get("name") == "fixture_drift"
                        for column in columns
                    ):
                        columns = [*columns, {"name": "fixture_drift", "type": "text"}]
                    await connection.execute(
                        """
                        UPDATE vault_tables
                           SET columns=$3::jsonb
                         WHERE vault_id=$1 AND name=$2
                        """,
                        uuid.UUID(vault_id),
                        table_name,
                        json.dumps(columns, separators=(",", ":")),
                    )
                    return True
                finally:
                    await connection.close()
            except Exception:
                # Controls are best effort and must not leak database details.
                return False
        if kind != "missing_owned_table":
            return False
        try:
            import asyncpg

            vault_id = fixture.get("vault_id")
            if not isinstance(vault_id, str):
                return False
            connection = await asyncpg.connect(
                host="127.0.0.1", port=15432, user="akb", password="akb", database="akb"
            )
            try:
                vault_name = await connection.fetchval("SELECT name FROM vaults WHERE id=$1", uuid.UUID(vault_id))
                if not isinstance(vault_name, str):
                    return False
                safe_vault = re.sub(r"[^a-zA-Z0-9_]", "_", vault_name)
                physical = f"vt_{safe_vault}__rollout_data"
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", physical):
                    return False
                await connection.execute(f"DROP TABLE IF EXISTS {physical}")
                return True
            finally:
                await connection.close()
        except Exception:
            # Controls are best effort and must not leak database details.
            return False

    async def _restore_fault(self, fixture: dict[str, object], kind: str) -> bool:
        """Restore the bounded fixture state removed by a fault control."""
        if kind == "legacy_schema_drift":
            try:
                import asyncpg

                vault_id = fixture.get("vault_id")
                table_name = fixture.get("table_name")
                baseline_columns = fixture.get("baseline_columns")
                if (
                    not isinstance(vault_id, str)
                    or not isinstance(table_name, str)
                    or not isinstance(baseline_columns, list)
                ):
                    return False
                connection = await asyncpg.connect(
                    host="127.0.0.1", port=15432, user="akb", password="akb", database="akb"
                )
                try:
                    await connection.execute(
                        """
                        UPDATE vault_tables
                           SET columns=$3::jsonb
                         WHERE vault_id=$1 AND name=$2
                        """,
                        uuid.UUID(vault_id),
                        table_name,
                        json.dumps(baseline_columns, separators=(",", ":")),
                    )
                    return True
                finally:
                    await connection.close()
            except Exception:
                # Controls are best effort and must not leak database details.
                return False
        if kind != "missing_owned_table":
            return False
        try:
            import asyncpg

            vault_id = fixture.get("vault_id")
            if not isinstance(vault_id, str):
                return False
            connection = await asyncpg.connect(
                host="127.0.0.1", port=15432, user="akb", password="akb", database="akb"
            )
            try:
                vault_name = await connection.fetchval(
                    "SELECT name FROM vaults WHERE id=$1", uuid.UUID(vault_id)
                )
                if not isinstance(vault_name, str):
                    return False
                safe_vault = re.sub(r"[^a-zA-Z0-9_]", "_", vault_name)
                physical = f"vt_{safe_vault}__rollout_data"
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", physical):
                    return False
                await connection.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {physical} (
                        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                        value TEXT,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                row_count = await connection.fetchval(f"SELECT COUNT(*) FROM {physical}")
                if row_count == 0:
                    await connection.executemany(
                        f"INSERT INTO {physical}(value) VALUES($1)",
                        [(None,) for _ in range(25)],
                    )
                return True
            finally:
                await connection.close()
        except Exception:
            # Controls are best effort and must not leak database details.
            return False

    async def _restart_backend(
        self,
        requested_generation: int | None = None,
    ) -> None:
        try:
            async with self._reset_lock:
                if requested_generation is None:
                    requested_generation = self._lifecycle_generation
                if (
                    requested_generation != self._lifecycle_generation
                    or self._resetting
                ):
                    return
                await self._stop_named_process("backend")
                await self._start_backend()
        except Exception:
            return

    def descriptor(self) -> dict[str, object]:
        descriptor: dict[str, object] = {
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
        # Keep the legacy default descriptor byte-for-byte compatible.  A
        # selected non-default profile advertises its added capabilities using
        # the same schema-v2 service map rather than a parallel MCP descriptor.
        if self.profile.name != DEFAULT_PROFILE or self.config.capabilities:
            descriptor["profile"] = self.profile.name
            descriptor["capabilities"] = list(self.selected_capabilities)
            descriptor["evidence"] = self.runtime_evidence()
        if self.profile.needs_stdio:
            services = descriptor["services"]
            assert isinstance(services, dict)
            services["stdio"] = {
                "origin": self.config.app_origin,
                "transport": "stdio",
                "package": "akb-mcp",
                "executable": "akb-mcp",
                "consumer_root": str(self.config.proxy_consumer_dir),
                "environment": {
                    "AKB_MCP_URL": f"{self.config.app_origin}/mcp/",
                    "AKB_PAT": self.config.credentials.pat_env,
                },
            }
            credentials = descriptor["credentials"]
            assert isinstance(credentials, dict)
            credentials["pat_env"] = self.config.credentials.pat_env
        if self.oidc_fixture is not None:
            services = descriptor["services"]
            assert isinstance(services, dict)
            app_service = services["app"]
            assert isinstance(app_service, dict)
            app_service["oauth_metadata"] = {
                "method": "GET",
                "url": f"{self.config.app_origin}/.well-known/oauth-protected-resource",
            }
            app_service["oauth_challenge"] = {
                "method": "POST",
                "url": f"{self.config.app_origin}/mcp/",
                "authorization": "omitted",
            }
            services["oidc"] = {
                "origin": self.config.fixture_origin,
                "health": {"method": "GET", "url": self.oidc_fixture.health_uri},
                "discovery": {"method": "GET", "url": self.oidc_fixture.metadata_uri},
                "jwks": {"method": "GET", "url": self.oidc_fixture.jwks_uri},
                "token": {
                    "method": "POST",
                    "url": self.oidc_fixture.token_uri,
                    "body": {"variant": "valid"},
                },
            }
        return descriptor

    def _validate_checkout(self) -> None:
        if not self.config.checkout.is_dir():
            raise ProvisioningFailure(f"checkout does not exist: {self.config.checkout}")
        required: tuple[Path, ...] = (
            self.config.backend_dir / "uv.lock",
            self.config.backend_dir / "pyproject.toml",
            self.config.backend_dir / "scripts" / "ci" / "embed_stub.py",
            self.config.backend_dir / "scripts" / "ci" / "e2e_suite_runner.py",
        )
        if self.profile.needs_stdio:
            required += (
                self.config.proxy_package_dir / "package.json",
                self.config.proxy_package_dir / "bin" / "akb-mcp.mjs",
            )
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            raise ProvisioningFailure("checkout is missing runtime inputs")
        if self.config.checkout == self.config.runtime_root or self.config.checkout in self.config.runtime_root.parents:
            raise ProvisioningFailure("runtime root must be outside the checkout")

    def _validate_profile(self) -> None:
        if self.profile.needs_keycloak_overlay:
            raise BlockedRuntimeConfig(
                "the common runtime does not start Keycloak; use the specialist SSO/IdP overlay"
            )
        if self.profile.needs_stdio:
            missing = [name for name in ("node", "npm") if shutil.which(name) is None]
            if missing:
                raise BlockedRuntimeConfig(
                    "stdio capability requires the installed Node consumer toolchain: "
                    + ", ".join(missing)
                )
            if not self.config.proxy_package_dir.is_dir():
                raise BlockedRuntimeConfig("stdio capability requires packages/akb-mcp-client")
        if self.profile.needs_oidc and self.oidc_fixture is None:
            raise BlockedRuntimeConfig("OIDC capability was selected without its fixture")

    def _write_config(self) -> None:
        import yaml
        from app.services.local_session_keys import generate_local_session_keyset

        local_key_dir = self.config.config_dir / "local-session"
        generate_local_session_keyset(local_key_dir)

        app_config = {
            "auth_mode": "local",
            "jwt_algorithm": "RS256",
            "local_session_private_key_path": str(local_key_dir / "private.pem"),
            "local_session_jwks_path": str(local_key_dir / "jwks.json"),
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
        if self.oidc_fixture is not None:
            app_config.update(
                {
                    "keycloak_enabled": True,
                    "mcp_oauth_enabled": True,
                    "keycloak_server_url": self.oidc_fixture.origin,
                    "keycloak_internal_url": self.oidc_fixture.origin,
                    "keycloak_realm": self.oidc_fixture.realm,
                    "keycloak_client_id": "runtime-mcp-client",
                    "mcp_oauth_audience": self.oidc_fixture.audience,
                    "keycloak_enrollment_mode": "open",
                    "keycloak_require_verified_email": True,
                }
            )
        secret_config = {
            "db_password": "akb",
            "system_hmac_secret": secrets.token_urlsafe(48),
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

    def _child_environment(self, overrides: dict[str, str] | None = None) -> dict[str, str]:
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
        if overrides:
            env.update(overrides)
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

    async def _spawn_interactive_process(
        self,
        name: str,
        command: list[str],
        log_name: str,
        *,
        environment: dict[str, str],
    ) -> ManagedProcess:
        """Start a real stdio boundary while keeping stderr in private logs."""

        if name in self._children:
            raise ProvisioningFailure(f"process already running: {name}")
        log_path = self.config.logs_dir / log_name
        with log_path.open("ab", buffering=0) as handle:
            os.chmod(log_path, 0o600)
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(self.config.runtime_root),
                env=self._child_environment(environment),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=handle,
                start_new_session=True,
            )
        managed = ManagedProcess(
            process,
            stdin=process.stdin,
            stdout=process.stdout,
        )
        self._children[name] = managed
        return managed

    async def _stop_named_process(self, name: str) -> None:
        managed = self._children.pop(name, None)
        if managed is None:
            return
        if managed.stdin is not None:
            managed.stdin.close()
            with contextlib.suppress(Exception):
                await managed.stdin.wait_closed()
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

    async def _mint_runtime_pat(self) -> None:
        """Mint one candidate-bound PAT without ever putting it in argv/logs."""

        username, password = self.config.credentials.values()
        status, body = await asyncio.to_thread(
            _http_json,
            f"{self.config.app_origin}/api/v1/auth/login",
            method="POST",
            body={"username": username, "password": password},
        )
        if status != 200:
            raise ProvisioningFailure("runtime PAT login failed")
        try:
            login_payload = json.loads(body)
            session_token = login_payload["token"]
            if not isinstance(session_token, str) or not session_token:
                raise ValueError
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            raise ProvisioningFailure("runtime PAT login returned an invalid response") from None
        # The session bearer is only an in-memory provisioning intermediate;
        # include it in the private-value redaction set before the token mint
        # request so a driver or child log can never echo it through discovery.
        self._fixture_private_values = (*self._fixture_private_values, session_token)

        status, body = await asyncio.to_thread(
            _http_json,
            f"{self.config.app_origin}/api/v1/auth/tokens",
            method="POST",
            authorization=f"Bearer {session_token}",
            body={"name": "runtime-mcp"},
        )
        if status != 200:
            raise ProvisioningFailure("runtime PAT mint failed")
        try:
            token_payload = json.loads(body)
            token = token_payload["token"]
            if not isinstance(token, str) or not token.startswith("akb_"):
                raise ValueError
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            raise ProvisioningFailure("runtime PAT mint returned an invalid response") from None
        self._pat_value = token
        self._fixture_private_values = (*self._fixture_private_values, token)

    async def _install_stdio_proxy(self) -> Path:
        """Install the checkout package into the private clean Node consumer."""

        consumer = self.config.proxy_consumer_dir
        consumer.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(consumer, 0o700)
        try:
            await self._run_logged_command(
                [
                    "npm",
                    "install",
                    "--prefix",
                    str(consumer),
                    "--no-save",
                    "--package-lock=false",
                    "--ignore-scripts",
                    "--no-audit",
                    "--no-fund",
                    str(self.config.proxy_package_dir),
                ],
                log_path=self.config.logs_dir / "stdio-install.log",
            )
        except ProvisioningFailure as exc:
            raise BlockedRuntimeConfig("clean Node consumer installation failed") from exc
        executable = consumer / "node_modules" / ".bin" / "akb-mcp"
        if not executable.is_file():
            executable = consumer / "node_modules" / "akb-mcp" / "bin" / "akb-mcp.mjs"
        if not executable.is_file():
            raise BlockedRuntimeConfig("installed akb-mcp package did not expose its bin")
        self._proxy_version = self._artifact_version(self.config.proxy_package_dir)
        return executable

    async def _start_stdio_proxy(self, generation: Literal["legacy", "modern"] = "legacy") -> None:
        if not self._pat_value:
            raise BlockedRuntimeConfig("stdio capability requires a runtime PAT")
        if generation not in {"legacy", "modern"}:
            raise BlockedRuntimeConfig("unsupported stdio protocol generation")
        executable = await self._install_stdio_proxy()
        node = shutil.which("node")
        if node is None:
            raise BlockedRuntimeConfig("stdio capability requires node")
        managed = await self._spawn_interactive_process(
            "stdio",
            [node, str(executable)],
            "stdio-proxy.log",
            environment={
                "AKB_MCP_URL": f"{self.config.app_origin}/mcp/",
                "AKB_PAT": self._pat_value,
            },
        )
        if managed.stdin is None or managed.stdout is None:
            raise BlockedRuntimeConfig("stdio proxy did not expose stdin/stdout pipes")
        if generation == "legacy":
            first_request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "akb-e2e-runtime", "version": "1"},
                },
            }
        else:
            first_request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "server/discover",
                "params": {
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": PROTOCOL_REVISION,
                        "io.modelcontextprotocol/clientCapabilities": {},
                        "io.modelcontextprotocol/clientInfo": {
                            "name": "akb-e2e-runtime",
                            "version": "1",
                        },
                    }
                },
            }
        managed.stdin.write((json.dumps(first_request, separators=(",", ":")) + "\n").encode())
        await managed.stdin.drain()
        try:
            for _ in range(8):
                line = await asyncio.wait_for(managed.stdout.readline(), timeout=10)
                if not line:
                    break
                response = json.loads(line)
                if not isinstance(response, dict):
                    continue
                if response.get("id") != 1:
                    continue
                if "error" in response:
                    raise ValueError
                if generation == "legacy":
                    self._stdio_initialize_observed = True
                    initialized = {
                        "jsonrpc": "2.0",
                        "method": "notifications/initialized",
                        "params": {},
                    }
                    managed.stdin.write((json.dumps(initialized, separators=(",", ":")) + "\n").encode())
                    await managed.stdin.drain()
                else:
                    self._stdio_discover_observed = True
                return
        except (asyncio.TimeoutError, ValueError, json.JSONDecodeError):
            pass
        await self._stop_named_process("stdio")
        raise BlockedRuntimeConfig(f"stdio proxy {generation} handshake did not cross the process boundary")

    async def _stdio_response(self, expected_id: int) -> dict[str, object]:
        managed = self._children.get("stdio")
        if managed is None or managed.stdout is None:
            raise ProductAssertionFailure("stdio process is unavailable")
        for _ in range(32):
            try:
                line = await asyncio.wait_for(managed.stdout.readline(), timeout=20)
            except asyncio.TimeoutError:
                raise ProductAssertionFailure("stdio response timed out") from None
            if not line:
                raise ProductAssertionFailure("stdio process closed its output")
            try:
                response = json.loads(line)
            except (TypeError, ValueError, json.JSONDecodeError):
                # A non-JSON line is not a valid protocol response.  Do not
                # copy it into an error because it could contain credentials.
                continue
            if isinstance(response, dict) and response.get("id") == expected_id:
                return response
        raise ProductAssertionFailure("stdio response id was not observed")

    async def _stdio_request(self, method: str, params: dict[str, object] | None = None) -> dict[str, object]:
        managed = self._children.get("stdio")
        if managed is None or managed.stdin is None:
            raise ProductAssertionFailure("stdio process is unavailable")
        request_id = self._stdio_next_id
        self._stdio_next_id += 1
        message: dict[str, object] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            message["params"] = params
        managed.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
        await managed.stdin.drain()
        return await self._stdio_response(request_id)

    async def _probe_stdio_behavior(self) -> None:
        """Observe the installed proxy at the protocol boundary.

        This is intentionally a small read-only product assertion.  The
        curated HTTP suites remain responsible for write/destructive side
        effects; the discovery evidence advertises the same tool cases for a
        source-blind transport comparison.
        """

        if not self.profile.needs_stdio:
            return
        response = await self._stdio_request("tools/list", {})
        result = response.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
            raise ProductAssertionFailure("stdio tools/list returned an invalid result")
        names = {
            item.get("name")
            for item in result["tools"]
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        required = {"akb_list_vaults", "akb_put", "akb_delete_vault"}
        if not required.issubset(names):
            raise ProductAssertionFailure("stdio tools/list omitted a required tool")
        self._stdio_tools_list_observed = True

        read_response = await self._stdio_request(
            "tools/call",
            {"name": "akb_list_vaults", "arguments": {}},
        )
        read_result = read_response.get("result")
        if not isinstance(read_result, dict) or "error" in read_response:
            raise ProductAssertionFailure("stdio read tool call was not accepted")
        self._stdio_read_call_observed = True

        # A process cannot switch protocol generations after its first
        # request. Start a fresh installed proxy for the modern arm so the
        # two observations remain independent and source-comparable.
        await self._stop_named_process("stdio")
        self._stdio_next_id = 2
        await self._start_stdio_proxy("modern")
        modern_meta = {
            "io.modelcontextprotocol/protocolVersion": PROTOCOL_REVISION,
            "io.modelcontextprotocol/clientCapabilities": {},
            "io.modelcontextprotocol/clientInfo": {
                "name": "akb-e2e-runtime",
                "version": "1",
            },
        }
        modern_response = await self._stdio_request(
            "tools/list",
            {"_meta": modern_meta},
        )
        modern_result = modern_response.get("result")
        if not isinstance(modern_result, dict) or not isinstance(modern_result.get("tools"), list):
            raise ProductAssertionFailure("modern stdio tools/list returned an invalid result")
        modern_names = {
            item.get("name")
            for item in modern_result["tools"]
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        if modern_names != names:
            raise ProductAssertionFailure("legacy and modern stdio catalogs differ")
        self._stdio_modern_tools_list_observed = True

        modern_read_response = await self._stdio_request(
            "tools/call",
            {
                "name": "akb_list_vaults",
                "arguments": {},
                "_meta": modern_meta,
            },
        )
        modern_read_result = modern_read_response.get("result")
        if not isinstance(modern_read_result, dict) or "error" in modern_read_response:
            raise ProductAssertionFailure("modern stdio read tool call was not accepted")
        self._stdio_modern_read_call_observed = True

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
        manifest_steps = manifest["steps"]
        assert isinstance(manifest_steps, list)
        checksum_payload = {
            "manifest_version": manifest["manifest_version"],
            "steps": [
                {key: value for key, value in step.items() if key != "checksum"}
                for step in manifest_steps
                if isinstance(step, dict)
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
        foreign_current, foreign_current_checksum, _foreign_current_manifest = await self._insert_rollout_release(
            connection, app_id=foreign_app_id, version="1.0.0", valid=True
        )
        foreign_next, foreign_checksum, _foreign_next_manifest = await self._insert_rollout_release(
            connection, app_id=foreign_app_id, version="2.0.0", valid=True
        )
        target_grants = [(owner_id, "owner")]
        installations: list[dict[str, object]] = []
        tables: list[dict[str, object]] = []
        for target_index in range(13):
            vault_id, vault_name = await self._insert_fixture_vault(
                connection,
                namespace=namespace,
                label=f"target-{target_index:02d}",
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
            installations.append(
                {
                    "fixture_id": f"target-{target_index:02d}",
                    "id": str(installation_id),
                    "vault_id": str(vault_id),
                    "vault_name": vault_name,
                }
            )
            tables.append({"installation_id": str(installation_id), "vault_id": str(vault_id), "name": table_name, "row_count": 25, "rows": row_ids})

        foreign_vault_id, foreign_vault_name = await self._insert_fixture_vault(
            connection,
            namespace=namespace,
            label="foreign",
            owner_id=owner_id,
            grants=target_grants,
            granted_by=system_admin_id,
        )
        foreign_table_name = "rollout_data"
        foreign_table_id = uuid.uuid4()
        foreign_physical_table = f"vt_{foreign_vault_name.replace('-', '_')}__{foreign_table_name}"
        await connection.execute(
            f"CREATE TABLE {foreign_physical_table} (id UUID PRIMARY KEY DEFAULT uuid_generate_v4(), value TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
        )
        await connection.execute(
            "INSERT INTO vault_tables(id,vault_id,name,description,columns,unique_keys,indexes,created_by) VALUES($1,$2,$3,'',$4::jsonb,'[]'::jsonb,'[]'::jsonb,$5)",
            foreign_table_id,
            foreign_vault_id,
            foreign_table_name,
            json.dumps([{"name": "value", "type": "text"}], separators=(",", ":")),
            str(owner_id),
        )
        await connection.executemany(
            f"INSERT INTO {foreign_physical_table}(value) VALUES($1)", [(None,) for _ in range(25)]
        )
        foreign_installation_id = await self._insert_fixture_installation(
            connection,
            app_id=foreign_app_id,
            vault_id=foreign_vault_id,
            desired_release_id=foreign_current,
            current_release_id=foreign_current,
            lifecycle="active",
            capabilities=["installation:read", "inventory:read", "rollout:read", "rollout:request"],
            resources=[("table", foreign_table_name, "owned")],
            observed_release_id=foreign_current,
            observed_release_version="1.0.0",
        )
        def rollout_coordinates(
            app_id: uuid.UUID,
            release_id: uuid.UUID,
            checksum: str,
        ) -> dict[str, object]:
            return {
                "release_id": str(release_id),
                "manifest_checksum": checksum,
                "request": {
                    "service": "app",
                    "method": "POST",
                    "path": f"/api/v1/apps/{app_id}/rollouts",
                    "body": {
                        "release_id": str(release_id),
                        "manifest_checksum": checksum,
                        "idempotency_key": "uuid-v4",
                    },
                    "headers": {"Idempotency-Key": "uuid-v4"},
                },
                "status": {
                    "service": "app",
                    "method": "GET",
                    "path": f"/api/v1/apps/{app_id}/rollouts/{{rollout_id}}",
                },
            }
        target_rollout_coordinates = rollout_coordinates(target_app_id, next_release, next_checksum)
        foreign_rollout_coordinates = rollout_coordinates(foreign_app_id, foreign_next, foreign_checksum)
        target_resume_coordinates = {
            "method": "POST",
            "path": f"/api/v1/apps/{target_app_id}/rollouts/{{rollout_id}}/resume",
            "body": {
                "release_id": str(next_release),
                "manifest_checksum": next_checksum,
                "idempotency_key": "uuid-v4",
            },
            "headers": {"Idempotency-Key": "uuid-v4"},
        }
        self_app_resume_coordinates = {
            "method": "POST",
            "path": "/api/v1/app/rollouts/{rollout_id}/resume",
            "body": {
                "release_id": str(next_release),
                "manifest_checksum": next_checksum,
            },
            "headers": {"Idempotency-Key": "uuid-v4"},
        }
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
            "apps": {
                "target": {"id": str(target_app_id), "rollout": target_rollout_coordinates},
                "foreign": {"id": str(foreign_app_id), "rollout": foreign_rollout_coordinates},
            },
            "releases": {
                "target_current": {"id": str(old_release), "version": "1.0.0", "manifest_checksum": old_checksum},
                "target_next": {"id": str(next_release), "version": "2.0.0", "manifest_checksum": next_checksum},
                "target_unsupported": {"id": str(unsupported_release), "version": "9.0.0", "manifest_checksum": unsupported_checksum},
                "foreign_current": {"id": str(foreign_current), "version": "1.0.0", "manifest_checksum": foreign_current_checksum},
                "foreign_next": {"id": str(foreign_next), "version": "2.0.0", "manifest_checksum": foreign_checksum},
            },
            "installations": installations,
            "tables": tables,
            "scope_cases": {
                "other_app": {
                    "app_id": str(foreign_app_id),
                    "release_id": str(foreign_next),
                    "manifest_checksum": foreign_checksum,
                    "current_release_id": str(foreign_current),
                    "installation_id": str(foreign_installation_id),
                    "vault_id": str(foreign_vault_id),
                },
                "unallocated": random_ids,
            },
            "coordinates": {
                "admin": {
                    "credential": {"service": "app", "method": "POST", "path": f"/api/v1/apps/{target_app_id}/credentials", "body": {"deployment": "fixture"}},
                    "exchange": {"service": "app", "method": "POST", "path": "/api/v1/auth/app-token", "body": {"credential": "<issued-value>"}},
                    "request": target_rollout_coordinates["request"],
                    "status": target_rollout_coordinates["status"],
                    "resume": target_resume_coordinates,
                    "registry": {
                        "app_create": {
                            "service": "app",
                            "method": "POST",
                            "path": "/api/v1/apps",
                            "body_fields": ["app_key", "display_name", "description", "metadata"],
                        },
                        "release_create": {
                            "service": "app",
                            "method": "POST",
                            "path": f"/api/v1/apps/{target_app_id}/releases",
                            "body_fields": ["version", "manifest", "manifest_checksum"],
                        },
                    },
                    "apps": {
                        "target": target_rollout_coordinates,
                        "foreign": foreign_rollout_coordinates,
                    },
                },
                "self_app": {
                    "request": {"service": "app", "method": "POST", "path": "/api/v1/app/rollouts", "body": {"release_id": str(next_release), "manifest_checksum": next_checksum}, "headers": {"Idempotency-Key": "uuid-v4"}},
                    "status": {"service": "app", "method": "GET", "path": "/api/v1/app/rollouts/{rollout_id}"},
                    "resume": self_app_resume_coordinates,
                },
                "installation_status": {"service": "app", "method": "GET", "path": f"/api/v1/apps/{target_app_id}/installations/{{vault_id}}"},
            },
            "controls": {
                "fault_injection": {
                    "service": "fixture",
                    "method": "POST",
                    "path": "/control",
                    "body": {"action": "fault_injection", "kind": "missing_owned_table", "target": "target-00", "enabled": True},
                    "kinds": ["missing_owned_table"],
                    "targets": [
                        {"fixture_id": item["fixture_id"], "installation_id": item["id"]}
                        for item in installations
                    ],
                },
                "restart": {
                    "service": "fixture",
                    "method": "POST",
                    "path": "/control",
                    "body": {"action": "restart", "enabled": True},
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

    def _publish_installation_lifecycle_commands(
        self,
        *,
        app_id: str,
        restore_vault_id: str,
        restore_release_id: str,
        fresh_vault_id: str,
        fresh_release_id: str,
    ) -> None:
        """Publish exact success coordinates for installation lifecycle probes."""
        commands = self._fixture_catalog.setdefault("commands", {})
        if not isinstance(commands, dict):
            commands = {}
            self._fixture_catalog["commands"] = commands
        capabilities = ["installation:read", "inventory:read"]
        commands["restore_compatible"] = {
            "app_id": app_id,
            "vault_id": restore_vault_id,
            "release_id": restore_release_id,
            "capabilities": capabilities,
            "mode": "restore",
            "request": {
                "service": "app",
                "method": "PUT",
                "path": f"/api/v1/apps/{app_id}/installations/{restore_vault_id}",
                "body": {
                    "release_id": restore_release_id,
                    "capabilities": capabilities,
                    "mode": "restore",
                },
            },
        }
        commands["fresh_empty"] = {
            "app_id": app_id,
            "vault_id": fresh_vault_id,
            "release_id": fresh_release_id,
            "capabilities": capabilities,
            "mode": "fresh",
            "request": {
                "service": "app",
                "method": "PUT",
                "path": f"/api/v1/apps/{app_id}/installations/{fresh_vault_id}",
                "body": {
                    "release_id": fresh_release_id,
                    "capabilities": capabilities,
                    "mode": "fresh",
                },
            },
        }

    async def _seed_control_plane_installation_lifecycle(
        self,
        connection: Any,
        *,
        system_admin_id: uuid.UUID,
    ) -> None:
        """Add valid restore/fresh installations to the control-plane scenario."""
        apps = self._fixture_catalog.get("apps")
        actors = self._fixture_catalog.get("actors")
        namespace = self._fixture_catalog.get("namespace")
        if not isinstance(apps, dict) or not isinstance(actors, dict) or not isinstance(namespace, str):
            raise ProvisioningFailure("control-plane lifecycle fixture coordinates are unavailable")
        target = apps.get("target")
        owner = actors.get("target_owner")
        if not isinstance(target, dict) or not isinstance(owner, dict):
            raise ProvisioningFailure("control-plane lifecycle fixture coordinates are unavailable")
        target_app_id = target.get("id")
        owner_id = owner.get("id")
        if not isinstance(target_app_id, str) or not isinstance(owner_id, str):
            raise ProvisioningFailure("control-plane lifecycle fixture coordinates are unavailable")
        app_uuid = uuid.UUID(target_app_id)
        owner_uuid = uuid.UUID(owner_id)
        restore_release_id = await self._insert_fixture_release(
            connection,
            app_id=app_uuid,
            version="3.0.0",
            expected_fingerprint="a" * 64,
        )
        fresh_release_id = await self._insert_fixture_release(
            connection,
            app_id=app_uuid,
            version="4.0.0",
            expected_fingerprint="c" * 64,
        )
        grants = [(owner_uuid, "owner")]
        restore_vault_id, restore_vault_name = await self._insert_fixture_vault(
            connection,
            namespace=namespace,
            label="lifecycle-restore-compatible",
            owner_id=owner_uuid,
            grants=grants,
            granted_by=system_admin_id,
        )
        fresh_vault_id, fresh_vault_name = await self._insert_fixture_vault(
            connection,
            namespace=namespace,
            label="lifecycle-fresh-empty",
            owner_id=owner_uuid,
            grants=grants,
            granted_by=system_admin_id,
        )
        restore_installation_id = await self._insert_fixture_installation(
            connection,
            app_id=app_uuid,
            vault_id=restore_vault_id,
            desired_release_id=restore_release_id,
            current_release_id=restore_release_id,
            lifecycle="uninstalled",
            capabilities=["installation:read"],
            resources=[("table", f"{namespace}-restore-table", "retained")],
            observed_release_id=restore_release_id,
            observed_release_version="3.0.0",
            schema_fingerprint="a" * 64,
        )
        fresh_installation_id = await self._insert_fixture_installation(
            connection,
            app_id=app_uuid,
            vault_id=fresh_vault_id,
            desired_release_id=restore_release_id,
            current_release_id=restore_release_id,
            lifecycle="uninstalled",
            capabilities=["installation:read"],
            resources=[],
        )
        vaults = self._fixture_catalog.setdefault("vaults", {})
        if isinstance(vaults, dict):
            vaults["lifecycle_restore_compatible"] = {
                "id": str(restore_vault_id),
                "name": restore_vault_name,
            }
            vaults["lifecycle_fresh_empty"] = {
                "id": str(fresh_vault_id),
                "name": fresh_vault_name,
            }
        releases = self._fixture_catalog.setdefault("releases", {})
        if isinstance(releases, dict):
            releases["target_restore_compatible"] = {
                "id": str(restore_release_id),
                "version": "3.0.0",
            }
            releases["target_fresh_empty"] = {
                "id": str(fresh_release_id),
                "version": "4.0.0",
            }
        fixtures = self._fixture_catalog.setdefault("fixtures", {})
        if isinstance(fixtures, dict):
            fixtures["restore_compatible"] = {
                "app_id": target_app_id,
                "vault_id": str(restore_vault_id),
                "release_id": str(restore_release_id),
                "installation_id": str(restore_installation_id),
            }
            fixtures["fresh_empty"] = {
                "app_id": target_app_id,
                "vault_id": str(fresh_vault_id),
                "release_id": str(fresh_release_id),
                "installation_id": str(fresh_installation_id),
            }
        self._publish_installation_lifecycle_commands(
            app_id=target_app_id,
            restore_vault_id=str(restore_vault_id),
            restore_release_id=str(restore_release_id),
            fresh_vault_id=str(fresh_vault_id),
            fresh_release_id=str(fresh_release_id),
        )

    async def _seed_control_plane_legacy_adoption(
        self,
        connection: Any,
        *,
        system_admin_id: uuid.UUID,
    ) -> None:
        """Seed one explicit table baseline for the adoption scenario.

        The fixture is intentionally an uninstalled control-plane target:
        the API creates the immutable plan and baseline rows during the
        behavior run.  The catalog contains only bounded before/after
        observations and a reversible registry-only schema-drift control.
        """
        apps = self._fixture_catalog.get("apps")
        actors = self._fixture_catalog.get("actors")
        namespace = self._fixture_catalog.get("namespace")
        if not isinstance(apps, dict) or not isinstance(actors, dict) or not isinstance(namespace, str):
            raise ProvisioningFailure("legacy adoption fixture coordinates are unavailable")
        target = apps.get("target")
        owner = actors.get("target_owner")
        if not isinstance(target, dict) or not isinstance(owner, dict):
            raise ProvisioningFailure("legacy adoption fixture coordinates are unavailable")
        target_app_id = target.get("id")
        owner_id = owner.get("id")
        if not isinstance(target_app_id, str) or not isinstance(owner_id, str):
            raise ProvisioningFailure("legacy adoption fixture coordinates are unavailable")

        table_name = "legacy_orders"
        baseline_columns: list[dict[str, str]] = [
            {"name": "amount", "type": "numeric"},
            {"name": "state", "type": "text"},
        ]
        descriptor = {
            "name": table_name,
            "columns": baseline_columns,
            "unique_keys": [],
            "indexes": [],
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                [descriptor],
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        app_uuid = uuid.UUID(target_app_id)
        owner_uuid = uuid.UUID(owner_id)
        release_id = await self._insert_fixture_release(
            connection,
            app_id=app_uuid,
            version="5.0.0",
            expected_fingerprint=fingerprint,
        )
        vault_id, vault_name = await self._insert_fixture_vault(
            connection,
            namespace=namespace,
            label="legacy-adoption",
            owner_id=owner_uuid,
            grants=[(owner_uuid, "owner")],
            granted_by=system_admin_id,
        )
        physical = f"vt_{re.sub(r'[^a-z0-9]', '_', vault_name.lower())}__{table_name}"
        if not re.fullmatch(r"[a-z0-9_]+", physical):
            raise ProvisioningFailure("legacy adoption fixture identifier is invalid")
        await connection.execute(
            f"""
            CREATE TABLE {physical} (
                id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                amount NUMERIC,
                state TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        await connection.executemany(
            f"INSERT INTO {physical}(amount, state) VALUES($1, $2)",
            [(10, "ready"), (20, "ready"), (30, "queued")],
        )
        table_id = uuid.uuid4()
        await connection.execute(
            """
            INSERT INTO vault_tables(
                id, vault_id, name, description, columns, unique_keys, indexes, created_by
            ) VALUES($1, $2, $3, '', $4::jsonb, '[]'::jsonb, '[]'::jsonb, $5)
            """,
            table_id,
            vault_id,
            table_name,
            json.dumps(baseline_columns, separators=(",", ":")),
            owner_id,
        )
        fixture_id = "legacy-adoption"
        fixture = {
            "fixture_id": fixture_id,
            "app_id": target_app_id,
            "vault_id": str(vault_id),
            "vault_name": vault_name,
            "release_id": str(release_id),
            "table_name": table_name,
            "table_allowlist": [table_name],
            "expected_schema_fingerprint": fingerprint,
            "baseline_columns": baseline_columns,
            "physical_table": physical,
            "before": {
                "installation_count": 0,
                "owned_resource_count": 0,
                "observed_state_count": 0,
                "table_name": table_name,
                "schema_fingerprint": fingerprint,
                "row_count": 3,
            },
            "after": {
                "lifecycle": "active",
                "desired_current_release_id": str(release_id),
                "grant_generation": 0,
                "schema_fingerprint": fingerprint,
                "row_count": 3,
            },
        }
        fixtures = self._fixture_catalog.setdefault("fixtures", {})
        if isinstance(fixtures, dict):
            fixtures["legacy_adoption"] = fixture
        vaults = self._fixture_catalog.setdefault("vaults", {})
        if isinstance(vaults, dict):
            vaults["legacy_adoption"] = {
                "id": str(vault_id),
                "name": vault_name,
            }
        releases = self._fixture_catalog.setdefault("releases", {})
        if isinstance(releases, dict):
            releases["target_legacy_adoption"] = {
                "id": str(release_id),
                "version": "5.0.0",
                "expected_schema_fingerprint": fingerprint,
            }
        coordinates = self._fixture_catalog.setdefault("coordinates", {})
        if isinstance(coordinates, dict):
            admin = coordinates.setdefault("admin", {})
            if isinstance(admin, dict):
                admin["legacy_adoption"] = {
                    "app_id": target_app_id,
                    "vault_id": str(vault_id),
                    "baseline_release_id": str(release_id),
                    "table_allowlist": [table_name],
                    "expected_schema_fingerprint": fingerprint,
                    "create": {
                        "service": "app",
                        "method": "POST",
                        "path": f"/api/v1/apps/{target_app_id}/legacy-adoptions",
                        "headers": {"Idempotency-Key": "uuid-v4"},
                        "body": {
                            "baseline_release_id": str(release_id),
                            "targets": [
                                {
                                    "vault_id": str(vault_id),
                                    "table_allowlist": [table_name],
                                    "expected_schema_fingerprint": fingerprint,
                                }
                            ],
                        },
                    },
                    "status": {
                        "service": "app",
                        "method": "GET",
                        "path": f"/api/v1/apps/{target_app_id}/legacy-adoptions/{{adoption_id}}",
                    },
                    "apply": {
                        "service": "app",
                        "method": "POST",
                        "path": f"/api/v1/apps/{target_app_id}/legacy-adoptions/{{adoption_id}}/apply",
                    },
                }

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
                "restore_compatible": {
                    "app_id": str(target_app_id),
                    "vault_id": str(target_vault_ids["restore-compatible"]),
                    "release_id": str(release_a),
                    "capabilities": ["installation:read", "inventory:read"],
                    "mode": "restore",
                    "request": {
                        "service": "app",
                        "method": "PUT",
                        "path": f"/api/v1/apps/{target_app_id}/installations/{target_vault_ids['restore-compatible']}",
                        "body": {
                            "release_id": str(release_a),
                            "capabilities": ["installation:read", "inventory:read"],
                            "mode": "restore",
                        },
                    },
                },
                "fresh_empty": {
                    "app_id": str(target_app_id),
                    "vault_id": str(target_vault_ids["fresh-empty"]),
                    "release_id": str(release_b),
                    "capabilities": ["installation:read", "inventory:read"],
                    "mode": "fresh",
                    "request": {
                        "service": "app",
                        "method": "PUT",
                        "path": f"/api/v1/apps/{target_app_id}/installations/{target_vault_ids['fresh-empty']}",
                        "body": {
                            "release_id": str(release_b),
                            "capabilities": ["installation:read", "inventory:read"],
                            "mode": "fresh",
                        },
                    },
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
                elif self.config.scenario == "app-control-plane":
                    await self._seed_app_release_rollout(
                        connection,
                        password_hash=password_hash,
                        system_admin_id=system_admin_id,
                    )
                    await self._seed_control_plane_installation_lifecycle(
                        connection,
                        system_admin_id=system_admin_id,
                    )
                    await self._seed_control_plane_legacy_adoption(
                        connection,
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
        await self._mint_runtime_pat()

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
        self._validate_profile()
        prepare_private_runtime_root(self.config.runtime_root)
        self._write_config()
        self.config.logs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        await self._start_dependencies()
        await self._start_fixture_control()
        await self._start_embed_stub()
        await self._bootstrap_backend_and_seed()
        if self.profile.needs_stdio:
            await self._start_stdio_proxy()
        self._prepared = True

    async def reset_scenario(self) -> None:
        async with self._reset_lock:
            if not self._prepared:
                raise ProvisioningFailure("fixture reset requested before runtime readiness")
            self._lifecycle_generation += 1
            self._resetting = True
            try:
                self._fixture_controls.clear()
                await self._stop_named_process("stdio")
                self._stdio_initialize_observed = False
                self._stdio_discover_observed = False
                self._stdio_tools_list_observed = False
                self._stdio_modern_tools_list_observed = False
                self._stdio_read_call_observed = False
                self._stdio_modern_read_call_observed = False
                self._stdio_next_id = 2
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
                if self.profile.needs_stdio:
                    await self._start_stdio_proxy()
            finally:
                self._resetting = False

    async def _serve_foreground(self) -> int:
        stop_task = asyncio.create_task(self._stop_event.wait(), name="serve-stop")
        try:
            while not self._stop_event.is_set():
                observed_children = tuple(self._children.items())
                child_tasks = {
                    name: (
                        child,
                        asyncio.create_task(child.process.wait(), name=f"serve-{name}"),
                    )
                    for name, child in observed_children
                }
                if not child_tasks:
                    await asyncio.sleep(0.1)
                    continue
                try:
                    wait_tasks = [task for _child, task in child_tasks.values()]
                    done, _pending = await asyncio.wait(
                        [stop_task, *wait_tasks], return_when=asyncio.FIRST_COMPLETED
                    )
                    if stop_task in done or self._stop_event.is_set():
                        return 0
                    current_child_exited = any(
                        task in done and self._children.get(name) is child
                        for name, (child, task) in child_tasks.items()
                    )
                    if not current_child_exited or self._resetting:
                        continue
                    LOGGER.error(
                        "E2E runtime child exited unexpectedly; see %s",
                        self.config.logs_dir,
                    )
                    return 1
                finally:
                    tasks = [task for _child, task in child_tasks.values()]
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    for task in tasks:
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
                "stage": "product_assertion",
                "profile": self.profile.name,
                "capabilities": list(self.selected_capabilities),
                "source_revision": self._source_revision(),
            },
        )

        if self.profile.needs_stdio:
            await self._probe_stdio_behavior()
            emit_gate_event(
                {
                    "event": "stdio_contract_start",
                    "process": "supervisor",
                    "stage": "product_assertion",
                    "profile": self.profile.name,
                }
            )
            try:
                await self._run_logged_command(
                    ["npm", "test", "--prefix", str(self.config.proxy_package_dir)],
                    log_path=self.config.logs_dir / "stdio-contract.log",
                )
            except ProvisioningFailure as exc:
                raise ProductAssertionFailure("stdio contract/reconnect tests failed") from exc
            emit_gate_event(
                {
                    "event": "stdio_contract_complete",
                    "process": "supervisor",
                    "stage": "product_assertion",
                    "profile": self.profile.name,
                    "returncode": 0,
                }
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
        await self._stop_named_process("stdio")
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
            self._validate_profile()
            self.config.credentials.values()
            await self.prepare()
            print(json.dumps(self.descriptor(), separators=(",", ":"), ensure_ascii=False), flush=True)
            if self.config.mode == "gate":
                return await self._run_gate()
            return await self._serve_foreground()
        except ProductAssertionFailure as exc:
            emit_gate_event(
                {
                    "event": "product_assertion_failed",
                    "process": "supervisor",
                    "code": ProductAssertionFailure.code,
                    "stage": "product_assertion",
                    "profile": self.profile.name,
                }
            )
            LOGGER.error("E2E product assertion failed: %s", str(exc))
            return 1
        except ProvisioningFailure as exc:
            if isinstance(exc, BlockedRuntimeConfig):
                emit_gate_event(
                    {
                        "event": "runtime_blocked",
                        "process": "supervisor",
                        "code": BlockedRuntimeConfig.code,
                        "stage": "provisioning",
                        "profile": self.profile.name,
                    }
                )
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
    parser.add_argument("--pat-env", default="AKB_E2E_PAT")
    parser.add_argument(
        "--profile",
        default=DEFAULT_PROFILE,
        help=(
            "capability profile: tool-only, transport-proxy, "
            "oidc-resource-server, transport-oidc, or keycloak-overlay"
        ),
    )
    parser.add_argument(
        "--capability",
        action="append",
        default=[],
        help="optional capability addition (stdio, oidc, or keycloak); repeatable",
    )
    parser.add_argument(
        "--scenario",
        choices=(
            "empty",
            "app-installation-lifecycle",
            "app-release-rollout",
            "app-control-plane",
        ),
        default=SCENARIO,
    )
    args = parser.parse_args(argv)

    try:
        select_capability_profile(args.profile, args.capability)
    except ValueError as exc:
        parser.error(str(exc))

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
        credentials=CredentialNames(args.username_env, args.password_env, args.pat_env),
        scenario=args.scenario,
        profile=args.profile,
        capabilities=tuple(args.capability),
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
