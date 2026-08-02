"""Guarded, File-shaped storage-neutral facade for the M1 W4 measurement.

This is intentionally a very small public API surface: it mirrors the File
route sequence (initiate -> client transfer -> confirm -> download -> delete)
without making the measurement driver a second production storage path.  It is
constructed only after the caller has checked the dedicated M1 measurement
guard.  The opaque transfer capability is signed with a process-local key and
is never written to a database or receipt.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit

from app.exceptions import AKBError, NotFoundError, ValidationError
from app.services.m1_binary_store import BinaryStore, PreparedBinary
from app.services.resource_hash import is_sha256_hex


_TRANSFER_TTL = 3600


@dataclass(slots=True)
class _File:
    file_id: str
    vault: str
    filename: str
    mime_type: str
    declared_digest: str | None
    transfer: bytes | None = None
    prepared: PreparedBinary | None = None


@dataclass(slots=True)
class _Capability:
    file_id: str
    operation: str
    expires_at: int
    used: bool = False


class MeasurementFileFacade:
    """A process-local public File facade used solely by the W4 adapter.

    The facade intentionally has no persistence.  A process restart therefore
    makes unfinished uploads non-visible, which is the safe outcome for a
    measurement run.  Confirmed bytes are immutable CAS objects; logical
    visibility is changed only after ``prepare_verified`` succeeds.
    """

    def __init__(
        self,
        store: BinaryStore,
        *,
        base_url: str,
        transfer_path: str = "/files/transfer",
        now: Callable[[], float] = time.time,
        capability_secret: bytes | None = None,
    ) -> None:
        if not base_url.strip():
            raise ValidationError("measurement File base URL is required")
        self.store = store
        self.base_url = base_url.rstrip("/")
        self.transfer_path = "/" + transfer_path.strip("/")
        self._now = now
        self._secret = capability_secret or secrets.token_bytes(32)
        self._files: dict[str, _File] = {}
        self._claims: dict[tuple[str, str, str], str] = {}
        self._capabilities: dict[str, _Capability] = {}

    @property
    def pending_count(self) -> int:
        return sum(1 for item in self._files.values() if item.prepared is None)

    @property
    def driver(self) -> str:
        return "s3cas" if self.store.driver == "s3" else self.store.driver

    def _token(self, file_id: str, operation: str) -> str:
        expires_at = int(self._now()) + _TRANSFER_TTL
        nonce = secrets.token_urlsafe(16)
        payload = f"v1.{file_id}.{operation}.{expires_at}.{nonce}"
        signature = hmac.new(self._secret, payload.encode(), hashlib.sha256).digest()
        token = payload + "." + base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
        self._capabilities[token] = _Capability(file_id, operation, expires_at)
        return token

    def _url(self, token: str) -> str:
        return f"{self.base_url}{self.transfer_path}/{token}"

    def _consume(self, url: str, method: str) -> _Capability:
        token = urlsplit(url).path.rsplit("/", 1)[-1]
        capability = self._capabilities.get(token)
        if capability is None:
            raise AKBError("invalid measurement transfer capability", status_code=403)
        pieces = token.rsplit(".", 1)
        try:
            signature = base64.urlsafe_b64decode(pieces[1] + "=")
        except (IndexError, ValueError) as exc:
            raise AKBError("invalid measurement transfer capability", status_code=403) from exc
        expected = hmac.new(self._secret, pieces[0].encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise AKBError("invalid measurement transfer capability", status_code=403)
        if int(self._now()) > capability.expires_at:
            raise AKBError("measurement transfer capability expired", status_code=403)
        if capability.operation != method.lower():
            raise AKBError("measurement transfer capability method is not allowed", status_code=405)
        if capability.used:
            raise AKBError("measurement transfer capability already used", status_code=409)
        capability.used = True
        return capability

    def initiate_upload(
        self,
        *,
        vault: str,
        filename: str,
        mime_type: str = "application/octet-stream",
        content_hash: str | None = None,
    ) -> dict:
        if content_hash is not None and not is_sha256_hex(content_hash):
            raise AKBError("content_hash must be a lowercase sha256 hex digest", status_code=400)
        claim = (vault, filename, content_hash) if content_hash else None
        if claim is not None and claim in self._claims:
            file_id = self._claims[claim]
            item = self._files.get(file_id)
            if item is not None:
                return {
                    "kind": "file", "file_id": file_id, "vault": vault,
                    "upload_url": self._url(self._token(file_id, "put")),
                    "expires_in": _TRANSFER_TTL, "deduplicated": True,
                }
        file_id = str(uuid.uuid4())
        item = _File(file_id, vault, filename, mime_type, content_hash)
        self._files[file_id] = item
        if claim is not None:
            self._claims[claim] = file_id
        return {
            "kind": "file", "file_id": file_id, "vault": vault,
            "upload_url": self._url(self._token(file_id, "put")),
            "expires_in": _TRANSFER_TTL, "deduplicated": False,
        }

    def transfer(self, url: str, body: bytes | None = None, *, method: str | None = None) -> bytes | None:
        selected_method = method or ("PUT" if body is not None else "GET")
        capability = self._consume(url, selected_method)
        item = self._files.get(capability.file_id)
        if item is None:
            raise NotFoundError("File", capability.file_id)
        if selected_method.lower() == "put":
            assert body is not None
            if item.prepared is not None:
                raise AKBError("file is already confirmed", status_code=409)
            item.transfer = bytes(body)
            return None
        if item.prepared is None:
            raise NotFoundError("File", capability.file_id)
        return self.store.open_verified(item.vault, item.prepared)

    def confirm_upload(self, file_id: str, *, content_hash: str | None = None) -> dict:
        item = self._files.get(file_id)
        if item is None:
            raise NotFoundError("File", file_id)
        if content_hash is not None and not is_sha256_hex(content_hash):
            raise AKBError("content_hash must be a lowercase sha256 hex digest", status_code=400)
        if item.prepared is not None:
            return self._confirmed(item)
        if item.transfer is None:
            self._discard(item)
            raise AKBError("Upload not found in storage — file record cleaned up", status_code=404)
        actual = hashlib.sha256(item.transfer).hexdigest()
        if (item.declared_digest and item.declared_digest != actual) or (
            content_hash is not None and content_hash != actual
        ):
            self._discard(item)
            raise AKBError("Uploaded file hash mismatch; file record was cleaned up.", status_code=409)
        try:
            item.prepared = self.store.prepare_verified(
                item.vault, item.transfer, actual, len(item.transfer),
            )
        except Exception:
            # Failed storage publication must never make a logical File visible.
            self._discard(item)
            raise
        item.transfer = None
        return self._confirmed(item)

    def _confirmed(self, item: _File) -> dict:
        assert item.prepared is not None
        revision_id = hashlib.sha1(
            f"{item.file_id}:{item.prepared.digest}".encode(), usedforsecurity=False,
        ).hexdigest()
        return {
            "kind": "file", "file_id": item.file_id, "vault": item.vault,
            "name": item.filename, "mime_type": item.mime_type,
            "size_bytes": item.prepared.size, "content_hash": item.prepared.digest,
            "hash_algorithm": "sha256", "storage_driver": self.driver,
            "revision_id": revision_id,
        }

    def get_download_url(self, file_id: str) -> dict:
        item = self._files.get(file_id)
        if item is None or item.prepared is None:
            raise NotFoundError("File", file_id)
        return {
            "kind": "file", "name": item.filename, "mime_type": item.mime_type,
            "size_bytes": item.prepared.size, "content_hash": item.prepared.digest,
            "hash_algorithm": "sha256", "download_url": self._url(self._token(file_id, "get")),
            "expires_in": _TRANSFER_TTL,
        }

    def open(self, file_id: str) -> bytes:
        item = self._files.get(file_id)
        if item is None or item.prepared is None:
            raise NotFoundError("File", file_id)
        return self.store.open_verified(item.vault, item.prepared)

    def delete(self, file_id: str) -> dict:
        item = self._files.get(file_id)
        if item is None:
            raise NotFoundError("File", file_id)
        # CAS may be shared by same-digest logical retries.  Retire physical
        # bytes only after this is the final logical reference in this run.
        prepared = item.prepared
        self._discard(item)
        if prepared is not None and not any(
            other.prepared == prepared for other in self._files.values()
        ):
            self.store.delete_verified(item.vault, prepared)
        return {"kind": "file", "file_id": file_id, "vault": item.vault, "name": item.filename, "deleted": True}

    def _discard(self, item: _File) -> None:
        self._files.pop(item.file_id, None)
        if item.declared_digest:
            self._claims.pop((item.vault, item.filename, item.declared_digest), None)


def create_guarded_measurement_file_facade() -> MeasurementFileFacade | None:
    """Return the one selected measurement driver, or ``None`` for normal S3.

    Settings validation repeats the guard at config-load time; this local
    assertion prevents a test/runtime monkeypatch from accidentally activating
    a CAS driver outside the exact M1 database.
    """
    from pathlib import Path

    from app.config import NATIVE_REVISION_M1_MEASUREMENT_DATABASE_NAME, settings
    from app.services.adapters import s3_adapter
    from app.services.m1_binary_store import FilesystemCAS, S3CAS

    driver = settings.native_revision_m1_file_driver
    if driver == "s3_current":
        return None
    if not settings.native_revision_m1_measurement_only or settings.db_name != NATIVE_REVISION_M1_MEASUREMENT_DATABASE_NAME:
        raise RuntimeError("M1 File CAS requires the dedicated measurement guard")
    if not settings.public_base_url:
        raise RuntimeError("M1 File CAS requires public_base_url for proxy-compatible transfer URLs")
    if driver == "fscas":
        store: BinaryStore = FilesystemCAS(Path(settings.native_revision_m1_file_fscas_root))
    elif driver == "s3cas":
        store = S3CAS(settings.s3_bucket, s3_adapter.client())
    else:  # Literal plus this branch keeps fail-closed behavior under monkeypatches.
        raise RuntimeError("M1 File CAS driver must be fscas or s3cas")
    return MeasurementFileFacade(
        store, base_url=settings.public_base_url, transfer_path="/api/v1/files/transfer",
    )
