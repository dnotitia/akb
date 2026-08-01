#!/usr/bin/env python3
"""Run one safe, non-public M1 BinaryStore logical W4 fixture."""
from __future__ import annotations
import hashlib
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from app.services.m1_binary_store import BinaryStore, FilesystemCAS, MeasurementUpload, S3CAS

def required(name: str) -> str:
    value = os.environ.get(name)
    if not value: raise SystemExit(f"{name} is required")
    return value

def main() -> int:
    driver = required("AKB_NATIVE_REVISION_BINARY_DRIVER")
    tenant = required("AKB_NATIVE_REVISION_BINARY_MEASUREMENT_TENANT")
    output = Path(required("AKB_NATIVE_REVISION_BINARY_OBSERVATION_PATH"))
    if driver == "fscas":
        root = Path(required("AKB_NATIVE_REVISION_BINARY_MEASUREMENT_ROOT")).resolve()
        if "measurement" not in root.name: raise SystemExit("measurement root basename must contain 'measurement'")
        store: BinaryStore = FilesystemCAS(root); profile: dict[str, object] = {"driver": driver, "root_kind": "local-measurement-cas"}
    elif driver == "s3":
        from app.services.adapters import s3_adapter
        bucket = required("AKB_NATIVE_REVISION_BINARY_MEASUREMENT_BUCKET")
        store = S3CAS(bucket, s3_adapter.client()); profile = {"driver": driver, "bucket_configured": True}
    else: raise SystemExit("driver must be fscas or s3")
    payload = b"m1-binary-fixture\x00exact-bytes"
    digest = hashlib.sha256(payload).hexdigest(); upload = MeasurementUpload(store, tenant)
    before = upload.confirm(digest, len(payload), lambda _: None)[0]
    upload.transfer(payload)
    failed = upload.confirm(digest, len(payload), lambda _: (_ for _ in ()).throw(RuntimeError("simulated DB failure")))[0]
    status, prepared = upload.confirm(digest, len(payload), lambda _: None)
    assert prepared is not None and store.open_verified(tenant, prepared) == payload
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists(): raise SystemExit("observation already exists")
    output.write_text(json.dumps({"driver": driver, "profile": profile, "tenant_hash": hashlib.sha256(tenant.encode()).hexdigest(), "outcomes": [before, failed, status, "idempotent_adopt"], "digest": digest, "size": len(payload)}, sort_keys=True))
    return 0
if __name__ == "__main__": raise SystemExit(main())
