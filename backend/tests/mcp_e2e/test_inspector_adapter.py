"""Focused regression tests for the Inspector driver adapter."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from .inspector_adapter import (
    INSPECTOR_BIN,
    INSPECTOR_PACKAGE,
    INSPECTOR_VERSION,
    MIN_NODE_VERSION,
    InspectorCliAdapter,
    InspectorInstallation,
    inspect_installation,
)


def _fake_installation(tmp_path: Path) -> InspectorInstallation:
    package_root = tmp_path / "client"
    inspector_root = package_root / "node_modules" / INSPECTOR_PACKAGE
    inspector_root.mkdir(parents=True)
    entry = inspector_root / INSPECTOR_BIN
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text("// public launcher\n", encoding="utf-8")
    (package_root / "package.json").write_text(
        json.dumps({"devDependencies": {INSPECTOR_PACKAGE: INSPECTOR_VERSION}}),
        encoding="utf-8",
    )
    (package_root / "package-lock.json").write_text(
        json.dumps({"packages": {f"node_modules/{INSPECTOR_PACKAGE}": {"version": INSPECTOR_VERSION}}}),
        encoding="utf-8",
    )
    (inspector_root / "package.json").write_text(
        json.dumps(
            {
                "name": INSPECTOR_PACKAGE,
                "version": INSPECTOR_VERSION,
                "engines": {"node": f">={MIN_NODE_VERSION}"},
                "bin": {"mcp-inspector": f"./{INSPECTOR_BIN}"},
            }
        ),
        encoding="utf-8",
    )
    node = tmp_path / "node"
    node.write_text("#!/bin/sh\nprintf 'v22.19.0\\n'\n", encoding="utf-8")
    node.chmod(node.stat().st_mode | stat.S_IXUSR)
    return InspectorInstallation(
        package_root=package_root,
        node=str(node),
        node_version="22.19.0",
        entry=entry,
    )


def test_installation_requires_exact_package_and_public_bin(tmp_path: Path) -> None:
    installation = _fake_installation(tmp_path)
    checked = inspect_installation(package_root=installation.package_root, node=installation.node)
    assert checked.entry == installation.entry.resolve()
    assert checked.node_version == "22.19.0"


class _FakeProcess:
    def __init__(self, output: dict[str, Any], *, returncode: int = 0, stderr: str = "") -> None:
        self.output = output
        self.returncode = returncode
        self.stderr = stderr
        self.pid = os.getpid()
        self.command: list[str] | None = None
        self.options: dict[str, Any] | None = None

    def poll(self) -> int:
        return self.returncode

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        del timeout
        return json.dumps(self.output) + "\n", self.stderr

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return self.returncode


def test_adapter_uses_public_cli_and_cleans_redacted_state(tmp_path: Path) -> None:
    installation = _fake_installation(tmp_path)
    marker = "akb_test_secret_marker"
    processes: list[_FakeProcess] = []

    def popen(command: list[str], **options: Any) -> _FakeProcess:
        method = command[command.index("--method") + 1]
        config = Path(command[command.index("--config") + 1])
        assert config.stat().st_mode & 0o777 == 0o600
        assert config.parent.stat().st_mode & 0o777 == 0o700
        assert (
            json.loads(config.read_text(encoding="utf-8"))["mcpServers"]["akb"]["headers"]["Authorization"]
            == f"Bearer {marker}"
        )
        assert marker not in command
        assert options["env"].get("AKB_TEST_PASSWORD") is None
        assert options["env"].get("AKB_TEST_PAT") is None
        payload: dict[str, Any]
        if method == "initialize":
            payload = {"result": {"protocolVersion": "2026-07-28", "serverInfo": {"name": "akb", "version": "1"}}}
        elif method == "tools/list":
            payload = {"result": {"tools": [{"name": "akb_list_vaults", "inputSchema": {"type": "object"}}]}}
        else:
            payload = {
                "result": {
                    "content": [
                        {"type": "text", "text": json.dumps({"vaults": [], "total": 0, "returned": 0, "token": marker})}
                    ],
                    "isError": False,
                }
            }
        process = _FakeProcess(payload, stderr=f"diagnostic {marker}")
        process.command = command
        process.options = options
        processes.append(process)
        return process

    adapter = InspectorCliAdapter(
        mcp_url="http://127.0.0.1:8000/mcp/",
        pat=marker,
        secrets=(marker, "fixture-password"),
        secret_env_names=("AKB_TEST_PASSWORD", "AKB_TEST_PAT"),
        installation=installation,
        popen_factory=popen,
    )
    try:
        assert adapter.initialize().passed
        assert adapter.list_tools().passed
        result = adapter.call_tool("akb_list_vaults", {})
        assert result.passed
        assert marker not in json.dumps(result.output)
        assert marker not in (result.diagnostics or "")
        assert processes[2].command is not None
        assert processes[2].command[-4:] == ["--tool-name", "akb_list_vaults", "--tool-args-json", "{}"]
        assert all(process.options["env"].get("AKB_TEST_PASSWORD") is None for process in processes if process.options)
        first_options = processes[0].options
        assert first_options is not None
        workspace = first_options["env"]["MCP_STORAGE_DIR"]
    finally:
        adapter.close()
    assert not Path(workspace).exists()


@pytest.mark.parametrize(
    ("output", "returncode", "expected"),
    [
        ({"error": {"message": "failed"}}, 0, "Inspector result is not an object"),
        (None, 0, "Inspector returned invalid JSON output"),
        ({"result": {}}, 7, "Inspector process failed"),
    ],
)
def test_adapter_fails_closed_for_bad_output_and_exit(
    tmp_path: Path,
    output: dict[str, Any] | None,
    returncode: int,
    expected: str,
) -> None:
    installation = _fake_installation(tmp_path)

    def popen(_command: list[str], **_options: Any) -> _FakeProcess:
        if output is None:
            process = _FakeProcess({}, returncode=returncode)
            process.communicate = lambda timeout=None: ("not-json\n", "")  # type: ignore[method-assign]
            return process
        return _FakeProcess(output, returncode=returncode)

    adapter = InspectorCliAdapter(
        mcp_url="http://127.0.0.1:8000/mcp/",
        pat="akb_test_token",
        installation=installation,
        popen_factory=popen,
    )
    try:
        result = adapter.initialize()
        assert result.status == "failed"
        assert result.error == expected
    finally:
        adapter.close()
