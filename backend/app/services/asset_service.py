"""Document image assets stored as hidden, vault-owned object-store files."""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import re
import uuid
import warnings
from datetime import datetime, timedelta, timezone
from typing import Any

from markdown_it import MarkdownIt
from PIL import Image, ImageSequence, UnidentifiedImageError

from app.config import settings
from app.db.postgres import get_pool
from app.exceptions import AKBError, ValidationError
from app.repositories import vault_files_repo
from app.services.adapters import s3_adapter
from app.services.m1_file_measurement import measurement_enabled
from app.services.s3_delete_worker import enqueue_delete


ASSET_URL_PREFIX = "/api/assets/"
IMAGE_ASSET_MAX_BYTES = 10 * 1024 * 1024
# Decode limits are intentionally lower than Pillow's generic bomb threshold.
# The API admits two decoders per worker, so a 12 MP RGBA frame keeps the
# aggregate working set bounded while still accepting 4K and common camera
# images. Animated formats are decoded one frame at a time, with a separate
# total-work bound to prevent a small compressed body monopolising a worker.
IMAGE_ASSET_MAX_PIXELS = 12_000_000
IMAGE_ASSET_MAX_DIMENSION = 8_192
IMAGE_ASSET_MAX_FRAMES = 200
IMAGE_ASSET_MAX_TOTAL_FRAME_PIXELS = 48_000_000

_ASSET_ID = r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
_ASSET_URL_RE = re.compile(
    rf"^{re.escape(ASSET_URL_PREFIX)}(?P<id>{_ASSET_ID})/?$"
)
_MARKDOWN_PARSER = MarkdownIt("commonmark")

_IMAGE_FORMATS = {
    "PNG": ("image/png", ".png"),
    "JPEG": ("image/jpeg", ".jpg"),
    "GIF": ("image/gif", ".gif"),
    "WEBP": ("image/webp", ".webp"),
}
_MIME_EXTENSIONS = {mime: extension for mime, extension in _IMAGE_FORMATS.values()}
_ALLOWED_MIMES = frozenset(_MIME_EXTENSIONS)
logger = logging.getLogger("akb.assets")


