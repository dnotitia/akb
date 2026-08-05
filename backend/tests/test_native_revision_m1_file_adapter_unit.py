"""The W4 receipt driver follows public HTTP routes, never a BinaryStore."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _adapter():
    path = Path(__file__).resolve().parents[1] / "scripts" / "native_revision_m1_file_adapter.py"
    spec = importlib.util.spec_from_file_location("m1_file_adapter_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_w4_adapter_exercises_public_urls_and_redacts_capabilities(monkeypatch):
    adapter = _adapter()
    payload = b"AKB public W4 File payload\x00"
    digest = __import__("hashlib").sha256(payload).hexdigest()
    calls: list[tuple[str, str]] = []

    def request(url, method, *, authorization=None, body=None):
        calls.append((method, url))
        if url.endswith("/upload?filename=w4-public-file.bin&mime_type=application%2Foctet-stream&content_hash=" + digest):
            return 200, {"uri": "akb://v/file/f-1", "upload_url": "https://akb.test/api/v1/files/transfer/opaque", "expires_in": 60, "deduplicated": len(calls) > 4}, b""
        if url.endswith("/transfer/opaque") and method == "PUT":
            return (403 if sum(1 for m, u in calls if u.endswith("/transfer/opaque") and m == "PUT") > 1 else 200), {}, b""
        if url.endswith("/f-1/confirm?content_hash=" + digest):
            return 200, {"storage_driver": "fscas", "content_hash": digest, "size_bytes": len(payload)}, b""
        if "/denied/" in url:
            return 404, {}, b""
        if url.endswith("/f-1/download"):
            return 200, {"download_url": "https://akb.test/api/v1/files/transfer/download", "expires_in": 60}, b""
        if url.endswith("/transfer/download"):
            return 200, {}, payload
        if url.endswith("/f-1") and method == "DELETE":
            return 200, {}, b""
        raise AssertionError((method, url))

    monkeypatch.setattr(adapter, "_request", request)
    trace = adapter.run_trace("https://akb.test", "allowed", "denied", "Bearer test")

    assert [entry["operation"] for entry in trace] == [
        "initiate", "client_transfer", "second_replica_transfer_reuse", "confirm",
        "same_digest_retry", "cross_vault_download", "get_download", "open", "delete",
    ]
    assert "opaque" not in str(trace)
    assert any(url.startswith("https://akb.test/api/v1/files/allowed/upload") for _, url in calls)
