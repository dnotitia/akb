#!/usr/bin/env python3
"""Emit a public-File W4 receipt over one guarded CAS driver.

Unlike the older BinaryStore diagnostic, this adapter invokes the public
File-shaped facade in the same order used by the stdio proxy.  It intentionally
records operation facts, not URLs or opaque transfer capabilities.
"""

from __future__ import annotations

import hashlib
import sys
import uuid
from pathlib import Path
from typing import Any

import asyncpg


BACKEND = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
for entry in (BACKEND, SCRIPT_DIR):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from app.config import settings  # noqa: E402
from app.exceptions import AKBError, NotFoundError  # noqa: E402
from app.services.adapters import s3_adapter  # noqa: E402
from app.services.m1_binary_store import FilesystemCAS, S3CAS  # noqa: E402
from app.services.m1_file_measurement import MeasurementFileFacade  # noqa: E402
from native_revision_m1_adapter import (  # noqa: E402
    AdapterError,
    receipt_provenance,
    required_environment,
    run_artifact_path,
    source_revision,
    validate_measurement_database,
    write_bound_json,
)


PROTOCOL_VERSION = "akb-native-revision-m1-file/v1"
WORKLOAD = "W4-public-file"


def _safe_root() -> Path:
    root = Path(required_environment("AKB_NATIVE_REVISION_BINARY_MEASUREMENT_ROOT")).resolve()
    if root.name != "binary-measurement" or root in {Path("/").resolve(), Path.home().resolve()}:
        raise AdapterError("FilesystemCAS root must be a dedicated directory named binary-measurement")
    if not root.is_dir():
        raise AdapterError("FilesystemCAS root must be pre-provisioned")
    return root


async def _measurement_database(dsn: str) -> str:
    connection = await asyncpg.connect(dsn, timeout=10)
    try:
        database = str(await connection.fetchval("SELECT current_database()"))
        validate_measurement_database(database)
        return database
    finally:
        await connection.close()


def _driver(name: str):
    if name == "fscas":
        return FilesystemCAS(_safe_root())
    if name == "s3cas":
        bucket = required_environment("AKB_NATIVE_REVISION_BINARY_MEASUREMENT_BUCKET")
        if bucket != settings.s3_bucket:
            raise AdapterError("measurement bucket must equal the configured AKB File bucket")
        return S3CAS(bucket, s3_adapter.client())
    raise AdapterError("driver must be fscas or s3cas")


def _outcome(operation: str, status: int, **fields: Any) -> dict[str, Any]:
    return {"operation": operation, "status": status, **fields}


