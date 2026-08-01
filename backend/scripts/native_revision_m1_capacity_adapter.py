#!/usr/bin/env python3
"""Measurement-only M1 candidate capacity and derived-boundary adapter.

This is intentionally not the current embedding/vector pipeline.  It drives
the internal native ledger and PG BodyStore directly, then drains its durable
``native_invalidation_intents`` into a small current-head derived projection.
The projection is run-owned measurement state: neither reads nor grep consult
it, and it is removed with the run namespace.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import math
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

import asyncpg

BACKEND = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
for entry in (BACKEND, SCRIPT_DIR):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from app.exceptions import NotFoundError
from app.services.index_service import chunk_markdown
from app.services.m1_native_grep_service import M1NativeGrepService
from app.services.m1_pg_body_store import M1PgBodyStore
from app.services.native_revision_service import NativeRevisionService
from native_revision_m1_adapter import (
    AdapterError, receipt_provenance, required_environment, run_artifact_path,
    source_revision, validate_measurement_database, write_bound_json,
)

PROTOCOL_VERSION = "akb-native-revision-m1-capacity-derived/v1"
EXPECTED_CELL_COUNT = 70
MAX_CELLS = 70
MATRIX_ENV = "AKB_NATIVE_REVISION_CAPACITY_MATRICES_JSON"
MATRIX_HASH_ENV = "AKB_NATIVE_REVISION_CAPACITY_MATRIX_SET_SHA256"
POOL_MAX_SIZE_ENV = "AKB_NATIVE_REVISION_CAPACITY_POOL_MAX_SIZE"
MEASUREMENT_TABLE = "m1_capacity_derived_projection"
# Exact frozen workbench comparison set.  The concurrency manifest contains two
# planned C=128 repeats that were not run after the first safety-censored cell;
# the fresh overload manifest replaced them.  Excluding those exact two IDs and
# including the localized matrix reproduces the 70 cells actually published by
# the M0 comparator (22 + 12 + 18 + 12 + 6).
EXPECTED_MATRIX_HASHES = {
    "settled_concurrency_matrix.json": "9beee2ee2aa36ec3d401f337b82e1061e5ab0e16a6fc8a2b849bddb44e22c7f6",
    "settled_localized_matrix.json": "de982a8bd84f12a8a770ebdf7f1b0796363c819efa38659b4c1841502309bfd6",
    "settled_overload_matrix.json": "b524c0c0c796a8c112f8d51d29a8fb16b612c5bae85b499032e5a3acc361d9a9",
    "settled_paced_matrix.json": "d1e915b0b9697d4ea8a3c674a85590fdc25c81b90fee0de65ae62ac368e6419f",
    "settled_topology_matrix.json": "60ebb15b01ddfc70f43cc02892b18fb806c023fdbfda9a0ea899f7966094db8f",
}
EXCLUDED_PLANNED_CELL_IDS = {"text-1k-c128-r2", "text-1k-c128-r3"}
WARMUP_ISSUANCE_SAFETY_CAP = 100_000


@dataclass(frozen=True)
class Timing:
    """Frozen matrix timing bound to every cell before it is executed."""

    warmup_seconds: float
    steady_seconds: float
    drain_timeout_seconds: float
    request_timeout_seconds: float
    settlement_timeout_seconds: float
    settlement_poll_interval_seconds: float


@dataclass(frozen=True)
class Cell:
    matrix: str
    cell_id: str
    concurrency: int
    topology: str
    size_bytes: int
    mutation_shape: str
    load_model: str
    target_rps: float | None
    request_budget: int
    timing: Timing


@dataclass(frozen=True)
class PhaseMetrics:
    name: str
    started_at: float
    measurement_ended_at: float
    drain_ended_at: float
    configured_seconds: float
    target_issuance: int
    achieved_issuance: int
    successful: int
    errors: dict[str, int]
    drain_successful: int
    drain_errors: dict[str, int]
    close_reason: str
    latencies_ms: list[float]

    def receipt(self, *, include_latency: bool) -> dict[str, Any]:
        result: dict[str, Any] = {
            "measurement_window": {
                "started_monotonic": round(self.started_at, 6),
                "ended_monotonic": round(self.measurement_ended_at, 6),
                "configured_seconds": self.configured_seconds,
                "observed_seconds": self.configured_seconds,
            },
            "issued": self.achieved_issuance,
            "successful": self.successful,
            "error": sum(self.errors.values()),
            "errors": self.errors,
            "close_reason": self.close_reason,
            "target_issuance": self.target_issuance,
            "achieved_issuance": self.achieved_issuance,
            "drain": {
                "completed_after_measurement": self.drain_successful + sum(self.drain_errors.values()),
                "successful": self.drain_successful,
                "error": sum(self.drain_errors.values()),
                "errors": self.drain_errors,
                "ended_monotonic": round(self.drain_ended_at, 6),
                "elapsed_seconds": round(max(0.0, self.drain_ended_at - self.measurement_ended_at), 6),
            },
        }
        if include_latency:
            result.update({
                "rps": round(self.successful / max(self.configured_seconds, 0.000001), 3),
                "p50_ms": _percentile(self.latencies_ms, .50),
                "p95_ms": _percentile(self.latencies_ms, .95),
                "writes": self.successful,
            })
        return result


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_env(name: str) -> Any:
    try:
        return json.loads(required_environment(name))
    except json.JSONDecodeError as exc:
        raise AdapterError(f"{name} must be JSON") from exc


def _required_pool_max_size() -> int:
    raw = required_environment(POOL_MAX_SIZE_ENV)
    try:
        value = int(raw)
    except ValueError as exc:
        raise AdapterError(f"{POOL_MAX_SIZE_ENV} must be an integer") from exc
    if str(value) != raw or not 1 <= value <= 128:
        raise AdapterError(f"{POOL_MAX_SIZE_ENV} must be in 1..128")
    return value


def _matrix_digest(paths: Iterable[Path]) -> str:
    """Bind the ordered exact bytes, not a mutable filename or parsed shape."""
    digest = hashlib.sha256()
    for path in paths:
        raw = path.read_bytes()
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return digest.hexdigest()


def _timing(value: Any, *, matrix: str) -> Timing:
    if not isinstance(value, dict):
        raise AdapterError(f"capacity matrix timing is missing: {matrix}")
    names = (
        "warmup_seconds", "steady_seconds", "drain_timeout_seconds",
        "request_timeout_seconds", "settlement_timeout_seconds",
        "settlement_poll_interval_seconds",
    )
    parsed: dict[str, float] = {}
    for name in names:
        raw = value.get(name)
        if not isinstance(raw, (int, float)) or isinstance(raw, bool) or not math.isfinite(float(raw)):
            raise AdapterError(f"capacity matrix timing {name} must be finite: {matrix}")
        parsed[name] = float(raw)
    if (parsed["warmup_seconds"] < 0 or parsed["steady_seconds"] <= 0
            or parsed["drain_timeout_seconds"] <= 0 or parsed["request_timeout_seconds"] <= 0
            or parsed["settlement_timeout_seconds"] <= 0 or parsed["settlement_poll_interval_seconds"] <= 0):
        raise AdapterError(f"capacity matrix timing is outside its safe range: {matrix}")
    return Timing(**parsed)


def load_cells() -> tuple[list[Cell], dict[str, str], str]:
    raw_paths = _json_env(MATRIX_ENV)
    if not isinstance(raw_paths, list) or len(raw_paths) != 5 or not all(isinstance(item, str) for item in raw_paths):
        raise AdapterError(f"{MATRIX_ENV} must be a JSON array of exactly five matrix paths")
    paths = [Path(item).resolve() for item in raw_paths]
    if len(set(paths)) != 5 or not all(path.is_file() for path in paths):
        raise AdapterError("capacity matrices must be five distinct readable files")
    if {path.name for path in paths} != set(EXPECTED_MATRIX_HASHES):
        raise AdapterError("capacity matrices must be the five frozen 70-cell M1 manifests")
    for path in paths:
        if _sha256(path.read_bytes()) != EXPECTED_MATRIX_HASHES[path.name]:
            raise AdapterError(f"capacity matrix SHA-256 differs from frozen M1 manifest: {path.name}")
    paths.sort(key=lambda path: path.name)
    actual_hash = _matrix_digest(paths)
    expected_hash = required_environment(MATRIX_HASH_ENV)
    if expected_hash != actual_hash:
        raise AdapterError("capacity matrix-set SHA-256 does not match the supplied exact files")
    cells: list[Cell] = []
    hashes: dict[str, str] = {}
    for path in paths:
        try:
            matrix = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise AdapterError(f"capacity matrix is not JSON: {path.name}") from exc
        if not isinstance(matrix, dict) or matrix.get("schema_version") not in {"akb-a0-settled-pipeline-matrix/v1", "akb-a0-settled-pipeline-matrix/v2"}:
            raise AdapterError(f"capacity matrix schema is unsupported: {path.name}")
        cases = matrix.get("cases")
        if not isinstance(cases, list) or not cases:
            raise AdapterError(f"capacity matrix cases are missing: {path.name}")
        timing = _timing(matrix.get("timing"), matrix=path.name)
        hashes[path.name] = _sha256(path.read_bytes())
        for item in cases:
            if not isinstance(item, dict):
                raise AdapterError("capacity cell must be an object")
            try:
                cell_id, concurrency, topology, size, budget = (
                    item["id"], item["concurrency"], item["vault_topology"], item["size_bytes"], item["request_budget"],
                )
            except KeyError as exc:
                raise AdapterError("capacity cell is missing a required dimension") from exc
            if cell_id in EXCLUDED_PLANNED_CELL_IDS:
                if path.name != "settled_concurrency_matrix.json":
                    raise AdapterError("excluded planned cell appeared outside the concurrency manifest")
                continue
            if (not isinstance(cell_id, str) or not isinstance(concurrency, int) or not 1 <= concurrency <= 128
                    or topology not in {"same-vault", "cross-vault"} or not isinstance(size, int) or not 32 <= size <= 16 * 1024 * 1024
                    or not isinstance(budget, int) or not 1 <= budget <= 1_000_000):
                raise AdapterError("capacity cell dimension is outside its safe range")
            shape = item.get("mutation_shape", "full-body")
            load = item.get("load_model", "closed-loop")
            target = item.get("target_rps")
            if shape not in {"full-body", "localized"} or load not in {"closed-loop", "paced"}:
                raise AdapterError("capacity cell has an unsupported shape or load model")
            if load == "paced":
                if not isinstance(target, (int, float)) or isinstance(target, bool) or not 0 < float(target) <= 1000:
                    raise AdapterError("paced capacity cell requires a bounded target_rps")
                target_value: float | None = float(target)
            elif target is not None:
                raise AdapterError("closed-loop capacity cell must have null target_rps")
            else:
                target_value = None
            cells.append(Cell(path.name, cell_id, concurrency, topology, size, shape, load, target_value, budget, timing))
    if len(cells) != EXPECTED_CELL_COUNT:
        raise AdapterError(f"capacity matrix set must contain exactly {EXPECTED_CELL_COUNT} cells")
    return cells, hashes, actual_hash


def _body(size: int, revision: int, localized: bool) -> str:
    """Exact-size markdown with stable sections for chunk reuse comparison."""
    marker = "m1-capacity-needle\n"
    mutable = (
        f"localized-marker-{revision:010d}\n"
        if localized
        else f"full-body-marker-{revision:010d}\n"
    )
    prefix = "# Capacity\n\n## Stable\n" + marker + ("s" * 160) + "\n\n## Mutable\n" + mutable
    if len(prefix.encode()) > size:
        raise AdapterError("capacity payload size cannot hold fixture markers")
    tail = "a" if localized else chr(ord("a") + revision % 26)
    return prefix + tail * (size - len(prefix.encode()))


def _chunk_hashes(body: str) -> list[str]:
    return [_sha256(chunk.content.encode("utf-8")) for chunk in chunk_markdown(body)]


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1)], 3)


async def _setup(dsn: str, *, pool_max_size: int = 24) -> tuple[asyncpg.Pool, str, uuid.UUID, list[uuid.UUID]]:
    conn = await asyncpg.connect(dsn, timeout=10)
    try:
        database = str(await conn.fetchval("SELECT current_database()"))
        validate_measurement_database(database)
        await conn.execute((BACKEND / "app" / "db" / "init.sql").read_text(encoding="utf-8"))
        for filename in ("048_native_revision_core.py", "049_native_revision_m1_pg_body.py"):
            path = BACKEND / "app" / "db" / "migrations" / filename
            spec = importlib.util.spec_from_file_location("m1_capacity_" + filename.replace(".", "_"), path)
            if spec is None or spec.loader is None:
                raise AdapterError(f"cannot load measurement migration {filename}")
            module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
            await module.migrate(conn=conn)
        await conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {MEASUREMENT_TABLE} (
                namespace_id UUID NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
                resource_id UUID NOT NULL,
                revision_id TEXT NOT NULL CHECK (revision_id ~ '^[0-9a-f]{{40}}$'),
                chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
                chunk_hash TEXT NOT NULL CHECK (chunk_hash ~ '^[0-9a-f]{{64}}$'),
                PRIMARY KEY (namespace_id, resource_id, revision_id, chunk_index)
            )
        """)
        owner = await conn.fetchval("INSERT INTO users (username, email, password_hash) VALUES ($1, $2, 'm1-measurement-disabled') RETURNING id", f"m1-capacity-{uuid.uuid4().hex}", f"m1-capacity-{uuid.uuid4().hex}@invalid.example")
        return await asyncpg.create_pool(dsn, min_size=1, max_size=pool_max_size), database, owner, []
    finally:
        await conn.close()


