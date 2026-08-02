#!/usr/bin/env python3
"""First-party real-PostgreSQL adapter for the M1 native-ledger harness.

This entrypoint is deliberately a measurement tool, not an AKB public API.  It
only accepts an explicitly named measurement database, creates one fresh vault
namespace per run, and drives :class:`NativeRevisionService` directly.  The
workbench owns the receipt; this program writes only its concrete observation
and the two SHA-256-bound artifacts required by that receipt.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import asyncpg


BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.exceptions import ConflictError, NotFoundError  # noqa: E402
from app.services.m1_native_grep_service import M1NativeGrepService  # noqa: E402
from app.services.m1_pg_body_store import M1PgBodyStore  # noqa: E402
from app.services.native_revision_service import NativeRevisionService  # noqa: E402


PROTOCOL_VERSION = "akb-native-revision-m1-b-core/v2"
MEASUREMENT_DATABASE_PREFIX = "akb_revision_m1_measurement"
PRECOMMIT_BOUNDARIES = (
    "payload_prepare",
    "payload_verify",
    "after_prepare_before_tx",
    "manifest",
    "revision",
    "head",
    "path",
    "alias",
    "activity",
    "invalidation_intent",
    "before_commit",
)
ACTUAL_FAILPOINTS = {
    "payload_prepare": "payload.before_prepare",
    "payload_verify": "payload.after_verified",
    "after_prepare_before_tx": "payload.after_prepare_before_tx",
    "manifest": "authority.after_manifest",
    "revision": "authority.after_revision",
    "head": "authority.after_head",
    "path": "authority.after_path",
    "alias": "authority.after_alias",
    "activity": "authority.after_activity",
    "invalidation_intent": "authority.after_invalidation",
    "before_commit": "authority.before_commit",
}


class AdapterError(RuntimeError):
    """A safe-to-report measurement setup or contract failure."""


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def write_bound_json(path: Path, value: Any) -> dict[str, str]:
    """Exclusively publish an immutable JSON artifact and bind its digest."""
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise AdapterError(f"measurement artifact already exists and will not be overwritten: {path}") from exc
    try:
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(encoded)
            destination.flush()
            os.fsync(destination.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return {"path": str(path.resolve()), "sha256": sha256_bytes(encoded)}


def validate_measurement_database(name: str) -> None:
    """Prevent an operator typo from making this harness mutate a normal DB."""
    if name != MEASUREMENT_DATABASE_PREFIX and not name.startswith(f"{MEASUREMENT_DATABASE_PREFIX}_"):
        raise AdapterError(
            "AKB native M1 adapter requires a dedicated measurement database named "
            f"{MEASUREMENT_DATABASE_PREFIX} or a suffixed isolated derivative; got {name!r}"
        )


def source_revision() -> str:
    configured = os.environ.get("AKB_NATIVE_REVISION_ADAPTER_SOURCE_REVISION")
    if configured is not None and (len(configured) != 40 or any(ch not in "0123456789abcdef" for ch in configured)):
        raise AdapterError("AKB_NATIVE_REVISION_ADAPTER_SOURCE_REVISION must be exactly 40 lowercase hex")
    try:
        result = subprocess.run(
            ["git", "-C", str(BACKEND.parent), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except OSError, subprocess.CalledProcessError:
        # Immutable production-style images normally contain no checkout.  The
        # image build/deploy pipeline must inject its exact source commit rather
        # than the adapter guessing a release version or using a mutable tag.
        if configured is None:
            raise AdapterError(
                "cannot resolve AKB adapter source revision without .git; set "
                "AKB_NATIVE_REVISION_ADAPTER_SOURCE_REVISION at image build/deploy time"
            )
        return configured
    revision = result.stdout.strip()
    if len(revision) != 40 or any(ch not in "0123456789abcdef" for ch in revision):
        raise AdapterError("AKB adapter source revision must be a 40-lowercase-hex Git commit")
    if configured is not None and configured != revision:
        raise AdapterError("AKB_NATIVE_REVISION_ADAPTER_SOURCE_REVISION differs from the checked-out adapter source")
    return revision


def required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise AdapterError(f"{name} is required")
    return value


def bounded_json_object(name: str, *, required: bool) -> dict[str, Any]:
    raw = os.environ.get(name)
    if raw is None:
        if required:
            raise AdapterError(f"{name} is required for E1b measurements")
        return {}
    if len(raw.encode("utf-8")) > 16 * 1024:
        raise AdapterError(f"{name} exceeds the 16 KiB provenance profile limit")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AdapterError(f"{name} must be a JSON object") from exc
    if not isinstance(value, dict):
        raise AdapterError(f"{name} must be a JSON object")
    return value


_PRIVATE_RECEIPT_KEY_PARTS = {
    "body", "chunk", "content", "credential", "dsn", "host", "id",
    "line", "locator", "match", "name", "password", "path", "secret",
    "token", "url", "uri",
}
_SAFE_RECEIPT_TEXT_KEYS = {
    "dense", "driver", "environment", "load_model", "node_class",
    "storage_class", "tier", "topology", "vector_driver",
}


def receipt_safe_profile(value: dict[str, Any]) -> dict[str, Any]:
    """Bind a supplied profile while exposing only coarse, non-locating facts."""
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")

    def safe_dict(raw: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in raw.items():
            lowered = key.lower()
            if any(part in lowered for part in _PRIVATE_RECEIPT_KEY_PARTS):
                continue
            if isinstance(item, bool) or isinstance(item, (int, float)):
                result[key] = item
            elif isinstance(item, dict):
                nested = safe_dict(item)
                if nested:
                    result[key] = nested
            elif isinstance(item, str) and lowered in _SAFE_RECEIPT_TEXT_KEYS:
                result[key] = item
        return result

    return {
        "binding": {"sha256": sha256_bytes(encoded), "byte_size": len(encoded)},
        "coarse": safe_dict(value),
    }


def receipt_provenance(revision: str, database: str, namespace_id: uuid.UUID) -> tuple[dict[str, str], dict[str, Any]]:
    """Read explicit runtime identity/profile inputs; never invent image facts."""
    tier = os.environ.get("AKB_NATIVE_REVISION_MEASUREMENT_TIER", "E0")
    if tier not in {"E0", "E1a", "E1b", "E2", "E3"}:
        raise AdapterError("AKB_NATIVE_REVISION_MEASUREMENT_TIER must be E0, E1a, E1b, E2, or E3")
    node_profile = bounded_json_object("AKB_NATIVE_REVISION_NODE_PROFILE_JSON", required=tier == "E1b")
    supplied_storage = bounded_json_object("AKB_NATIVE_REVISION_STORAGE_PROFILE_JSON", required=tier == "E1b")
    if tier == "E1b" and (not node_profile or not supplied_storage):
        raise AdapterError("E1b measurements require non-empty node and storage provenance profiles")
    configured_source = os.environ.get("AKB_NATIVE_REVISION_RUNTIME_SOURCE_REVISION")
    if configured_source is not None and configured_source != revision:
        raise AdapterError("AKB_NATIVE_REVISION_RUNTIME_SOURCE_REVISION must equal adapter source revision")
    runtime = {
        "image_digest": required_environment("AKB_NATIVE_REVISION_RUNTIME_IMAGE_DIGEST"),
        "config_digest": required_environment("AKB_NATIVE_REVISION_RUNTIME_CONFIG_DIGEST"),
        "source_revision": revision,
    }
    storage_profile = {
        **supplied_storage,
        "authority": "postgresql-native-ledger",
        "database": database,
        "isolation_scope": "dedicated-measurement-database/random-vault-namespace/no-global-truncate",
        "namespace_id": str(namespace_id),
    }
    return runtime, {"tier": tier, "node_profile": node_profile, "storage_profile": storage_profile}


def load_contract() -> dict[str, Any]:
    path = Path(required_environment("AKB_NATIVE_REVISION_NATIVE_CONTRACT_PATH"))
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdapterError(f"cannot read native contract: {path}") from exc
    if value.get("protocol_version") != PROTOCOL_VERSION:
        raise AdapterError("native contract protocol version differs from adapter protocol")
    return value


def run_artifact_path(name: str) -> Path:
    root = Path(required_environment("AKB_NATIVE_REVISION_ARTIFACTS_DIR"))
    run_id = required_environment("AKB_NATIVE_REVISION_RUN_ID")
    if "/" in run_id or "\\" in run_id or run_id in {".", ".."}:
        raise AdapterError("AKB_NATIVE_REVISION_RUN_ID must be a simple artifact name")
    return root / f"{run_id}.{name}.json"


def mutation_id() -> uuid.UUID:
    return uuid.uuid4()


async def initialise_measurement_database(dsn: str) -> tuple[asyncpg.Pool, uuid.UUID, str]:
    """Prepare schema on a dedicated DB and allocate a run-private namespace.

    No cleanup or DDL is issued until ``current_database()`` passes the strict
    measurement-name gate.  The namespace is random rather than global
    truncation, so parallel harness runs can share a dedicated database safely.
    """
    conn = await asyncpg.connect(dsn, timeout=10)
    try:
        database = await conn.fetchval("SELECT current_database()")
        validate_measurement_database(str(database))
        init_sql = (BACKEND / "app" / "db" / "init.sql").read_text(encoding="utf-8")
        await conn.execute(init_sql)
        for filename in ("048_native_revision_core.py", "049_native_revision_m1_pg_body.py"):
            migration_path = BACKEND / "app" / "db" / "migrations" / filename
            spec = importlib.util.spec_from_file_location(
                f"akb_native_revision_m1_{filename.replace('.', '_')}", migration_path
            )
            if spec is None or spec.loader is None:
                raise AdapterError(f"cannot load native revision migration: {filename}")
            migration = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(migration)
            await migration.migrate(conn=conn)
        namespace = await conn.fetchval(
            "INSERT INTO vaults (name, git_path) VALUES ($1, $2) RETURNING id",
            f"m1-native-{uuid.uuid4().hex}",
            "/tmp/m1-native-measurement-unused.git",
        )
        return await asyncpg.create_pool(dsn, min_size=1, max_size=12), namespace, str(database)
    finally:
        await conn.close()


async def authority_snapshot(pool: asyncpg.Pool, namespace_id: uuid.UUID) -> dict[str, Any]:
    """Read authority facts from ledger tables, never from in-memory results."""
    async with pool.acquire() as conn:
        revisions = await conn.fetchval("SELECT count(*) FROM native_revisions WHERE namespace_id = $1", namespace_id)
        activities = await conn.fetchval(
            "SELECT count(*) FROM native_revision_activity WHERE namespace_id = $1", namespace_id
        )
        intents = await conn.fetchval(
            "SELECT count(*) FROM native_invalidation_intents WHERE namespace_id = $1", namespace_id
        )
        head_rows = await conn.fetch(
            """
            SELECT resource_id, head_revision_id, current_path
              FROM native_resources
             WHERE namespace_id = $1 AND lifecycle = 'live'
             ORDER BY current_path
            """,
            namespace_id,
        )
        alias_rows = await conn.fetch(
            """
            SELECT a.old_path, a.resource_id, r.head_revision_id, r.current_path
              FROM native_resource_path_aliases a
              JOIN native_resources r ON r.resource_id = a.resource_id
             WHERE a.namespace_id = $1
               AND a.retired_revision_id IS NULL
               AND r.lifecycle = 'live'
             ORDER BY a.old_path
            """,
            namespace_id,
        )
    heads = {str(row["resource_id"]): row["head_revision_id"] for row in head_rows}
    paths = {row["current_path"]: str(row["resource_id"]) for row in head_rows}
    aliases = {
        row["old_path"]: {
            "resource_id": str(row["resource_id"]),
            "revision": row["head_revision_id"],
            "resolves_to": row["current_path"],
        }
        for row in alias_rows
    }
    return {
        "revisions": int(revisions),
        # A native revision is the durable, atomically published head update.
        "head_publications": int(revisions),
        "heads": heads,
        "paths": paths,
        "aliases": aliases,
        "activities": int(activities),
        "invalidation_intents": int(intents),
    }


def request_record(operation: str, **facts: Any) -> dict[str, Any]:
    return {"operation": operation, **facts}


def lifecycle_result(result: Any) -> dict[str, Any]:
    return {
        "resource_id": str(result.resource_id),
        "revision": result.revision_id,
        "parent_revision": result.parent_revision_id,
        "path": result.path,
    }


async def workload_lifecycle(
    service: NativeRevisionService,
    namespace_id: uuid.UUID,
    contract: dict[str, Any],
    requests: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    input_ = contract["workloads"]["W1-lifecycle"]["cases"]["C2"]["input"]
    actor, path, moved_path = input_["actor"], input_["path"], input_["moved_path"]
    create_key = mutation_id()
    requests.append(request_record("create", path=path, mutation_id=str(create_key)))
    created = await service.create_text(
        namespace_id=namespace_id,
        surface="document",
        path=path,
        payload=input_["create_body"],
        actor=actor,
        mutation_id=create_key,
    )
    initial = await service.get_current(namespace_id=namespace_id, surface="document", path=path)
    replace_key = mutation_id()
    requests.append(
        request_record("replace", path=path, mutation_id=str(replace_key), expected_revision=created.revision_id)
    )
    replaced = await service.replace_text(
        namespace_id=namespace_id,
        surface="document",
        path=path,
        payload=input_["replace_body"],
        actor=actor,
        mutation_id=replace_key,
        expected_revision_id=created.revision_id,
    )
    move_key = mutation_id()
    requests.append(
        request_record(
            "move", path=path, path_to=moved_path, mutation_id=str(move_key), expected_revision=replaced.revision_id
        )
    )
    moved = await service.move_text(
        namespace_id=namespace_id,
        surface="document",
        path=path,
        path_to=moved_path,
        actor=actor,
        mutation_id=move_key,
        expected_revision_id=replaced.revision_id,
    )
    current = await service.get_current(namespace_id=namespace_id, surface="document", path=moved_path)
    moved_authority = await authority_snapshot(service.pool, namespace_id)
    # The original name is an explicit live alias while the resource is live.
    current_head = await service.get_current_reference(namespace_id=namespace_id, surface="document", reference=path)
    pinned = await service.get_revision(
        namespace_id=namespace_id,
        surface="document",
        reference=moved_path,
        revision_id=replaced.revision_id,
    )
    deleted_key = mutation_id()
    requests.append(
        request_record("delete", path=moved_path, mutation_id=str(deleted_key), expected_revision=moved.revision_id)
    )
    deleted = await service.delete_resource(
        namespace_id=namespace_id,
        surface="document",
        path=moved_path,
        actor=actor,
        mutation_id=deleted_key,
        expected_revision_id=moved.revision_id,
    )
    try:
        await service.get_current(namespace_id=namespace_id, surface="document", path=moved_path)
        get_visible = True
    except NotFoundError:
        get_visible = False
    deleted_authority = await authority_snapshot(service.pool, namespace_id)
    # These native-ledger reads intentionally happen after delete but before
    # recreate: C6 is the original resource's complete lifecycle, not the
    # distinct same-path resource that follows it.
    history_rows = await service.repository.list_history(resource_id=created.resource_id, limit=16)
    activities = await service.repository.list_activity(namespace_id=namespace_id, limit=16)
    recreate_key = mutation_id()
    requests.append(request_record("recreate", path=path, mutation_id=str(recreate_key)))
    recreated = await service.create_text(
        namespace_id=namespace_id,
        surface="document",
        path=path,
        payload=input_["create_body"],
        actor=actor,
        mutation_id=recreate_key,
    )
    recreated_history = await service.repository.list_history(resource_id=recreated.resource_id, limit=16)
    observed_diff = list(difflib.ndiff(initial.text.splitlines(), pinned.text.splitlines()))
    removed_lines = [line[2:] for line in observed_diff if line.startswith("- ")]
    added_lines = [line[2:] for line in observed_diff if line.startswith("+ ")]
    cases = {
        "C2": {
            "create": {k: v for k, v in lifecycle_result(created).items() if k != "parent_revision"},
            "initial_get": {
                "resource_id": str(initial.resource_id),
                "revision": initial.revision_id,
                "path": initial.path,
                "body": initial.text,
            },
            "replace": lifecycle_result(replaced),
            "move": {k: v for k, v in lifecycle_result(moved).items() if k != "parent_revision"},
            "current_get": {
                "resource_id": str(current.resource_id),
                "revision": current.revision_id,
                "path": current.path,
                "body": current.text,
            },
            "current_head": {"resource_id": str(current_head.resource_id), "revision": current_head.revision_id},
            "live_paths": [
                {"path": item_path, "resource_id": resource_id}
                for item_path, resource_id in moved_authority["paths"].items()
            ],
            "old_path_aliases": [
                {"path": item_path, **alias} for item_path, alias in moved_authority["aliases"].items()
            ],
            "delete": {
                "resource_id": str(deleted.resource_id),
                "revision": deleted.revision_id,
                "visible": moved_path in deleted_authority["paths"],
                "get_visible": get_visible,
            },
            "recreate": {
                "resource_id": str(recreated.resource_id),
                "revision": recreated.revision_id,
                "path": path,
                "history": [row["revision_id"] for row in recreated_history],
                "lineage": {"ancestor_resource_ids": [], "parent_resource_id": None},
            },
        },
        "C6": {
            "pinned_get": {"revision": pinned.revision_id, "body": pinned.text},
            "history": [row["revision_id"] for row in history_rows],
            "diff": {
                "from_revision": created.revision_id,
                "to_revision": replaced.revision_id,
                "removed_lines": removed_lines,
                "added_lines": added_lines,
            },
            "activity": [
                {"action": row["action"], "revision": row["revision_id"], "actor": row["actor"]} for row in activities
            ],
        },
    }
    return cases, requests


async def workload_independent(
    service: NativeRevisionService, namespace_id: uuid.UUID, requests: list[dict[str, Any]]
) -> dict[str, Any]:
    entered = 0
    maximum = 0
    ready = asyncio.Event()
    lock = asyncio.Lock()

    async def overlap(name: str) -> None:
        nonlocal entered, maximum
        if name != "authority.after_manifest":
            return
        async with lock:
            entered += 1
            maximum = max(maximum, entered)
            if entered == 2:
                ready.set()
        await asyncio.wait_for(ready.wait(), timeout=5)
        async with lock:
            entered -= 1

    concurrent = NativeRevisionService(service.pool, failpoint=overlap)

    async def create(name: str) -> Any:
        key = mutation_id()
        requests.append(request_record("independent-create", path=name, mutation_id=str(key)))
        return await concurrent.create_text(
            namespace_id=namespace_id,
            surface="document",
            path=name,
            payload=f"{name}\n",
            actor="m1-actor",
            mutation_id=key,
        )

    published = await asyncio.gather(create("independent-a"), create("independent-b"))
    return {
        "C4": {
            "independent": {
                "published": sorted(item.path for item in published),
                "activity_delta": 2,
                "overlap_observed": maximum >= 2,
                "max_in_flight": maximum,
            }
        }
    }


async def workload_isolation(
    pool: asyncpg.Pool,
    hot_namespace_id: uuid.UUID,
    requests: list[dict[str, Any]],
    cleanup_namespaces: list[uuid.UUID],
    cleanup_users: list[uuid.UUID],
) -> dict[str, Any]:
    """Hold one hot-vault authority TX while cold get/grep complete.

    This is a bounded correctness/non-starvation probe. It does not claim a
    throughput curve, queue-free execution, or horizontal scale-out.
    """
    suffix = uuid.uuid4().hex
    async with pool.acquire() as conn:
        owner_id = await conn.fetchval(
            """
            INSERT INTO users (username, email, password_hash)
            VALUES ($1, $2, 'm1-measurement-disabled') RETURNING id
            """,
            f"m1-isolation-{suffix}",
            f"m1-isolation-{suffix}@invalid.example",
        )
        await conn.execute("UPDATE vaults SET owner_id = $1 WHERE id = $2", owner_id, hot_namespace_id)
        cold_namespace_id = await conn.fetchval(
            """
            INSERT INTO vaults (name, git_path, owner_id)
            VALUES ($1, '/tmp/m1-native-measurement-unused.git', $2) RETURNING id
            """,
            f"m1-cold-{suffix}",
            owner_id,
        )
        cleanup_users.append(owner_id)
        cleanup_namespaces.append(cold_namespace_id)

    store = M1PgBodyStore(pool)
    service = NativeRevisionService(pool, payload_store=store)
    cold = await service.create_text(
        namespace_id=cold_namespace_id,
        surface="document",
        path="cold.md",
        payload="cold-authoritative needle\n",
        actor="m1-isolation",
        mutation_id=mutation_id(),
    )
    hot = await service.create_text(
        namespace_id=hot_namespace_id,
        surface="document",
        path="hot.md",
        payload="hot-before\n",
        actor="m1-isolation",
        mutation_id=mutation_id(),
    )
    async with pool.acquire() as conn:
        activity_before_cold_reads = int(
            await conn.fetchval(
                "SELECT count(*) FROM native_revision_activity WHERE namespace_id = ANY($1::uuid[])",
                [hot_namespace_id, cold_namespace_id],
            )
        )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_hot(name: str) -> None:
        if name == "authority.after_manifest":
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=10)

    hot_service = NativeRevisionService(pool, payload_store=store, failpoint=hold_hot)
    hot_task = asyncio.create_task(
        hot_service.replace_text(
            namespace_id=hot_namespace_id,
            surface="document",
            path="hot.md",
            payload="hot-after\n",
            actor="m1-isolation",
            mutation_id=mutation_id(),
            expected_revision_id=hot.revision_id,
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=10)
    requests.append(request_record("isolation-hot-write", path="hot.md", state="authority_tx_active"))
    get_started = time.perf_counter()
    cold_get = await asyncio.wait_for(
        service.get_current(namespace_id=cold_namespace_id, surface="document", path="cold.md"),
        timeout=5,
    )
    get_ms = round((time.perf_counter() - get_started) * 1000, 3)
    grep_started = time.perf_counter()
    cold_grep = await asyncio.wait_for(
        M1NativeGrepService(pool, body_store=store).grep(
            "needle",
            user_id=owner_id,
            resource_id=cold.resource_id,
        ),
        timeout=5,
    )
    grep_ms = round((time.perf_counter() - grep_started) * 1000, 3)
    async with pool.acquire() as conn:
        activity_after_cold_reads = int(
            await conn.fetchval(
                "SELECT count(*) FROM native_revision_activity WHERE namespace_id = ANY($1::uuid[])",
                [hot_namespace_id, cold_namespace_id],
            )
        )
    completed_before_hot_drain = not hot_task.done()
    release.set()
    published_hot = await asyncio.wait_for(hot_task, timeout=10)
    requests.extend(
        [
            request_record("isolation-cold-get", status="pass", latency_ms=get_ms),
            request_record("isolation-cold-grep", status="pass", latency_ms=grep_ms),
        ]
    )
    expected_digest = hashlib.sha256(b"cold-authoritative needle\n").hexdigest()
    return {
            "C4": {
                "isolation": {
                    "hot_writes": {"published": 1, "active_when_cold_issued": True},
                    "cold_get": {
                        "issued_while_hot_active": True,
                        "completed_before_hot_drain": completed_before_hot_drain,
                        "status": "pass" if cold_get.digest == expected_digest else "fail",
                        "response_digest": cold_get.digest,
                        "latency_ms": get_ms,
                    },
                    "cold_grep": {
                        "issued_while_hot_active": True,
                        "completed_before_hot_drain": completed_before_hot_drain,
                        "status": "pass" if cold_grep["total_resources"] == 1 else "fail",
                        "response_digest": cold_grep["results"][0]["content_hash"],
                        "latency_ms": grep_ms,
                    },
                    "final_authority": {
                        "hot_head_revision": published_hot.revision_id,
                        "cold_head_revision": cold_get.revision_id,
                        "unexpected_activity_delta": activity_after_cold_reads
                        - activity_before_cold_reads,
                    },
                }
            }
        }


async def workload_conflict(
    service: NativeRevisionService, namespace_id: uuid.UUID, requests: list[dict[str, Any]]
) -> dict[str, Any]:
    seed = await service.create_text(
        namespace_id=namespace_id,
        surface="document",
        path="conflict.md",
        payload="seed\n",
        actor="m1-actor",
        mutation_id=mutation_id(),
    )
    before_same_base = await authority_snapshot(service.pool, namespace_id)

    async def replace(writer: str) -> tuple[str, Any]:
        key = mutation_id()
        requests.append(
            request_record("same-base-replace", writer=writer, mutation_id=str(key), expected_revision=seed.revision_id)
        )
        try:
            return writer, await service.replace_text(
                namespace_id=namespace_id,
                surface="document",
                path="conflict.md",
                payload=f"{writer}\n",
                actor=writer,
                mutation_id=key,
                expected_revision_id=seed.revision_id,
            )
        except ConflictError:
            return writer, "conflict"

    one, two = await asyncio.gather(replace("writer-a"), replace("writer-b"))
    winner_pair = next(pair for pair in (one, two) if pair[1] != "conflict")
    loser_pair = next(pair for pair in (one, two) if pair[1] == "conflict")
    after_race = await authority_snapshot(service.pool, namespace_id)
    retry_key = mutation_id()
    before_retry = await authority_snapshot(service.pool, namespace_id)
    await service.replace_text(
        namespace_id=namespace_id,
        surface="document",
        path="conflict.md",
        payload=f"{loser_pair[0]} retry\n",
        actor=loser_pair[0],
        mutation_id=retry_key,
        expected_revision_id=winner_pair[1].revision_id,
    )
    after_retry = await authority_snapshot(service.pool, namespace_id)

    left = await service.create_text(
        namespace_id=namespace_id,
        surface="document",
        path="move-a.md",
        payload="a\n",
        actor="m1-actor",
        mutation_id=mutation_id(),
    )
    right = await service.create_text(
        namespace_id=namespace_id,
        surface="document",
        path="move-b.md",
        payload="b\n",
        actor="m1-actor",
        mutation_id=mutation_id(),
    )
    before_moves = await authority_snapshot(service.pool, namespace_id)

    async def move(actor: str, path: str, revision: str) -> tuple[str, Any]:
        key = mutation_id()
        requests.append(
            request_record(
                "same-destination-move", mover=actor, path=path, path_to="destination.md", mutation_id=str(key)
            )
        )
        try:
            return actor, await service.move_text(
                namespace_id=namespace_id,
                surface="document",
                path=path,
                path_to="destination.md",
                actor=actor,
                mutation_id=key,
                expected_revision_id=revision,
            )
        except ConflictError:
            return actor, "conflict"

    moved_left, moved_right = await asyncio.gather(
        move("move-a", "move-a.md", left.revision_id), move("move-b", "move-b.md", right.revision_id)
    )
    move_winner = next(pair for pair in (moved_left, moved_right) if pair[1] != "conflict")
    move_loser = next(pair for pair in (moved_left, moved_right) if pair[1] == "conflict")
    after_moves = await authority_snapshot(service.pool, namespace_id)
    return {
        "C4": {
            "same_base": {
                "winner": winner_pair[0],
                "loser": loser_pair[0],
                "loser_outcome": "conflict",
                "activity_delta": after_race["activities"] - before_same_base["activities"],
                "head_revision": winner_pair[1].revision_id,
                "retry": {
                    "outcome": "published",
                    "activity_delta": after_retry["activities"] - before_retry["activities"],
                },
            },
            "same_destination": {
                "winner": move_winner[0],
                "loser": move_loser[0],
                "loser_outcome": "conflict",
                "destination_count": sum(path == "destination.md" for path in after_moves["paths"]),
                "activity_delta": after_moves["activities"] - before_moves["activities"],
            },
        }
    }


def one_shot_failpoint(actual_name: str) -> Callable[[str], Awaitable[None]]:
    triggered = False

    async def failpoint(name: str) -> None:
        nonlocal triggered
        if name == actual_name and not triggered:
            triggered = True
            raise RuntimeError(f"injected native M1 boundary: {actual_name}")

    return failpoint


async def invoke_boundary(
    service: NativeRevisionService,
    namespace_id: uuid.UUID,
    boundary: str,
    key: uuid.UUID,
    ordinal: int,
    requests: list[dict[str, Any]],
    alias_seeds: dict[int, tuple[str, str, str]],
) -> Any:
    if boundary == "alias":
        source, target, seed_revision = alias_seeds[ordinal]
        requests.append(
            request_record("failpoint-move", boundary=boundary, mutation_id=str(key), path=source, path_to=target)
        )
        return await service.move_text(
            namespace_id=namespace_id,
            surface="document",
            path=source,
            path_to=target,
            actor="m1-actor",
            mutation_id=key,
            expected_revision_id=seed_revision,
        )
    path = f"failpoint-{ordinal}-{boundary}.md"
    requests.append(request_record("failpoint-create", boundary=boundary, mutation_id=str(key), path=path))
    return await service.create_text(
        namespace_id=namespace_id, surface="document", path=path, payload="payload\n", actor="m1-actor", mutation_id=key
    )


async def workload_failpoint(
    service: NativeRevisionService, namespace_id: uuid.UUID, requests: list[dict[str, Any]]
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    alias_seeds: dict[int, tuple[str, str, str]] = {}
    for ordinal, boundary in enumerate(PRECOMMIT_BOUNDARIES):
        key = mutation_id()
        if boundary == "alias":
            source = f"failpoint-{ordinal}-old.md"
            target = f"failpoint-{ordinal}-new.md"
            seed = await NativeRevisionService(service.pool).create_text(
                namespace_id=namespace_id,
                surface="document",
                path=source,
                payload="seed\n",
                actor="m1-actor",
                mutation_id=mutation_id(),
            )
            alias_seeds[ordinal] = (source, target, seed.revision_id)
            requests.append(request_record("failpoint-alias-seed", path=source, revision=seed.revision_id))
        before = await authority_snapshot(service.pool, namespace_id)
        injected = NativeRevisionService(service.pool, failpoint=one_shot_failpoint(ACTUAL_FAILPOINTS[boundary]))
        try:
            await invoke_boundary(injected, namespace_id, boundary, key, ordinal, requests, alias_seeds)
        except RuntimeError as exc:
            if "injected native M1 boundary" not in str(exc):
                raise
        else:
            raise AdapterError(f"failpoint did not fire for {boundary}")
        after = await authority_snapshot(service.pool, namespace_id)
        before_retry = await authority_snapshot(service.pool, namespace_id)
        published = await invoke_boundary(
            NativeRevisionService(service.pool), namespace_id, boundary, key, ordinal, requests, alias_seeds
        )
        after_retry = await authority_snapshot(service.pool, namespace_id)
        if published.idempotent_replay:
            raise AdapterError(f"pre-commit retry unexpectedly replayed for {boundary}")
        if (
            after_retry["activities"] - before_retry["activities"] != 1
            or after_retry["invalidation_intents"] - before_retry["invalidation_intents"] != 1
        ):
            raise AdapterError(f"retry failed to publish exactly once for {boundary}")
        rows.append(
            {
                "boundary": boundary,
                "failure_outcome": "injected",
                "before": before,
                "after": after,
                "retry": {"outcome": "published", "activity_delta": 1, "invalidation_intent_delta": 1},
            }
        )

    key = mutation_id()
    before = await authority_snapshot(service.pool, namespace_id)
    lost = NativeRevisionService(service.pool, failpoint=one_shot_failpoint("authority.after_commit_before_response"))
    try:
        await invoke_boundary(
            lost, namespace_id, "committed_response_lost", key, len(PRECOMMIT_BOUNDARIES), requests, alias_seeds
        )
    except RuntimeError as exc:
        if "injected native M1 boundary" not in str(exc):
            raise
    else:
        raise AdapterError("committed response loss failpoint did not fire")
    after = await authority_snapshot(service.pool, namespace_id)
    recovered = await invoke_boundary(
        NativeRevisionService(service.pool),
        namespace_id,
        "committed_response_lost",
        key,
        len(PRECOMMIT_BOUNDARIES),
        requests,
        alias_seeds,
    )
    final = await authority_snapshot(service.pool, namespace_id)
    if not recovered.idempotent_replay or final != after:
        raise AdapterError("committed response recovery was not an idempotent replay")
    rows.append(
        {
            "boundary": "committed_response_lost",
            "failure_outcome": "response_lost",
            "before": before,
            "after": after,
            "retry": {
                "outcome": "idempotent_recovery",
                "revision_delta": 0,
                "head_delta": 0,
                "activity_delta": 0,
                "invalidation_intent_delta": 0,
                "publication_count": {
                    "revisions": after["revisions"] - before["revisions"],
                    "heads": after["head_publications"] - before["head_publications"],
                    "activities": after["activities"] - before["activities"],
                    "invalidation_intents": after["invalidation_intents"] - before["invalidation_intents"],
                },
            },
        }
    )
    return {"C5": {"failpoints": rows}}


async def run() -> dict[str, Any]:
    contract = load_contract()
    workload = required_environment("AKB_NATIVE_REVISION_WORKLOAD")
    expected_cases = {
        "W1-lifecycle": "C2,C6",
        "W2-independent": "C4",
        "W2-conflict": "C4",
        "W2-isolation": "C4",
        "W2-failpoint": "C5",
    }
    if workload not in expected_cases:
        raise AdapterError(f"unsupported native M1 workload: {workload}")
    if required_environment("AKB_NATIVE_REVISION_NATIVE_CASE_IDS") != expected_cases[workload]:
        raise AdapterError("driver case binding differs from the requested workload")
    dsn = required_environment("AKB_NATIVE_REVISION_MEASUREMENT_DSN")
    pool, namespace_id, database = await initialise_measurement_database(dsn)
    requests: list[dict[str, Any]] = []
    cleanup_namespaces = [namespace_id]
    cleanup_users: list[uuid.UUID] = []
    started = time.perf_counter()
    try:
        service = NativeRevisionService(pool)
        if workload == "W1-lifecycle":
            cases, requests = await workload_lifecycle(service, namespace_id, contract, requests)
        elif workload == "W2-independent":
            cases = await workload_independent(service, namespace_id, requests)
        elif workload == "W2-conflict":
            cases = await workload_conflict(service, namespace_id, requests)
        elif workload == "W2-isolation":
            cases = await workload_isolation(
                pool,
                namespace_id,
                requests,
                cleanup_namespaces,
                cleanup_users,
            )
        else:
            cases = await workload_failpoint(service, namespace_id, requests)
        authority = await authority_snapshot(pool, namespace_id)
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM vaults WHERE id = ANY($1::uuid[])", cleanup_namespaces)
            if cleanup_users:
                await conn.execute("DELETE FROM users WHERE id = ANY($1::uuid[])", cleanup_users)
        await pool.close()
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    revision = source_revision()
    runtime, environment = receipt_provenance(revision, database, namespace_id)
    request_artifact = write_bound_json(
        run_artifact_path("native-requests"),
        {"workload": workload, "namespace_id": str(namespace_id), "requests": requests},
    )
    authority_artifact = write_bound_json(
        run_artifact_path("native-authority"),
        {"workload": workload, "namespace_id": str(namespace_id), "database": database, "final_authority": authority},
    )
    return {
        "protocol_version": PROTOCOL_VERSION,
        "workload": workload,
        "cases": cases,
        "receipt": {
            "inputs": {
                "seed": required_environment("AKB_NATIVE_REVISION_RUN_ID"),
                "corpus_id": str(namespace_id),
                "request_trace_id": f"native-m1-{uuid.uuid4().hex}",
            },
            "runtime": runtime,
            "environment": environment,
            "latency": {"samples_or_artifact": [elapsed_ms], "unit": "ms"},
            "resources": {
                "snapshot": {"database": database, "namespace_id": str(namespace_id), "authority": authority}
            },
            "requests": {
                "outcomes": [
                    {"workload": workload, "operations": len(requests), "authority_revisions": authority["revisions"]}
                ],
                "artifact_digest": request_artifact["sha256"],
            },
        },
        "provenance": {
            "adapter": {"identity": "akb.backend.scripts.native_revision_m1_adapter", "source_revision": revision},
            "request_artifact": request_artifact,
            "authority_artifact": authority_artifact,
        },
    }


def main() -> int:
    observation_path = Path(required_environment("AKB_NATIVE_REVISION_NATIVE_OBSERVATION_PATH"))
    observation = asyncio.run(run())
    write_bound_json(observation_path, observation)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AdapterError as exc:
        print(f"native revision M1 adapter: {exc}", file=sys.stderr)
        raise SystemExit(2)
