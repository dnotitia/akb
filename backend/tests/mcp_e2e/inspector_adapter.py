"""MCP client driver backed by the repository's pinned Inspector CLI.

The adapter deliberately knows about Inspector's public command line only. A
scenario sees :class:`McpClientDriver` and never needs to know how the child
process, temporary config, or credentials are managed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .driver import DriverResult

INSPECTOR_PACKAGE = "@modelcontextprotocol/inspector"
INSPECTOR_VERSION = "2.4.0"
MIN_NODE_VERSION = "22.19.0"
INSPECTOR_BIN = "clients/launcher/build/index.js"
SERVER_NAME = "akb"
PROTOCOL_VERSION = "2026-07-28"

_REDACTED = "[REDACTED]"
_ENV_NAMES_TO_CLEAR = (
    "AKB_PAT",
    "AKB_MCP_URL",
    "DANGEROUSLY_OMIT_AUTH",
    "MCP_CATALOG_PATH",
    "MCP_CLIENT_CONFIG_PATH",
    "MCP_INSPECTOR_SECRET_FILE",
    "MCP_INSPECTOR_SECRET_KEY",
    "MCP_INSPECTOR_API_TOKEN",
)


class InspectorAdapterError(RuntimeError):
    """Raised when the pinned Inspector installation cannot be used."""


@dataclass(frozen=True, slots=True)
class InspectorInstallation:
    """Validated paths and versions for the public Inspector executable."""

    package_root: Path
    node: str
    node_version: str
    entry: Path


def _json_file(path: Path, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError, TypeError:
        raise InspectorAdapterError(f"{label} is missing or invalid") from None
    if not isinstance(parsed, dict):
        raise InspectorAdapterError(f"{label} must be an object")
    return parsed


def _version_tuple(value: str) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", value.strip())
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def _node_meets_floor(value: str, floor: str = MIN_NODE_VERSION) -> bool:
    actual = _version_tuple(value)
    minimum = _version_tuple(floor)
    return actual is not None and minimum is not None and actual >= minimum


def _node_version(node: str) -> str:
    try:
        completed = subprocess.run(
            [node, "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except OSError, subprocess.SubprocessError:
        raise InspectorAdapterError("Node.js version check failed") from None
    version = completed.stdout.strip()
    if not _node_meets_floor(version):
        raise InspectorAdapterError(f"Node.js {MIN_NODE_VERSION} or newer is required")
    return version.removeprefix("v")


def _declared_dependency(manifest: Mapping[str, Any]) -> str | None:
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        dependencies = manifest.get(section)
        if isinstance(dependencies, dict) and INSPECTOR_PACKAGE in dependencies:
            value = dependencies[INSPECTOR_PACKAGE]
            return value if isinstance(value, str) else None
    return None


def inspect_installation(
    *,
    package_root: Path | None = None,
    node: str | Path | None = None,
) -> InspectorInstallation:
    """Validate the exact package, lock entry, Node floor, and public bin."""

    if package_root is None:
        package_root = Path(__file__).resolve().parents[3] / "packages" / "akb-mcp-client"
    package_root = package_root.resolve()
    package_manifest = _json_file(package_root / "package.json", "akb-mcp package manifest")
    if _declared_dependency(package_manifest) != INSPECTOR_VERSION:
        raise InspectorAdapterError("the MCP Inspector dependency is not pinned to version 2.4.0")

    lock = _json_file(package_root / "package-lock.json", "akb-mcp package lock")
    lock_packages = lock.get("packages")
    locked_inspector = (
        lock_packages.get(f"node_modules/{INSPECTOR_PACKAGE}") if isinstance(lock_packages, dict) else None
    )
    if not isinstance(locked_inspector, dict) or locked_inspector.get("version") != INSPECTOR_VERSION:
        raise InspectorAdapterError("the package lock does not pin MCP Inspector to version 2.4.0")

    inspector_root = package_root / "node_modules" / INSPECTOR_PACKAGE
    inspector_manifest = _json_file(inspector_root / "package.json", "installed MCP Inspector manifest")
    if inspector_manifest.get("name") != INSPECTOR_PACKAGE or inspector_manifest.get("version") != INSPECTOR_VERSION:
        raise InspectorAdapterError("installed MCP Inspector is not exactly version 2.4.0")
    engines = inspector_manifest.get("engines")
    if not isinstance(engines, dict) or engines.get("node") != f">={MIN_NODE_VERSION}":
        raise InspectorAdapterError("MCP Inspector Node.js engine contract is unexpected")
    bins = inspector_manifest.get("bin")
    declared_bin = bins.get("mcp-inspector") if isinstance(bins, dict) else None
    if not isinstance(declared_bin, str) or declared_bin.removeprefix("./") != INSPECTOR_BIN:
        raise InspectorAdapterError("MCP Inspector public bin is unexpected")
    entry = (inspector_root / declared_bin).resolve()
    if not entry.is_relative_to(inspector_root) or not entry.is_file():
        raise InspectorAdapterError("MCP Inspector public launcher is missing")

    node_path = str(node) if node is not None else shutil.which("node")
    if not node_path:
        raise InspectorAdapterError("Node.js executable is unavailable")
    return InspectorInstallation(
        package_root=package_root,
        node=node_path,
        node_version=_node_version(node_path),
        entry=entry,
    )


def _redact_text(value: str, secrets: Iterable[str]) -> str:
    output = value
    for secret in sorted({secret for secret in secrets if secret}, key=len, reverse=True):
        output = output.replace(secret, _REDACTED)
    return re.sub(r"Bearer\s+[^\s,}]+", "Bearer [REDACTED]", output, flags=re.IGNORECASE)


def _redact_json(value: Any, secrets: Iterable[str]) -> Any:
    if isinstance(value, str):
        return _redact_text(value, secrets)
    if isinstance(value, list):
        return [_redact_json(item, secrets) for item in value]
    if isinstance(value, dict):
        return {str(key): _redact_json(item, secrets) for key, item in value.items()}
    return value


def _text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def _terminate_process(process: Any) -> None:
    """Stop only the process group created for one Inspector operation."""

    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError, ProcessLookupError:
        with_exception = getattr(process, "terminate", None)
        if callable(with_exception):
            with_exception()
    try:
        process.wait(timeout=2)
    except OSError, subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError, ProcessLookupError:
            with_exception = getattr(process, "kill", None)
            if callable(with_exception):
                with_exception()
        with_exception = getattr(process, "wait", None)
        if callable(with_exception):
            with_exception()


class InspectorCliAdapter:
    """Implement the MCP driver seam with the pinned Inspector CLI."""

    transport = "http"

    def __init__(
        self,
        *,
        mcp_url: str,
        pat: str,
        secrets: Iterable[str] = (),
        secret_env_names: Iterable[str] = (),
        package_root: Path | None = None,
        node: str | Path | None = None,
        timeout_seconds: float = 30,
        installation: InspectorInstallation | None = None,
        popen_factory: Callable[..., Any] | None = None,
    ) -> None:
        if not isinstance(pat, str) or not pat:
            raise InspectorAdapterError("MCP credential is missing")
        self._mcp_url = self._validate_mcp_url(mcp_url)
        self._pat = pat
        self._secrets = tuple(dict.fromkeys(secret for secret in (*secrets, pat) if secret))
        self._secret_env_names = tuple({name for name in secret_env_names if name})
        self._timeout_seconds = timeout_seconds
        self._popen = popen_factory or subprocess.Popen
        self.installation = installation or inspect_installation(package_root=package_root, node=node)
        self._workspace: Path | None = None
        self._config_path: Path | None = None
        self._active_process: Any | None = None

    @staticmethod
    def _validate_mcp_url(value: str) -> str:
        from urllib.parse import urlsplit

        if not isinstance(value, str):
            raise InspectorAdapterError("MCP endpoint is invalid")
        try:
            parsed = urlsplit(value)
        except ValueError:
            raise InspectorAdapterError("MCP endpoint is invalid") from None
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise InspectorAdapterError("MCP endpoint is invalid")
        return value

    def _ensure_workspace(self) -> tuple[Path, Path]:
        if self._workspace is not None and self._config_path is not None:
            return self._workspace, self._config_path
        workspace = Path(tempfile.mkdtemp(prefix="akb-mcp-inspector-"))
        os.chmod(workspace, 0o700)
        config_path = workspace / "config.json"
        config = {
            "mcpServers": {
                SERVER_NAME: {
                    "type": "http",
                    "url": self._mcp_url,
                    "headers": {"Authorization": f"Bearer {self._pat}"},
                    "protocolEra": "modern",
                }
            }
        }
        try:
            config_path.write_text(json.dumps(config, separators=(",", ":")) + "\n", encoding="utf-8")
            os.chmod(config_path, 0o600)
        except OSError:
            shutil.rmtree(workspace, ignore_errors=True)
            raise InspectorAdapterError("Inspector temporary config could not be created") from None
        self._workspace = workspace
        self._config_path = config_path
        return workspace, config_path

    def _environment(self, workspace: Path) -> dict[str, str]:
        env = os.environ.copy()
        for name in (*_ENV_NAMES_TO_CLEAR, *self._secret_env_names):
            env.pop(name, None)
        env["MCP_STORAGE_DIR"] = str(workspace)
        env["MCP_INSPECTOR_SECRET_STORE"] = "memory"  # pragma: allowlist secret
        return env

    @staticmethod
    def _arguments(
        operation: str, config_path: Path, *, name: str | None = None, arguments: str | None = None
    ) -> list[str]:
        args = [
            "--cli",
            "--config",
            str(config_path),
            "--stored-auth-only",
            "--server",
            SERVER_NAME,
            "--method",
            operation,
        ]
        if operation == "tools/list":
            args.append("--strict")
        args += ["--format", "json"]
        if operation == "tools/call":
            assert name is not None and arguments is not None
            args += ["--tool-name", name, "--tool-args-json", arguments]
        return args

    def _failure(
        self,
        operation: str,
        *,
        exit_code: int | None,
        error: str,
        diagnostics: str = "",
        output: dict[str, Any] | None = None,
    ) -> DriverResult:
        return DriverResult(
            transport=self.transport,
            operation=operation,
            status="failed",
            exit_code=exit_code,
            output=output,
            diagnostics=_redact_text(diagnostics, self._secrets) or None,
            error=_redact_text(error, self._secrets),
        )

    def _run(
        self,
        operation: str,
        *,
        name: str | None = None,
        arguments: str | None = None,
    ) -> DriverResult:
        workspace, config_path = self._ensure_workspace()
        command = [
            self.installation.node,
            str(self.installation.entry),
            *self._arguments(operation, config_path, name=name, arguments=arguments),
        ]
        try:
            process = self._popen(
                command,
                cwd=str(self.installation.package_root.parent.parent),
                env=self._environment(workspace),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except OSError:
            return self._failure(operation, exit_code=None, error="Inspector process could not start")
        self._active_process = process
        try:
            try:
                stdout, stderr = process.communicate(timeout=self._timeout_seconds)
            except subprocess.TimeoutExpired:
                _terminate_process(process)
                stdout, stderr = process.communicate()
                return self._failure(
                    operation,
                    exit_code=process.returncode,
                    error="Inspector process timed out",
                    diagnostics=_text(stderr),
                )
            except BaseException:
                _terminate_process(process)
                with_exception = getattr(process, "communicate", None)
                if callable(with_exception):
                    with_exception()
                raise
        finally:
            self._active_process = None

        diagnostics = _text(stderr)
        exit_code = process.returncode
        lines = [line for line in _text(stdout).splitlines() if line.strip()]
        if len(lines) != 1:
            return self._failure(
                operation,
                exit_code=exit_code,
                error="Inspector returned invalid JSON output",
                diagnostics=diagnostics,
            )
        try:
            parsed = json.loads(lines[0])
        except TypeError, ValueError:
            return self._failure(
                operation,
                exit_code=exit_code,
                error="Inspector returned invalid JSON output",
                diagnostics=diagnostics,
            )
        if not isinstance(parsed, dict):
            return self._failure(
                operation,
                exit_code=exit_code,
                error="Inspector output is not an object",
                diagnostics=diagnostics,
            )
        output = _redact_json(parsed, self._secrets)
        if not isinstance(parsed.get("result"), dict):
            return self._failure(
                operation,
                exit_code=exit_code,
                error="Inspector result is not an object",
                diagnostics=diagnostics,
                output=output,
            )
        if exit_code != 0:
            return self._failure(
                operation,
                exit_code=exit_code,
                error="Inspector process failed",
                diagnostics=diagnostics,
                output=output,
            )
        return DriverResult(
            transport=self.transport,
            operation=operation,
            status="passed",
            exit_code=exit_code,
            output=output,
            diagnostics=_redact_text(diagnostics, self._secrets) or None,
        )

    def initialize(self) -> DriverResult:
        return self._run("initialize")

    def list_tools(self) -> DriverResult:
        return self._run("tools/list")

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> DriverResult:
        if not isinstance(name, str) or not name:
            return self._failure("tools/call", exit_code=None, error="tool name is required")
        if not isinstance(arguments, Mapping):
            return self._failure("tools/call", exit_code=None, error="tool arguments must be a JSON object")
        try:
            encoded = json.dumps(dict(arguments), separators=(",", ":"), ensure_ascii=False)
        except TypeError, ValueError:
            return self._failure("tools/call", exit_code=None, error="tool arguments are not JSON serializable")
        return self._run("tools/call", name=name, arguments=encoded)

    def close(self) -> None:
        if self._active_process is not None:
            _terminate_process(self._active_process)
            self._active_process = None
        if self._workspace is not None:
            shutil.rmtree(self._workspace, ignore_errors=True)
            self._workspace = None
            self._config_path = None
        self._pat = ""
        self._secrets = ()

    def __enter__(self) -> "InspectorCliAdapter":
        return self

    def __exit__(self, _exc_type: Any, _exc_value: Any, _traceback: Any) -> None:
        self.close()
