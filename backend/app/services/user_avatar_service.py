"""Private, self-managed profile photos with revision CAS and durable cleanup."""

from __future__ import annotations

import asyncio
import io
import uuid
import warnings
import time

from PIL import Image, ImageOps, UnidentifiedImageError

from app.config import settings
from app.db.postgres import get_pool
from app.exceptions import AKBError
from app.models.user_avatar import AvatarCapability, AvatarState
from app.repositories import user_avatars_repo as repo, vault_files_repo
from app.services.adapters import s3_adapter
from app.services.auth_service import AuthenticatedUser
from app.services.image_decode import run_bounded_decode
from app.services.s3_delete_worker import cancel_delete, enqueue_delete

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_OUTPUT_BYTES = 256 * 1024
MAX_PIXELS = 12_000_000
MAX_DIMENSION = 8_192
ALLOWED_CONTENT_TYPES = ["image/jpeg", "image/png", "image/webp"]
_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


def carrier_reason(user: AuthenticatedUser) -> str | None:
    if (user.auth_method not in {"jwt", "browser_session"} or user.account_kind != "human"
            or user.token_id is not None or user.key_class is not None
            or user.token_scopes is not None
            or (user.auth_method == "jwt" and user.oauth_scopes is not None)):
        return "human_session_required"
    return None


def require_carrier(user: AuthenticatedUser) -> None:
    if reason := carrier_reason(user):
        raise AKBError("Sign in with a human browser session to manage your photo.", 403, code=reason)


def capability(user: AuthenticatedUser) -> AvatarCapability:
    reason = carrier_reason(user)
    if reason is None and not settings.object_storage_enabled:
        reason = "avatar_storage_unavailable"
    return AvatarCapability(can_upload=reason is None, can_remove=reason is None, reason=reason,
                            max_upload_bytes=MAX_UPLOAD_BYTES,
                            allowed_content_types=ALLOWED_CONTENT_TYPES)


def avatar_state(row) -> AvatarState:
    if row is None:
        return AvatarState()
    if row["object_key"] is None:
        return AvatarState(revision=row["revision"])
    revision = row["revision"]
    return AvatarState(revision=revision, url=f"/api/v1/auth/me/avatar?v={revision}",
                       width=row["width"], height=row["height"])


def parse_expected_revision(value: str) -> uuid.UUID | None:
    if value == "none":
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        raise AKBError("Use the current photo revision, or 'none' for the first photo.", 422,
                       code="avatar_revision_invalid") from None


def require_revision(row, expected: uuid.UUID | None) -> None:
    current = None if row is None else row["revision"]
    if expected != current:
        raise AKBError("Your profile photo changed. Reload it before saving again.", 409,
                       code="avatar_changed")


async def _lock_active_user(conn, user: AuthenticatedUser, *, lock: str = "UPDATE"):
    # The only lock variants are supplied internally, never by a request.
    if lock not in {"UPDATE", "SHARE"}:
        raise ValueError("Invalid user lock")
    if user.auth_method == "browser_session":
        from app.services.sso_session_epoch import current_sso_session_authority, lock_active_sso_session_epoch
        authority = current_sso_session_authority()
        if (user.browser_session_id is None or user.browser_session_epoch != authority.session_epoch
                or user.browser_session_runtime_generation != authority.runtime_generation):
            raise AKBError("This browser session is no longer active.", 401, code="account_session_expired")
        # Authority comes first, matching session resolution/logout/epoch
        # reconciliation; never pin a stale instance to a newer session epoch.
        await lock_active_sso_session_epoch(conn, authority)
    query = ("SELECT * FROM users WHERE id=$1 FOR UPDATE" if lock == "UPDATE"
             else "SELECT * FROM users WHERE id=$1 FOR SHARE")
    row = await conn.fetchrow(query, uuid.UUID(user.user_id))
    if row is None or row["account_status"] != "active":
        raise AKBError("This account session is no longer active.", 401, code="account_session_expired")
    if row["account_kind"] != "human":
        raise AKBError("Profile photos require a human account.", 403, code="human_session_required")
    if row["credential_change_required"]:
        raise AKBError("Change your issued password first.", 403, code="credential_change_required")
    provider = "local" if user.auth_method == "jwt" else "keycloak"
    if row["auth_provider"] != provider:
        raise AKBError("The account sign-in method changed. Sign in again.", 401,
                       code="account_session_expired")
    if user.auth_method == "jwt":
        if (user.local_session_generation is None
                or user.local_session_generation != row["session_generation"]
                or user.local_session_expires_at is None or user.local_session_expires_at <= time.time()):
            raise AKBError("This browser session is no longer active.", 401, code="account_session_expired")
    else:
        live_session = await conn.fetchrow("""
            SELECT s.id FROM sso_browser_sessions s
            JOIN external_identities e ON e.id=s.external_identity_id AND e.user_id=s.user_id
            WHERE s.id=$1 AND s.user_id=$2 AND s.session_epoch=$3
              AND s.identity_issuer=$4 AND e.issuer=s.identity_issuer AND e.subject=s.identity_subject
              AND s.idle_expires_at>clock_timestamp() AND s.absolute_expires_at>clock_timestamp()
              AND s.refresh_expires_at>clock_timestamp()
            FOR SHARE OF s
        """, user.browser_session_id, uuid.UUID(user.user_id), user.browser_session_epoch, settings.keycloak_issuer)
        if live_session is None:
            raise AKBError("This browser session is no longer active.", 401, code="account_session_expired")
    return row