def inspect_image(data: bytes) -> tuple[str, int, int]:
    """Decode and verify a bounded raster image, returning MIME + dimensions."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                format_spec = _IMAGE_FORMATS.get(image.format or "")
                if format_spec is None:
                    raise AKBError(
                        "Only PNG, JPEG, GIF, and WebP images are supported",
                        status_code=415,
                    )
                mime, _extension = format_spec
                width, height = image.size
                frame_count = int(getattr(image, "n_frames", 1) or 1)
                if width < 1 or height < 1:
                    raise AKBError("Image dimensions must be positive", status_code=415)
                if (
                    width > IMAGE_ASSET_MAX_DIMENSION
                    or height > IMAGE_ASSET_MAX_DIMENSION
                    or width * height > IMAGE_ASSET_MAX_PIXELS
                ):
                    raise AKBError("Image dimensions are too large", status_code=413)
                if (
                    frame_count > IMAGE_ASSET_MAX_FRAMES
                    or width * height * frame_count > IMAGE_ASSET_MAX_TOTAL_FRAME_PIXELS
                ):
                    raise AKBError("Animated image has too many pixels or frames", status_code=413)
                # ``verify`` checks the container/chunk structure without
                # trusting a signature and trailer alone.
                image.verify()

            # Re-open after verify and force every bounded frame through its
            # decoder. This rejects corrupt compressed payloads before bytes
            # become a durable attachment.
            with Image.open(io.BytesIO(data)) as decoded:
                for frame in ImageSequence.Iterator(decoded):
                    frame.load()
    except AKBError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        EOFError,
        OSError,
        SyntaxError,
        ValueError,
    ) as exc:
        raise AKBError("Invalid or unsupported image encoding", status_code=415) from exc
    return mime, width, height


def extract_asset_ids(markdown: str) -> set[uuid.UUID]:
    """Extract stable asset URLs from images rendered by CommonMark.

    Authorization follows parsed image nodes rather than source-text regexes so
    inline destinations with titles and reference-style images behave exactly
    like their rendered equivalents. Code spans, fenced examples, ordinary
    links, and raw HTML never produce an image token and therefore cannot retain
    or publicly authorize bytes.
    """
    if ASSET_URL_PREFIX not in markdown:
        return set()

    result: set[uuid.UUID] = set()
    pending = list(_MARKDOWN_PARSER.parse(markdown))
    while pending:
        token = pending.pop()
        if token.children:
            pending.extend(token.children)
        if token.type != "image":
            continue
        source = token.attrGet("src")
        if not isinstance(source, str):
            continue
        match = _ASSET_URL_RE.fullmatch(source)
        if match:
            result.add(uuid.UUID(match.group("id")))
    return result


async def extract_asset_ids_async(markdown: str) -> set[uuid.UUID]:
    """Parse a document manifest without occupying the asyncio event loop."""
    if ASSET_URL_PREFIX not in markdown:
        return set()
    return await asyncio.to_thread(extract_asset_ids, markdown)


async def _settle_must_complete_task(task: asyncio.Task[Any]) -> None:
    """Wait for a shielded storage call even after its caller is cancelled.

    Cancelling ``asyncio.to_thread`` only abandons the await; it cannot stop an
    already-running boto3 call. Cleanup waits for the call to finish so the
    final object state is known before deletion is scheduled.
    """
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:
            break
    # Retrieve a storage exception when cancellation and task completion occur
    # together; otherwise asyncio reports an unobserved exception.
    try:
        task.result()
    except BaseException:
        pass


async def claim_document_assets(
    conn,
    *,
    vault_id: uuid.UUID,
    markdown: str,
    strict: bool = True,
    previous_markdown: str | None = None,
) -> set[uuid.UUID]:
    """Validate and retain the generated image references in ``markdown``.

    The vault predicate is part of the database lookup, so copying an asset URL
    from another vault cannot turn it into a readable reference. Updates may
    retain unavailable URLs already present in ``previous_markdown`` but reject
    newly introduced ones; valid local assets are retained. Claiming is
    performed by the caller's document transaction, so a failed Git/PG write
    rolls back with the document's authoritative commit pointer.
    """
    asset_ids = await extract_asset_ids_async(markdown)
    previous_asset_ids = (
        await extract_asset_ids_async(previous_markdown)
        if previous_markdown is not None
        else None
    )
    found = await claim_document_asset_ids(
        conn,
        vault_id=vault_id,
        asset_ids=asset_ids,
        # Existing broken references may remain editable, but newly introduced
        # references are validated below. The transaction rolls this claim back
        # if that validation fails.
        strict=strict and previous_asset_ids is None,
    )
    required = asset_ids if strict else set()
    if previous_asset_ids is not None:
        required = asset_ids - previous_asset_ids
    if not required.issubset(found):
        raise ValidationError(
            "Document contains an unavailable image reference; upload the image to this vault first"
        )
    return found


async def claim_document_asset_ids(
    conn,
    *,
    vault_id: uuid.UUID,
    asset_ids: set[uuid.UUID],
    strict: bool = True,
) -> set[uuid.UUID]:
    """Claim an already-parsed image manifest inside a document transaction."""
    found = await vault_files_repo.claim_attachment_references(
        conn, vault_id, asset_ids, strict=strict,
    )
    if strict and found != asset_ids:
        raise ValidationError(
            "Document contains an unavailable image reference; upload the image to this vault first"
        )
    return found


def _revision_retain_until() -> datetime:
    return datetime.now(timezone.utc) + timedelta(
        days=settings.document_asset_revision_retention_days,
    )


async def sync_document_assets(
    conn,
    *,
    document_id: uuid.UUID,
    vault_id: uuid.UUID,
    document_path: str,
    commit_hash: str,
    asset_ids: set[uuid.UUID],
    previous_commit: str | None = None,
    previous_path: str | None = None,
) -> None:
    """Publish the current image set and its bounded Git manifest."""
    await vault_files_repo.sync_document_asset_references(
        conn,
        document_id=document_id,
        vault_id=vault_id,
        document_path=document_path,
        commit_hash=commit_hash,
        asset_ids=asset_ids,
        retain_until=_revision_retain_until(),
        previous_commit=previous_commit,
        previous_path=previous_path,
    )


async def list_live_document_asset_ids(
    conn,
    *,
    document_id: uuid.UUID,
    vault_id: uuid.UUID,
) -> set[uuid.UUID]:
    """Return the current attachment set through the asset-service boundary."""
    return await vault_files_repo.list_live_document_asset_ids(
        conn,
        document_id=document_id,
        vault_id=vault_id,
    )


async def retain_document_assets_for_delete(
    conn,
    *,
    document_id: uuid.UUID,
    vault_id: uuid.UUID,
    document_path: str,
    commit_hash: str | None,
) -> None:
    """Keep the deleted document's last image-bearing revision temporarily."""
    await vault_files_repo.retain_current_document_assets(
        conn,
        document_id=document_id,
        vault_id=vault_id,
        document_path=document_path,
        commit_hash=commit_hash,
        retain_until=_revision_retain_until(),
    )


def _safe_filename(filename: str, mime: str) -> str:
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch >= " " and ch not in "\x7f/").strip()
    if not name or name in {".", ".."}:
        return f"image{_MIME_EXTENSIONS[mime]}"
    return name[:160]


