from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import uuid

import asyncpg
import pytest

from app.services.m1_pg_body_store import M1PgBodyStore
from app.services.m1_reference_payload_store import M1ReferencePayloadStore


BACKEND = Path(__file__).resolve().parents[1]
SCRIPT = BACKEND / "scripts" / "native_revision_m1_capacity_adapter.py"
SPEC = importlib.util.spec_from_file_location("native_revision_m1_capacity_adapter", SCRIPT)
assert SPEC and SPEC.loader
CAPACITY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CAPACITY
SPEC.loader.exec_module(CAPACITY)


def _matrix(cells: int) -> dict:
    return {
        "schema_version": "akb-a0-settled-pipeline-matrix/v2",
        "profile": {"dense_embedding_enabled": False},
        "timing": {
            "warmup_seconds": 0,
            "steady_seconds": 1,
            "drain_timeout_seconds": 1,
            "request_timeout_seconds": 1,
            "settlement_timeout_seconds": 1,
            "settlement_poll_interval_seconds": 0.01,
        },
        "cases": [
            {"id": f"cell-{index}", "repeat": 1, "factor": "test", "concurrency": 1,
             "vault_topology": "same-vault", "size_bytes": 1024, "mutation_shape": "localized" if index == 0 else "full-body",
             "request_budget": 1, "load_model": "closed-loop", "target_rps": None}
            for index in range(cells)
        ],
    }


def test_load_cells_binds_five_exact_files_and_seventy_cells(monkeypatch, tmp_path: Path):
    paths = []
    for index in range(5):
        path = tmp_path / f"matrix-{index}.json"
        path.write_text(json.dumps(_matrix(14)), encoding="utf-8")
        paths.append(path)
    digest = CAPACITY._matrix_digest(paths)
    monkeypatch.setattr(CAPACITY, "EXPECTED_MATRIX_HASHES", {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths})
    monkeypatch.setenv(CAPACITY.MATRIX_ENV, json.dumps([str(path) for path in paths]))
    monkeypatch.setenv(CAPACITY.MATRIX_HASH_ENV, digest)
    cells, hashes, observed = CAPACITY.load_cells()
    assert len(cells) == 70
    assert observed == digest
    assert len(hashes) == 5
    assert cells[0].mutation_shape == "localized"
    assert cells[0].timing.steady_seconds == 1


def test_pool_max_size_is_required_and_bounded(monkeypatch):
    monkeypatch.setenv(CAPACITY.POOL_MAX_SIZE_ENV, "30")
    assert CAPACITY._required_pool_max_size() == 30
    monkeypatch.setenv(CAPACITY.POOL_MAX_SIZE_ENV, "0")
    with pytest.raises(CAPACITY.AdapterError, match="1..128"):
        CAPACITY._required_pool_max_size()


class _FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def now(self) -> float:
        return self.value

    async def sleep(self, seconds: float) -> None:
        self.value += seconds


@pytest.mark.asyncio
async def test_paced_phase_uses_global_ticket_schedule_and_fixed_window():
    clock = _FakeClock()
    issued_at: list[tuple[int, float]] = []

    async def operation(ticket: int, _slot: int) -> None:
        issued_at.append((ticket, clock.now()))

    result = await CAPACITY._run_phase(
        name="steady",
        duration_seconds=.31,
        concurrency=1,
        load_model="paced",
        target_rps=10,
        issuance_cap=100,
        request_timeout_seconds=1,
        operation=operation,
        clock=clock.now,
        sleep=clock.sleep,
    )
    assert issued_at == [(0, 0), (1, .1), (2, .2), (3, .3)]
    assert result.target_issuance == result.achieved_issuance == 4
    assert result.close_reason == "steady_duration"
    assert result.receipt(include_latency=True)["measurement_window"]["observed_seconds"] == .31


@pytest.mark.asyncio
async def test_closed_loop_excludes_late_completion_from_steady_goodput():
    clock = _FakeClock()

    async def operation(_ticket: int, _slot: int) -> None:
        clock.value += .03

    result = await CAPACITY._run_phase(
        name="steady",
        duration_seconds=.1,
        concurrency=1,
        load_model="closed-loop",
        target_rps=None,
        issuance_cap=100,
        request_timeout_seconds=1,
        operation=operation,
        clock=clock.now,
        sleep=clock.sleep,
    )
    receipt = result.receipt(include_latency=True)
    assert result.achieved_issuance == 4
    assert result.successful == 3
    assert receipt["drain"]["successful"] == 1
    assert receipt["rps"] == 30.0