def _require_storage() -> None:
    if not settings.object_storage_enabled:
        raise AKBError("Profile photo storage is not configured on this server.", 503,
                       code="avatar_storage_unavailable")


async def profile_metadata(user: AuthenticatedUser) -> dict:
    caps = capability(user)
    if carrier_reason(user):
        # Never expose private browser assets via PAT/OAuth/service principals.
        return {"avatar": AvatarState().model_dump(),
                "profile_capabilities": {"avatar": caps.model_dump()}}
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _lock_active_user(conn, user, lock="SHARE")
            state = avatar_state(await repo.current(conn, uuid.UUID(user.user_id)))
    return {"avatar": state.model_dump(exclude_none=False),
            "profile_capabilities": {"avatar": caps.model_dump()}}


async def preflight_write(user: AuthenticatedUser, expected: uuid.UUID | None) -> None:
    require_carrier(user)
    _require_storage()
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _lock_active_user(conn, user)
            require_revision(await repo.current(conn, uuid.UUID(user.user_id)), expected)
            allowed = await repo.consume_write_budget(conn, uuid.UUID(user.user_id))
    if not allowed:
        error = AKBError("Too many photo changes. Try again in a minute.", 429,
                         code="avatar_rate_limited")
        error.retry_after_secs = 60  # type: ignore[attr-defined]
        raise error


def normalize_photo(body: bytes, declared_mime: str) -> bytes:
    if not body:
        raise AKBError("Choose a non-empty image.", 415, code="avatar_unsupported_image")
    if len(body) > MAX_UPLOAD_BYTES:
        raise AKBError("Choose an image no larger than 10 MiB.", 413, code="avatar_too_large")
    declared = declared_mime.split(";", 1)[0].strip().lower()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(body)) as source:
                actual = _FORMATS.get(source.format or "")
                if actual is None or actual != declared:
                    raise AKBError("Choose a JPEG, PNG, or still WebP image.", 415,
                                   code="avatar_unsupported_image")
                width, height = source.size
                if (width < 1 or height < 1 or width > MAX_DIMENSION or height > MAX_DIMENSION
                        or width * height > MAX_PIXELS):
                    raise AKBError("Reduce the image resolution (maximum 12 MP and 8192px per side).", 413,
                                   code="avatar_dimensions_exceeded")
                if getattr(source, "n_frames", 1) != 1 or getattr(source, "is_animated", False):
                    raise AKBError("Animated images are not supported for profile photos.", 415,
                                   code="avatar_unsupported_image")
                source.verify()
            with Image.open(io.BytesIO(body)) as source:
                source.load()
                # Avoid three full-resolution copies per decoder. Pillow can
                # transpose in place, and ordinary RGB/RGBA needs no conversion.
                ImageOps.exif_transpose(source, in_place=True)
                mode = "RGBA" if "A" in source.getbands() or "transparency" in source.info else "RGB"
                converted = source if source.mode == mode else source.convert(mode)
                fitted = ImageOps.fit(converted, (512, 512), method=Image.Resampling.LANCZOS)
                # Fresh pixels guarantee that EXIF/GPS/XMP/ICC are not retained.
                clean = Image.new(mode, fitted.size)
                clean.paste(fitted)
                for quality in (85, 75, 60, 40):
                    output = io.BytesIO()
                    clean.save(output, format="WEBP", quality=quality, method=4)
                    encoded = output.getvalue()
                    if len(encoded) <= MAX_OUTPUT_BYTES:
                        return encoded
    except AKBError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise AKBError("Reduce the image resolution before uploading.", 413,
                       code="avatar_dimensions_exceeded") from None
    except (UnidentifiedImageError, OSError, EOFError, SyntaxError, ValueError):
        raise AKBError("This image could not be decoded. Choose a JPEG, PNG, or still WebP.", 415,
                       code="avatar_unsupported_image") from None
    raise AKBError("This image cannot be reduced to the supported size.", 413, code="avatar_too_large")


