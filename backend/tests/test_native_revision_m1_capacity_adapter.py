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
        "timing": {},
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
async def test_single_cell_real_pg_closes_authority_and_projection():
    if not await _reachable():
        pytest.skip("Postgres is not reachable for focused M1 capacity test")
    base, _ = DSN.rsplit("/", 1)
    name = f"akb_revision_m1_measurement_test_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(DSN)
    await admin.execute(f'CREATE DATABASE "{name}"')
    pool = None
    try:
        pool, _database, owner, _ = await CAPACITY._setup(f"{base}/{name}")
        result = await CAPACITY._run_cell(
            pool, owner,
            CAPACITY.Cell("test", "single", 1, "same-vault", 1024, "localized", "closed-loop", None, 2),
        )
        assert result["closed"] is True
        assert result["settled"]["exact_current_head"] is True
        assert result["settled"]["direct_head_grep"] is True
        assert result["intents"]["pending"] == 0
        assert result["resource_snapshot"]["final"]["projection_rows"] == 0
        assert result["derived_boundary"]["failure_retry_duplicate_current_head_pinned"] is True
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
