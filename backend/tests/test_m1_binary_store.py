from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from app.exceptions import ValidationError
from app.services.m1_binary_store import (
    BinaryStoreError,
    FilesystemCAS,
    PreparedBinary,
    S3CAS,
)


def test_fscas_adopts_exact_bytes_and_never_overwrites(tmp_path: Path):
    store = FilesystemCAS(tmp_path)
    data = b"binary\x00bytes"
    digest = hashlib.sha256(data).hexdigest()

    first = store.prepare_verified("tenant", data, digest, len(data))
    second = store.prepare_verified("tenant", data, digest, len(data))

    assert first == second
    assert store.open_verified("tenant", first) == data
    assert store.stat_verified("tenant", first) == len(data)


def test_fscas_rejects_digest_mismatch_and_locator_escape(tmp_path: Path):
    store = FilesystemCAS(tmp_path)
    with pytest.raises(ValidationError, match="digest"):
        store.prepare_verified("tenant", b"x", "0" * 64, 1)

    escaped = PreparedBinary("../outside", hashlib.sha256(b"x").hexdigest(), 1, "fscas")
    with pytest.raises(BinaryStoreError, match="locator"):
        store.open_verified("tenant", escaped)


def test_fscas_failed_atomic_publication_removes_private_temp(tmp_path: Path, monkeypatch):
    store = FilesystemCAS(tmp_path)

    def fail_link(_source, _destination):
        raise OSError("injected link failure")

    monkeypatch.setattr("app.services.m1_binary_store.os.link", fail_link)
    with pytest.raises(OSError, match="injected link failure"):
        store.prepare_verified("tenant", b"exact")

    assert not list(tmp_path.rglob("*.tmp"))
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


@pytest.mark.parametrize("tenant", ["", "../escape", "a/b", ".hidden"])
def test_fscas_rejects_invalid_tenant(tmp_path: Path, tenant: str):
    with pytest.raises(ValidationError, match="tenant"):
        FilesystemCAS(tmp_path).prepare_verified(tenant, b"x")


class _S3:
    def __init__(self):
        self.objects: dict[tuple[str, str], bytes] = {}
        self.put_error: ClientError | None = None

    def put_object(self, *, Bucket, Key, Body, IfNoneMatch):
        assert IfNoneMatch == "*"
        if self.put_error is not None:
            raise self.put_error
        identity = (Bucket, Key)
        if identity in self.objects:
            raise ClientError(
                {"Error": {"Code": "PreconditionFailed"}, "ResponseMetadata": {"HTTPStatusCode": 412}},
                "PutObject",
            )
        self.objects[identity] = Body

    def get_object(self, *, Bucket, Key):
        return {"Body": BytesIO(self.objects[(Bucket, Key)])}

    def head_object(self, *, Bucket, Key):
        return {"ContentLength": len(self.objects[(Bucket, Key)])}


def test_s3_cas_adopts_only_a_reverified_existing_object():
    client = _S3()
    store = S3CAS("bucket", client)
    data = b"exact"
    digest = hashlib.sha256(data).hexdigest()

    first = store.prepare_verified("tenant", data, digest, len(data))
    second = store.prepare_verified("tenant", data, digest, len(data))

    assert first == second
    assert store.open_verified("tenant", first) == data
    assert store.stat_verified("tenant", first) == len(data)


def test_s3_cas_does_not_hide_auth_or_network_class_errors():
    client = _S3()
    client.put_error = ClientError(
        {"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": 403}},
        "PutObject",
    )
    with pytest.raises(BinaryStoreError, match="conditional publish"):
        S3CAS("bucket", client).prepare_verified("tenant", b"x")