async def create_image_asset(
    *,
    vault_id: uuid.UUID,
    vault_name: str,
    filename: str,
    declared_mime: str,
    body: bytes,
    actor_id: str,
) -> dict:
    if measurement_enabled():
        raise AKBError("Document images are unavailable in the M1 File measurement mode", status_code=409)
    if not body:
        raise AKBError("Image is empty", status_code=400)
    if len(body) > IMAGE_ASSET_MAX_BYTES:
        raise AKBError("Image exceeds the 10 MB limit", status_code=413)

    # Pillow verifies and fully decodes every bounded frame. Keep that CPU work
    # off the asyncio event loop so a large animated upload cannot stall probes
    # and unrelated API requests. The route-level semaphore bounds concurrent
    # calls into this thread path.
    # Cancelling ``to_thread`` does not stop Pillow. Shield and settle the real
    # decoder task before returning so the route-level admission slot remains
    # an actual concurrency bound even when clients disconnect repeatedly.
    inspect_task = asyncio.create_task(
        asyncio.to_thread(inspect_image, body),
        name="document-image-inspect",
    )
    try:
        actual_mime, width, height = await asyncio.shield(inspect_task)
    except BaseException:
        await _settle_must_complete_task(inspect_task)
        raise
    claimed = declared_mime.split(";", 1)[0].strip().lower()
    if claimed not in _ALLOWED_MIMES or claimed != actual_mime:
        raise AKBError("Image content does not match its declared MIME type", status_code=415)

    file_id = uuid.uuid4()
    safe_name = _safe_filename(filename, actual_mime)
    s3_key = f"{vault_name}/.akb-assets/{file_id}/{safe_name}"
    digest = hashlib.sha256(body).hexdigest()

    await asyncio.to_thread(s3_adapter.ensure_bucket, settings.s3_bucket)
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Revalidate the vault under the same lock used by the transfer
            # phase. This serializes queued uploads with permanent deletion and
            # ensures that every committed pending row is included in the
            # deletion sweep.
            vault_exists = await conn.fetchval(
                "SELECT id FROM vaults WHERE id = $1 FOR KEY SHARE",
                vault_id,
            )
            if vault_exists is None:
                raise AKBError(
                    "Vault was deleted while the image upload was queued",
                    status_code=409,
                )
            await vault_files_repo.insert_pending_attachment(
                conn,
                file_id=file_id,
                vault_id=vault_id,
                name=safe_name,
                s3_key=s3_key,
                mime_type=actual_mime,
                size_bytes=len(body),
                content_hash=digest,
                created_by=actor_id,
            )

    put_task: asyncio.Task[None] | None = None
    try:
        # The pending row is the durable owner of this key. Perform remote I/O
        # without holding a PostgreSQL connection or vault row lock; finalizing
        # below revalidates both after the PUT finishes. Vault deletion records
        # an additional delayed delete for pending keys, so a transfer that was
        # already accepted by object storage cannot outlive its deleted vault.
        put_task = asyncio.create_task(
            asyncio.to_thread(s3_adapter.put_bytes, s3_key, body, actual_mime),
            name=f"document-image-put:{file_id}",
        )
        await asyncio.shield(put_task)

        async with pool.acquire() as conn:
            async with conn.transaction():
                # Serialize final publication with vault deletion. The S3 call
                # has completed, so this transaction contains only bounded DB
                # work and does not pin a pool connection on remote latency.
                vault_exists = await conn.fetchval(
                    "SELECT id FROM vaults WHERE id = $1 FOR KEY SHARE",
                    vault_id,
                )
                if vault_exists is None:
                    raise AKBError(
                        "Vault was deleted while the image upload was starting",
                        status_code=409,
                    )
                finalized = await vault_files_repo.finalize_attachment(
                    conn,
                    file_id=file_id,
                    vault_id=vault_id,
                    s3_key=s3_key,
                )
                if not finalized:
                    raise RuntimeError("pending document image disappeared before finalization")
    except BaseException:
        # ``to_thread`` continues after request/task cancellation. Settle the
        # actual PUT before enqueueing its key so deletion is ordered after the
        # last possible object creation.
        if put_task is not None:
            await _settle_must_complete_task(put_task)
        # A normal failure uses the same transactional outbox as every other
        # object deletion. A hard process exit cannot run this block, but the
        # already-committed pending row lets asset_gc_worker discover and delete
        # that object after the bounded unclaimed TTL.
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    row = await vault_files_repo.delete_failed_pending_attachment(
                        conn,
                        vault_id=vault_id,
                        file_id=file_id,
                        created_by=actor_id,
                    )
                    if row is not None:
                        await enqueue_delete(conn, row["s3_key"])
        except Exception as cleanup_error:  # noqa: BLE001 — preserve the upload failure
            logger.warning(
                "document image cleanup deferred to GC for %s: %s",
                file_id,
                cleanup_error,
            )
        raise

    return {
        "id": str(file_id),
        "url": f"{ASSET_URL_PREFIX}{file_id}",
        "name": safe_name,
        "mime_type": actual_mime,
        "size_bytes": len(body),
        "width": width,
        "height": height,
    }
