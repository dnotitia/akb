from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from mcp_catalog.benchmark_runtime import (
    BENCHMARK_CELL_KEYS,
    BENCHMARK_PROFILE,
    BENCHMARK_SCENARIO,
    OPENROUTER_BASE_URL_ENV,
    OPENROUTER_PROVIDER_KEY_ENV,
    BenchmarkCellProcess,
    BenchmarkCellSupervisor,
    BenchmarkCredentialNames,
    BenchmarkRuntimeConfig,
    _parse_args,
)


ROOT = Path(__file__).parents[1]
REPO_ROOT = ROOT.parents[2]
COMPOSE_FILE = REPO_ROOT / "scripts" / "ci" / "dependency-compose.yaml"


def make_config(tmp_path: Path) -> BenchmarkRuntimeConfig:
    return BenchmarkRuntimeConfig(
        checkout=REPO_ROOT,
        runtime_root=tmp_path / "runtime",
        compose_file=COMPOSE_FILE,
        compose_project="akb-catalog-unit",
        credentials=BenchmarkCredentialNames("TEST_USERNAME_ENV", "TEST_PASSWORD_ENV"),
        scenario=BENCHMARK_SCENARIO,
    )


def _option(command: list[str], name: str) -> str:
    return command[command.index(name) + 1]


def _base_descriptor() -> dict[str, object]:
    return {
        "schema_version": 2,
        "status": "ready",
        "scenario": BENCHMARK_SCENARIO,
        "services": {
            "app": {
                "origin": "http://127.0.0.1:8000",
                "health": {"method": "GET", "url": "http://127.0.0.1:8000/readyz"},
                "discovery": {"method": "GET", "url": "http://127.0.0.1:8000/openapi.json"},
            },
            "fixture": {
                "origin": "http://127.0.0.1:8889",
                "health": {"method": "GET", "url": "http://127.0.0.1:8889/health"},
                "reset": {
                    "method": "POST",
                    "url": "http://127.0.0.1:8889/reset",
                    "body": {"scenario": BENCHMARK_SCENARIO},
                },
                "discovery": {"method": "GET", "url": "http://127.0.0.1:8889/discover"},
            },
        },
        "credentials": {
            "username_env": "TEST_USERNAME_ENV",
            "password_env": "TEST_PASSWORD_ENV",
            "pat_env": "AKB_E2E_PAT",
        },
        "profile": BENCHMARK_PROFILE,
        "capabilities": ["http", "pat", "stdio"],
        "evidence": {
            "source_revision": "a" * 40,
            "backend_artifact_version": "0.0.0",
            "proxy_artifact_version": "0.0.0",
        },
    }


def test_launcher_uses_native_runtime_cli_and_isolates_ports(tmp_path: Path) -> None:
    supervisor = BenchmarkCellSupervisor(make_config(tmp_path))
    commands = [
        supervisor._child_command(key, index, tmp_path / key.replace(":", "-"))
        for index, key in enumerate(BENCHMARK_CELL_KEYS)
    ]

    assert all(_option(command, "--profile") == BENCHMARK_PROFILE for command in commands)
    assert all(_option(command, "--scenario") == BENCHMARK_SCENARIO for command in commands)
    assert all(command[command.index("python") + 1].endswith("scripts/ci/e2e_runtime.py") for command in commands)
    assert len({_option(command, "--app-port") for command in commands}) == 4
    assert len({_option(command, "--postgres-port") for command in commands}) == 4
    assert len({_option(command, "--compose-project") for command in commands}) == 4


@pytest.mark.asyncio
async def test_launcher_starts_the_registered_cells_in_parallel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor = BenchmarkCellSupervisor(make_config(tmp_path))
    started: list[tuple[str, int]] = []
    active = 0
    max_active = 0

    async def fake_start(key: str, index: int) -> BenchmarkCellProcess:
        nonlocal active, max_active
        started.append((key, index))
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0)
        active -= 1
        return BenchmarkCellProcess(key, object(), {"status": "ready"})  # type: ignore[arg-type]

    monkeypatch.setattr(supervisor, "_start_cell", fake_start)

    cells = await supervisor._start_cells()

    assert [(cell.key, index) for index, cell in enumerate(cells)] == [
        (key, index) for index, key in enumerate(BENCHMARK_CELL_KEYS)
    ]
    assert sorted(started) == sorted((key, index) for index, key in enumerate(BENCHMARK_CELL_KEYS))
    assert max_active == len(BENCHMARK_CELL_KEYS)


def test_launcher_aggregates_exact_cell_descriptors_and_provider_names(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    cells = [
        BenchmarkCellProcess(key, object(), json.loads(json.dumps(_base_descriptor())))  # type: ignore[arg-type]
        for key in BENCHMARK_CELL_KEYS
    ]
    supervisor = BenchmarkCellSupervisor(config)

    aggregated = supervisor.descriptor(cells)

    assert aggregated["schema_version"] == 2
    assert aggregated["scenario"] == BENCHMARK_SCENARIO
    assert set(aggregated["benchmark_cells"]) == set(BENCHMARK_CELL_KEYS)
    assert aggregated["credentials"]["openrouter_base_url_env"] == OPENROUTER_BASE_URL_ENV
    assert aggregated["credentials"]["openrouter_provider_key_env"] == OPENROUTER_PROVIDER_KEY_ENV
    assert aggregated["evidence"]["benchmark_cells"]


@pytest.mark.asyncio
async def test_launcher_cleanup_terminates_every_cell_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor = BenchmarkCellSupervisor(make_config(tmp_path))
    processes = {key: object() for key in BENCHMARK_CELL_KEYS}
    supervisor._children = {
        key: BenchmarkCellProcess(key, process, {})  # type: ignore[arg-type]
        for key, process in processes.items()
    }
    terminated: list[object] = []

    async def fake_terminate(process: object) -> None:
        terminated.append(process)

    monkeypatch.setattr("mcp_catalog.benchmark_runtime.terminate_process", fake_terminate)

    await supervisor.cleanup()
    await supervisor.cleanup()

    assert terminated == list(processes.values())
    assert supervisor._children == {}


def test_launcher_pins_the_registered_scenario() -> None:
    config = _parse_args(["serve", "--scenario", BENCHMARK_SCENARIO])
    assert config.scenario == BENCHMARK_SCENARIO

    with pytest.raises(SystemExit):
        _parse_args(["serve", "--scenario", "empty"])
