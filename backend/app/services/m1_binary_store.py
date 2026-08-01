"""Measurement-only BinaryStore comparators for M1 C8/W4.

They are deliberately not wired to the public File service.  A run selects one
driver; it never falls back or dual-writes.  A prepared CAS object is private
until the caller's logical publication callback succeeds.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from app.exceptions import ValidationError


class BinaryStoreError(RuntimeError): pass


@dataclass(frozen=True, slots=True)
class PreparedBinary:
    locator: str
    digest: str
    size: int
    driver: str


def _verify(data: bytes, digest: str | None, size: int | None) -> tuple[str, int]:
    actual = hashlib.sha256(data).hexdigest()
    if digest is not None and digest != actual:
        raise ValidationError("binary digest does not match transferred bytes")
    if size is not None and size != len(data):
        raise ValidationError("binary size does not match transferred bytes")
    return actual, len(data)


class BinaryStore(Protocol):
    def prepare_verified(self, tenant: str, data: bytes, expected_digest: str | None, expected_size: int | None) -> PreparedBinary: ...
    def open_verified(self, tenant: str, prepared: PreparedBinary) -> bytes: ...
    def delete(self, tenant: str, prepared: PreparedBinary) -> None: ...


class FilesystemCAS:
    """Content-addressed immutable bytes under a measurement-owned root."""
    driver = "fscas"
    def __init__(self, root: Path): self.root = root.resolve()
    def _path(self, tenant: str, digest: str) -> Path:
        if not tenant or "/" in tenant or ".." in tenant: raise ValidationError("invalid measurement tenant")
        return self.root / tenant / "sha256" / digest[:2] / digest
    def prepare_verified(self, tenant: str, data: bytes, expected_digest: str | None = None, expected_size: int | None = None) -> PreparedBinary:
        digest, size = _verify(data, expected_digest, expected_size); path = self._path(tenant, digest); path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            existing = path.read_bytes(); _verify(existing, digest, size)
        else:
            with os.fdopen(fd, "wb") as out: out.write(data); out.flush(); os.fsync(out.fileno())
        return PreparedBinary(str(path.relative_to(self.root)), digest, size, self.driver)
    def open_verified(self, tenant: str, prepared: PreparedBinary) -> bytes:
        if prepared.driver != self.driver: raise BinaryStoreError("wrong BinaryStore driver")
        data: bytes = (self.root / prepared.locator).read_bytes(); _verify(data, prepared.digest, prepared.size); return data
    def delete(self, tenant: str, prepared: PreparedBinary) -> None:
        # append-only M1 profile: logical delete makes it non-visible; physical
        # GC is intentionally outside this measurement contract.
        return None


class S3CAS:
    """Current S3 adapter comparator, using conditional content-addressed put."""
    driver = "s3"
    def __init__(self, bucket: str, client): self.bucket, self.client = bucket, client
    def _key(self, tenant: str, digest: str) -> str:
        if not tenant or "/" in tenant or ".." in tenant: raise ValidationError("invalid measurement tenant")
        return f"m1-binary/{tenant}/sha256/{digest[:2]}/{digest}"
    def prepare_verified(self, tenant: str, data: bytes, expected_digest: str | None = None, expected_size: int | None = None) -> PreparedBinary:
        digest, size = _verify(data, expected_digest, expected_size); key = self._key(tenant, digest)
        try: self.client.put_object(Bucket=self.bucket, Key=key, Body=data, IfNoneMatch="*")
        except Exception: pass  # existing object is adopted only after revalidation below
        try: got = self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except Exception as exc: raise BinaryStoreError("S3 CAS prepare/open failed") from exc
        _verify(got, digest, size); return PreparedBinary(key, digest, size, self.driver)
    def open_verified(self, tenant: str, prepared: PreparedBinary) -> bytes:
        if prepared.driver != self.driver: raise BinaryStoreError("wrong BinaryStore driver")
        try: data = self.client.get_object(Bucket=self.bucket, Key=prepared.locator)["Body"].read()
        except Exception as exc: raise BinaryStoreError("S3 CAS object is missing") from exc
        _verify(data, prepared.digest, prepared.size); return data
    def delete(self, tenant: str, prepared: PreparedBinary) -> None: return None


class MeasurementUpload:
    """Initiate/transfer/confirm state: bytes are not visible before confirm."""
    def __init__(self, store: BinaryStore, tenant: str):
        self.store, self.tenant = store, tenant
        self._bytes: bytes | None = None
    def transfer(self, data: bytes) -> None: self._bytes = data
    def confirm(self, expected_digest: str | None, expected_size: int | None, publish: Callable[[PreparedBinary], None]) -> tuple[str, PreparedBinary | None]:
        if self._bytes is None: return "unconfirmed_not_visible", None
        prepared = self.store.prepare_verified(self.tenant, self._bytes, expected_digest, expected_size)
        try: publish(prepared)
        except Exception: return "failed_db_publish_unconfirmed", None
        return "confirmed", prepared
