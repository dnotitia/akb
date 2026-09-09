from __future__ import annotations

import json
import os
import subprocess
import sys
import pytest
from pathlib import Path

from mcp_catalog.runtime import RuntimeDescriptor
from mcp_catalog.runner import CredentialResolver
from test_runtime_contract import descriptor_dict

ROOT = Path(__file__).parents[1]


def _descriptor_json() -> str:
    raw = descriptor_dict()
    raw["scenario"] = "app-control-plane"
    raw["services"]["fixture"]["reset"]["body"]["scenario"] = "app-control-plane"
    return json.dumps(raw)


def test_validate_consumes_schema_v2_descriptor_from_stdin() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "mcp_catalog.cli", "validate", "--descriptor", "-"],
        cwd=ROOT,
        input=_descriptor_json(),
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0
    assert json.loads(completed.stdout)["descriptor"]["supports_stdio"] is True
    assert "No such file" not in completed.stderr


def test_explicit_descriptor_file_path_remains_supported(tmp_path: Path) -> None:
    path = tmp_path / "descriptor.json"
    path.write_text(_descriptor_json(), encoding="utf-8")

    descriptor = RuntimeDescriptor.from_file(path)

    assert descriptor.scenario == "app-control-plane"
    assert descriptor.supports_stdio


def test_run_stdin_reaches_auth_preflight_without_catalog_or_model_calls(tmp_path: Path) -> None:
    environment = os.environ.copy()
    provider_key_env = "MCP_BENCH_OPENROUTER_" + "API_KEY"
    environment["MCP_BENCH_OPENROUTER_BASE_URL"] = "https://openrouter.ai/api/v1"
    environment[provider_key_env] = "fixture-provider-key"
    environment["AKB_E2E_PAT"] = "fixture-pat"
    environment.pop("MCP_BENCH_READ_ONLY_PAT", None)
    output = tmp_path / "run.json"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "mcp_catalog.cli",
            "run",
            "--arm",
            "baseline",
            "--descriptor",
            "-",
            "--output",
            str(output),
        ],
        cwd=ROOT,
        input=_descriptor_json(),
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )

    assert completed.returncode == 2
    assert "needs_user_input" in completed.stderr
    assert "MCP_BENCH_READ_ONLY_PAT" in completed.stderr
    assert "No such file" not in completed.stderr
    assert not output.exists()


def test_run_stdin_stops_before_live_calls_when_provider_key_is_missing(tmp_path: Path) -> None:
    environment = os.environ.copy()
    provider_key_env = "MCP_BENCH_OPENROUTER_" + "API_KEY"
    environment["MCP_BENCH_OPENROUTER_BASE_URL"] = "https://openrouter.ai/api/v1"
    environment.pop(provider_key_env, None)
    environment["AKB_E2E_PAT"] = "fixture-pat"
    environment.pop("MCP_BENCH_READ_ONLY_PAT", None)
    output = tmp_path / "run.json"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "mcp_catalog.cli",
            "run",
            "--arm",
            "baseline",
            "--descriptor",
            "-",
            "--output",
            str(output),
        ],
        cwd=ROOT,
        input=_descriptor_json(),
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )

    assert completed.returncode == 2
    assert "needs_user_input" in completed.stderr
    assert provider_key_env in completed.stderr
    assert "No such file" not in completed.stderr
    assert not output.exists()


@pytest.mark.asyncio
async def test_default_descriptor_pat_is_used_before_minting(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = descriptor_dict()
    descriptor = RuntimeDescriptor.from_dict(raw)
    from mcp_catalog.contracts import load_run_manifest

    manifest = load_run_manifest(ROOT / "config" / "run.json")
    resolver = CredentialResolver(manifest, descriptor, ())
    monkeypatch.setenv("AKB_E2E_PAT", "fixture-pat")

    class NoMintFixture:
        async def mint_pat(self, _username: str, _password: str) -> tuple[str, str]:
            raise AssertionError("descriptor PAT should avoid minting")

    await resolver.prepare(NoMintFixture(), ["default"])

    assert resolver.token_for("default") == "fixture-pat"
