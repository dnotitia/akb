from __future__ import annotations

import os
from pathlib import Path

import pytest

from mcp_catalog.runtime import RuntimeContractError, RuntimeDescriptor, RuntimeFixture


def descriptor_dict() -> dict:
    return {
        "schema_version": 2,
        "status": "ready",
        "scenario": "empty",
        "services": {
            "app": {
                "origin": "http://127.0.0.1:8000",
                "health": {"method": "GET", "url": "/readyz"},
                "discovery": {"method": "GET", "url": "/openapi.json"},
            },
            "fixture": {
                "origin": "http://127.0.0.1:8889",
                "health": {"method": "GET", "url": "/health"},
                "reset": {"method": "POST", "url": "/reset", "body": {"scenario": "empty"}},
                "discovery": {"method": "GET", "url": "/discover"},
            },
            "stdio": {
                "transport": "stdio",
                "executable": "akb-mcp",
                "consumer_root": "/tmp/consumer",
                "environment": {"AKB_MCP_URL": "http://127.0.0.1:8000/mcp/", "AKB_PAT": "AKB_E2E_PAT"},
            },
        },
        "credentials": {
            "username_env": "AKB_E2E_USERNAME",
            "password" + "_env": "AKB_E2E_PASSWORD",
            "pat_env": "AKB_E2E_PAT",
        },
        "evidence": {
            "source_revision": "a" * 40,
            "backend_artifact_version": "0.0.0",
            "proxy_artifact_version": "0.0.0",
        },
    }


def test_schema_v2_descriptor_resolves_exact_source_and_artifacts() -> None:
    descriptor = RuntimeDescriptor.from_dict(descriptor_dict())
    discovery = {
        "runtime": {
            "source_revision": "a" * 40,
            "backend_artifact_version": "0.0.0",
            "proxy_artifact_version": "0.0.0",
        }
    }

    assert descriptor.source_revision_from(discovery) == "a" * 40
    assert descriptor.artifact_versions_from(discovery) == {
        "backend_artifact_version": "0.0.0",
        "proxy_artifact_version": "0.0.0",
    }


def test_descriptor_rejects_cross_origin_reset() -> None:
    raw = descriptor_dict()
    raw["services"]["fixture"]["reset"]["url"] = "http://other.invalid/reset"

    with pytest.raises(RuntimeContractError, match="declared origin"):
        RuntimeDescriptor.from_dict(raw)


def test_descriptor_rejects_revision_mismatch() -> None:
    descriptor = RuntimeDescriptor.from_dict(descriptor_dict())

    with pytest.raises(RuntimeContractError, match="source revisions differ"):
        descriptor.source_revision_from({"runtime": {"source_revision": "b" * 40}})


@pytest.mark.asyncio
async def test_stdio_command_uses_consumer_bin_and_child_env(tmp_path: Path) -> None:
    root = tmp_path / "consumer"
    executable = root / "node_modules" / ".bin" / "akb-mcp"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    os.chmod(executable, 0o755)
    raw = descriptor_dict()
    raw["services"]["stdio"]["consumer_root"] = str(root)
    descriptor = RuntimeDescriptor.from_dict(raw)
    fixture = RuntimeFixture(descriptor)
    try:
        command, args, environment = fixture.stdio_command("secret-pat")
    finally:
        await fixture.close()

    assert command.endswith("node") or Path(command).name == "node"
    assert args == [str(executable)]
    assert environment["AKB_PAT"] == "secret-pat"
