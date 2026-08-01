"""Measurement-only BinaryStore comparators for M1 C8/W4.

The comparators are intentionally not wired into the public File service.  A
measurement run selects exactly one driver; there is no fallback or dual write.
CAS bytes may exist before logical publication, but no caller can open them
through a logical File until the publication transaction succeeds.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from botocore.exceptions import ClientError

from app.exceptions import ValidationError


_TENANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class BinaryStoreError(RuntimeError):
    """A selected BinaryStore cannot safely fulfil the requested operation."""


@dataclass(frozen=True, slots=True)
class PreparedBinary:
    locator: str
    digest: str
    size: int
    driver: str


def _validate_tenant(tenant: str) -> None:
    if not _TENANT_RE.fullmatch(tenant):
        raise ValidationError("invalid measurement tenant")


def _verify(data: bytes, digest: str | None, size: int | None) -> tuple[str, int]:
    actual = hashlib.sha256(data).hexdigest()
    if digest is not None and digest != actual:
        raise ValidationError("binary digest does not match transferred bytes")
    if size is not None and size != len(data):
        raise ValidationError("binary size does not match transferred bytes")
    return actual, len(data)


class BinaryStore(Protocol):
    driver: str

    def prepare_verified(
        self,
        tenant: str,
        data: bytes,
        expected_digest: str | None = None,
        expected_size: int | None = None,
    ) -> PreparedBinary: ...

    def open_verified(self, tenant: str, prepared: PreparedBinary) -> bytes: ...

    def stat_verified(self, tenant: str, prepared: PreparedBinary) -> int: ...


class FilesystemCAS:
    """Content-addressed immutable bytes under one measurement-owned root."""

    driver = "fscas"

    def __init__(self, root: Path):
        self.root = root.resolve()
        if not self.root.is_dir():
            raise ValidationError("FilesystemCAS root must be a pre-provisioned directory")

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _durable_mkdirs(self, directory: Path) -> None:
        """Create and durably publish each directory below the storage root."""
        relative = directory.relative_to(self.root)
        current = self.root
        for component in relative.parts:
            parent = current
            current = current / component
            try:
                os.mkdir(current, 0o700)
            except FileExistsError:
                if not current.is_dir():
                    raise BinaryStoreError("FilesystemCAS hierarchy contains a non-directory")
            else:
                self._fsync_directory(current)
                self._fsync_directory(parent)

    def _path(self, tenant: str, digest: str) -> Path:
        _validate_tenant(tenant)
        if not _DIGEST_RE.fullmatch(digest):
            raise ValidationError("invalid sha256 digest")
        return self.root / tenant / "sha256" / digest[:2] / digest

    def _validated_path(self, tenant: str, prepared: PreparedBinary) -> Path:
        if prepared.driver != self.driver:
            raise BinaryStoreError("wrong BinaryStore driver")
        expected = self._path(tenant, prepared.digest)
        try:
            supplied = (self.root / prepared.locator).resolve(strict=False)
            supplied.relative_to(self.root)
        except (OSError, ValueError) as exc:
            raise BinaryStoreError("FilesystemCAS locator escapes the selected root") from exc
        if supplied != expected:
            raise BinaryStoreError("FilesystemCAS locator does not match tenant and digest")
        return expected

    def prepare_verified(
        self,
        tenant: str,
        data: bytes,
        expected_digest: str | None = None,
        expected_size: int | None = None,
    ) -> PreparedBinary:
        digest, size = _verify(data, expected_digest, expected_size)
        path = self._path(tenant, digest)
        self._durable_mkdirs(path.parent)
        temporary = path.parent / f".{digest}.{uuid.uuid4().hex}.tmp"
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                _verify(temporary.read_bytes(), digest, size)
                try:
                    # link(2) is an atomic no-replace publication on the same
                    # filesystem. A crash before it leaves only a private temp;
                    # a crash after it leaves a fully fsynced immutable object.
                    os.link(temporary, path)
                except FileExistsError:
                    _verify(path.read_bytes(), digest, size)
            except Exception:
                raise
            finally:
                temporary.unlink(missing_ok=True)
            self._fsync_directory(path.parent)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return PreparedBinary(str(path.relative_to(self.root)), digest, size, self.driver)

    def open_verified(self, tenant: str, prepared: PreparedBinary) -> bytes:
        path = self._validated_path(tenant, prepared)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise BinaryStoreError("FilesystemCAS object is missing") from exc
        _verify(data, prepared.digest, prepared.size)
        return data

    def stat_verified(self, tenant: str, prepared: PreparedBinary) -> int:
        path = self._validated_path(tenant, prepared)
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise BinaryStoreError("FilesystemCAS object is missing") from exc
        if size != prepared.size:
            raise BinaryStoreError("FilesystemCAS object size mismatch")
        return size


class S3CAS:
    """S3 content-addressed comparator with conditional immutable creation."""

    driver = "s3"

    def __init__(self, bucket: str, client):
        if not bucket:
            raise ValidationError("measurement S3 bucket is required")
        self.bucket = bucket
        self.client = client

    def _key(self, tenant: str, digest: str) -> str:
        _validate_tenant(tenant)
        if not _DIGEST_RE.fullmatch(digest):
            raise ValidationError("invalid sha256 digest")
        return f"m1-binary/{tenant}/sha256/{digest[:2]}/{digest}"

    def _validated_key(self, tenant: str, prepared: PreparedBinary) -> str:
        if prepared.driver != self.driver:
            raise BinaryStoreError("wrong BinaryStore driver")
        expected = self._key(tenant, prepared.digest)
        if prepared.locator != expected:
            raise BinaryStoreError("S3 CAS locator does not match tenant and digest")
        return expected

    @staticmethod
    def _is_existing_object(exc: ClientError) -> bool:
        response = exc.response or {}
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        code = str(response.get("Error", {}).get("Code", ""))
        return status in {409, 412} or code in {
            "409",
            "412",
            "ConditionalRequestConflict",
            "PreconditionFailed",
        }

    @staticmethod
    def _is_missing_object(exc: ClientError) -> bool:
        """Recognize only an explicit S3 HEAD not-found response.

        Cleanup is a safety boundary: an arbitrary client exception (including
        a malformed response) cannot prove that the just-created object is
        absent.  In particular, do not treat ``KeyError`` from a fake/client
        response parser as an S3 NoSuchKey result.
        """
        response = exc.response
        if not isinstance(response, dict):
            return False
        metadata = response.get("ResponseMetadata")
        error = response.get("Error")
        status = metadata.get("HTTPStatusCode") if isinstance(metadata, dict) else None
        code = error.get("Code") if isinstance(error, dict) else None
        return status == 404 or code in {"404", "NoSuchKey", "NotFound"}

    def _remove_created_object(self, key: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
            try:
                self.client.head_object(Bucket=self.bucket, Key=key)
            except ClientError as exc:
                if not self._is_missing_object(exc):
                    raise
            else:
                raise BinaryStoreError("new S3 CAS object remained after cleanup")
        except Exception as exc:
            if isinstance(exc, BinaryStoreError):
                raise
            raise BinaryStoreError("new S3 CAS object cleanup failed") from exc

    def prepare_verified(
        self,
        tenant: str,
        data: bytes,
        expected_digest: str | None = None,
        expected_size: int | None = None,
    ) -> PreparedBinary:
        digest, size = _verify(data, expected_digest, expected_size)
        key = self._key(tenant, digest)
        created = False
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                IfNoneMatch="*",
            )
            created = True
        except ClientError as exc:
            if not self._is_existing_object(exc):
                raise BinaryStoreError("S3 CAS conditional publish failed") from exc
        prepared = PreparedBinary(key, digest, size, self.driver)
        try:
            self.open_verified(tenant, prepared)
        except Exception:
            if created:
                self._remove_created_object(key)
            raise
        return prepared

    def open_verified(self, tenant: str, prepared: PreparedBinary) -> bytes:
        key = self._validated_key(tenant, prepared)
        try:
            data = self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except Exception as exc:
            raise BinaryStoreError("S3 CAS object is missing") from exc
        _verify(data, prepared.digest, prepared.size)
        return data

    def stat_verified(self, tenant: str, prepared: PreparedBinary) -> int:
        key = self._validated_key(tenant, prepared)
        try:
            size = int(self.client.head_object(Bucket=self.bucket, Key=key)["ContentLength"])
        except Exception as exc:
            raise BinaryStoreError("S3 CAS object is missing") from exc
        if size != prepared.size:
            raise BinaryStoreError("S3 CAS object size mismatch")
        return size