async def _settle(task: asyncio.Task) -> None:
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:
            break
    if not task.cancelled():
        task.exception()


async def save_photo(user: AuthenticatedUser, expected: uuid.UUID | None,
                     body: bytes, declared_mime: str) -> AvatarState:
    require_carrier(user)
    _require_storage()
    try:
        # The deadline cancels the request, not the native decoder. The shared
        # helper settles its real thread before returning the capacity slot.
        async with asyncio.timeout(20):
            encoded = await run_bounded_decode(normalize_photo, body, declared_mime, admission_timeout=5)
    except TimeoutError:
        raise AKBError("Photo processing is busy. Try again shortly.", 503,
                       code="avatar_processing_timeout") from None
    uid, revision = uuid.UUID(user.user_id), uuid.uuid4()
    key = f"{repo.AVATAR_KEY_PREFIX}{uid}/{revision}.webp"
    pool = await get_pool()
    # A durable delayed cleanup exists BEFORE any remote write. A second
    # reconciliation also covers a PUT that outlives a lost DB connection.
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _lock_active_user(conn, user)
            require_revision(await repo.current(conn, uid), expected)
            cleanup_ids = [await enqueue_delete(conn, key, delay_seconds=3600),
                           await enqueue_delete(conn, key, delay_seconds=86400)]
    async with pool.acquire() as conn:
        locked = await vault_files_repo.try_lock_s3_key_for_cleanup(conn, key)
        if not locked:
            raise AKBError("Photo storage is busy. Try again.", 503, code="avatar_storage_busy")
        try:
            task = asyncio.create_task(asyncio.to_thread(s3_adapter.put_private_bytes, key, encoded, "image/webp"))
            try:
                await asyncio.shield(task)
            except BaseException:
                await _settle(task)
                raise
            # No transaction spans the remote PUT. User locking serializes
            # first-write/replace/remove/account-delete and final state checks.
            async with conn.transaction():
                await _lock_active_user(conn, user)
                old = await repo.current(conn, uid)
                require_revision(old, expected)
                await repo.publish(conn, uid, revision, key, len(encoded))
                for cleanup_id in cleanup_ids:
                    await cancel_delete(conn, cleanup_id)
                if old is not None and old["object_key"]:
                    await enqueue_delete(conn, old["object_key"])
        except s3_adapter.StorageError:
            raise AKBError("Photo storage is temporarily unavailable. Try again.", 503,
                           code="avatar_storage_unavailable") from None
        finally:
            await vault_files_repo.unlock_s3_key_after_cleanup(conn, key)
    return AvatarState(revision=revision, url=f"/api/v1/auth/me/avatar?v={revision}", width=512, height=512)


async def remove_photo(user: AuthenticatedUser, expected: uuid.UUID | None) -> AvatarState:
    require_carrier(user)
    _require_storage()
    uid = uuid.UUID(user.user_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _lock_active_user(conn, user)
            row = await repo.current(conn, uid)
            require_revision(row, expected)
            if row is None or row["object_key"] is None:
                return avatar_state(row)
            revision = uuid.uuid4()
            await repo.publish(conn, uid, revision, None)
            await enqueue_delete(conn, row["object_key"])
    return AvatarState(revision=revision)


async def read_photo(user: AuthenticatedUser, revision: uuid.UUID) -> bytes:
    require_carrier(user)
    _require_storage()
    uid = uuid.UUID(user.user_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _lock_active_user(conn, user, lock="SHARE")
            row = await repo.current(conn, uid)
            if row is None or row["revision"] != revision or row["object_key"] is None:
                raise AKBError("Profile photo not found.", 404, code="avatar_not_found")
            key = row["object_key"]
    try:
        body = await asyncio.to_thread(s3_adapter.get_bounded_bytes, key, MAX_OUTPUT_BYTES)
    except (s3_adapter.StorageError, AKBError):
        raise AKBError("Photo storage is temporarily unavailable. Try again.", 503,
                       code="avatar_storage_unavailable") from None
    # A delayed download must not revive a photo removed/replaced meanwhile.
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _lock_active_user(conn, user, lock="SHARE")
            current = await repo.current(conn, uid)
            if current is None or current["revision"] != revision or current["object_key"] != key:
                raise AKBError("Profile photo not found.", 404, code="avatar_not_found")
    return body
