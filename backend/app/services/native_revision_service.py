"""Internal M1 native-ledger transaction substrate.

This module intentionally has no public route or compatibility composition.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import secrets
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import asyncpg

from app.exceptions import ConflictError, NotFoundError, ValidationError
from app.repositories.native_revision_repo import NativeRevisionRepository
from app.services.m1_reference_payload_store import (
    M1ReferencePayloadStore,
    PreparedReferencePayload,
    ReferencePayloadIntegrityError,
)


Failpoint = Callable[[str], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class NativeMutationResult:
    resource_id: uuid.UUID
    revision_id: str
    parent_revision_id: str | None
    action: str
    path: str
    payload_manifest_id: uuid.UUID | None
    occurred_at: datetime
    idempotent_replay: bool = False


@dataclass(frozen=True, slots=True)
class NativeRevisionSnapshot:
    resource_id: uuid.UUID
    revision_id: str
    parent_revision_id: str | None
    surface: str
    content_profile: str
    path: str
    action: str
    occurred_at: datetime
    payload_manifest_id: uuid.UUID
    digest: str
    byte_size: int
    encoding: str
    selected_placement: str
    verification_profile: str
    payload_bytes: bytes
    text: str


class NativeRevisionService:
    """Publish one Resource mutation as one PostgreSQL authority transaction."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        repository: NativeRevisionRepository | None = None,
        payload_store: M1ReferencePayloadStore | None = None,
        failpoint: Failpoint | None = None,
    ):
        """Build the internal substrate.

        ``failpoint`` is a deterministic test-only hook; production
        composition must leave it unset.
        """
        self.pool = pool
        self.repository = repository or NativeRevisionRepository(pool)
        self.payload_store = payload_store or M1ReferencePayloadStore(pool)
        self.failpoint = failpoint

    async def _hit(self, name: str) -> None:
        if self.failpoint is None:
            return
        value = self.failpoint(name)
        if inspect.isawaitable(value):
            await value

    @staticmethod
    def _validate_common(
        *,
        surface: str,
        path: str,
        actor: str,
        mutation_id: uuid.UUID,
    ) -> None:
        if surface not in {"document", "file"}:
            raise ValidationError("Native Resource surface must be document or file")
        if not isinstance(path, str) or not path.strip():
            raise ValidationError("Native Resource path must be non-empty")
        if not isinstance(actor, str) or not actor.strip():
            raise ValidationError("Native mutation actor must be non-empty")
        if not isinstance(mutation_id, uuid.UUID):
            raise ValidationError("Native mutation idempotency key must be a UUID")

    @staticmethod
    def _fingerprint(
        *,
        action: str,
        namespace_id: uuid.UUID,
        surface: str,
        path: str,
        payload: PreparedReferencePayload,
        expected_revision_id: str | None,
        actor: str,
        message: str | None,
        subject: str | None,
        summary: str | None,
    ) -> str:
        canonical = json.dumps(
            {
                "action": action,
                "namespace_id": str(namespace_id),
                "surface": surface,
                "path": path,
                "payload_digest": payload.digest,
                "payload_size": payload.byte_size,
                "expected_revision_id": expected_revision_id,
                "actor": actor,
                "message": message,
                "subject": subject,
                "summary": summary,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    @staticmethod
    def _from_row(row: dict, *, replay: bool) -> NativeMutationResult:
        return NativeMutationResult(
            resource_id=row["resource_id"],
            revision_id=row["revision_id"],
            parent_revision_id=row["parent_revision_id"],
            action=row["action"],
            path=row["path"],
            payload_manifest_id=row["payload_manifest_id"],
            occurred_at=row["occurred_at"],
            idempotent_replay=replay,
        )

    @staticmethod
    def _opaque_revision_id() -> str:
        # P0 froze this at random 20 bytes rendered as 40 lowercase hex.
        return secrets.token_hex(20)

    async def _allocate_revision_id(self) -> str:
        """Reject observed collisions and retry with a fresh server token."""
        for _ in range(3):
            candidate = self._opaque_revision_id()
            async with self.pool.acquire() as conn:
                exists = await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM native_revisions WHERE revision_id = $1)",
                    candidate,
                )
            if not exists:
                return candidate
        raise ConflictError("Unable to allocate a unique native Revision ID after collisions")

    async def create_text(
        self,
        *,
        namespace_id: uuid.UUID,
        surface: str,
        path: str,
        payload: str | bytes,
        actor: str,
        mutation_id: uuid.UUID,
        message: str | None = None,
        subject: str | None = None,
        summary: str | None = None,
        expected_digest: str | None = None,
        expected_size: int | None = None,
    ) -> NativeMutationResult:
        self._validate_common(surface=surface, path=path, actor=actor, mutation_id=mutation_id)
        await self._hit("payload.before_prepare")
        prepared = await self.payload_store.prepare_text(
            namespace_id=namespace_id,
            payload=payload,
            expected_digest=expected_digest,
            expected_size=expected_size,
        )
        await self._hit("payload.after_verified")
        fingerprint = self._fingerprint(
            action="create",
            namespace_id=namespace_id,
            surface=surface,
            path=path,
            payload=prepared,
            expected_revision_id=None,
            actor=actor,
            message=message,
            subject=subject,
            summary=summary,
        )
        result = await self._publish_create(
            namespace_id=namespace_id,
            surface=surface,
            path=path,
            actor=actor,
            mutation_id=mutation_id,
            message=message,
            subject=subject,
            summary=summary,
            prepared=prepared,
            fingerprint=fingerprint,
        )
        await self._hit("authority.after_commit_before_response")
        return result

    async def _publish_create(
        self,
        *,
        namespace_id: uuid.UUID,
        surface: str,
        path: str,
        actor: str,
        mutation_id: uuid.UUID,
        message: str | None,
        subject: str | None,
        summary: str | None,
        prepared: PreparedReferencePayload,
        fingerprint: str,
    ) -> NativeMutationResult:
        resource_id = uuid.uuid4()
        revision_id = await self._allocate_revision_id()
        manifest_id = uuid.uuid4()
        activity_id = uuid.uuid4()
        intent_id = uuid.uuid4()
        occurred_at = datetime.now(UTC)
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    await self.repository.lock_mutation(conn, namespace_id, mutation_id)
                    prior = await self.repository.find_mutation(conn, namespace_id, mutation_id)
                    if prior is not None:
                        if prior["request_fingerprint"] != fingerprint:
                            raise ConflictError("Native idempotency key was reused with different input")
                        return self._from_row(prior, replay=True)

                    await self.repository.lock_paths(conn, namespace_id, surface, path)
                    if await self.repository.find_live_path(conn, namespace_id, surface, path) is not None:
                        raise ConflictError(f"Native Resource already exists at path: {path}")

                    await self.repository.insert_resource(
                        conn,
                        resource_id=resource_id,
                        namespace_id=namespace_id,
                        surface=surface,
                        content_profile="text",
                        path=path,
                        occurred_at=occurred_at,
                    )
                    await self._hit("authority.after_resource")
                    await self.repository.insert_manifest(
                        conn,
                        payload_manifest_id=manifest_id,
                        namespace_id=namespace_id,
                        resource_id=resource_id,
                        payload=prepared,
                        occurred_at=occurred_at,
                    )
                    await self._hit("authority.after_manifest")
                    await self.repository.insert_revision(
                        conn,
                        revision_id=revision_id,
                        namespace_id=namespace_id,
                        resource_id=resource_id,
                        parent_revision_id=None,
                        action="create",
                        path=path,
                        path_from=None,
                        path_to=path,
                        payload_manifest_id=manifest_id,
                        mutation_id=mutation_id,
                        request_fingerprint=fingerprint,
                        message=message,
                        subject=subject,
                        summary=summary,
                        actor=actor,
                        occurred_at=occurred_at,
                        activity_event_id=activity_id,
                        invalidation_intent_id=intent_id,
                    )
                    await self._hit("authority.after_revision")
                    await self.repository.set_head(
                        conn,
                        resource_id=resource_id,
                        revision_id=revision_id,
                        path=path,
                        lifecycle="live",
                        occurred_at=occurred_at,
                    )
                    await self._hit("authority.after_head")
                    await self.repository.insert_activity(
                        conn,
                        activity_event_id=activity_id,
                        namespace_id=namespace_id,
                        resource_id=resource_id,
                        revision_id=revision_id,
                        action="create",
                        actor=actor,
                        subject=subject,
                        summary=summary,
                        path_from=None,
                        path_to=path,
                        occurred_at=occurred_at,
                    )
                    await self._hit("authority.after_activity")
                    await self.repository.insert_invalidation_intent(
                        conn,
                        intent_id=intent_id,
                        namespace_id=namespace_id,
                        resource_id=resource_id,
                        revision_id=revision_id,
                        reason="create",
                        occurred_at=occurred_at,
                    )
                    await self._hit("authority.after_invalidation")
        except asyncpg.UniqueViolationError as exc:
            if exc.constraint_name == "uq_native_resources_live_path":
                raise ConflictError(f"Native Resource already exists at path: {path}") from exc
            raise
        return NativeMutationResult(
            resource_id=resource_id,
            revision_id=revision_id,
            parent_revision_id=None,
            action="create",
            path=path,
            payload_manifest_id=manifest_id,
            occurred_at=occurred_at,
        )

    async def replace_text(
        self,
        *,
        namespace_id: uuid.UUID,
        surface: str,
        path: str,
        payload: str | bytes,
        actor: str,
        mutation_id: uuid.UUID,
        expected_revision_id: str | None = None,
        message: str | None = None,
        subject: str | None = None,
        summary: str | None = None,
        expected_digest: str | None = None,
        expected_size: int | None = None,
    ) -> NativeMutationResult:
        self._validate_common(surface=surface, path=path, actor=actor, mutation_id=mutation_id)
        if expected_revision_id is not None and (
            len(expected_revision_id) != 40
            or any(ch not in "0123456789abcdef" for ch in expected_revision_id)
        ):
            raise ValidationError("Expected native Revision ID must be exactly 40 lowercase hex")
        await self._hit("payload.before_prepare")
        prepared = await self.payload_store.prepare_text(
            namespace_id=namespace_id,
            payload=payload,
            expected_digest=expected_digest,
            expected_size=expected_size,
        )
        await self._hit("payload.after_verified")
        fingerprint = self._fingerprint(
            action="replace",
            namespace_id=namespace_id,
            surface=surface,
            path=path,
            payload=prepared,
            expected_revision_id=expected_revision_id,
            actor=actor,
            message=message,
            subject=subject,
            summary=summary,
        )
        result = await self._publish_replace(
            namespace_id=namespace_id,
            surface=surface,
            path=path,
            actor=actor,
            mutation_id=mutation_id,
            expected_revision_id=expected_revision_id,
            message=message,
            subject=subject,
            summary=summary,
            prepared=prepared,
            fingerprint=fingerprint,
        )
        await self._hit("authority.after_commit_before_response")
        return result

    async def _publish_replace(
        self,
        *,
        namespace_id: uuid.UUID,
        surface: str,
        path: str,
        actor: str,
        mutation_id: uuid.UUID,
        expected_revision_id: str | None,
        message: str | None,
        subject: str | None,
        summary: str | None,
        prepared: PreparedReferencePayload,
        fingerprint: str,
    ) -> NativeMutationResult:
        revision_id = await self._allocate_revision_id()
        manifest_id = uuid.uuid4()
        activity_id = uuid.uuid4()
        intent_id = uuid.uuid4()
        occurred_at = datetime.now(UTC)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await self.repository.lock_mutation(conn, namespace_id, mutation_id)
                prior = await self.repository.find_mutation(conn, namespace_id, mutation_id)
                if prior is not None:
                    if prior["request_fingerprint"] != fingerprint:
                        raise ConflictError("Native idempotency key was reused with different input")
                    return self._from_row(prior, replay=True)

                await self.repository.lock_paths(conn, namespace_id, surface, path)
                resource = await self.repository.find_live_path(
                    conn, namespace_id, surface, path, for_update=True
                )
                if resource is None:
                    raise NotFoundError("Native Resource", path)
                parent_revision_id = resource["head_revision_id"]
                if expected_revision_id is not None and expected_revision_id != parent_revision_id:
                    raise ConflictError(
                        "Native Revision conflict: expected "
                        f"{expected_revision_id}, current Head is {parent_revision_id}"
                    )
                resource_id = resource["resource_id"]

                await self.repository.insert_manifest(
                    conn,
                    payload_manifest_id=manifest_id,
                    namespace_id=namespace_id,
                    resource_id=resource_id,
                    payload=prepared,
                    occurred_at=occurred_at,
                )
                await self._hit("authority.after_manifest")
                await self.repository.insert_revision(
                    conn,
                    revision_id=revision_id,
                    namespace_id=namespace_id,
                    resource_id=resource_id,
                    parent_revision_id=parent_revision_id,
                    action="replace",
                    path=path,
                    path_from=None,
                    path_to=None,
                    payload_manifest_id=manifest_id,
                    mutation_id=mutation_id,
                    request_fingerprint=fingerprint,
                    message=message,
                    subject=subject,
                    summary=summary,
                    actor=actor,
                    occurred_at=occurred_at,
                    activity_event_id=activity_id,
                    invalidation_intent_id=intent_id,
                )
                await self._hit("authority.after_revision")
                await self.repository.set_head(
                    conn,
                    resource_id=resource_id,
                    revision_id=revision_id,
                    path=path,
                    lifecycle="live",
                    occurred_at=occurred_at,
                )
                await self._hit("authority.after_head")
                await self.repository.insert_activity(
                    conn,
                    activity_event_id=activity_id,
                    namespace_id=namespace_id,
                    resource_id=resource_id,
                    revision_id=revision_id,
                    action="replace",
                    actor=actor,
                    subject=subject,
                    summary=summary,
                    path_from=None,
                    path_to=None,
                    occurred_at=occurred_at,
                )
                await self._hit("authority.after_activity")
                await self.repository.insert_invalidation_intent(
                    conn,
                    intent_id=intent_id,
                    namespace_id=namespace_id,
                    resource_id=resource_id,
                    revision_id=revision_id,
                    reason="replace",
                    occurred_at=occurred_at,
                )
                await self._hit("authority.after_invalidation")
        return NativeMutationResult(
            resource_id=resource_id,
            revision_id=revision_id,
            parent_revision_id=parent_revision_id,
            action="replace",
            path=path,
            payload_manifest_id=manifest_id,
            occurred_at=occurred_at,
        )

    async def get_current(
        self,
        *,
        namespace_id: uuid.UUID,
        surface: str,
        path: str,
    ) -> NativeRevisionSnapshot:
        row = await self.repository.get_current(
            namespace_id=namespace_id,
            surface=surface,
            path=path,
        )
        if row is None:
            raise NotFoundError("Native Resource", path)
        if row["payload_manifest_id"] is None or row["private_locator"] is None:
            raise ReferencePayloadIntegrityError("Live native Head does not pin a payload manifest")
        payload_bytes = await self.payload_store.open_verified(row["private_locator"])
        if len(payload_bytes) != row["byte_size"]:
            raise ReferencePayloadIntegrityError("Head manifest byte size mismatch")
        if hashlib.sha256(payload_bytes).hexdigest() != row["digest"]:
            raise ReferencePayloadIntegrityError("Head manifest digest mismatch")
        text = payload_bytes.decode(row["encoding"], errors="strict")
        return NativeRevisionSnapshot(
            resource_id=row["resource_id"],
            revision_id=row["revision_id"],
            parent_revision_id=row["parent_revision_id"],
            surface=row["surface"],
            content_profile=row["content_profile"],
            path=row["path"],
            action=row["action"],
            occurred_at=row["occurred_at"],
            payload_manifest_id=row["payload_manifest_id"],
            digest=row["digest"],
            byte_size=row["byte_size"],
            encoding=row["encoding"],
            selected_placement=row["selected_placement"],
            verification_profile=row["verification_profile"],
            payload_bytes=payload_bytes,
            text=text,
        )