def test_settled_effective_rps_excludes_probe_and_cleanup_time():
    assert CAPACITY._settled_effective_rps(
        successful=30,
        steady_seconds=1,
        capacity_settlement_seconds=.5,
    ) == 20.0


def test_body_is_exact_sized_and_localized_chunk_hashes_reuse_content():
    before = CAPACITY._body(65_536, 0, True)
    after = CAPACITY._body(65_536, 1, True)
    assert len(before.encode()) == len(after.encode()) == 65_536
    assert set(CAPACITY._chunk_hashes(before)) & set(CAPACITY._chunk_hashes(after))
    assert set(CAPACITY._chunk_hashes(before)) != set(CAPACITY._chunk_hashes(after))


DSN = os.environ.get("AKB_TEST_DSN", "postgresql://akb:akb@localhost:5433/akb")  # pragma: allowlist secret


async def _reachable() -> bool:
    try:
        conn = await asyncpg.connect(DSN, timeout=2)
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


@pytest.mark.asyncio
async def test_single_cell_real_pg_closes_authority_and_projection(monkeypatch):
    if not await _reachable():
        pytest.skip("Postgres is not reachable for focused M1 capacity test")
    base, _ = DSN.rsplit("/", 1)
    name = f"akb_revision_m1_measurement_test_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(DSN)
    await admin.execute(f'CREATE DATABASE "{name}"')
    pool = None
    try:
        pool, _database, owner, _ = await CAPACITY._setup(f"{base}/{name}")
        timing = CAPACITY.Timing(0, 1, 1, 1, 3, .01)
        cell = CAPACITY.Cell("test", "single", 1, "same-vault", 1024, "localized", "closed-loop", None, 2, timing)
        result = await CAPACITY._run_cell(
            pool, owner,
            cell,
        )
        assert result["closed"] is True
        assert result["settled"]["exact_current_head"] is True
        assert result["settled"]["direct_head_grep"] is True
        assert result["settled"]["derived_projection_exact_current_head"] is True
        assert result["settled"]["capacity_settlement"]["closed"] is True
        assert result["settled"]["derived_probe"]["closed"] is True
        assert result["settled"]["cleanup"]["closed"] is True
        capacity_elapsed = result["settled"]["capacity_settlement"]["elapsed_seconds"]
        expected_effective_rps = CAPACITY._settled_effective_rps(
            successful=result["settled"]["successful"],
            steady_seconds=result["settled"]["measurement_window"]["configured_seconds"],
            capacity_settlement_seconds=capacity_elapsed,
        )
        assert result["settled"]["settled_effective_rps"] == expected_effective_rps
        assert result["front"]["issuance_schedule"]["scope"] == "worker-closed-loop"
        assert set(("measurement_window", "issued", "successful", "error", "close_reason", "target_issuance", "achieved_issuance")) <= set(result["front"])
        assert set(("measurement_window", "issued", "successful", "error", "close_reason", "target_issuance", "achieved_issuance")) <= set(result["settled"])
        assert result["cell"]["resource"]["pool_max_size"] == 24
        assert result["intents"]["pending"] == 0
        assert result["resource_snapshot"]["final"]["projection_rows"] == 0
        assert result["derived_boundary"]["failure_retry_duplicate_current_head_pinned"] is True
        assert result["derived_boundary"]["failure_retry_projection_exact_current_head"] is True
        original_deliver = CAPACITY._deliver

        async def no_projection(*args, **kwargs):
            delivered = await original_deliver(*args, **kwargs)
            namespace_id = args[2]
            async with pool.acquire() as conn:
                await conn.execute(
                    f"DELETE FROM {CAPACITY.MEASUREMENT_TABLE} WHERE namespace_id=$1",
                    namespace_id,
                )
            return delivered

        monkeypatch.setattr(CAPACITY, "_deliver", no_projection)
        false_green = await CAPACITY._run_cell(pool, owner, cell)
        assert false_green["settled"]["derived_projection_exact_current_head"] is False
        assert false_green["derived_boundary"]["failure_retry_projection_exact_current_head"] is False
        assert false_green["closed"] is False
        mixed_profile_vault = await CAPACITY._new_vault(pool, owner)
        await M1ReferencePayloadStore(pool).prepare_text(
            namespace_id=mixed_profile_vault,
            payload="reference placement",
        )
        with pytest.raises(asyncpg.CheckViolationError, match="cannot mix"):
            await M1PgBodyStore(pool).prepare_text(
                namespace_id=mixed_profile_vault,
                payload="different PostgreSQL body placement",
            )
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()
