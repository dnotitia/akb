"""The staging inventory's storage boundary and worker failure isolation."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from botocore.exceptions import ClientError

from app.exceptions import AKBError
from app.services import s3_delete_worker as worker
from app.services.adapters import s3_adapter


def test_inventory_lists_only_one_bounded_prefix_page(monkeypatch):
    client = MagicMock()
    rows = [{"Key": worker.REPLACEMENT_STAGING_PREFIX + "object"}]
    client.list_objects_v2.return_value = {"Contents": rows, "NextContinuationToken": "next"}
    monkeypatch.setattr(s3_adapter, "client", lambda: client)
    result = s3_adapter.list_objects_page(
        worker.REPLACEMENT_STAGING_PREFIX, continuation_token="previous", limit=100,
    )
    assert result == (rows, "next")
    client.list_objects_v2.assert_called_once_with(
        Bucket=s3_adapter.settings.s3_bucket, Prefix=worker.REPLACEMENT_STAGING_PREFIX,
        MaxKeys=100, ContinuationToken="previous",
    )
    client.list_objects_v2.return_value = {}
    assert s3_adapter.list_objects_page(worker.REPLACEMENT_STAGING_PREFIX) == ([], None)


def test_listing_denial_is_not_an_empty_success(monkeypatch):
    client = MagicMock()
    client.list_objects_v2.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "listing denied"}}, "ListObjectsV2",
    )
    monkeypatch.setattr(s3_adapter, "client", lambda: client)
    with pytest.raises(AKBError) as error:
        s3_adapter.list_objects_page(worker.REPLACEMENT_STAGING_PREFIX)
    assert error.value.status_code == 502


async def test_inventory_failure_does_not_stop_existing_delete_work(monkeypatch, caplog):
    monkeypatch.setattr(worker, "_reconcile_staging_once", AsyncMock(side_effect=RuntimeError("listing denied")))
    deletes = AsyncMock(return_value=1)
    sweep = AsyncMock()
    monkeypatch.setattr(worker, "_process_deletes_once", deletes)
    monkeypatch.setattr(worker, "_sweep_outbox_once", sweep)
    assert await worker._process_once() == 1
    deletes.assert_awaited_once()
    sweep.assert_awaited_once()
    assert "staging inventory failed" in caplog.text
