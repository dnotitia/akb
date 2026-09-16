"""File service — S3-backed binary file storage for vaults.

AKB does not store file bytes in the application database. It:
1. Issues capability URLs — this service plus an opaque token — that a client
   PUTs to or GETs from with no Authorization header, the way it used to use a
   presigned URL. Bytes stream through this service; the object store's
   endpoint, bucket and keys stay private to it.
2. Manages file metadata in PostgreSQL (`vault_files_repo`).
3. Streams uploaded object bytes once at confirmation to certify sha256.

Access control inherits from vault permissions.

S3 client lifecycle and low-level primitives (head/get/put/delete,
presigning, error mapping) live in `app.services.adapters.s3_adapter`.
This module is the file-domain layer over those primitives.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
import secrets
import uuid
from typing import Any, AsyncIterator, Iterator
from urllib.parse import quote

import anyio

from app.config import settings
from app.db.postgres import get_pool
from app.exceptions import AKBError, ConflictError, NotFoundError, ValidationError
from app.repositories import vault_files_repo
from app.repositories.document_repo import CollectionRepository
from app.repositories.events_repo import emit_event
from app.repositories.vault_repo import lock_vault_for_child_write
from app.services.adapters import s3_adapter
from app.services.index_service import (
    build_file_chunk, delete_file_chunks, write_source_chunks,
)
from app.services.resource_hash import (
    HASH_ALGORITHM,
    compute_stream_content_hash,
    is_sha256_hex,
)
from app.services.s3_delete_worker import cancel_delete as _cancel_s3_delete
from app.services.s3_delete_worker import enqueue_delete as _enqueue_s3_delete
from app.services import skill_policy
from app.services.uri_service import file_uri
from app.services.m1_file_measurement import MeasurementFileService, measurement_enabled
from app.services.native_file_projection import (
    enqueue_native_file_projection,
    enqueue_native_file_projection_delete,
)

# Re-export so existing callers (publication_service, public routes)
# don't break. New code should import directly from s3_adapter.
from app.services.adapters.s3_adapter import StorageError  # noqa: F401

logger = logging.getLogger("akb.files")

_PRESIGN_UPLOAD_TTL = 3600
_PRESIGN_DOWNLOAD_TTL = 3600
_S3_STREAM_CHUNK_SIZE = 64 * 1024
_REPLACEMENT_STAGING_DELETE_DELAY = _PRESIGN_UPLOAD_TTL + 300
_REPLACEMENT_CANDIDATE_DELETE_DELAY = 24 * 60 * 60

_DELEGATED_ACTOR_EVENT_KEYS = (
    "delegated_user_id",
    "service_user_id",
    "service_token_id",
)


def _delegated_actor_event_fields(
    delegated_actor: dict[str, str] | None,
) -> dict[str, str]:
    """Return the exact non-secret delegation fields allowed in file events."""
    if delegated_actor is None:
        return {}
    return {
        key: delegated_actor[key]
        for key in _DELEGATED_ACTOR_EVENT_KEYS
    }


# ── HTTP header helper (kept here — not S3-specific) ─────────────


def content_disposition_attachment(filename: str) -> str:
    """Build a safe RFC 5987 Content-Disposition: attachment header value.

    The non-ASCII ``filename*=UTF-8''...`` form is what modern browsers honor;
    the ASCII ``filename=...`` is a fallback. CR/LF/quote chars are stripped
    from the ASCII part to prevent header injection when ``filename`` is
    user-controlled.
    """
    ascii_safe = (
        filename.encode("ascii", "replace")
        .decode("ascii")
        .translate({ord(c): None for c in '"\r\n'})
    )
    utf8_encoded = quote(filename, safe="")
    return f'attachment; filename="{ascii_safe}"; filename*=UTF-8\'\'{utf8_encoded}'


def content_disposition_inline(filename: str) -> str:
    """Same escaping, but naming the file without forcing a save.

    A preview needs the browser to render the bytes; it still wants the real
    name for the save dialog a reader may reach for afterwards."""
    ascii_safe = (
        filename.encode("ascii", "replace")
        .decode("ascii")
        .translate({ord(c): None for c in '"\r\n'})
    )
    utf8_encoded = quote(filename, safe="")
    return f'inline; filename="{ascii_safe}"; filename*=UTF-8\'\'{utf8_encoded}'


# ── Top-level S3 helpers (thin wrappers around s3_adapter) ───────


def get_object_bytes(s3_key: str, max_bytes: int | None = None) -> bytes:
    """Read the whole object into memory. When ``max_bytes`` is given, abort with
    StorageError if the ACTUAL object exceeds it. Callers (e.g. /raw) pre-check a
    DB ``size_bytes`` gate, but that recorded size can drift from the stored
    object — the presigned PUT stays usable after confirm — so we also bound the
    READ itself and never buffer an over-cap body into the single process."""
    if max_bytes is None:
        return s3_adapter.get_bytes(s3_key)
    buf = bytearray()
    gen = iter_object_chunks(s3_key, max_bytes=max_bytes)
    try:
        for chunk in gen:
            buf.extend(chunk)
    finally:
        # release the boto stream promptly on cap-abort or exhaustion; the
        # concrete iterator is a generator, but the annotation is Iterator[bytes]
        close = getattr(gen, "close", None)
        if callable(close):
            close()
    return bytes(buf)


def head_object(s3_key: str) -> dict:
    """Cheap existence/metadata probe (S3 HEAD, no body). Raises StorageError/
    AKBError if the object is missing or unreadable. Callers streaming a large
    object HEAD it first (off the event loop) so a missing object is a clean 502
    before the response is committed, without buffering the whole body."""
    return s3_adapter.head(s3_key)


def iter_object_chunks(
    s3_key: str,
    chunk_size: int = _S3_STREAM_CHUNK_SIZE,
    *,
    max_bytes: int | None = None,
    byte_range: str | None = None,
) -> Iterator[bytes]:
    """Stream an object with an optional hard bound on transferred bytes."""
    transferred = 0
    gen = s3_adapter.iter_chunks(
        s3_key, chunk_size=chunk_size, byte_range=byte_range,
    )
    try:
        for chunk in gen:
            transferred += len(chunk)
            if max_bytes is not None and transferred > max_bytes:
                raise StorageError(f"object {s3_key} exceeds {max_bytes} bytes")
            yield chunk
    finally:
        close = getattr(gen, "close", None)
        if callable(close):
            close()


def put_object_bytes(
    s3_key: str, body: bytes, content_type: str = "application/octet-stream",
) -> None:
    s3_adapter.put_bytes(s3_key, body, content_type=content_type)


# ── File-key naming convention ───────────────────────────────────


# A key's leading path component is `{prefix}_{safe_filename}`. Two prefix
# flavours exist and are told apart by `_CONTENT_KEY_MARKER`:
#
#   `{8 random hex}_name`        — opaque. The historical default, still used
#                                  whenever the caller cannot state the bytes
#                                  up front. Never collides, so re-uploading
#                                  the same artifact always makes a new row.
#   `sha256-{16 hex}_name`       — content-addressed. The digest is the leading
#                                  16 hex chars of the sha256 of the object's
#                                  bytes, so the key is a function of *what the
#                                  object is* rather than of when it was
#                                  uploaded. Re-uploading the same artifact
#                                  lands on the same key and therefore on the
#                                  existing `UNIQUE(vault_id, s3_key)` row.
#
# The digest is deliberately a plain prefix of the content hash and not a
# derived value: an operator holding a `vault_files.content_hash` can pair it
# to a key by eye, which is what makes an acquired artifact traceable.
_CONTENT_KEY_MARKER = "sha256-"
_CONTENT_KEY_DIGEST_LEN = 16


def _content_key_prefix(content_hash: str) -> str:
    """Key prefix that makes an object's key a function of its bytes."""
    return f"{_CONTENT_KEY_MARKER}{content_hash[:_CONTENT_KEY_DIGEST_LEN]}"


