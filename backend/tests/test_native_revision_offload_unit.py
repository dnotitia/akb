from __future__ import annotations

import hashlib
import threading
import uuid
from datetime import UTC, datetime

import pytest

from app.services import native_revision_service
from app.services.native_revision_service import NativeRevisionService


@pytest.mark.asyncio
async def test_snapshot_manifest_hash_and_decode_run_off_event_loop(monkeypatch):
    body = "worker payload".encode()
    digest = hashlib.sha256(body).hexdigest()

    class _PayloadStore:
        async def open_verified(self, _payload_id):
            return body

    service = NativeRevisionService(
        object(),  # type: ignore[arg-type]
        repository=object(),  # type: ignore[arg-type]
        payload_store=_PayloadStore(),  # type: ignore[arg-type]
    )
    now = datetime.now(UTC)
    row = {
        "resource_id": uuid.uuid4(),
        "revision_id": "a" * 40,
        "parent_revision_id": None,
        "surface": "file",
        "content_profile": "text",
        "path": "worker.txt",
        "action": "create",
        "occurred_at": now,
        "resource_created_at": now,
        "resource_updated_at": now,
        "payload_manifest_id": uuid.uuid4(),
        "private_locator": uuid.uuid4(),
        "digest": digest,
        "byte_size": len(body),
        "encoding": "utf-8",
        "selected_placement": "pg-bodystore-v1",
        "verification_profile": "sha256-size-utf8-v1",
    }
    loop_thread = threading.get_ident()
    hash_threads = []
    original_sha256 = hashlib.sha256

    def guarded_sha256(payload):
        hash_threads.append(threading.get_ident())
        assert threading.get_ident() != loop_thread
        return original_sha256(payload)

    monkeypatch.setattr(native_revision_service.hashlib, "sha256", guarded_sha256)

    snapshot = await service._snapshot_from_row(row)

    assert snapshot.payload_bytes == body
    assert snapshot.text == "worker payload"
    assert hash_threads
