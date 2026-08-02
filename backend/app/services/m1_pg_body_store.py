"""Explicit PostgreSQL BYTEA BodyStore candidate for the M1 B-text arm.

This module is measurement-only.  It proves one canonical UTF-8 byte
representation, manifest binding, content-addressed deduplication, and verified
open semantics without changing AKB's default revision backend.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass

import asyncpg

from app.exceptions import ValidationError
from app.services.m1_reference_payload_store import PreparedReferencePayload


class PgBodyIntegrityError(RuntimeError):
    """A persisted body no longer agrees with its manifest facts."""


M1_PG_TEXT_MAX_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class VerifiedPgTextBody:
    """Receipt-quality facts returned only after byte-level verification."""

    payload_id: uuid.UUID
    namespace_id: uuid.UUID
    digest: str
    byte_size: int
    canonical_bytes: bytes
    selected_placement: str
    verification_profile: str


class M1PgBodyStore:
    """Prepare and verify canonical text bytes in PostgreSQL."""

    selected_placement = "pg-bodystore-v1"
    verification_profile = "sha256-size-utf8-v1"

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    @staticmethod
    def _verified_bytes(
        payload: str | bytes,
        *,
        expected_digest: str | None = None,
        expected_size: int | None = None,
    ) -> tuple[bytes, str]:
        if isinstance(payload, str):
            if len(payload) > M1_PG_TEXT_MAX_BYTES:
                raise ValidationError("PostgreSQL text body exceeds the 10 MiB limit")
            canonical = payload.encode("utf-8")
        elif isinstance(payload, bytes):
            canonical = payload
        else:
            raise ValidationError("PostgreSQL body must be str or bytes")
        if len(canonical) > M1_PG_TEXT_MAX_BYTES:
            raise ValidationError("PostgreSQL text body exceeds the 10 MiB limit")
        try:
            canonical.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValidationError("PostgreSQL text body must be valid UTF-8") from exc
        if b"\x00" in canonical:
            raise ValidationError("PostgreSQL text body must not contain NUL bytes")

        digest = hashlib.sha256(canonical).hexdigest()
        if expected_digest is not None and expected_digest != digest:
            raise ValidationError("PostgreSQL body digest does not match expected digest")
        if expected_size is not None and expected_size != len(canonical):
            raise ValidationError("PostgreSQL body size does not match expected size")
        return canonical, digest

    async def prepare_text(
        self,
        *,
        namespace_id: uuid.UUID,
        payload: str | bytes,
        expected_digest: str | None = None,
        expected_size: int | None = None,
    ) -> PreparedReferencePayload:
        canonical, digest = await asyncio.to_thread(
            self._verified_bytes,
            payload,
            expected_digest=expected_digest,
            expected_size=expected_size,
        )
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO m1_reference_payloads (
                    namespace_id, content_profile, digest, byte_size, encoding,
                    selected_placement, verification_profile, canonical_bytes
                )
                VALUES ($1, 'text', $2, $3, 'utf-8', $4, $5, $6)
                ON CONFLICT (namespace_id, digest, byte_size) DO NOTHING
                RETURNING payload_id, namespace_id, content_profile, digest,
                          byte_size, encoding, selected_placement,
                          verification_profile, canonical_bytes
                """,
                namespace_id,
                digest,
                len(canonical),
                self.selected_placement,
                self.verification_profile,
                canonical,
            )
            if row is None:
                row = await conn.fetchrow(
                    """
                    SELECT payload_id, namespace_id, content_profile, digest,
                           byte_size, encoding, selected_placement,
                           verification_profile, canonical_bytes
                      FROM m1_reference_payloads
                     WHERE namespace_id = $1
                       AND digest = $2
                       AND byte_size = $3
                       AND selected_placement = $4
                    """,
                    namespace_id,
                    digest,
                    len(canonical),
                    self.selected_placement,
                )
        if row is None:
            raise PgBodyIntegrityError(
                "namespace already contains the same body under a different measurement placement"
            )
        await asyncio.to_thread(self._verify_row, row, expected=canonical)
        return PreparedReferencePayload(
            payload_id=row["payload_id"],
            namespace_id=row["namespace_id"],
            content_profile=row["content_profile"],
            digest=row["digest"],
            byte_size=row["byte_size"],
            encoding=row["encoding"],
            selected_placement=row["selected_placement"],
            verification_profile=row["verification_profile"],
        )

    @classmethod
    def _verify_row(cls, row, *, expected: bytes | None = None) -> bytes:
        canonical = bytes(row["canonical_bytes"])
        if expected is not None and canonical != expected:
            raise PgBodyIntegrityError("PostgreSQL body bytes changed after preparation")
        if len(canonical) != row["byte_size"]:
            raise PgBodyIntegrityError("PostgreSQL body byte size mismatch")
        if hashlib.sha256(canonical).hexdigest() != row["digest"]:
            raise PgBodyIntegrityError("PostgreSQL body digest mismatch")
        if row["encoding"] != "utf-8":
            raise PgBodyIntegrityError("PostgreSQL body encoding mismatch")
        try:
            canonical.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise PgBodyIntegrityError("PostgreSQL body is not valid UTF-8") from exc
        if b"\x00" in canonical:
            raise PgBodyIntegrityError("PostgreSQL body contains NUL bytes")
        if row["selected_placement"] != cls.selected_placement:
            raise PgBodyIntegrityError("PostgreSQL body placement mismatch")
        if row["verification_profile"] != cls.verification_profile:
            raise PgBodyIntegrityError("PostgreSQL body verification profile mismatch")
        return canonical

    @classmethod
    def _receipt_from_row(cls, row) -> VerifiedPgTextBody:
        canonical = cls._verify_row(row)
        return VerifiedPgTextBody(
            payload_id=row["payload_id"],
            namespace_id=row["namespace_id"],
            digest=row["digest"],
            byte_size=row["byte_size"],
            canonical_bytes=canonical,
            selected_placement=row["selected_placement"],
            verification_profile=row["verification_profile"],
        )

    async def open_verified(self, payload_id: uuid.UUID) -> bytes:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT payload_id, namespace_id, content_profile, digest,
                       byte_size, encoding, selected_placement,
                       verification_profile, canonical_bytes
                  FROM m1_reference_payloads
                 WHERE payload_id = $1
                   AND selected_placement = $2
                """,
                payload_id,
                self.selected_placement,
            )
        if row is None:
            raise PgBodyIntegrityError(f"PostgreSQL body is missing: {payload_id}")
        return await asyncio.to_thread(self._verify_row, row)

    async def open_verified_receipt(self, payload_id: uuid.UUID) -> VerifiedPgTextBody:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT payload_id, namespace_id, content_profile, digest,
                       byte_size, encoding, selected_placement,
                       verification_profile, canonical_bytes
                  FROM m1_reference_payloads
                 WHERE payload_id = $1 AND selected_placement = $2
                """,
                payload_id,
                self.selected_placement,
            )
        if row is None:
            raise PgBodyIntegrityError(f"PostgreSQL body is missing: {payload_id}")
        return await asyncio.to_thread(self._receipt_from_row, row)

    async def namespace_residue(self, namespace_id: uuid.UUID) -> dict[str, int]:
        """Return bounded residue counts after a measurement namespace cleanup."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT COUNT(*)::int AS bodies,
                       COALESCE(SUM(byte_size), 0)::bigint AS body_bytes,
                       COUNT(DISTINCT digest)::int AS distinct_digests
                  FROM m1_reference_payloads
                 WHERE namespace_id = $1 AND selected_placement = $2
                """,
                namespace_id,
                self.selected_placement,
            )
        return dict(row)