def _s3_key(
    vault_name: str,
    collection: str,
    filename: str,
    *,
    content_hash: str | None = None,
) -> str:
    """Build the storage key for an uploaded file.

    With `content_hash`, the key is deterministic: the same bytes under the
    same vault/collection/filename always produce the same key. Without it,
    the key carries a random prefix and is unique per call — the pre-existing
    behaviour, preserved for callers that cannot hash before uploading.
    """
    safe_name = filename.replace("/", "_")
    prefix = (
        _content_key_prefix(content_hash) if content_hash
        else uuid.uuid4().hex[:8]
    )
    if collection:
        return f"{vault_name}/{collection}/{prefix}_{safe_name}"
    return f"{vault_name}/{prefix}_{safe_name}"


def _content_key_honors_hash(s3_key: str, content_hash: str) -> bool:
    """Whether `s3_key`'s content claim is borne out by `content_hash`.

    Only content-addressed keys make a claim about their bytes; a random
    (legacy) key asserts nothing and therefore always passes. This is what
    stops a caller from declaring one hash at `initiate_upload` — thereby
    reserving that content's key — and then storing unrelated bytes under it.
    """
    last_segment = s3_key.rsplit("/", 1)[-1]
    if not last_segment.startswith(_CONTENT_KEY_MARKER):
        return True
    return last_segment.startswith(f"{_content_key_prefix(content_hash)}_")


def _replacement_staging_key(
    vault_name: str,
    file_id: uuid.UUID,
    replacement_id: uuid.UUID,
) -> str:
    """Private upload target for one replacement attempt.

    The caller receives a presigned URL for this key, never for the live file
    key.  Confirmation copies these bytes to a fresh, non-presigned key before
    publishing the metadata switch.
    """
    return f"__akb_file_replacements__/{vault_name}/{file_id}/{replacement_id}"


def _replacement_final_key(
    vault_name: str,
    collection: str,
    filename: str,
    replacement_id: uuid.UUID,
) -> str:
    """Non-presigned destination for one certified replacement.

    Unlike the legacy create key, this uses the full replacement UUID.  A
    collision must never overwrite another file before PostgreSQL can reject
    the duplicate key.
    """
    safe_name = filename.replace("/", "_")
    prefix = replacement_id.hex
    if collection:
        return f"{vault_name}/{collection}/{prefix}_{safe_name}"
    return f"{vault_name}/{prefix}_{safe_name}"


def _file_version(row: dict) -> str | None:
    """Return the opaque optimistic-concurrency token exposed to callers."""
    return row.get("storage_version") or row.get("etag")


