"""Verified M1-only reference payload adapter for native-ledger experiments.

This is not a final searchable-body placement decision.  It stores one
canonical byte representation solely so B-core can pin, open, and verify a
payload while transaction semantics are measured independently.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

import asyncpg

from app.exceptions import ValidationError


class ReferencePayloadIntegrityError(RuntimeError):
    """A prepared reference payload no longer matches its verified facts."""


@dataclass(frozen=True, slots=True)
class PreparedReferencePayload:
    payload_id: uuid.UUID
    namespace_id: uuid.UUID
    content_profile: str
    digest: str
    byte_size: int
    encoding: str
    selected_placement: str
    verification_profile: str


class M1ReferencePayloadStore:
    """Prepare and verify canonical UTF-8 bytes outside the authority TX."""

    selected_placement = "m1-reference-payload-v1"
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
            canonical = payload.encode("utf-8")
        elif isinstance(payload, bytes):
            canonical = payload
        else:
            raise ValidationError("Reference payload must be str or bytes")
        try:
            canonical.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValidationError("Reference text payload must be valid UTF-8") from exc
        if b"\x00" in canonical:
            raise ValidationError("Reference text payload must not contain NUL bytes")

        observed_digest = hashlib.sha256(canonical).hexdigest()
        if expected_digest is not None and expected_digest != observed_digest:
            raise ValidationError("Reference payload digest does not match expected digest")
        if expected_size is not None and expected_size != len(canonical):
            raise ValidationError("Reference payload size does not match expected size")
        return canonical, observed_digest

    async def prepare_text(
        self,
        *,
        namespace_id: uuid.UUID,
        payload: str | bytes,
        expected_digest: str | None = None,
        expected_size: int | None = None,
    ) -> PreparedReferencePayload:
        canonical, observed_digest = self._verified_bytes(
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
                ON CONFLICT DO NOTHING
                RETURNING payload_id, namespace_id, content_profile, digest,
                          byte_size, encoding, selected_placement,
                          verification_profile, canonical_bytes
                """,
                namespace_id,
                observed_digest,
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
                       AND verification_profile = $5
                    """,
                    namespace_id,
                    observed_digest,
                    len(canonical),
                    self.selected_placement,
                    self.verification_profile,
                )
        if row is None:
            raise ReferencePayloadIntegrityError(
                "Reference payload disappeared during placement-scoped deduplication"
            )
        self._verify_row(row, expected=canonical)
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
            raise ReferencePayloadIntegrityError("Reference payload bytes changed after preparation")
        if len(canonical) != row["byte_size"]:
            raise ReferencePayloadIntegrityError("Reference payload byte size mismatch")
        if hashlib.sha256(canonical).hexdigest() != row["digest"]:
            raise ReferencePayloadIntegrityError("Reference payload digest mismatch")
        if row["encoding"] != "utf-8":
            raise ReferencePayloadIntegrityError("Reference payload encoding mismatch")
        try:
            canonical.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ReferencePayloadIntegrityError("Reference payload is not valid UTF-8") from exc
        if b"\x00" in canonical:
            raise ReferencePayloadIntegrityError("Reference payload contains NUL bytes")
        if row["selected_placement"] != cls.selected_placement:
            raise ReferencePayloadIntegrityError("Reference payload placement mismatch")
        if row["verification_profile"] != cls.verification_profile:
            raise ReferencePayloadIntegrityError("Reference payload verification profile mismatch")
        return canonical

    async def open_verified(self, payload_id: uuid.UUID) -> bytes:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT payload_id, namespace_id, content_profile, digest,
                       byte_size, encoding, selected_placement,
                       verification_profile, canonical_bytes
                  FROM m1_reference_payloads
                 WHERE payload_id = $1
                """,
                payload_id,
            )
        if row is None:
            raise ReferencePayloadIntegrityError(f"Reference payload is missing: {payload_id}")
        return self._verify_row(row)
