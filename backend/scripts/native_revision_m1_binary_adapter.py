#!/usr/bin/env python3
"""Run the same logical W4 fixture against one selected BinaryStore."""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import asyncpg
from botocore.exceptions import ClientError


BACKEND = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
for entry in (BACKEND, SCRIPT_DIR):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from app.exceptions import ValidationError  # noqa: E402
from app.services.m1_binary_store import (  # noqa: E402
    BinaryStore,
    FilesystemCAS,
    PreparedBinary,
    S3CAS,
)
from native_revision_m1_adapter import (  # noqa: E402
    AdapterError,
    receipt_provenance,
    required_environment,
    run_artifact_path,
    source_revision,
    validate_measurement_database,
    write_bound_json,
)


PROTOCOL_VERSION = "akb-native-revision-m1-binary/v1"
WORKLOAD = "W4-public-file"
# M0 inventory: min 0, p50 70,530.5, p95 1,517,640.9, max 4,076,641,280.
# The bounded M1 fixture rounds p50/p95 and omits the 4 GiB maximum; the maximum
# is an operations/deployment qualification cohort, not needed to select logical
# semantics in every comparison run.
SIZE_COHORTS = (0, 70_531, 1_517_641)


def _payload(size: int) -> bytes:
    seed = b"akb-m1-binary-fixture\x00"
    if size <= len(seed):
        return seed[:size]
    return (seed * ((size // len(seed)) + 1))[:size]


def _safe_root() -> Path:
    root = Path(required_environment("AKB_NATIVE_REVISION_BINARY_MEASUREMENT_ROOT")).resolve()
    if root.name != "binary-measurement" or root in {Path("/").resolve(), Path.home().resolve()}:
        raise AdapterError("FilesystemCAS root must be a dedicated directory named binary-measurement")
    root.mkdir(parents=True, exist_ok=True)
    if os.environ.get("AKB_NATIVE_REVISION_MEASUREMENT_TIER", "E0") == "E1b" and not root.is_mount():
        raise AdapterError("E1b FilesystemCAS root must be the run-owned PVC mountpoint")
    return root


async def _initialise_database(dsn: str) -> tuple[asyncpg.Pool, str, uuid.UUID, uuid.UUID]:
    connection = await asyncpg.connect(dsn, timeout=10)
    try:
        database = str(await connection.fetchval("SELECT current_database()"))
        validate_measurement_database(database)
        await connection.execute((BACKEND / "app" / "db" / "init.sql").read_text(encoding="utf-8"))
        suffix = uuid.uuid4().hex
        owner_id = await connection.fetchval(
            """
            INSERT INTO users (username, email, password_hash)
            VALUES ($1, $2, 'm1-measurement-disabled') RETURNING id
            """,
            f"m1-binary-owner-{suffix}",
            f"m1-binary-owner-{suffix}@invalid.example",
        )
        namespace_id = await connection.fetchval(
            """
            INSERT INTO vaults (name, git_path, owner_id)
            VALUES ($1, '/tmp/m1-native-measurement-unused.git', $2) RETURNING id
            """,
            f"m1-binary-{suffix}",
            owner_id,
        )
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS m1_binary_measurement_publications (
                file_id UUID PRIMARY KEY,
                namespace_id UUID NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
                logical_path TEXT NOT NULL,
                revision_id TEXT NOT NULL CHECK (revision_id ~ '^[0-9a-f]{40}$'),
                digest TEXT NOT NULL CHECK (digest ~ '^[0-9a-f]{64}$'),
                byte_size BIGINT NOT NULL CHECK (byte_size >= 0),
                driver TEXT NOT NULL CHECK (driver IN ('fscas', 's3')),
                private_locator TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (namespace_id, logical_path)
            )
            """
        )
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
        return pool, database, owner_id, namespace_id
    finally:
        await connection.close()


async def _publish(
    pool: asyncpg.Pool,
    *,
    namespace_id: uuid.UUID,
    logical_path: str,
    prepared: PreparedBinary,
) -> dict[str, Any]:
    revision_id = secrets.token_hex(20)
    file_id = uuid.uuid4()
    async with pool.acquire() as connection:
        async with connection.transaction():
            row = await connection.fetchrow(
                """
                INSERT INTO m1_binary_measurement_publications (
                    file_id, namespace_id, logical_path, revision_id, digest,
                    byte_size, driver, private_locator
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (namespace_id, logical_path) DO NOTHING
                RETURNING file_id, revision_id, digest, byte_size, driver, private_locator
                """,
                file_id,
                namespace_id,
                logical_path,
                revision_id,
                prepared.digest,
                prepared.size,
                prepared.driver,
                prepared.locator,
            )
            if row is None:
                row = await connection.fetchrow(
                    """
                    SELECT file_id, revision_id, digest, byte_size, driver, private_locator
                      FROM m1_binary_measurement_publications
                     WHERE namespace_id = $1 AND logical_path = $2
                    """,
                    namespace_id,
                    logical_path,
                )
                if (
                    row is None
                    or row["digest"] != prepared.digest
                    or row["byte_size"] != prepared.size
                    or row["driver"] != prepared.driver
                    or row["private_locator"] != prepared.locator
                ):
                    raise AdapterError("logical File path was reused with different immutable bytes")
    return {"file_id": row["file_id"], "revision_id": row["revision_id"]}


async def _visible_count(pool: asyncpg.Pool, namespace_id: uuid.UUID) -> int:
    async with pool.acquire() as connection:
        return int(
            await connection.fetchval(
                "SELECT count(*) FROM m1_binary_measurement_publications WHERE namespace_id = $1",
                namespace_id,
            )
        )


class _InjectedPublicationFailure(RuntimeError):
    pass


async def _publish_then_rollback(
    pool: asyncpg.Pool,
    *,
    namespace_id: uuid.UUID,
    logical_path: str,
    prepared: PreparedBinary,
) -> bool:
    """Insert a logical publication and force its transaction to roll back."""
    try:
        async with pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    """
                    INSERT INTO m1_binary_measurement_publications (
                        file_id, namespace_id, logical_path, revision_id, digest,
                        byte_size, driver, private_locator
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    """,
                    uuid.uuid4(),
                    namespace_id,
                    logical_path,
                    secrets.token_hex(20),
                    prepared.digest,
                    prepared.size,
                    prepared.driver,
                    prepared.locator,
                )
                visible_inside = int(
                    await connection.fetchval(
                        "SELECT count(*) FROM m1_binary_measurement_publications WHERE namespace_id = $1",
                        namespace_id,
                    )
                )
                if visible_inside != 1:
                    raise AdapterError("injected publication was not visible inside its transaction")
                raise _InjectedPublicationFailure
    except _InjectedPublicationFailure:
        return await _visible_count(pool, namespace_id) == 0


async def _logical_open(
    pool: asyncpg.Pool,
    store: BinaryStore,
    tenant: str,
    namespace_id: uuid.UUID,
    logical_path: str,
) -> tuple[bytes, str]:
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            """
            SELECT revision_id, digest, byte_size, driver, private_locator
              FROM m1_binary_measurement_publications
             WHERE namespace_id = $1 AND logical_path = $2
            """,
            namespace_id,
            logical_path,
        )
    if row is None:
        raise AdapterError("logical File is not visible")
    prepared = PreparedBinary(
        locator=row["private_locator"],
        digest=row["digest"],
        size=row["byte_size"],
        driver=row["driver"],
    )
    return store.open_verified(tenant, prepared), row["revision_id"]


async def _logical_delete(pool: asyncpg.Pool, namespace_id: uuid.UUID, logical_path: str) -> None:
    async with pool.acquire() as connection:
        await connection.execute(
            "DELETE FROM m1_binary_measurement_publications WHERE namespace_id = $1 AND logical_path = $2",
            namespace_id,
            logical_path,
        )


def _delete_and_verify_s3_objects(
    client,
    bucket: str,
    tenant: str,
    objects: list[PreparedBinary],
) -> None:
    prefix = f"m1-binary/{tenant}/"
    errors: list[str] = []
    keys = {prepared.locator for prepared in objects}
    try:
        page = client.list_objects_v2(Bucket=bucket, Prefix=prefix)
        keys.update(item["Key"] for item in page.get("Contents", []))
    except Exception:
        errors.append("list_before_cleanup")
    for key in sorted(keys):
        try:
            client.delete_object(Bucket=bucket, Key=key)
            try:
                client.head_object(Bucket=bucket, Key=key)
            except ClientError as exc:
                response = exc.response or {}
                status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                code = str(response.get("Error", {}).get("Code", ""))
                if status != 404 and code not in {"404", "NoSuchKey", "NotFound"}:
                    raise
            else:
                raise AdapterError("run-owned S3 measurement object remained after delete")
        except Exception as exc:
            errors.append(type(exc).__name__)
    try:
        remaining = client.list_objects_v2(Bucket=bucket, Prefix=prefix).get("Contents", [])
    except Exception:
        errors.append("list_after_cleanup")
        remaining = ["unknown"]
    if errors or remaining:
        raise AdapterError(
            "run-owned S3 cleanup was incomplete "
            f"(error_count={len(errors)}, remaining_count={len(remaining)})"
        )


async def _run_fixture(
    pool: asyncpg.Pool,
    store: BinaryStore,
    tenant: str,
    namespace_id: uuid.UUID,
    prepared_objects: list[PreparedBinary],
) -> tuple[dict[str, Any], list[float]]:
    assertions: dict[str, bool] = {}
    timings: list[float] = []
    assertions["initiated_not_visible"] = await _visible_count(pool, namespace_id) == 0
    interrupted_payload = _payload(1024)
    try:
        store.prepare_verified(
            tenant,
            interrupted_payload[:-1],
            hashlib.sha256(interrupted_payload).hexdigest(),
            len(interrupted_payload),
        )
    except ValidationError:
        assertions["interrupted_transfer_not_visible"] = (
            await _visible_count(pool, namespace_id) == 0
        )
    else:
        assertions["interrupted_transfer_not_visible"] = False

    for size in SIZE_COHORTS:
        data = _payload(size)
        digest = hashlib.sha256(data).hexdigest()
        logical_path = f"cohort-{size}.bin"
        started = time.perf_counter()
        prepared = store.prepare_verified(tenant, data, digest, size)
        timings.append(round((time.perf_counter() - started) * 1000, 3))
        prepared_objects.append(prepared)

        # A prepared CAS object does not publish a logical File.  This is the
        # injected DB-publication failure boundary.
        assertions[f"failed_db_publish_nonvisible_{size}"] = await _publish_then_rollback(
            pool,
            namespace_id=namespace_id,
            logical_path=f"rollback-{logical_path}",
            prepared=prepared,
        )

        published = await _publish(
            pool,
            namespace_id=namespace_id,
            logical_path=logical_path,
            prepared=prepared,
        )
        opened, revision_id = await _logical_open(pool, store, tenant, namespace_id, logical_path)
        assertions[f"confirmed_exact_bytes_{size}"] = opened == data
        assertions[f"revision_shape_{size}"] = len(revision_id) == 40
        assertions[f"stat_verified_{size}"] = store.stat_verified(tenant, prepared) == size

        adopted = store.prepare_verified(tenant, data, digest, size)
        retry = await _publish(
            pool,
            namespace_id=namespace_id,
            logical_path=logical_path,
            prepared=adopted,
        )
        assertions[f"confirmed_idempotent_{size}"] = retry == published and adopted == prepared
        assertions[f"confirmed_{size}"] = True

        await _logical_delete(pool, namespace_id, logical_path)
        assertions[f"delete_nonvisible_{size}"] = await _visible_count(pool, namespace_id) == 0

    return {"assertions": assertions, "size_cohorts": list(SIZE_COHORTS)}, timings


async def run() -> dict[str, Any]:
    if required_environment("AKB_NATIVE_REVISION_WORKLOAD") != WORKLOAD:
        raise AdapterError("binary adapter only supports W4-public-file")
    driver = required_environment("AKB_NATIVE_REVISION_BINARY_DRIVER")
    dsn = required_environment("AKB_NATIVE_REVISION_MEASUREMENT_DSN")
    pool, database, owner_id, namespace_id = await _initialise_database(dsn)
    tenant = f"m1-{uuid.uuid4().hex}"
    s3_client = None
    if driver == "fscas":
        store: BinaryStore = FilesystemCAS(_safe_root())
    elif driver == "s3":
        from app.config import settings
        from app.services.adapters import s3_adapter

        bucket = required_environment("AKB_NATIVE_REVISION_BINARY_MEASUREMENT_BUCKET")
        if bucket != settings.s3_bucket:
            raise AdapterError("measurement bucket must equal the configured AKB File bucket")
        s3_client = s3_adapter.client()
        store = S3CAS(bucket, s3_client)
    else:
        raise AdapterError("driver must be fscas or s3")

    started = time.perf_counter()
    prepared_objects: list[PreparedBinary] = []
    try:
        cases, timings = await _run_fixture(
            pool,
            store,
            tenant,
            namespace_id,
            prepared_objects,
        )
        if not all(cases["assertions"].values()):
            failed = [name for name, passed in cases["assertions"].items() if not passed]
            raise AdapterError(f"binary semantic assertions failed: {failed}")
        revision = source_revision()
        runtime, environment = receipt_provenance(revision, database, namespace_id)
        environment["storage_profile"].update(
            {
                "binary_driver": driver,
                "binary_authority": "verified-cas-plus-postgresql-logical-publication",
                "locator_exposure": "private",
            }
        )
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        cleanup = "namespace_pvc_owned_verified_mountpoint"
        if s3_client is not None:
            bucket = required_environment("AKB_NATIVE_REVISION_BINARY_MEASUREMENT_BUCKET")
            _delete_and_verify_s3_objects(s3_client, bucket, tenant, prepared_objects)
            prepared_objects.clear()
            cleanup = "run_owned_s3_objects_deleted_and_absence_verified"
        request_artifact = write_bound_json(
            run_artifact_path("native-binary-requests"),
            {"workload": WORKLOAD, "driver": driver, "size_cohorts": list(SIZE_COHORTS), "stage_latency_ms": timings},
        )
        authority_artifact = write_bound_json(
            run_artifact_path("native-binary-authority"),
            {
                "workload": WORKLOAD,
                "driver": driver,
                "assertions": cases["assertions"],
                "cleanup": cleanup,
            },
        )
        return {
            "protocol_version": PROTOCOL_VERSION,
            "workload": WORKLOAD,
            "driver": driver,
            "cases": cases,
            "receipt": {
                "inputs": {
                    "seed": required_environment("AKB_NATIVE_REVISION_RUN_ID"),
                    "corpus_id": str(namespace_id),
                    "request_trace_id": f"native-m1-binary-{uuid.uuid4().hex}",
                },
                "runtime": runtime,
                "environment": environment,
                "latency": {"samples_or_artifact": timings, "unit": "ms", "total_ms": elapsed_ms},
                "resources": {
                    "snapshot": {
                        "database": database,
                        "driver": driver,
                        "size_cohorts": list(SIZE_COHORTS),
                        "cleanup": cleanup,
                    }
                },
                "requests": {
                    "outcomes": [{"operation": name, "success": passed} for name, passed in cases["assertions"].items()],
                    "artifact_digest": request_artifact["sha256"],
                },
            },
            "provenance": {
                "adapter": {
                    "identity": "akb.backend.scripts.native_revision_m1_binary_adapter",
                    "source_revision": revision,
                },
                "request_artifact": request_artifact,
                "authority_artifact": authority_artifact,
            },
        }
    finally:
        try:
            if s3_client is not None:
                bucket = required_environment("AKB_NATIVE_REVISION_BINARY_MEASUREMENT_BUCKET")
                _delete_and_verify_s3_objects(s3_client, bucket, tenant, prepared_objects)
            async with pool.acquire() as connection:
                await connection.execute("DELETE FROM vaults WHERE id = $1", namespace_id)
                await connection.execute("DELETE FROM users WHERE id = $1", owner_id)
        finally:
            await pool.close()


def main() -> int:
    output = Path(required_environment("AKB_NATIVE_REVISION_NATIVE_OBSERVATION_PATH"))
    write_bound_json(output, asyncio.run(run()))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AdapterError as exc:
        print(f"native revision M1 binary adapter: {exc}", file=sys.stderr)
        raise SystemExit(2)