def _new_capability_token() -> str:
    """256 bits of URL-safe randomness. The token IS the capability, so it is
    never stored — only its digest is, the same way a password would be."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")


# What `_new_capability_token` produces: base64url, no padding. Anything else
# is not a token this service issued, and must be refused before it reaches
# `.encode("ascii")` — which raises on non-ASCII input, and an unauthenticated
# route that raises lets anyone write tracebacks into the log.
_CAPABILITY_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


def _is_capability_token_shaped(token: str | None) -> bool:
    return bool(token) and bool(_CAPABILITY_TOKEN_RE.fullmatch(token or ""))


def _capability_digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _capability_url(token: str) -> str:
    base = (settings.public_base_url or "").rstrip("/")
    return f"{base}/api/v1/files/download/{token}"


def _write_capability_url(token: str) -> str:
    base = (settings.public_base_url or "").rstrip("/")
    return f"{base}/api/v1/files/upload/{token}"


async def _issue_write_capability(
    conn,
    *,
    file_id: uuid.UUID,
    vault_id: uuid.UUID,
    object_key: str,
    filename: str,
    mime_type: str,
    actor_id: str,
    collection_id: uuid.UUID | None = None,
    declared_content_hash: str | None = None,
    intent_id: uuid.UUID | None = None,
    ttl: int = _PRESIGN_UPLOAD_TTL,
) -> str:
    """Grant one PUT to one object key and return the bearer token.

    Only the digest is stored, exactly as for a download grant: the URL handed
    back to the caller is the only copy of the token that will ever exist.
    Naming the key is what keeps a capability issued for a replacement's
    staging key from being redeemable against the live object.
    """
    token = _new_capability_token()
    await conn.execute(
        """
        INSERT INTO m1_file_transfer_intents (
            id, file_id, vault_id, collection_id, method, filename, mime_type,
            actor_id, declared_content_hash, object_key, token_digest, expires_at
        ) VALUES (
            $1, $2, $3, $4, 'PUT', $5, $6, $7, $8, $9, $10,
            NOW() + ($11 * INTERVAL '1 second')
        )
        """,
        intent_id or uuid.uuid4(), file_id, vault_id, collection_id,
        filename, mime_type, actor_id, declared_content_hash, object_key,
        _capability_digest(token), ttl,
    )
    return token


async def store_object_stream(
    object_key: str,
    chunks: AsyncIterator[bytes],
    *,
    content_type: str,
    max_bytes: int,
    declared_bytes: int | None = None,
) -> int:
    """Write a request body of unknown length to one key; return its size.

    Buffers up to one part before deciding how to store. A body that fits in
    that buffer is a single PUT — which is the overwhelming majority of Files —
    so the common case costs one round trip rather than three. Only a body that
    outgrows the buffer becomes a multipart upload, and then no more than one
    part is ever held in memory at a time. A File can be multi-gigabyte, so
    holding the body was never an option.

    Parts are sent one at a time. Overlapping them would read from the client
    while a part is in flight, but it would also double the resident buffer
    and leave a part in flight to reason about on the abort path; the transfer
    is bounded by how fast the client sends in any case.
    """
    part_size = s3_adapter.MULTIPART_MIN_PART_BYTES
    buf = bytearray()
    received = 0
    upload_id: str | None = None
    parts: list[dict[str, Any]] = []

    async def _send_part() -> None:
        nonlocal upload_id, buf
        if upload_id is None:
            upload_id = await asyncio.to_thread(
                s3_adapter.multipart_create, object_key, content_type,
            )
        number = len(parts) + 1
        etag = await asyncio.to_thread(
            s3_adapter.multipart_part, object_key, upload_id, number, bytes(buf),
        )
        parts.append({"PartNumber": number, "ETag": etag})
        buf = bytearray()

    try:
        async for chunk in chunks:
            if not chunk:
                continue
            received += len(chunk)
            if received > max_bytes:
                raise AKBError(
                    "Upload exceeds the maximum accepted size", status_code=413,
                )
            buf.extend(chunk)
            if len(buf) >= part_size:
                await _send_part()
        # A presigned PUT could not be short: the object store held the
        # sender to its own Content-Length and stored nothing when the body
        # ran out early. That guarantee moved into this service along with
        # the bytes, so it has to be restated here — otherwise a truncated
        # upload is stored, and `confirm_upload` certifies the truncation as
        # the File's hash.
        if declared_bytes is not None and received != declared_bytes:
            raise AKBError(
                "Upload body did not match its declared length", status_code=400,
            )
        if upload_id is None:
            # Never grew past one part. A zero-byte body stores a zero-byte
            # object, which is what a presigned PUT of the same body did.
            await asyncio.to_thread(
                s3_adapter.put_bytes, object_key, bytes(buf),
                content_type=content_type,
            )
            return received
        if buf:
            await _send_part()
        await asyncio.to_thread(
            s3_adapter.multipart_complete, object_key, upload_id, parts,
        )
        return received
    except BaseException:
        # An abandoned multipart upload is billable storage that no object
        # listing shows and nothing garbage-collects, so it has to be
        # abandoned explicitly.
        if upload_id is not None:
            await _abandon_multipart(object_key, upload_id)
        raise


async def _abandon_multipart(object_key: str, upload_id: str) -> None:
    """Abandon a multipart upload, even while being cancelled.

    The exception that brings us here is most often the cancellation raised
    when a client disconnects mid-body, and handlers run under anyio, whose
    cancellation is level-triggered: a plain `await` here raises again before
    the cleanup can run — measured, and only once the default executor is
    warm, which in a serving process it always is. A shield is what lets an
    ordinary awaited call finish inside that scope, so the abort stays
    ordered and its failure stays observable.
    """
    with anyio.CancelScope(shield=True):
        await asyncio.to_thread(s3_adapter.multipart_abort, object_key, upload_id)


def _file_envelope(row: dict, vault_name: str) -> dict:
    """The File metadata envelope, shared by the list and single-File reads.

    Both must emit the same shape: a caller that resolves a File by id has to
    see exactly what it would have seen had that File been inside the listing
    window."""
    return {
        "kind": "file",
        "uri": file_uri(vault_name, str(row["id"]), collection=row["collection"]),
        "collection": row["collection"],
        "name": row["name"],
        "mime_type": row["mime_type"],
        "size_bytes": row["size_bytes"],
        "content_hash": row["content_hash"],
        "hash_algorithm": row["hash_algorithm"],
        "etag": row["etag"],
        "storage_version": row["storage_version"],
        "version": _file_version(row),
        "description": row["description"],
        "created_by": row["created_by"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


def _check_file_preconditions(
    row: dict,
    *,
    expected_content_hash: str | None,
    expected_version: str | None,
) -> None:
    if expected_content_hash is not None and row.get("content_hash") != expected_content_hash:
        raise ConflictError(f"content_hash moved: expected {expected_content_hash}, actual {row.get('content_hash')}")
    current_version = _file_version(row)
    if expected_version is not None and current_version != expected_version:
        raise ConflictError(f"file version moved: expected {expected_version}, actual {current_version}")


async def _discard_replacement_objects(*s3_keys: str) -> None:
    """Best-effort cleanup for unpublished replacement objects.

    A precondition failure must remain a 409 even if storage cleanup has a
    transient failure.  Successful publishes use the durable delete outbox;
    this helper is only for objects that never became database-owned.
    """
    for s3_key in s3_keys:
        if not s3_key:
            continue
        try:
            await asyncio.to_thread(s3_adapter.delete, s3_key)
        except NotFoundError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not discard unpublished replacement object %s: %s", s3_key, exc)


from app.util.text import normalize_collection_path as _normalize_collection_path  # noqa: E402
from app.util.text import normalize_content_type as _normalize_content_type  # noqa: E402


async def _delete_file_publications(conn, vault_id: uuid.UUID, file_id: str) -> None:
    """Drop this file's publications on the CALLER's connection, before its
    `vault_files` row goes.

    Publication creation now requires a confirmed ``kind='file'`` row, but
    this remains the shared deletion chokepoint for both confirm-time cleanup
    and ordinary File deletion. Keeping the cascade here protects legacy rows
    and avoids coupling correctness to the order in which callers discovered
    the invalid or deleted object. A file URI carries a UUID, so any surviving
    publication would be stale rather than re-bound to another resource.

    Takes `vault_id` because `confirm_upload` never resolves the vault NAME,
    which the shared helper needs to build the canonical URI.
    """
    from app.services.publication_service import delete_publications_for_file
    vault_row = await conn.fetchrow("SELECT name FROM vaults WHERE id = $1", vault_id)
    if not vault_row:
        return
    await delete_publications_for_file(
        file_id, vault_row["name"], expected_vault_id=vault_id, conn=conn,
    )


# ── File domain service ──────────────────────────────────────────


class FileService:
    def __init__(self):
        self._bucket = settings.s3_bucket
        # Normal deployments retain the existing direct-S3 File service.  The
        # facade can only exist under the config/database guard checked by its
        # factory, so there is no runtime fallback or dual write.
        self._measurement = MeasurementFileService() if measurement_enabled() else None

    async def initiate_upload(
        self,
        vault_name: str,
        vault_id: uuid.UUID,
        collection: str,
        filename: str,
        *,
        actor_id: str,
        mime_type: str = "application/octet-stream",
        description: str = "",
        content_hash: str | None = None,
    ) -> dict:
        """Create a file record and return the URL its bytes are PUT to.

        The client PUTs the bytes to that absolute URL, then calls
        confirm_upload(). `collection` is a path string (empty / None
        for vault root); a matching `collections` row is auto-created
        if needed so files share the same FK-normalized hierarchy as
        documents and tables.

        `content_hash` is the caller's sha256 of the bytes it is about to
        upload. Supplying it makes the upload **idempotent**: the storage key
        becomes a function of the bytes, so re-uploading the same artifact to
        the same vault/collection/filename resolves to the file that is
        already there instead of creating a second row for the same content.
        The returned envelope is unchanged in shape and still carries a usable
        `upload_url` — an unaware client can re-PUT the identical bytes and
        confirm as usual; the net effect is one row, not two.
        `deduplicated` says which happened, for clients that would rather skip
        the redundant transfer.

        The hash is a *claim* at this point — AKB certifies the real one in
        `confirm_upload`, which rejects bytes that do not match the key they
        were stored under. Omitting `content_hash` preserves the historical
        behaviour exactly: a random key, and one new row per call.
        """
        mime_type = _normalize_content_type(mime_type)
        if content_hash is not None and not is_sha256_hex(content_hash):
            raise AKBError("content_hash must be a lowercase sha256 hex digest", status_code=400)
        # Ahead of the backend split: the measurement facade normalizes the
        # collection itself, so guarding at this method's own normalization
        # site (below) would leave the native backend unreserved. A path that
        # will not normalize cannot reach the reserved namespace either, so it
        # falls through and raises at the original site — hoisting the
        # normalizer must not move that error ahead of the storage call.
        try:
            _requested_collection = _normalize_collection_path(collection)
        except ValueError:
            _requested_collection = None
        skill_policy.check_resource_collection(_requested_collection)
        if self._measurement is not None:
            return await self._measurement.initiate_upload(
                vault_name=vault_name, vault_id=vault_id, collection=collection,
                filename=filename, actor_id=actor_id, mime_type=mime_type,
                description=description, content_hash=content_hash,
            )

        s3_adapter.ensure_bucket(self._bucket)
        collection_path = _normalize_collection_path(collection)
        preferred_s3_key = _s3_key(
            vault_name, collection_path, filename, content_hash=content_hash,
        )
        file_id = uuid.uuid4()

        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                if not await lock_vault_for_child_write(conn, vault_id):
                    raise ConflictError("Vault was deleted during file upload")
                collection_id = None
                if collection_path:
                    coll_repo = CollectionRepository(pool)
                    collection_id = await coll_repo.get_or_create(
                        vault_id, collection_path, conn=conn,
                    )
                # A delayed delete intent may still own the deterministic key
                # after its original metadata row disappeared. Never wait for
                # remote object-store I/O on the request path: reserve the
                # preferred key with a non-blocking advisory lock, otherwise
                # use a fresh random key whose upload cannot be removed by the
                # older intent.
                s3_key = preferred_s3_key
                for _attempt in range(4):
                    if await vault_files_repo.s3_key_available_for_registration(
                        conn, vault_id=vault_id, s3_key=s3_key,
                    ):
                        break
                    s3_key = _s3_key(
                        vault_name, collection_path, filename, content_hash=None,
                    )
                else:
                    raise AKBError(
                        "Could not reserve an object storage key",
                        status_code=503,
                    )
                stored_id = await vault_files_repo.insert_or_adopt(
                    conn,
                    file_id=file_id, vault_id=vault_id,
                    name=filename,
                    s3_key=s3_key, mime_type=mime_type,
                    size_bytes=0, description=description,
                    created_by=actor_id,
                    collection_id=collection_id,
                )
                # Issued inside the same transaction as the reservation it
                # authorizes: a capability that outlived a rolled-back
                # reservation would name a key nothing owns.
                token = await _issue_write_capability(
                    conn,
                    file_id=stored_id,
                    vault_id=vault_id,
                    collection_id=collection_id,
                    object_key=s3_key,
                    filename=filename,
                    mime_type=mime_type,
                    actor_id=actor_id,
                    declared_content_hash=content_hash,
                )

        deduplicated = stored_id != file_id
        file_id = stored_id

        logger.info(
            "Upload reserved for %s (file_id=%s, collection=%s, deduplicated=%s)",
            vault_name, file_id, collection_path or "<root>", deduplicated,
        )
        return {
            "kind": "file",
            "uri": file_uri(vault_name, str(file_id), collection=collection_path),
            "vault": vault_name,
            "collection": collection_path or None,
            # Names this service and an opaque token — never the object
            # store's endpoint, bucket or key. What a caller does with it is
            # unchanged: PUT the bytes to this absolute URL, no Authorization
            # header. `s3_key` is gone; it was a storage locator in a public
            # response and nothing ever read it.
            "upload_url": _write_capability_url(token),
            "expires_in": _PRESIGN_UPLOAD_TTL,
            "deduplicated": deduplicated,
        }

    async def initiate_replace(
        self,
        vault_name: str,
        vault_id: uuid.UUID,
        file_id: str,
        *,
        actor_id: str,
        content_hash: str,
        mime_type: str | None = None,
        expected_content_hash: str | None = None,
        expected_version: str | None = None,
    ) -> dict:
        """Prepare an isolated upload that can replace one logical file.

        The capability is bound to a replacement-specific staging key, so it
        cannot be redeemed against the live object.  ``confirm_replace``
        publishes a fresh object only after re-checking the
        optimistic-concurrency pins.
        """
        if not is_sha256_hex(content_hash):
            raise AKBError("content_hash must be a lowercase sha256 hex digest", status_code=400)
        if expected_content_hash is not None and not is_sha256_hex(expected_content_hash):
            raise AKBError(
                "expected_content_hash must be a lowercase sha256 hex digest",
                status_code=400,
            )
        if self._measurement is not None:
            raise ConflictError("File replacement is unavailable for the active measurement storage driver")

        fid = uuid.UUID(file_id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await vault_files_repo.find_by_id(conn, vault_id, fid)
        if not row or row.get("kind") != "file":
            raise NotFoundError("File", file_id)
        _check_file_preconditions(
            row,
            expected_content_hash=expected_content_hash,
            expected_version=expected_version,
        )

        current_version = _file_version(row)
        canonical_uri = file_uri(vault_name, file_id, collection=row["collection"])
        if row.get("content_hash") == content_hash:
            return {
                "kind": "file",
                "uri": canonical_uri,
                "vault": vault_name,
                "collection": row["collection"],
                "name": row["name"],
                "mime_type": row["mime_type"],
                "size_bytes": row["size_bytes"],
                "content_hash": row["content_hash"],
                "hash_algorithm": row["hash_algorithm"],
                "etag": row["etag"],
                "storage_version": row["storage_version"],
                "version": current_version,
                "unchanged": True,
            }

        s3_adapter.ensure_bucket(self._bucket)
        replacement_id = uuid.uuid4()
        staging_key = _replacement_staging_key(vault_name, fid, replacement_id)
        upload_mime_type = _normalize_content_type(
            mime_type or row.get("mime_type")
        )
        # An abandoned replacement has no vault_files row from which a normal
        # delete can discover its staging key.  Schedule cleanup just after the
        # capability expires; successful confirmation also enqueues an
        # immediate delete, and duplicate S3 deletes are intentionally safe.
        async with pool.acquire() as conn:
            await _enqueue_s3_delete(
                conn,
                staging_key,
                delay_seconds=_REPLACEMENT_STAGING_DELETE_DELAY,
            )
            # The capability names the staging key, so it cannot be redeemed
            # against the live object this replacement is meant to supersede.
            token = await _issue_write_capability(
                conn,
                file_id=fid,
                vault_id=vault_id,
                object_key=staging_key,
                filename=row["name"],
                mime_type=upload_mime_type,
                actor_id=actor_id,
                declared_content_hash=content_hash,
                intent_id=replacement_id,
            )
        return {
            "kind": "file",
            "uri": canonical_uri,
            "vault": vault_name,
            "collection": row["collection"],
            "name": row["name"],
            "mime_type": upload_mime_type,
            "replacement_id": str(replacement_id),
            "upload_url": _write_capability_url(token),
            "expires_in": _PRESIGN_UPLOAD_TTL,
            "current_content_hash": row.get("content_hash"),
            "current_version": current_version,
            "unchanged": False,
        }

    async def confirm_replace(
        self,
        vault_name: str,
        vault_id: uuid.UUID,
        file_id: str,
        replacement_id: str,
        *,
        actor_id: str,
        delegated_actor: dict[str, str] | None = None,
        content_hash: str,
        expected_content_hash: str | None = None,
        expected_version: str | None = None,
    ) -> dict:
        """Publish staged replacement bytes under the existing file URI."""
        if not is_sha256_hex(content_hash):
            raise AKBError("content_hash must be a lowercase sha256 hex digest", status_code=400)
        if expected_content_hash is not None and not is_sha256_hex(expected_content_hash):
            raise AKBError(
                "expected_content_hash must be a lowercase sha256 hex digest",
                status_code=400,
            )
        if self._measurement is not None:
            raise ConflictError("File replacement is unavailable for the active measurement storage driver")

        fid = uuid.UUID(file_id)
        rid = uuid.UUID(replacement_id)
        staging_key = _replacement_staging_key(vault_name, fid, rid)
        pool = await get_pool()

        # Cheap stale-request rejection before any server-side copy.  The same
        # checks run again under a row lock below; this first pass is only an
        # optimization and is not the concurrency boundary.
        async with pool.acquire() as conn:
            row = await vault_files_repo.find_by_id(conn, vault_id, fid)
        if not row or row.get("kind") != "file":
            await _discard_replacement_objects(staging_key)
            raise NotFoundError("File", file_id)
        try:
            _check_file_preconditions(
                row,
                expected_content_hash=expected_content_hash,
                expected_version=expected_version,
            )
        except ConflictError:
            await _discard_replacement_objects(staging_key)
            raise

        # The presigned staging URL remains valid until its TTL elapses.  Copy
        # to a fresh, non-presigned key and certify THAT immutable candidate;
        # a later reuse of the staging URL therefore cannot mutate live bytes.
        final_key = _replacement_final_key(
            vault_name,
            row.get("collection") or "",
            row["name"],
            rid,
        )
        # The candidate has no owning vault_files row until the metadata swap
        # commits.  Schedule a durable fallback cleanup before creating it,
        # then cancel that cleanup in the SAME transaction that adopts it.
        # This is safe even if the client cannot tell whether COMMIT succeeded.
        async with pool.acquire() as conn:
            candidate_cleanup_id = await _enqueue_s3_delete(
                conn,
                final_key,
                delay_seconds=_REPLACEMENT_CANDIDATE_DELETE_DELAY,
            )
        try:
            final_meta = await asyncio.to_thread(s3_adapter.copy, staging_key, final_key)
            server_content_hash = await asyncio.to_thread(
                compute_stream_content_hash,
                s3_adapter.iter_chunks(final_key),
            )
        except Exception:
            await _discard_replacement_objects(staging_key, final_key)
            raise

        if server_content_hash != content_hash:
            await _discard_replacement_objects(staging_key, final_key)
            raise ConflictError("Uploaded replacement file hash mismatch")

        size_bytes = final_meta["ContentLength"]
        mime_type = _normalize_content_type(
            final_meta.get("ContentType") or row.get("mime_type")
        )
        etag = (final_meta.get("ETag") or "").strip('"') or None
        storage_version = final_meta.get("VersionId")
        previous_content_hash: str | None = None
        previous_version: str | None = None
        unchanged = False
        ready_to_commit = False

        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    locked = await vault_files_repo.find_by_id_for_update(conn, vault_id, fid)
                    if not locked:
                        raise NotFoundError("File", file_id)
                    _check_file_preconditions(
                        locked,
                        expected_content_hash=expected_content_hash,
                        expected_version=expected_version,
                    )
                    previous_content_hash = locked.get("content_hash")
                    previous_version = _file_version(locked)

                    # A concurrent unconditional writer may have published the
                    # same bytes since initiation.  Keep its object/version and
                    # retire both candidates without manufacturing a write.
                    if previous_content_hash == server_content_hash:
                        unchanged = True
                        await _enqueue_s3_delete(conn, staging_key)
                        await _enqueue_s3_delete(conn, final_key)
                        result_row = locked
                    else:
                        await vault_files_repo.replace_confirmed_metadata(
                            conn,
                            fid,
                            s3_key=final_key,
                            mime_type=mime_type,
                            size_bytes=size_bytes,
                            content_hash=server_content_hash,
                            hash_algorithm=HASH_ALGORITHM,
                            etag=etag,
                            storage_version=storage_version,
                        )
                        # Publishing the candidate and cancelling its fallback
                        # cleanup are one atomic PostgreSQL decision.
                        await _cancel_s3_delete(conn, candidate_cleanup_id)
                        await _enqueue_s3_delete(conn, locked["s3_key"])
                        await _enqueue_s3_delete(conn, staging_key)
                        await emit_event(
                            conn,
                            "file.update",
                            vault_id=vault_id,
                            resource_uri=file_uri(
                                vault_name,
                                file_id,
                                collection=locked["collection"],
                            ),
                            actor_id=actor_id,
                            payload={
                                "vault": vault_name,
                                "collection": locked["collection"],
                                "name": locked["name"],
                                "mime_type": mime_type,
                                "size_bytes": size_bytes,
                                "content_hash": server_content_hash,
                                "hash_algorithm": HASH_ALGORITHM,
                                "etag": etag,
                                "storage_version": storage_version,
                                "previous_content_hash": previous_content_hash,
                                "previous_version": previous_version,
                                **_delegated_actor_event_fields(delegated_actor),
                            },
                        )
                        await enqueue_native_file_projection(
                            conn,
                            file_id=fid,
                            namespace_id=vault_id,
                            collection=locked["collection"],
                            name=locked["name"],
                            mime_type=mime_type,
                            content_hash=server_content_hash,
                            byte_size=size_bytes,
                            s3_key=final_key,
                            actor=actor_id,
                        )
                        result_row = {
                            **locked,
                            "s3_key": final_key,
                            "mime_type": mime_type,
                            "size_bytes": size_bytes,
                            "content_hash": server_content_hash,
                            "hash_algorithm": HASH_ALGORITHM,
                            "etag": etag,
                            "storage_version": storage_version,
                        }
                    # From this point onward, an exception can be an ambiguous
                    # COMMIT result rather than a known rollback.
                    ready_to_commit = True
        except Exception:
            # Staging is never database-owned and can be removed immediately.
            # Do not blindly delete final_key: after the transaction body
            # finishes, COMMIT may have succeeded even when the connection
            # reports an error. Its delayed outbox row is cancelled atomically
            # iff publication committed.
            await _discard_replacement_objects(staging_key)
            if not ready_to_commit:
                # The transaction body did not finish, so asyncpg enters its
                # rollback path and the candidate cannot have been adopted.
                await _discard_replacement_objects(final_key)
            raise

        if not unchanged:
            try:
                await index_file_metadata(
                    file_id,
                    vault_id=vault_id,
                    vault_name=vault_name,
                    collection=result_row["collection"] or "",
                    name=result_row["name"],
                    mime_type=result_row["mime_type"],
                    size_bytes=result_row["size_bytes"],
                    description=result_row["description"],
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("file metadata indexing failed for %s: %s", file_id, exc)

        result_version = _file_version(result_row)
        return {
            "kind": "file",
            "uri": file_uri(vault_name, file_id, collection=result_row["collection"]),
            "vault": vault_name,
            "collection": result_row["collection"],
            "name": result_row["name"],
            "mime_type": result_row["mime_type"],
            "size_bytes": result_row["size_bytes"],
            "content_hash": result_row["content_hash"],
            "hash_algorithm": result_row["hash_algorithm"],
            "etag": result_row["etag"],
            "storage_version": result_row["storage_version"],
            "version": result_version,
            "previous_content_hash": previous_content_hash,
            "previous_version": previous_version,
            "unchanged": unchanged,
        }


    async def confirm_upload(
        self,
        vault_id: uuid.UUID,
        file_id: str,
        *,
        actor_id: str,
        delegated_actor: dict[str, str] | None = None,
        content_hash: str | None = None,
        hash_algorithm: str = HASH_ALGORITHM,
    ) -> dict:
        """Confirm upload completion and persist AKB-certified byte hash.

        If the file doesn't exist in S3 (upload failed/abandoned),
        deletes the orphan DB record and returns an error.
        """
        if hash_algorithm != HASH_ALGORITHM:
            raise AKBError(
                f"Unsupported file hash algorithm: {hash_algorithm}",
                status_code=400,
            )
        if content_hash is not None and not is_sha256_hex(content_hash):
            raise AKBError("content_hash must be a lowercase sha256 hex digest", status_code=400)

        if self._measurement is not None:
            return await self._measurement.confirm_upload(vault_id, file_id, content_hash=content_hash)

        fid = uuid.UUID(file_id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await vault_files_repo.lease_file_upload_confirmation(
                    conn, vault_id, fid,
                )
                if not row:
                    raise NotFoundError("File", file_id)

        # Read object size. Treat NoSuchKey specially: that means the
        # client never finished its presigned upload; clean up the
        # orphan DB record so the same filename can be retried.
        try:
            meta = s3_adapter.head(row["s3_key"])
            size_bytes = meta["ContentLength"]
        except NotFoundError:
            async with pool.acquire() as conn:
                # One transaction so the publication cannot outlive the row
                # it points at — the same atomicity the delete paths need.
                async with conn.transaction():
                    await _delete_file_publications(conn, vault_id, file_id)
                    await vault_files_repo.delete(conn, fid)
            logger.warning("Orphan file record deleted: %s (S3 object missing)", file_id)
            raise AKBError(
                f"Upload not found in storage — file record cleaned up: {file_id}",
                status_code=404,
            )

        server_content_hash = await asyncio.to_thread(
            compute_stream_content_hash,
            s3_adapter.iter_chunks(row["s3_key"]),
        )
        # Two ways the stored bytes can fail to be what was declared: the
        # caller's `content_hash` argument disagrees with them, or the key they
        # were stored under is content-addressed and does not. The second check
        # is what keeps a content-addressed key honest end to end — without it a
        # caller could reserve some other content's key at `initiate_upload`,
        # omit `content_hash` here, and leave unrelated bytes sitting where the
        # next caller's idempotent upload would adopt them. Both are the same
        # fault ("these are not the bytes you said") and take the same
        # pre-existing 409 + cleanup path.
        stored_bytes_disowned = (
            (content_hash is not None and content_hash != server_content_hash)
            or not _content_key_honors_hash(row["s3_key"], server_content_hash)
        )
        if stored_bytes_disowned:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await _delete_file_publications(conn, vault_id, file_id)
                    await vault_files_repo.delete(conn, fid)
                    await _enqueue_s3_delete(conn, row["s3_key"])
            raise AKBError(
                "Uploaded file hash mismatch; file record was cleaned up.",
                status_code=409,
            )

        etag = (meta.get("ETag") or "").strip('"') or None
        storage_version = meta.get("VersionId")

        async with pool.acquire() as conn:
            async with conn.transaction():
                confirmed = await vault_files_repo.confirm_file_upload_metadata(
                    conn, fid, vault_id,
                    size_bytes=size_bytes,
                    content_hash=server_content_hash,
                    hash_algorithm=HASH_ALGORITHM,
                    etag=etag,
                    storage_version=storage_version,
                )
                if not confirmed:
                    raise NotFoundError("File", file_id)
                vault_row = await conn.fetchrow(
                    "SELECT name FROM vaults WHERE id = $1", vault_id,
                )
                vault_name_for_event = vault_row["name"] if vault_row else None
                await emit_event(
                    conn, "file.put",
                    vault_id=vault_id,
                    resource_uri=(
                        file_uri(
                            vault_name_for_event,
                            file_id,
                            collection=row["collection"],
                        )
                        if vault_name_for_event else None
                    ),
                    actor_id=actor_id,
                    payload={
                        "vault": vault_name_for_event,
                        "collection": row["collection"],
                        "name": row["name"],
                        "mime_type": row["mime_type"],
                        "size_bytes": size_bytes,
                        "content_hash": server_content_hash,
                        "hash_algorithm": HASH_ALGORITHM,
                        "etag": etag,
                        "storage_version": storage_version,
                        **_delegated_actor_event_fields(delegated_actor),
                    },
                )
                await enqueue_native_file_projection(
                    conn,
                    file_id=fid,
                    namespace_id=vault_id,
                    collection=row["collection"],
                    name=row["name"],
                    mime_type=row["mime_type"],
                    content_hash=server_content_hash,
                    byte_size=size_bytes,
                    s3_key=row["s3_key"],
                    actor=actor_id,
                )

        # Index file metadata for hybrid search.
        try:
            await index_file_metadata(
                file_id,
                vault_id=vault_id,
                vault_name=vault_row["name"] if vault_row else "",
                collection=row["collection"] or "",
                name=row["name"],
                mime_type=row["mime_type"],
                size_bytes=size_bytes,
                description=row["description"],
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("file metadata indexing failed for %s: %s", file_id, e)

        logger.info("Upload confirmed: %s (%d bytes)", row["name"], size_bytes)
        vault_name = vault_row["name"] if vault_row else None
        return {
            "kind": "file",
            "uri": (
                file_uri(vault_name, file_id, collection=row["collection"])
                if vault_name else None
            ),
            "vault": vault_name,
            "name": row["name"],
            "collection": row["collection"],
            "mime_type": row["mime_type"],
            "size_bytes": size_bytes,
            "content_hash": server_content_hash,
            "hash_algorithm": HASH_ALGORITHM,
            "etag": etag,
            "storage_version": storage_version,
            "version": storage_version or etag,
        }

    async def get_download_url(
        self, vault_id: uuid.UUID, file_id: str, *, actor_id: str | None = None,
    ) -> dict:
        """Issue a capability URL for these bytes.

        The URL names this service and an opaque token — never the object
        store's endpoint, bucket or key. A holder still GETs it with no
        Authorization header, which is what every non-browser consumer is
        written against.

        `actor_id` is recorded with the grant. Nothing enforces it, but a
        capability is a bearer credential and who was handed one is the only
        thing left to record: the fetch no longer reaches the object store, so
        its access log no longer sees any of this."""
        if self._measurement is not None:
            return await self._measurement.get_download_url(vault_id, file_id)
        token = _new_capability_token()
        pool = await get_pool()
        # One connection for both reads and the grant. Splitting them made a
        # plain GET take two round-trips through the pool for no gain.
        async with pool.acquire() as conn:
            row = await vault_files_repo.find_by_id(
                conn, vault_id, uuid.UUID(file_id),
            )
            if (
                not row
                or row.get("kind") != "file"
                or row.get("upload_state") != "confirmed"
            ):
                raise NotFoundError("File", file_id)
            await conn.execute(
                """
                INSERT INTO m1_file_transfer_intents (
                    id, file_id, vault_id, method, token_digest, expires_at,
                    actor_id
                ) VALUES (
                    $1, $2, $3, 'GET', $4,
                    NOW() + ($5 * INTERVAL '1 second'), $6
                )
                """,
                uuid.uuid4(), uuid.UUID(file_id), vault_id,
                _capability_digest(token), _PRESIGN_DOWNLOAD_TTL, actor_id,
            )

        return {
            "kind": "file",
            "name": row["name"],
            # Carried so a caller never has to parse the storage locator out of
            # `download_url` to learn where the File lives.
            "collection": row["collection"],
            "download_url": _capability_url(token),
            "mime_type": row["mime_type"],
            "size_bytes": row["size_bytes"],
            "content_hash": row["content_hash"],
            "hash_algorithm": row["hash_algorithm"],
            "etag": row["etag"],
            "storage_version": row["storage_version"],
            "version": _file_version(row),
            "expires_in": _PRESIGN_DOWNLOAD_TTL,
        }

    async def get_file(
        self, vault_id: uuid.UUID, vault_name: str, file_id: str,
    ) -> dict:
        """Return one confirmed File's metadata by id.

        Resolving a File used to be possible only by listing the vault and
        searching the page that came back, so a File outside that window could
        not be opened at all. This read is by primary key: it does not depend
        on how many Files the vault holds, nor on where this one sorts."""
        if self._measurement is not None:
            return await self._measurement.get_file(vault_id, vault_name, file_id)
        # A malformed id is a client error, not a missing File — same boundary
        # the download route draws.
        try:
            fid = uuid.UUID(file_id)
        except (ValueError, AttributeError):
            raise ValidationError("file_id must be a UUID") from None
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await vault_files_repo.find_by_id(conn, vault_id, fid)
        # An attachment or a still-pending upload is not a readable File here,
        # and must not be distinguishable from one that does not exist.
        if (
            not row
            or row.get("kind") != "file"
            or row.get("upload_state") != "confirmed"
        ):
            raise NotFoundError("File", file_id)
        return _file_envelope(row, vault_name)

    async def resolve_download_capability(self, token: str) -> dict:
        """Resolve a download capability to the File row it grants.

        Looked up by digest and checked for expiry, but NOT single-use: a
        browser fetches the same URL for the preview, again on a reload, and
        once more for the download control. A presigned URL behaved the same
        way inside its lifetime, and callers are written against that.

        Every rejection is the same 404 — an expired token, a token that was
        never issued, and a File that has since been deleted must not be
        distinguishable from one another."""
        if not _is_capability_token_shaped(token):
            raise NotFoundError("File", "capability")
        pool = await get_pool()
        async with pool.acquire() as conn:
            grant = await conn.fetchrow(
                """
                SELECT file_id, vault_id FROM m1_file_transfer_intents
                 WHERE token_digest = $1 AND method = 'GET' AND expires_at > NOW()
                """,
                _capability_digest(token),
            )
            if grant is None:
                raise NotFoundError("File", "capability")
            row = await vault_files_repo.find_by_id(
                conn, grant["vault_id"], grant["file_id"],
            )
        if (
            not row
            or row.get("kind") != "file"
            or row.get("upload_state") != "confirmed"
        ):
            raise NotFoundError("File", "capability")
        return row

    async def resolve_write_capability(self, token: str) -> dict:
        """Resolve an upload capability to the single key it may write.

        Not single-use, for the same reason the download grant is not: a
        transfer that dropped at 90% has to be startable again, which is
        exactly what re-PUTting a presigned URL did. Expiry is the bound.

        Whether the bytes ever arrived is deliberately not recorded here. The
        object store is the authority on that and `confirm_upload` asks it
        directly; a second record of the same fact could only disagree.

        `already_confirmed` is carried because a deduplicating reservation
        adopts the File that already holds the key — including one whose
        bytes are final. Writing to that key is how a caller could replace
        another File's content, and the damage does not stop at replacement:
        `confirm_upload` re-derives the digest, finds it disagrees with the
        content-addressed key, and deletes the row. The route uses this to
        keep the body away from storage in that case.

        A row without `object_key` belongs to the measurement lane, which
        carries its bytes in the database and addresses no object store. It is
        refused rather than having a key inferred for it — and like every
        other rejection here it is the same 404, so an expired token, a token
        that was never issued and a token for another lane stay
        indistinguishable.

        The join is what a presigned URL could not do: if the File was deleted
        between the reservation and the transfer, the capability stops working
        instead of writing bytes to a key nothing references any more. One
        query, because the grant alone was never the whole answer.
        """
        if not _is_capability_token_shaped(token):
            raise NotFoundError("File", "capability")
        pool = await get_pool()
        async with pool.acquire() as conn:
            grant = await conn.fetchrow(
                """
                SELECT i.file_id, i.vault_id, i.object_key, i.mime_type,
                       f.upload_state
                  FROM m1_file_transfer_intents AS i
                  JOIN vault_files AS f
                    ON f.id = i.file_id AND f.vault_id = i.vault_id
                 WHERE i.token_digest = $1 AND i.method = 'PUT'
                   AND i.object_key IS NOT NULL AND i.expires_at > NOW()
                """,
                _capability_digest(token),
            )
        if grant is None:
            raise NotFoundError("File", "capability")
        return {
            "file_id": grant["file_id"],
            "vault_id": grant["vault_id"],
            "object_key": grant["object_key"],
            # The type the bytes will be stored and later served under is the
            # one AKB normalized when the capability was issued, not one the
            # uploading client restates at PUT time.
            "mime_type": _normalize_content_type(grant["mime_type"]),
            # Whether this reservation adopted a File that is already
            # confirmed. It changes what the route may do with the body —
            # see `upload_by_capability`.
            "already_confirmed": grant["upload_state"] == "confirmed",
        }

    async def list_files(
        self,
        vault_id: uuid.UUID,
        vault_name: str,
        collection: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """List files in a vault. When `collection` is set (path string),
        only files in that exact collection (NULL collection_id for empty
        path = vault root) are returned. The path is resolved to a
        collection_id at query time. `vault_name` is required so each
        row's canonical `uri` can be built without a second join."""
        if self._measurement is not None:
            return await self._measurement.list_files(vault_id, vault_name, collection, limit)
        pool = await get_pool()
        async with pool.acquire() as conn:
            if collection is None:
                rows = await vault_files_repo.list_for_vault(
                    conn, vault_id, limit=limit,
                )
            else:
                collection_path = _normalize_collection_path(collection)
                if collection_path == "":
                    # Empty path => vault root scope.
                    rows = await vault_files_repo.list_for_vault(
                        conn, vault_id, collection_id=None, scoped=True, limit=limit,
                    )
                else:
                    cid_row = await conn.fetchrow(
                        "SELECT id FROM collections WHERE vault_id = $1 AND path = $2",
                        vault_id, collection_path,
                    )
                    if not cid_row:
                        return []  # collection doesn't exist → no files
                    rows = await vault_files_repo.list_for_vault(
                        conn, vault_id,
                        collection_id=cid_row["id"], scoped=True, limit=limit,
                    )

        return [_file_envelope(r, vault_name) for r in rows]

    async def delete(
        self,
        vault_id: uuid.UUID,
        file_id: str,
        *,
        actor_id: str,
    ) -> dict:
        if self._measurement is not None:
            return await self._measurement.delete(vault_id, file_id, actor_id=actor_id)
        fid = uuid.UUID(file_id)
        pool = await get_pool()

        # Atomic PG mutations: row lock + vault_files DELETE + edges cleanup +
        # chunk-delete outbox + s3-delete outbox under one TX. The
        # actual S3 delete is performed asynchronously by
        # s3_delete_worker after the TX commits, so a crash between
        # commit and S3 cannot leave us with an orphan blob (or a
        # missing-blob row). The outbox row carries the s3_key.
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Serialize with confirm_replace.  Reading the key before this
                # transaction can enqueue a stale object if a replacement wins
                # the race, leaving the newly published object orphaned.
                row = await vault_files_repo.find_by_id_for_update(conn, vault_id, fid)
                if not row:
                    raise NotFoundError("File", file_id)
                vault_row = await conn.fetchrow(
                    "SELECT name FROM vaults WHERE id = $1", vault_id,
                )
                vault_name = vault_row["name"] if vault_row else ""
                if vault_name:
                    f_uri = file_uri(vault_name, file_id, collection=row["collection"])
                    # App-level publication cascade — `publications.
                    # file_id` FK is gone after migration 022. Goes through
                    # the shared helper instead of an inline DELETE (see
                    # `delete_publications_for_document` for why there is
                    # exactly one implementation), and runs BEFORE the
                    # `vault_files` row is removed: the helper re-derives
                    # the file's collection from `vault_files JOIN
                    # collections`, and inside this transaction an
                    # already-deleted row is invisible to it — it would
                    # build a vault-root URI and match nothing.
                    from app.services.publication_service import (
                        delete_publications_for_file,
                    )
                    await delete_publications_for_file(
                        file_id, vault_name,
                        expected_vault_id=vault_id, conn=conn,
                    )
                    await conn.execute(
                        "DELETE FROM edges WHERE source_uri = $1 OR target_uri = $1",
                        f_uri,
                    )

                await vault_files_repo.delete(conn, fid)

                # Drop the metadata chunk (outbox-driven vector-store delete).
                # delete_file_chunks → _drop_source_chunks_with_outbox is
                # designed to RAISE so a failed outbox enqueue rolls back the
                # whole file delete (03-F1). Do NOT swallow it: swallowing
                # commits the vault_files delete + s3 enqueue while leaving
                # the chunk's vector-store point orphaned.
                await delete_file_chunks(conn, file_id)

                await _enqueue_s3_delete(conn, row["s3_key"])

                await emit_event(
                    conn, "file.delete",
                    vault_id=vault_id,
                    resource_uri=(
                        file_uri(vault_name, file_id, collection=row["collection"])
                        if vault_name else None
                    ),
                    actor_id=actor_id,
                    payload={
                        "vault": vault_name,
                        "collection": row["collection"],
                        "name": row["name"],
                        "s3_key": row["s3_key"],
                        "size_bytes": row["size_bytes"],
                    },
                )
                await enqueue_native_file_projection_delete(
                    conn,
                    file_id=fid,
                    namespace_id=vault_id,
                    collection=row["collection"],
                    name=row["name"],
                    actor=actor_id,
                )

        logger.info("Deleted file %s (s3://%s/%s)", file_id, self._bucket, row["s3_key"])
        return {
            "kind": "file",
            "uri": (
                file_uri(vault_name, file_id, collection=row.get("collection"))
                if vault_name else None
            ),
            "vault": vault_name,
            "collection": row.get("collection"),
            "name": row["name"],
            "deleted": True,
        }

    async def transfer_measurement_capability(
        self, token: str, *, method: str, body: bytes | None = None,
    ) -> bytes | None:
        """Serve a guarded FS-CAS PUT/GET capability through the File route.

        This is deliberately unavailable outside M1 measurement mode.  The
        opaque token itself is process-local and is neither a database value
        nor part of the measurement receipt.
        """
        if self._measurement is None:
            raise NotFoundError("File transfer", token)
        return await self._measurement.transfer(token, body=body, method=method)

    async def namespace_placement_observation(
        self, vault_id: uuid.UUID, vault_name: str,
    ) -> dict:
        """Report which body placements a vault's native bodies still use.

        Measurement-only, and gated exactly like
        `transfer_measurement_capability`: outside M1 measurement mode the
        facade does not exist, so the surface is simply absent (404) rather
        than answering with an empty or synthesized census.
        """
        if self._measurement is None:
            raise NotFoundError("File placement observation", vault_name)
        return await self._measurement.namespace_placement_observation(vault_id, vault_name)


async def index_file_metadata(
    file_id: str,
    vault_id: uuid.UUID,
    vault_name: str,
    collection: str,
    name: str,
    mime_type: str | None,
    size_bytes: int | None,
    description: str | None,
) -> None:
    """Build + upsert the metadata chunk for a file so hybrid search
    can surface it. Safe to call repeatedly — write_source_chunks
    replaces all prior chunks for this file first."""
    chunk = build_file_chunk(
        vault_name=vault_name, collection=collection, name=name,
        mime_type=mime_type, size_bytes=size_bytes, description=description,
    )
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"akb:index:file:{file_id}",
            )
            await write_source_chunks(
                conn, "file", file_id,
                vault_id=vault_id,
                chunks=[chunk],
            )