def run_trace(files: MeasurementFileFacade, *, vault: str, payload: bytes) -> dict[str, Any]:
    """The ordered public File trace; deliberately never returns a capability."""
    digest = hashlib.sha256(payload).hexdigest()
    trace: list[dict[str, Any]] = []
    initiated = files.initiate_upload(
        vault=vault, filename="w4-public-file.bin", mime_type="application/octet-stream", content_hash=digest,
    )
    file_id = initiated["file_id"]
    trace.append(_outcome(
        "initiate", 200, driver=files.driver, logical_file_id=file_id,
        digest=digest, size_bytes=len(payload), transfer_expires_in=initiated["expires_in"],
    ))
    files.transfer(initiated["upload_url"], payload)
    trace.append(_outcome("client_transfer", 200, logical_file_id=file_id, transfer_one_time="consumed"))
    try:
        files.transfer(initiated["upload_url"], payload)
    except AKBError as exc:
        trace.append(_outcome("client_transfer_reuse", exc.status_code, logical_file_id=file_id, transfer_one_time="rejected"))
    else:  # pragma: no cover - an assertion below turns this into a hard failure
        trace.append(_outcome("client_transfer_reuse", 200, logical_file_id=file_id, transfer_one_time="incorrectly_accepted"))

    confirmed = files.confirm_upload(file_id, content_hash=digest)
    trace.append(_outcome(
        "confirm", 200, driver=confirmed["storage_driver"], logical_file_id=file_id,
        logical_revision_id=confirmed["revision_id"], digest=confirmed["content_hash"], size_bytes=confirmed["size_bytes"],
    ))
    retry = files.initiate_upload(
        vault=vault, filename="w4-public-file.bin", mime_type="application/octet-stream", content_hash=digest,
    )
    trace.append(_outcome("same_digest_retry", 200, logical_file_id=retry["file_id"], deduplicated=retry["deduplicated"]))
    download = files.get_download_url(file_id)
    received = files.transfer(download["download_url"])
    trace.append(_outcome(
        "get_download", 200, logical_file_id=file_id, transfer_expires_in=download["expires_in"],
        downloaded_digest=hashlib.sha256(received or b"").hexdigest(), exact_bytes=(received == payload),
    ))
    opened = files.open(file_id)
    trace.append(_outcome("open", 200, logical_file_id=file_id, exact_bytes=(opened == payload)))
    deleted = files.delete(file_id)
    trace.append(_outcome("delete", 200, logical_file_id=file_id, deleted=deleted["deleted"]))
    try:
        files.open(file_id)
    except NotFoundError:
        trace.append(_outcome("open_after_delete", 404, logical_file_id=file_id, residue="absent"))
    else:  # pragma: no cover
        trace.append(_outcome("open_after_delete", 200, logical_file_id=file_id, residue="present"))
    if not all(
        item["status"] == expected
        for item, expected in zip(trace, (200, 200, 409, 200, 200, 200, 200, 200, 404), strict=True)
    ):
        raise AdapterError("public File trace did not meet required outcomes")
    return {"trace": trace, "digest": digest, "size_bytes": len(payload), "file_id": file_id}


async def run() -> dict[str, Any]:
    if required_environment("AKB_NATIVE_REVISION_WORKLOAD") != WORKLOAD:
        raise AdapterError("file adapter only supports W4-public-file")
    driver_name = required_environment("AKB_NATIVE_REVISION_BINARY_DRIVER")
    database = await _measurement_database(required_environment("AKB_NATIVE_REVISION_MEASUREMENT_DSN"))
    store = _driver(driver_name)
    vault = f"m1-w4-{uuid.uuid4().hex}"
    files = MeasurementFileFacade(store, base_url=settings.public_base_url or "https://measurement.invalid")
    result = run_trace(files, vault=vault, payload=b"AKB public W4 File payload\x00")
    revision = source_revision()
    runtime, environment = receipt_provenance(revision, database, uuid.uuid5(uuid.NAMESPACE_URL, vault))
    environment["storage_profile"].update({
        "binary_driver": files.driver,
        "binary_authority": "guarded-public-file-facade-plus-verified-cas",
        "locator_exposure": "opaque-expiring-capability-not-persisted",
    })
    request_artifact = write_bound_json(
        run_artifact_path("native-file-public-trace"),
        {"workload": WORKLOAD, "driver": files.driver, "trace": result["trace"]},
    )
    return {
        "protocol_version": PROTOCOL_VERSION,
        "workload": WORKLOAD,
        "driver": files.driver,
        "cases": {"public_file_trace": result["trace"]},
        "receipt": {
            "inputs": {"seed": required_environment("AKB_NATIVE_REVISION_RUN_ID"), "corpus_id": vault, "request_trace_id": f"native-m1-file-{uuid.uuid4().hex}"},
            "runtime": runtime,
            "environment": environment,
            "latency": {"samples_or_artifact": [], "unit": "ms"},
            "resources": {"snapshot": {"database": database, "driver": files.driver, "run_owned_residue": "absent_after_delete"}},
            "requests": {"outcomes": result["trace"], "artifact_digest": request_artifact["sha256"]},
        },
        "provenance": {"adapter": {"identity": "akb.backend.scripts.native_revision_m1_file_adapter", "source_revision": revision}, "request_artifact": request_artifact},
    }


def main() -> int:
    output = Path(required_environment("AKB_NATIVE_REVISION_NATIVE_OBSERVATION_PATH"))
    write_bound_json(output, __import__("asyncio").run(run()))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AdapterError as exc:
        print(f"native revision M1 file adapter: {exc}", file=sys.stderr)
        raise SystemExit(2)