async def _new_vault(pool: asyncpg.Pool, owner: uuid.UUID) -> uuid.UUID:
    async with pool.acquire() as conn:
        return await conn.fetchval("INSERT INTO vaults (name, git_path, owner_id) VALUES ($1, $2, $3) RETURNING id", f"m1-capacity-{uuid.uuid4().hex}", "/tmp/m1-capacity-unused.git", owner)


async def _pending(pool: asyncpg.Pool, namespace_id: uuid.UUID) -> int:
    async with pool.acquire() as conn:
        return int(await conn.fetchval("SELECT count(*) FROM native_invalidation_intents WHERE namespace_id=$1 AND completed_at IS NULL", namespace_id))


async def _deliver(pool: asyncpg.Pool, service: NativeRevisionService, namespace_id: uuid.UUID, *, fail_once: bool = False) -> dict[str, int]:
    """Drain durable intents with current-head coalescing.  No read path calls this."""
    stats = {"delivered": 0, "coalesced": 0, "failed": 0}
    injected = False
    while True:
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT intent_id, resource_id, revision_id FROM native_invalidation_intents
                 WHERE namespace_id=$1 AND completed_at IS NULL
                 ORDER BY occurred_at, intent_id FOR UPDATE SKIP LOCKED LIMIT 1
            """, namespace_id)
            if row is None:
                return stats
            await conn.execute("UPDATE native_invalidation_intents SET claimed_at=NOW() WHERE intent_id=$1", row["intent_id"])
        if fail_once and not injected:
            injected = True; stats["failed"] += 1
            async with pool.acquire() as conn:
                await conn.execute("UPDATE native_invalidation_intents SET claimed_at=NULL, last_error='measurement-injected-delivery-failure' WHERE intent_id=$1", row["intent_id"])
            return stats
        try:
            current = await service.get_current(namespace_id=namespace_id, surface="document", path=await _current_path(pool, row["resource_id"]))
        except NotFoundError:
            current = None
        async with pool.acquire() as conn:
            if current is None:
                await conn.execute(f"DELETE FROM {MEASUREMENT_TABLE} WHERE namespace_id=$1 AND resource_id=$2", namespace_id, row["resource_id"])
                await conn.execute("UPDATE native_invalidation_intents SET completed_at=NOW(), selected_delivery='m1-current-head-projection-v1' WHERE intent_id=$1", row["intent_id"])
                stats["delivered"] += 1; continue
            if current.revision_id != row["revision_id"]:
                await conn.execute("UPDATE native_invalidation_intents SET completed_at=NOW(), selected_delivery='m1-current-head-coalesced-v1' WHERE intent_id=$1", row["intent_id"])
                stats["coalesced"] += 1; continue
            await conn.execute(f"DELETE FROM {MEASUREMENT_TABLE} WHERE namespace_id=$1 AND resource_id=$2", namespace_id, row["resource_id"])
            for index, digest in enumerate(_chunk_hashes(current.text)):
                await conn.execute(f"INSERT INTO {MEASUREMENT_TABLE} (namespace_id, resource_id, revision_id, chunk_index, chunk_hash) VALUES ($1,$2,$3,$4,$5)", namespace_id, row["resource_id"], current.revision_id, index, digest)
            await conn.execute("UPDATE native_invalidation_intents SET completed_at=NOW(), selected_delivery='m1-current-head-projection-v1' WHERE intent_id=$1", row["intent_id"])
            stats["delivered"] += 1


async def _current_path(pool: asyncpg.Pool, resource_id: uuid.UUID) -> str:
    async with pool.acquire() as conn:
        value = await conn.fetchval("SELECT current_path FROM native_resources WHERE resource_id=$1", resource_id)
    if not isinstance(value, str):
        raise NotFoundError("native resource", str(resource_id))
    return value


async def _snapshot(pool: asyncpg.Pool, namespace_ids: list[uuid.UUID]) -> dict[str, int]:
    async with pool.acquire() as conn:
        return {
            "resources": int(await conn.fetchval("SELECT count(*) FROM native_resources WHERE namespace_id=ANY($1::uuid[])", namespace_ids)),
            "revisions": int(await conn.fetchval("SELECT count(*) FROM native_revisions WHERE namespace_id=ANY($1::uuid[])", namespace_ids)),
            "payloads": int(await conn.fetchval("SELECT count(*) FROM m1_reference_payloads WHERE namespace_id=ANY($1::uuid[])", namespace_ids)),
            "pending_intents": int(await conn.fetchval("SELECT count(*) FROM native_invalidation_intents WHERE namespace_id=ANY($1::uuid[]) AND completed_at IS NULL", namespace_ids)),
            "projection_rows": int(await conn.fetchval(f"SELECT count(*) FROM {MEASUREMENT_TABLE} WHERE namespace_id=ANY($1::uuid[])", namespace_ids)),
        }


async def _projection_matches_current_head(
    pool: asyncpg.Pool,
    *,
    namespace_id: uuid.UUID,
    resource_id: uuid.UUID,
    revision_id: str,
    text: str,
) -> bool:
    """Verify the durable derived projection, not a re-chunked authority body."""
    expected = [(revision_id, index, digest) for index, digest in enumerate(_chunk_hashes(text))]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT revision_id, chunk_index, chunk_hash FROM {MEASUREMENT_TABLE} "
            "WHERE namespace_id=$1 AND resource_id=$2 ORDER BY chunk_index",
            namespace_id,
            resource_id,
        )
    observed = [(str(row["revision_id"]), int(row["chunk_index"]), str(row["chunk_hash"])) for row in rows]
    return observed == expected


async def _run_phase(
    *,
    name: str,
    duration_seconds: float,
    concurrency: int,
    load_model: str,
    target_rps: float | None,
    issuance_cap: int,
    request_timeout_seconds: float,
    operation: Callable[[int, int], Awaitable[None]],
    clock: Callable[[], float] = time.perf_counter,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> PhaseMetrics:
    """Issue work against one global clock and ticket sequence.

    ``issuance_cap`` is the steady request-budget safety ceiling.  Warmup uses
    a separate fixed safety cap so it can never consume or be counted as part
    of the steady budget.  Paced tickets are global: a worker never derives a
    per-worker rate from concurrency.
    """
    if concurrency < 1 or issuance_cap < 1:
        raise AdapterError("phase concurrency and issuance cap must be positive")
    if load_model == "paced" and (target_rps is None or target_rps <= 0):
        raise AdapterError("paced phase requires target_rps")
    phase_start = clock()
    phase_end = phase_start + duration_seconds
    if load_model == "paced":
        assert target_rps is not None
        target_issuance = min(issuance_cap, math.ceil(duration_seconds * target_rps))
    else:
        target_issuance = issuance_cap
    ticket_lock = asyncio.Lock()
    next_ticket = 0
    close_reason: str | None = None
    issued = 0
    successful = 0
    errors: list[str] = []
    drain_successful = 0
    drain_errors: list[str] = []
    latencies: list[float] = []

    async def worker(slot: int) -> None:
        nonlocal close_reason, issued, successful, next_ticket, drain_successful
        while True:
            async with ticket_lock:
                now = clock()
                if next_ticket >= target_issuance:
                    close_reason = close_reason or ("steady_duration" if load_model == "paced" else "request_budget")
                    return
                if now >= phase_end:
                    close_reason = close_reason or "steady_duration"
                    return
                ticket = next_ticket
                if load_model == "paced":
                    assert target_rps is not None
                    scheduled_at = phase_start + ticket / target_rps
                    if scheduled_at >= phase_end:
                        close_reason = close_reason or "steady_duration"
                        return
                else:
                    scheduled_at = now
                next_ticket += 1
            if scheduled_at > clock():
                await sleep(scheduled_at - clock())
            # A ticket scheduled before the boundary is not issued late merely
            # because a loaded loop woke it after the measurement window.
            if clock() >= phase_end:
                async with ticket_lock:
                    close_reason = close_reason or "steady_duration"
                return
            began = clock()
            try:
                await asyncio.wait_for(operation(ticket, slot), timeout=request_timeout_seconds)
            except Exception as exc:  # output only the class, never message/body/DSN
                if clock() <= phase_end:
                    errors.append(type(exc).__name__)
                else:
                    drain_errors.append(type(exc).__name__)
            else:
                if clock() <= phase_end:
                    successful += 1
                    latencies.append((clock() - began) * 1000)
                else:
                    drain_successful += 1
            finally:
                issued += 1

    await asyncio.gather(*(worker(slot) for slot in range(concurrency)))
    drain_ended_at = clock()
    if close_reason is None:
        close_reason = ("steady_duration" if load_model == "paced" or issued < target_issuance else "request_budget")
    return PhaseMetrics(
        name=name,
        started_at=phase_start,
        measurement_ended_at=phase_end,
        drain_ended_at=drain_ended_at,
        configured_seconds=duration_seconds,
        target_issuance=target_issuance,
        achieved_issuance=issued,
        successful=successful,
        errors={error: errors.count(error) for error in sorted(set(errors))},
        drain_successful=drain_successful,
        drain_errors={error: drain_errors.count(error) for error in sorted(set(drain_errors))},
        close_reason=close_reason,
        latencies_ms=latencies,
    )


async def _run_cell(
    pool: asyncpg.Pool,
    owner: uuid.UUID,
    cell: Cell,
    *,
    run_namespaces: list[uuid.UUID] | None = None,
    pool_max_size: int = 24,
    clock: Callable[[], float] = time.perf_counter,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> dict[str, Any]:
    vaults = [await _new_vault(pool, owner) for _ in range(cell.concurrency if cell.topology == "cross-vault" else 1)]
    if run_namespaces is not None:
        run_namespaces.extend(vaults)
    store = M1PgBodyStore(pool); service = NativeRevisionService(pool, payload_store=store); grep = M1NativeGrepService(pool, body_store=store)
    states: list[tuple[uuid.UUID, uuid.UUID, str, str]] = []
    for index in range(cell.concurrency):
        namespace = vaults[index] if cell.topology == "cross-vault" else vaults[0]
        path = f"cell/{index}.md"; body = _body(cell.size_bytes, 0, cell.mutation_shape == "localized")
        created = await service.create_text(namespace_id=namespace, surface="document", path=path, payload=body, actor="m1-capacity", mutation_id=uuid.uuid4(), expected_size=cell.size_bytes)
        states.append((namespace, created.resource_id, path, created.revision_id))
    for namespace in vaults:
        await _deliver(pool, service, namespace)
    baseline = await _snapshot(pool, vaults)
    revisions: dict[uuid.UUID, str] = {resource: revision for _, resource, _, revision in states}

    async def writer(ordinal: int, slot: int) -> None:
        namespace, resource, path, _ = states[slot]
        result = await service.replace_text(
            namespace_id=namespace,
            surface="document",
            path=path,
            payload=_body(cell.size_bytes, ordinal + 1, cell.mutation_shape == "localized"),
            actor="m1-capacity",
            mutation_id=uuid.uuid4(),
            expected_revision_id=revisions[resource],
            expected_resource_id=resource,
        )
        revisions[resource] = result.revision_id

    warmup = await _run_phase(
        name="warmup",
        duration_seconds=cell.timing.warmup_seconds,
        concurrency=cell.concurrency,
        load_model=cell.load_model,
        target_rps=cell.target_rps,
        issuance_cap=WARMUP_ISSUANCE_SAFETY_CAP,
        request_timeout_seconds=cell.timing.request_timeout_seconds,
        operation=writer,
        clock=clock,
        sleep=sleep,
    )
    steady = await _run_phase(
        name="steady",
        duration_seconds=cell.timing.steady_seconds,
        concurrency=cell.concurrency,
        load_model=cell.load_model,
        target_rps=cell.target_rps,
        issuance_cap=cell.request_budget,
        request_timeout_seconds=cell.timing.request_timeout_seconds,
        operation=writer,
        clock=clock,
        sleep=sleep,
    )
    published = sum([await _pending(pool, namespace) for namespace in vaults])

    async def settle() -> tuple[dict[str, int], bool, bool, bool, bool, bool, set[str], set[str], dict[str, int]]:
        delivery = {"delivered": 0, "coalesced": 0, "failed": 0}

        async def drain(namespace: uuid.UUID, *, fail_once: bool = False) -> dict[str, int]:
            return await asyncio.wait_for(
                _deliver(pool, service, namespace, fail_once=fail_once),
                timeout=cell.timing.drain_timeout_seconds,
            )

        for namespace in vaults:
            result = await drain(namespace)
            for key in delivery:
                delivery[key] += result[key]
        exact_head = True; direct_grep = True; projection_exact = True; changed: set[str] = set(); reused: set[str] = set()
        for namespace, resource, path, _ in states:
            current = await asyncio.wait_for(service.get_current(namespace_id=namespace, surface="document", path=path), timeout=cell.timing.request_timeout_seconds)
            exact_head = exact_head and current.revision_id == revisions[resource]
            found = await asyncio.wait_for(grep.grep("m1-capacity-needle", user_id=owner, resource_id=resource), timeout=cell.timing.request_timeout_seconds)
            direct_grep = direct_grep and found["total_resources"] == 1 and found["results"][0]["revision"] == current.revision_id
            projection_exact = projection_exact and await asyncio.wait_for(
                _projection_matches_current_head(
                    pool,
                    namespace_id=namespace,
                    resource_id=resource,
                    revision_id=current.revision_id,
                    text=current.text,
                ),
                timeout=cell.timing.request_timeout_seconds,
            )
            before_hashes = set(_chunk_hashes(_body(cell.size_bytes, 0, cell.mutation_shape == "localized")))
            after_hashes = set(_chunk_hashes(current.text)); changed.update(after_hashes - before_hashes); reused.update(after_hashes & before_hashes)
        # Delivery failure must not block current Head/get/grep, then retry and duplicate delivery are harmless.
        probe_ns, probe_resource, probe_path, _ = states[0]
        probe = await asyncio.wait_for(service.replace_text(namespace_id=probe_ns, surface="document", path=probe_path, payload=_body(cell.size_bytes, warmup.achieved_issuance + steady.achieved_issuance + 7, cell.mutation_shape == "localized"), actor="m1-capacity", mutation_id=uuid.uuid4(), expected_revision_id=revisions[probe_resource], expected_resource_id=probe_resource), timeout=cell.timing.request_timeout_seconds)
        delivery_started = asyncio.Event()
        release_delivery = asyncio.Event()

        async def delayed_failed_delivery() -> dict[str, int]:
            delivery_started.set()
            await release_delivery.wait()
            return await drain(probe_ns, fail_once=True)

        delayed_task = asyncio.create_task(delayed_failed_delivery())
        await asyncio.wait_for(delivery_started.wait(), timeout=cell.timing.drain_timeout_seconds)
        visible = await asyncio.wait_for(service.get_current(namespace_id=probe_ns, surface="document", path=probe_path), timeout=cell.timing.request_timeout_seconds)
        grepped = await asyncio.wait_for(grep.grep("m1-capacity-needle", user_id=owner, resource_id=probe_resource), timeout=cell.timing.request_timeout_seconds)
        reads_completed_before_delivery = not delayed_task.done()
        release_delivery.set()
        failed = await asyncio.wait_for(delayed_task, timeout=cell.timing.drain_timeout_seconds)
        retried = await drain(probe_ns); duplicate = await drain(probe_ns)
        probe_current = await asyncio.wait_for(service.get_current(namespace_id=probe_ns, surface="document", path=probe_path), timeout=cell.timing.request_timeout_seconds)
        retry_projection_exact = await asyncio.wait_for(
            _projection_matches_current_head(
                pool,
                namespace_id=probe_ns,
                resource_id=probe_resource,
                revision_id=probe_current.revision_id,
                text=probe_current.text,
            ),
            timeout=cell.timing.request_timeout_seconds,
        )
        retry_ok = reads_completed_before_delivery and failed["failed"] == 1 and visible.revision_id == probe.revision_id and grepped["results"][0]["revision"] == probe.revision_id and retried["delivered"] + retried["coalesced"] >= 1 and duplicate == {"delivered": 0, "coalesced": 0, "failed": 0} and retry_projection_exact
        # Tombstones must erase run-owned projection and leave no pending delivery.
        for namespace, resource, path, _ in states:
            current = await asyncio.wait_for(service.get_current(namespace_id=namespace, surface="document", path=path), timeout=cell.timing.request_timeout_seconds)
            await asyncio.wait_for(service.delete_resource(namespace_id=namespace, surface="document", path=path, actor="m1-capacity", mutation_id=uuid.uuid4(), expected_revision_id=current.revision_id, expected_resource_id=resource), timeout=cell.timing.request_timeout_seconds)
        for namespace in vaults:
            result = await drain(namespace)
            for key in delivery:
                delivery[key] += result[key]
        return delivery, exact_head, direct_grep, projection_exact, retry_ok, retry_projection_exact, changed, reused, await _snapshot(pool, vaults)

    settlement_started = clock()
    settlement_error: str | None = None
    try:
        delivery, exact_head, direct_grep, projection_exact, retry_ok, retry_projection_exact, changed, reused, final = await asyncio.wait_for(settle(), timeout=cell.timing.settlement_timeout_seconds)
    except Exception as exc:  # a timeout/error is evidence, never a green settlement
        settlement_error = type(exc).__name__
        delivery = {"delivered": 0, "coalesced": 0, "failed": 0}
        exact_head = direct_grep = projection_exact = retry_ok = retry_projection_exact = False
        changed = set(); reused = set()
        final = await _snapshot(pool, vaults)
    settled = steady.receipt(include_latency=False)
    settled["measurement_window"].update({
        "settlement_timeout_seconds": cell.timing.settlement_timeout_seconds,
        "settlement_elapsed_seconds": round(max(0.0, clock() - settlement_started), 6),
    })
    settled.update({
        "exact_current_head": exact_head,
        "direct_head_grep": direct_grep,
        "derived_projection_exact_current_head": projection_exact,
        "settlement_error": settlement_error,
    })
    all_phase_errors = sum(warmup.errors.values()) + sum(warmup.drain_errors.values()) + sum(steady.errors.values()) + sum(steady.drain_errors.values())
    front = steady.receipt(include_latency=True)
    front["measurement_window"]["warmup"] = warmup.receipt(include_latency=False)["measurement_window"]
    front["warmup"] = warmup.receipt(include_latency=False)
    issuance_schedule = {
        "load_model": cell.load_model,
        "target_rps": cell.target_rps,
        "scope": "global" if cell.load_model == "paced" else "worker-closed-loop",
        "due_ticket": "phase_start + ticket/target_rps" if cell.load_model == "paced" else None,
    }
    front["issuance_schedule"] = issuance_schedule
    settled["issuance_schedule"] = issuance_schedule
    return {"cell": {"matrix": cell.matrix, "id": cell.cell_id, "concurrency": cell.concurrency, "vault_topology": cell.topology, "size_bytes": cell.size_bytes, "mutation_shape": cell.mutation_shape, "load_model": cell.load_model, "target_rps": cell.target_rps, "request_budget": cell.request_budget, "timing": {"warmup_seconds": cell.timing.warmup_seconds, "steady_seconds": cell.timing.steady_seconds, "drain_timeout_seconds": cell.timing.drain_timeout_seconds, "request_timeout_seconds": cell.timing.request_timeout_seconds, "settlement_timeout_seconds": cell.timing.settlement_timeout_seconds, "settlement_poll_interval_seconds": cell.timing.settlement_poll_interval_seconds}, "resource": {"pool_max_size": pool_max_size}}, "front": front, "settled": settled, "intents": {"published": published, "coalesced": delivery["coalesced"], "delivered": delivery["delivered"], "pending": final["pending_intents"]}, "derived_boundary": {"failure_retry_duplicate_current_head_pinned": retry_ok, "failure_retry_projection_exact_current_head": retry_projection_exact, "derived_projection_exact_current_head": projection_exact, "changed_chunk_hashes": sorted(changed), "reused_chunk_hashes": sorted(reused)}, "resource_snapshot": {"baseline": baseline, "final": final}, "closed": exact_head and direct_grep and projection_exact and retry_ok and not all_phase_errors and settlement_error is None and final["pending_intents"] == 0 and final["projection_rows"] == 0}


async def run() -> dict[str, Any]:
    cells, matrix_hashes, matrix_set_hash = load_cells()
    dsn = required_environment("AKB_NATIVE_REVISION_MEASUREMENT_DSN")
    pool_max_size = _required_pool_max_size()
    pool, database, owner, _ = await _setup(dsn, pool_max_size=pool_max_size)
    namespaces: list[uuid.UUID] = []
    try:
        results = []
        for cell in cells:
            result = await _run_cell(pool, owner, cell, run_namespaces=namespaces, pool_max_size=pool_max_size); results.append(result)
            if not result["closed"]:
                raise AdapterError(f"capacity cell closed unsuccessfully: {cell.matrix}/{cell.cell_id}")
        revision = source_revision(); runtime, environment = receipt_provenance(revision, database, namespaces[0])
        environment["storage_profile"].update({"body_store": "pg-bodystore-v1", "derived_projection": "measurement-only-current-head-coalescing-v1", "claim_scope": "measurement_only", "pool_max_size": pool_max_size})
        artifact = write_bound_json(run_artifact_path("native-capacity-derived-cells"), {"protocol_version": PROTOCOL_VERSION, "matrix_hashes": matrix_hashes, "matrix_set_sha256": matrix_set_hash, "cells": results})
        return {"protocol_version": PROTOCOL_VERSION, "matrix_set_sha256": matrix_set_hash, "matrix_hashes": matrix_hashes, "cell_count": len(results), "closed": all(item["closed"] for item in results), "receipt": {"runtime": runtime, "environment": environment, "resources": {"snapshot": {"cell_count": len(results), "measurement_only": True, "pool_max_size": pool_max_size}}, "requests": {"artifact_digest": artifact["sha256"]}}, "provenance": {"adapter": {"identity": "akb.backend.scripts.native_revision_m1_capacity_adapter", "source_revision": revision}, "cells_artifact": artifact}}
    finally:
        async with pool.acquire() as conn:
            if namespaces:
                await conn.execute("DELETE FROM vaults WHERE id=ANY($1::uuid[])", namespaces)
            await conn.execute("DELETE FROM users WHERE id=$1", owner)
        await pool.close()


def main() -> int:
    output = Path(required_environment("AKB_NATIVE_REVISION_NATIVE_OBSERVATION_PATH"))
    write_bound_json(output, asyncio.run(run()))
    return 0


if __name__ == "__main__":
    try: raise SystemExit(main())
    except AdapterError as exc:
        print(f"native revision M1 capacity adapter: {exc}", file=sys.stderr); raise SystemExit(2)
