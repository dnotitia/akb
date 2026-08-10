"""Document image assets stored as hidden, vault-owned object-store files."""

from __future__ import annotations

import asyncio
import hashlib
import io
import re
import uuid
import warnings

from PIL import Image, ImageSequence, UnidentifiedImageError

from app.config import settings
from app.db.postgres import get_pool
from app.exceptions import AKBError, ValidationError
from app.repositories import vault_files_repo
from app.services.adapters import s3_adapter
from app.services.m1_file_measurement import measurement_enabled


ASSET_URL_PREFIX = "/api/assets/"
IMAGE_ASSET_MAX_BYTES = 10 * 1024 * 1024
IMAGE_ASSET_MAX_PIXELS = 40_000_000
IMAGE_ASSET_MAX_DIMENSION = 16_384
IMAGE_ASSET_MAX_FRAMES = 300
IMAGE_ASSET_MAX_TOTAL_FRAME_PIXELS = 80_000_000

_ASSET_ID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}"
_MARKDOWN_ASSET_RE = re.compile(
    rf"!\[[^\n]*?\]\(\s*<?{re.escape(ASSET_URL_PREFIX)}(?P<id>{_ASSET_ID})>?\s*\)"
)
_FENCE_RE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})")

_FORMAT_MIMES = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "GIF": "image/gif",
    "WEBP": "image/webp",
}
_ALLOWED_MIMES = frozenset(_FORMAT_MIMES.values())


def inspect_image(data: bytes) -> tuple[str, int, int]:
    """Decode and verify a bounded raster image, returning MIME + dimensions."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                mime = _FORMAT_MIMES.get(image.format or "")
                if mime is None:
                    raise AKBError(
                        "Only PNG, JPEG, GIF, and WebP images are supported",
                        status_code=415,
                    )
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


def _strip_inline_code(line: str) -> str:
    """Remove CommonMark-style code spans before looking for image syntax."""
    out: list[str] = []
    i = 0
    while i < len(line):
        if line[i] != "`":
            out.append(line[i])
            i += 1
            continue
        run = 1
        while i + run < len(line) and line[i + run] == "`":
            run += 1
        closing = line.find("`" * run, i + run)
        if closing < 0:
            out.append(line[i:i + run])
            i += run
        else:
            out.append(" " * (closing + run - i))
            i = closing + run
    return "".join(out)


def extract_asset_ids(markdown: str) -> set[uuid.UUID]:
    """Extract generated asset references, excluding fenced/inline code.

    This parser is deliberately conservative: only the exact inline image form
    emitted by the editor authorizes public bytes.  Unsupported Markdown forms
    fail closed instead of accidentally widening a publication's asset set.
    """
    result: set[uuid.UUID] = set()
    fence_char: str | None = None
    fence_len = 0
    for line in markdown.splitlines():
        fence = _FENCE_RE.match(line)
        if fence:
            token = fence.group("fence")
            if fence_char is None:
                fence_char, fence_len = token[0], len(token)
                continue
            if token[0] == fence_char and len(token) >= fence_len:
                fence_char, fence_len = None, 0
                continue
        if fence_char is not None:
            continue
        for match in _MARKDOWN_ASSET_RE.finditer(_strip_inline_code(line)):
            result.add(uuid.UUID(match.group("id")))
    return result


async def claim_document_assets(
    conn,
    *,
    vault_id: uuid.UUID,
    markdown: str,
    strict: bool = True,
) -> set[uuid.UUID]:
    """Validate and retain the generated image references in ``markdown``.

    The vault predicate is part of the database lookup, so copying an asset URL
    from another vault cannot turn it into a readable reference.  Claiming is
    performed by the caller's document transaction: if the Git/PG write fails,
    the claim rolls back with the document's authoritative commit pointer.
    """
    referenced = extract_asset_ids(markdown)
    found = await vault_files_repo.claim_attachment_references(
        conn, vault_id, referenced, strict=strict,
    )
    if strict and found != referenced:
        raise ValidationError(
            "Document contains an unavailable image reference; upload the image to this vault first"
        )
    return found


def _safe_filename(filename: str, mime: str) -> str:
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch >= " " and ch not in "\x7f/").strip()
    if not name:
        ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}[mime]
        return f"image{ext}"
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

    actual_mime, width, height = inspect_image(body)
    claimed = declared_mime.split(";", 1)[0].strip().lower()
    if claimed not in _ALLOWED_MIMES or claimed != actual_mime:
        raise AKBError("Image content does not match its declared MIME type", status_code=415)

    file_id = uuid.uuid4()
    safe_name = _safe_filename(filename, actual_mime)
    s3_key = f"{vault_name}/.akb-assets/{file_id}/{safe_name}"
    digest = hashlib.sha256(body).hexdigest()

    await asyncio.to_thread(s3_adapter.ensure_bucket, settings.s3_bucket)
    await asyncio.to_thread(s3_adapter.put_bytes, s3_key, body, actual_mime)

    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await vault_files_repo.insert_attachment(
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
    except Exception:
        # The per-file key is unique, so immediate cleanup cannot delete a
        # different row's bytes (unlike content-addressed shared keys).
        try:
            await asyncio.to_thread(s3_adapter.delete, s3_key)
        except Exception:  # noqa: BLE001 — preserve the database failure
            pass
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
