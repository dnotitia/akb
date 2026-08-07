"""Worker-lifecycle coverage for guarded native File invalidation intents."""

from __future__ import annotations

import pytest

from app.config import settings
from app.services import embed_worker, native_derived_worker


class _Transaction:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *_args):
        return None


class _Connection:
    def transaction(self):
        return _Transaction()


class _Acquire:
    async def __aenter__(self):
        return _Connection()

    async def __aexit__(self, *_args):
        return None


class _Pool:
    def acquire(self):
        return _Acquire()


@pytest.mark.asyncio
async def test_file_measurement_drains_native_intents_with_legacy_document_backend(
    monkeypatch,
):
    pool = _Pool()
    calls: list[object] = []
    processed = iter((1, 1, 0))

    async def get_pool():
        return pool

    async def claim_no_chunks(_conn):
        return []

    class ObservedNativeDerivedWorker:
        def __init__(self, received_pool):
            assert received_pool is pool

        async def process_once(self):
            calls.append(pool)
            return next(processed)

    monkeypatch.setattr(embed_worker, "get_pool", get_pool)
    monkeypatch.setattr(embed_worker, "_claim_batch", claim_no_chunks)
    monkeypatch.setattr(native_derived_worker, "NativeDerivedWorker", ObservedNativeDerivedWorker)
    monkeypatch.setattr(settings, "document_revision_backend", "bare_git_current")
    monkeypatch.setattr(settings, "native_revision_m1_measurement_only", True)
    monkeypatch.setattr(settings, "native_revision_m1_file_driver", "fscas")
    monkeypatch.setattr(settings, "db_name", "akb_revision_m1_measurement")

    # Positive native-only work must keep BackfillRunner hot even when no
    # chunks were materialized in this pass (delete, superseded, and abandoned
    # intents all report work done without adding a row to the embed queue).
    assert await embed_worker._process_once() == 1
    assert await embed_worker._process_once() == 1
    assert await embed_worker._process_once() == 0
    assert calls == [pool, pool, pool]
