#!/usr/bin/env python3
"""Drive the public W4 File HTTP trace and emit a token-free receipt."""

from __future__ import annotations

import hashlib
import json
import sys
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BACKEND = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
for entry in (BACKEND, SCRIPT_DIR):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from native_revision_m1_adapter import AdapterError, required_environment, write_bound_json  # noqa: E402


WORKLOAD = "W4-public-file"
PROTOCOL_VERSION = "akb-native-revision-m1-file/v2"


def _request(url: str, method: str, *, authorization: str | None = None, body: bytes | None = None) -> tuple[int, dict, bytes]:
    headers = {"Authorization": authorization} if authorization else {}
    if body is not None:
        headers["Content-Type"] = "application/octet-stream"
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310 -- explicit measurement URL
            raw = response.read()
            return response.status, json.loads(raw) if raw and "json" in response.headers.get("Content-Type", "") else {}, raw
    except HTTPError as exc:
        raw = exc.read()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {}
        return exc.code, parsed, raw


def run_trace(base_url: str, vault: str, denied_vault: str, authorization: str) -> list[dict]:
    payload = b"AKB public W4 File payload\x00"
    digest = hashlib.sha256(payload).hexdigest()
    query = urlencode({"filename": "w4-public-file.bin", "mime_type": "application/octet-stream", "content_hash": digest})
    status, initiated, _ = _request(f"{base_url}/api/v1/files/{vault}/upload?{query}", "POST", authorization=authorization)
    if status != 200:
        raise AdapterError(f"initiate failed: {status}")
    trace = [{"operation": "initiate", "status": status, "digest": digest, "size_bytes": len(payload), "transfer_expires_in": initiated.get("expires_in")}]
    upload_url = initiated["upload_url"]
    file_id = initiated["uri"].rsplit("/", 1)[-1]
    status, _, _ = _request(upload_url, "PUT", body=payload)
    trace.append({"operation": "client_transfer", "status": status, "logical_file_id": file_id, "transfer_one_time": "consumed" if status == 200 else "failed"})
    status, _, _ = _request(upload_url, "PUT", body=payload)
    trace.append({"operation": "second_replica_transfer_reuse", "status": status, "logical_file_id": file_id, "transfer_one_time": "rejected" if status in {403, 409} else "incorrectly_accepted"})
    confirm_url = f"{base_url}/api/v1/files/{vault}/{file_id}/confirm?content_hash={digest}"
    status, confirmed, _ = _request(confirm_url, "POST", authorization=authorization)
    trace.append({"operation": "confirm", "status": status, "logical_file_id": file_id, "driver": confirmed.get("storage_driver"), "digest": confirmed.get("content_hash"), "size_bytes": confirmed.get("size_bytes")})
    status, retry, _ = _request(f"{base_url}/api/v1/files/{vault}/upload?{query}", "POST", authorization=authorization)
    trace.append({"operation": "same_digest_retry", "status": status, "logical_file_id": file_id, "deduplicated": retry.get("deduplicated")})
    status, _, _ = _request(f"{base_url}/api/v1/files/{denied_vault}/{file_id}/download", "GET", authorization=authorization)
    trace.append({"operation": "cross_vault_download", "status": status, "logical_file_id": file_id, "acl": "denied"})
    status, download, _ = _request(f"{base_url}/api/v1/files/{vault}/{file_id}/download", "GET", authorization=authorization)
    trace.append({"operation": "get_download", "status": status, "logical_file_id": file_id, "transfer_expires_in": download.get("expires_in")})
    status, _, opened = _request(download["download_url"], "GET")
    trace.append({"operation": "open", "status": status, "logical_file_id": file_id, "downloaded_digest": hashlib.sha256(opened).hexdigest(), "exact_bytes": opened == payload})
    status, _, _ = _request(f"{base_url}/api/v1/files/{vault}/{file_id}", "DELETE", authorization=authorization)
    trace.append({"operation": "delete", "status": status, "logical_file_id": file_id})
    expected = [200, 200, 403, 200, 200, 404, 200, 200, 200]
    if [event["status"] for event in trace] != expected or not trace[-2]["exact_bytes"]:
        raise AdapterError("public File trace assertions failed")
    return trace


def main() -> int:
    if required_environment("AKB_NATIVE_REVISION_WORKLOAD") != WORKLOAD:
        raise AdapterError("file adapter only supports W4-public-file")
    base_url = required_environment("AKB_NATIVE_REVISION_PUBLIC_URL").rstrip("/")
    vault = required_environment("AKB_NATIVE_REVISION_FILE_VAULT")
    denied_vault = required_environment("AKB_NATIVE_REVISION_FILE_DENIED_VAULT")
    authorization = required_environment("AKB_NATIVE_REVISION_FILE_AUTHORIZATION")
    trace = run_trace(base_url, vault, denied_vault, authorization)
    output = Path(required_environment("AKB_NATIVE_REVISION_NATIVE_OBSERVATION_PATH"))
    write_bound_json(output, {"protocol_version": PROTOCOL_VERSION, "workload": WORKLOAD, "request_trace_id": f"m1-file-{uuid.uuid4().hex}", "requests": {"outcomes": trace}})
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AdapterError as exc:
        print(f"native revision M1 file adapter: {exc}", file=sys.stderr)
        raise SystemExit(2)
