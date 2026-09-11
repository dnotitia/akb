"""Malformed file ids are client errors at the download boundary."""

from __future__ import annotations

import uuid

import pytest

from app.exceptions import ValidationError
from app.services import file_service as fs
from app.services import m1_file_measurement as m1


@pytest.mark.asyncio
async def test_direct_s3_download_rejects_malformed_file_id_before_database_access(
    monkeypatch,
):
    monkeypatch.setattr(fs, "measurement_enabled", lambda: False)
    service = fs.FileService()

    async def unexpected_pool():
        raise AssertionError("malformed file ids must be rejected before database access")

    monkeypatch.setattr(fs, "get_pool", unexpected_pool)

    with pytest.raises(ValidationError, match="file_id must be a UUID") as error:
        await service.get_download_url(uuid.uuid4(), "not-a-uuid")

    assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_measurement_download_rejects_malformed_file_id_before_database_access(
    monkeypatch,
):
    monkeypatch.setattr(m1, "measurement_enabled", lambda: True)
    monkeypatch.setattr(m1.settings, "public_base_url", "https://akb.test")
    service = m1.MeasurementFileService()

    async def unexpected_pool():
        raise AssertionError("malformed file ids must be rejected before database access")

    monkeypatch.setattr(m1, "get_pool", unexpected_pool)

    with pytest.raises(ValidationError, match="file_id must be a UUID") as error:
        await service.get_download_url(uuid.uuid4(), "not-a-uuid")

    assert error.value.status_code == 422
