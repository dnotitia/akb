"""Public File-shaped M1 W4 trace over either guarded CAS driver."""

from __future__ import annotations

import hashlib
from io import BytesIO

import pytest
from botocore.exceptions import ClientError

from app.exceptions import AKBError, NotFoundError
from app.services.m1_file_measurement import MeasurementFileFacade
from app.services.m1_binary_store import FilesystemCAS, S3CAS


class _S3:
    def __init__(self):
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_object(self, *, Bucket, Key, Body, IfNoneMatch):
        if (Bucket, Key) in self.objects:
            raise ClientError(
                {"Error": {"Code": "PreconditionFailed"}, "ResponseMetadata": {"HTTPStatusCode": 412}},
                "PutObject",
            )
        self.objects[(Bucket, Key)] = Body

    def get_object(self, *, Bucket, Key):
        try:
            return {"Body": BytesIO(self.objects[(Bucket, Key)])}
        except KeyError as exc:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
                "GetObject",
            ) from exc

    def head_object(self, *, Bucket, Key):
        try:
            return {"ContentLength": len(self.objects[(Bucket, Key)])}
        except KeyError as exc:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
                "HeadObject",
            ) from exc

    def delete_object(self, *, Bucket, Key):
        self.objects.pop((Bucket, Key), None)


@pytest.mark.parametrize("driver", ["fscas", "s3cas"])
def test_public_file_trace_is_identical_for_both_measurement_drivers(tmp_path, driver):
    data = b"the actual public File trace\x00"
    digest = hashlib.sha256(data).hexdigest()
    store = FilesystemCAS(tmp_path) if driver == "fscas" else S3CAS("measurement", _S3())
    files = MeasurementFileFacade(store, base_url="http://akb.test")

    initiated = files.initiate_upload(
        vault="run-owned-vault", filename="fixture.bin", mime_type="application/octet-stream",
        content_hash=digest,
    )
    assert initiated["upload_url"].startswith("http://akb.test/files/transfer/")
    assert "token" not in initiated
    file_id = initiated["file_id"]

    files.transfer(initiated["upload_url"], data)
    confirmed = files.confirm_upload(file_id, content_hash=digest)
    assert confirmed["content_hash"] == digest
    assert confirmed["size_bytes"] == len(data)

    downloaded = files.get_download_url(file_id)
    assert files.transfer(downloaded["download_url"]) == data
    assert files.open(file_id) == data

    retry = files.initiate_upload(
        vault="run-owned-vault", filename="fixture.bin", mime_type="application/octet-stream",
        content_hash=digest,
    )
    assert retry["file_id"] == file_id
    assert retry["deduplicated"] is True

    assert files.delete(file_id)["deleted"] is True
    with pytest.raises(NotFoundError):
        files.open(file_id)


def test_public_trace_refuses_interrupted_or_mismatched_transfers(tmp_path):
    files = MeasurementFileFacade(FilesystemCAS(tmp_path), base_url="http://akb.test")
    initiated = files.initiate_upload(vault="run-owned-vault", filename="fixture.bin")

    with pytest.raises(AKBError, match="not found in storage"):
        files.confirm_upload(initiated["file_id"])
    assert files.pending_count == 0

    initiated = files.initiate_upload(vault="run-owned-vault", filename="fixture.bin")
    files.transfer(initiated["upload_url"], b"short")
    with pytest.raises(AKBError, match="hash mismatch"):
        files.confirm_upload(initiated["file_id"], content_hash="0" * 64)
    assert files.pending_count == 0


def test_transfer_tokens_are_scoped_expiring_and_not_reusable(tmp_path):
    files = MeasurementFileFacade(FilesystemCAS(tmp_path), base_url="http://akb.test", now=lambda: 100)
    initiated = files.initiate_upload(vault="run-owned-vault", filename="fixture.bin")
    with pytest.raises(AKBError, match="method"):
        files.transfer(initiated["upload_url"], method="GET")
    files.transfer(initiated["upload_url"], b"ok")
    with pytest.raises(AKBError, match="already used"):
        files.transfer(initiated["upload_url"], b"again")
