"""Offline, operator-only retirement of an external-Git mirror.

The Collector creates the adoption manifest while AKB is still the read-only
mirror authority.  This module owns the deliberately narrow handoff from that
state to a retained manual vault.  The manifest parser is strict by design:
it accepts only the credential-free facts needed to bind the handoff and never
accepts document bodies or authentication material.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import asyncpg

from app.config import settings
from app.exceptions import MirrorMarkerError
from app.services.external_git_validation import ExternalGitPolicyError, validate
from app.services.git_service import GitService
from app.services.uri_service import doc_uri
from app.util.text import to_nfc


_MANIFEST_SCHEMA_VERSION = 1
_OID_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "vault_id",
        "vault_name",
        "remote_url",
        "remote_branch",
        "last_synced_sha",
        "documents",
    }
)
_DOCUMENT_KEYS = frozenset({"uri", "path", "content_hash", "managed_metadata"})
_MANAGED_METADATA_KEYS = frozenset(
    {"title", "type", "status", "tags", "domain", "summary", "metadata"}
)
_MAX_MANIFEST_BYTES = 8 * 1024 * 1024


class ExternalGitRetirementError(RuntimeError):
    """A safe, operator-actionable external-Git retirement refusal.

    Manifest content can contain customer metadata.  Every message in this
    class is deliberately value-free so a CLI caller can print it without
    accidentally emitting a document body, token, or unvalidated remote.
    """


class ExternalGitRetirementConflict(ExternalGitRetirementError):
    """A retry did not exactly match a durable retirement record."""


@dataclass(frozen=True, slots=True)
class AdoptionDocument:
    """One active external-Git document's credential-free adoption facts."""

    uri: str
    path: str
    content_hash: str
    managed_metadata: dict[str, Any]

    def fact(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "path": self.path,
            "content_hash": self.content_hash,
            "managed_metadata": self.managed_metadata,
        }


@dataclass(frozen=True, slots=True)
class AdoptionManifest:
    """Canonical, body-free manifest accepted by the retirement command."""

    vault_id: uuid.UUID
    vault_name: str
    remote_url: str
    remote_branch: str
    last_synced_sha: str
    documents: tuple[AdoptionDocument, ...]
    digest: str

    @property
    def document_count(self) -> int:
        return len(self.documents)

    def fact(self) -> dict[str, Any]:
        return {
            "schema_version": _MANIFEST_SCHEMA_VERSION,
            "vault_id": str(self.vault_id),
            "vault_name": self.vault_name,
            "remote_url": self.remote_url,
            "remote_branch": self.remote_branch,
            "last_synced_sha": self.last_synced_sha,
            "documents": [document.fact() for document in self.documents],
        }


def _manifest_shape_error() -> ExternalGitRetirementError:
    return ExternalGitRetirementError("external Git adoption manifest shape is invalid")


def _require_exact_keys(value: Mapping[str, Any], expected: frozenset[str]) -> None:
    if set(value) != expected:
        raise _manifest_shape_error()


def _text(value: object, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str):
        raise _manifest_shape_error()
    return to_nfc(value)


def _json_value(value: object) -> Any:
    """Return NFC-normalized JSON data without accepting Python-only values."""
    if value is None or isinstance(value, (str, bool, int)):
        return to_nfc(value) if isinstance(value, str) else value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _manifest_shape_error()
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str):
                raise _manifest_shape_error()
            key = to_nfc(raw_key)
            if key in normalized:
                raise _manifest_shape_error()
            normalized[key] = _json_value(raw_value)
        return normalized
    raise _manifest_shape_error()


def _managed_metadata(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _manifest_shape_error()
    _require_exact_keys(value, _MANAGED_METADATA_KEYS)

    title = _text(value["title"])
    doc_type = _text(value["type"], allow_none=True)
    status = _text(value["status"])
    domain = _text(value["domain"], allow_none=True)
    summary = _text(value["summary"], allow_none=True)
    tags = value["tags"]
    metadata = value["metadata"]
    if status != "active" or not isinstance(tags, list) or not isinstance(metadata, Mapping):
        raise _manifest_shape_error()
    normalized_tags: list[str] = []
    for tag in tags:
        normalized_tag = _text(tag)
        assert normalized_tag is not None
        normalized_tags.append(normalized_tag)
    normalized_metadata = _json_value(metadata)
    if not isinstance(normalized_metadata, dict):
        raise _manifest_shape_error()
    assert title is not None and status is not None
    return {
        "title": title,
        "type": doc_type,
        "status": status,
        "tags": normalized_tags,
        "domain": domain,
        "summary": summary,
        "metadata": normalized_metadata,
    }


def _canonical_digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def parse_adoption_manifest(value: object) -> AdoptionManifest:
    """Parse one strict credential-free Collector adoption manifest.

    Unknown keys are refused rather than ignored.  That makes a future
    credential/body field a hard compatibility break instead of something that
    could be silently accepted, logged, or persisted by an older AKB image.
    """
    if not isinstance(value, Mapping):
        raise _manifest_shape_error()
    _require_exact_keys(value, _MANIFEST_KEYS)
    if isinstance(value["schema_version"], bool) or value["schema_version"] != _MANIFEST_SCHEMA_VERSION:
        raise _manifest_shape_error()
    try:
        vault_id = uuid.UUID(_text(value["vault_id"]) or "")
    except (TypeError, ValueError, AttributeError):
        raise _manifest_shape_error() from None
    vault_name = _text(value["vault_name"])
    remote_url = _text(value["remote_url"])
    remote_branch = _text(value["remote_branch"])
    last_synced_sha = _text(value["last_synced_sha"])
    documents = value["documents"]
    if (
        not vault_name
        or not remote_url
        or not remote_branch
        or not last_synced_sha
        or not isinstance(documents, list)
        or _OID_RE.fullmatch(last_synced_sha) is None
    ):
        raise _manifest_shape_error()
    try:
        remote = validate(
            {
                "remote_url": remote_url,
                "remote_branch": remote_branch,
                "auth_token": None,
            },
            settings=settings,
            resolve=False,
        )
    except ExternalGitPolicyError as exc:
        raise ExternalGitRetirementError("external Git adoption manifest remote is invalid") from exc

    parsed_documents: list[AdoptionDocument] = []
    paths: set[str] = set()
    uris: set[str] = set()
    for document in documents:
        if not isinstance(document, Mapping):
            raise _manifest_shape_error()
        _require_exact_keys(document, _DOCUMENT_KEYS)
        uri = _text(document["uri"])
        path = _text(document["path"])
        content_hash = _text(document["content_hash"])
        if (
            not uri
            or not path
            or not content_hash
            or _DIGEST_RE.fullmatch(content_hash) is None
            or uri != doc_uri(vault_name, path)
        ):
            raise ExternalGitRetirementError("external Git adoption manifest document URI is invalid")
        if path in paths or uri in uris:
            raise ExternalGitRetirementError("external Git adoption manifest contains duplicate documents")
        paths.add(path)
        uris.add(uri)
        parsed_documents.append(
            AdoptionDocument(
                uri=uri,
                path=path,
                content_hash=content_hash,
                managed_metadata=_managed_metadata(document["managed_metadata"]),
            )
        )

    ordered = tuple(sorted(parsed_documents, key=lambda document: document.path))
    preliminary = AdoptionManifest(
        vault_id=vault_id,
        vault_name=vault_name,
        remote_url=remote.canonical_url,
        remote_branch=remote.branch,
        last_synced_sha=last_synced_sha,
        documents=ordered,
        digest="",
    )
    return AdoptionManifest(
        vault_id=preliminary.vault_id,
        vault_name=preliminary.vault_name,
        remote_url=preliminary.remote_url,
        remote_branch=preliminary.remote_branch,
        last_synced_sha=preliminary.last_synced_sha,
        documents=preliminary.documents,
        digest=_canonical_digest(preliminary.fact()),
    )


def load_adoption_manifest(path: str | Path) -> AdoptionManifest:
    """Load a bounded JSON manifest without reflecting its raw contents.

    A malformed input is reported only as a shape/read failure.  In particular,
    neither a JSON decoder message nor a file body is allowed into the
    operator-facing exception path.
    """
    try:
        raw = Path(path).read_bytes()
    except (OSError, TypeError, ValueError):
        raise ExternalGitRetirementError("external Git adoption manifest cannot be read") from None
    if len(raw) > _MAX_MANIFEST_BYTES:
        raise ExternalGitRetirementError("external Git adoption manifest exceeds the operator size limit")
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ExternalGitRetirementError("external Git adoption manifest is not valid JSON") from None
    return parse_adoption_manifest(value)


_RETIREMENT_PENDING_REASON = "external_git_retirement_pending"
_RETIREMENT_PENDING_ERROR = "[quarantine] external_git_retirement_pending"


@dataclass(frozen=True, slots=True)
class ExternalGitRetirementReceipt:
    """The durable, credential-free operator receipt."""

    retirement_id: uuid.UUID
    vault_id: uuid.UUID
    vault_name: str
    manifest_digest: str
    document_count: int
    remote_url: str
    remote_branch: str
    last_synced_sha: str
    idempotency_key: uuid.UUID
    requested_by: str
    status: str
    created_at: Any
    retired_at: Any


def _receipt(row: Mapping[str, Any]) -> ExternalGitRetirementReceipt:
    return ExternalGitRetirementReceipt(
        retirement_id=row["retirement_id"],
        vault_id=row["vault_id"],
        vault_name=row["vault_name"],
        manifest_digest=row["manifest_digest"],
        document_count=int(row["document_count"]),
        remote_url=row["remote_url"],
        remote_branch=row["remote_branch"],
        last_synced_sha=row["last_synced_sha"],
        idempotency_key=row["idempotency_key"],
        requested_by=row["requested_by"],
        status=row["status"],
        created_at=row["created_at"],
        retired_at=row["retired_at"],
    )


def _command_updated_one(status: str | None) -> bool:
    try:
        return int(status.split()[-1]) == 1  # type: ignore[union-attr]
    except (AttributeError, IndexError, ValueError):
        return False


def _command_count(status: str | None) -> int | None:
    try:
        return int(status.split()[-1])  # type: ignore[union-attr]
    except (AttributeError, IndexError, ValueError):
        return None


def _requested_by(value: object) -> str:
    if not isinstance(value, str):
        raise ExternalGitRetirementError("external Git retirement operator identity is invalid")
    normalized = to_nfc(value)
    if not normalized.strip() or len(normalized) > 255:
        raise ExternalGitRetirementError("external Git retirement operator identity is invalid")
    return normalized


def _strict_db_metadata(value: object) -> dict[str, Any]:
    """Decode the JSONB driver value without silently replacing corruption."""
    decoded = value
    if isinstance(decoded, str):
        try:
            decoded = json.loads(decoded)
        except json.JSONDecodeError:
            raise ExternalGitRetirementError("external Git managed metadata is invalid") from None
    if not isinstance(decoded, Mapping):
        raise ExternalGitRetirementError("external Git managed metadata is invalid")
    normalized = _json_value(decoded)
    if not isinstance(normalized, dict):
        raise ExternalGitRetirementError("external Git managed metadata is invalid")
    return normalized


class ExternalGitRetirement:
    """One narrow, offline transition from an AKB mirror to a manual vault.

    PostgreSQL and the filesystem cannot share a transaction.  The protocol is
    therefore deliberately three-phase and only moves forward:

    1. lock and validate live rows, then persist a quarantine intent and move
       the sidecar out of every poller claim state;
    2. replace the normal mirror marker with a fail-closed tombstone under the
       per-vault Git lock;
    3. re-lock/revalidate rows, reclassify documents, delete just the sidecar,
       and commit the immutable receipt.  A final tombstone cleanup is safe to
       replay after that commit.
    """

    def __init__(self, pool: asyncpg.Pool, *, git: GitService | None = None):
        self.pool = pool
        self.git = git or GitService()

    @staticmethod
    def _same_binding(
        receipt: ExternalGitRetirementReceipt,
        manifest: AdoptionManifest,
        *,
        idempotency_key: uuid.UUID,
        requested_by: str,
    ) -> bool:
        return (
            receipt.vault_id == manifest.vault_id
            and receipt.vault_name == manifest.vault_name
            and receipt.manifest_digest == manifest.digest
            and receipt.document_count == manifest.document_count
            and receipt.remote_url == manifest.remote_url
            and receipt.remote_branch == manifest.remote_branch
            and receipt.last_synced_sha == manifest.last_synced_sha
            and receipt.idempotency_key == idempotency_key
            and receipt.requested_by == requested_by
        )

    @classmethod
    def _require_same_binding(
        cls,
        receipt: ExternalGitRetirementReceipt,
        manifest: AdoptionManifest,
        *,
        idempotency_key: uuid.UUID,
        requested_by: str,
    ) -> None:
        if not cls._same_binding(
            receipt,
            manifest,
            idempotency_key=idempotency_key,
            requested_by=requested_by,
        ):
            raise ExternalGitRetirementConflict("external Git retirement replay conflicts with its durable receipt")

    @staticmethod
    async def _locked_vault(conn: asyncpg.Connection, manifest: AdoptionManifest) -> asyncpg.Record:
        row = await conn.fetchrow(
            "SELECT id, name, status FROM vaults WHERE id = $1 FOR UPDATE",
            manifest.vault_id,
        )
        if row is None or row["name"] != manifest.vault_name:
            raise ExternalGitRetirementError("external Git retirement vault binding is stale")
        if row["status"] == "deleted":
            raise ExternalGitRetirementError("external Git retirement cannot reclassify a deleted vault")
        return row

    @staticmethod
    async def _require_open_authority(conn: asyncpg.Connection) -> None:
        committed = await conn.fetchval(
            """
            SELECT 1
              FROM native_revision_existing_authority
             WHERE marker_id = TRUE AND status = 'committed'
            """
        )
        if committed:
            raise ExternalGitRetirementError("external Git retirement is unavailable after Native authority commit")

    @staticmethod
    async def _locked_sidecar(conn: asyncpg.Connection, vault_id: uuid.UUID) -> asyncpg.Record:
        row = await conn.fetchrow(
            """
            SELECT vault_id, remote_url, remote_branch, last_synced_sha,
                   sync_state, sync_state_reason
              FROM vault_external_git
             WHERE vault_id = $1
             FOR UPDATE
            """,
            vault_id,
        )
        if row is None:
            raise ExternalGitRetirementError("external Git retirement sidecar is missing")
        return row

    @staticmethod
    def _validate_sidecar(row: Mapping[str, Any], manifest: AdoptionManifest) -> None:
        try:
            remote = validate(
                {
                    "remote_url": row["remote_url"],
                    "remote_branch": row["remote_branch"],
                    "auth_token": None,
                },
                settings=settings,
                resolve=False,
            )
        except ExternalGitPolicyError as exc:
            raise ExternalGitRetirementError("external Git retirement sidecar remote is invalid") from exc
        if (
            remote.canonical_url != manifest.remote_url
            or remote.branch != manifest.remote_branch
            or row["last_synced_sha"] != manifest.last_synced_sha
            or _OID_RE.fullmatch(str(row["last_synced_sha"] or "")) is None
        ):
            raise ExternalGitRetirementError("external Git retirement sidecar binding is stale")

    @staticmethod
    def _live_document(vault_name: str, row: Mapping[str, Any]) -> AdoptionDocument:
        path = row["path"]
        content_hash = row["content_hash"]
        tags = row["tags"]
        if (
            row["source"] != "external_git"
            or row["external_path"] != path
            or not isinstance(row["external_blob"], str)
            or _OID_RE.fullmatch(row["external_blob"]) is None
            or not isinstance(path, str)
            or not isinstance(content_hash, str)
            or _DIGEST_RE.fullmatch(content_hash) is None
            or row["hash_algorithm"] != "sha256"
            or not isinstance(tags, (list, tuple))
        ):
            raise ExternalGitRetirementError("external Git retirement document facts are invalid")
        try:
            managed_metadata = _managed_metadata(
                {
                    "title": row["title"],
                    "type": row["doc_type"],
                    "status": row["status"],
                    "tags": list(tags),
                    "domain": row["domain"],
                    "summary": row["summary"],
                    "metadata": _strict_db_metadata(row["metadata"]),
                }
            )
        except ExternalGitRetirementError:
            raise
        except Exception as exc:  # noqa: BLE001 - never expose persisted document values
            raise ExternalGitRetirementError("external Git retirement document facts are invalid") from exc
        normalized_path = to_nfc(path)
        normalized_hash = to_nfc(content_hash)
        return AdoptionDocument(
            uri=doc_uri(vault_name, normalized_path),
            path=normalized_path,
            content_hash=normalized_hash,
            managed_metadata=managed_metadata,
        )

    async def _validate_live_documents(
        self,
        conn: asyncpg.Connection,
        manifest: AdoptionManifest,
    ) -> None:
        rows = await conn.fetch(
            """
            SELECT id, path, title, doc_type, status, tags, domain, summary,
                   metadata, source, external_path, external_blob, content_hash,
                   hash_algorithm
              FROM documents
             WHERE vault_id = $1 AND source = 'external_git'
             ORDER BY path
             FOR UPDATE
            """,
            manifest.vault_id,
        )
        live = tuple(
            sorted(
                (self._live_document(manifest.vault_name, row) for row in rows),
                key=lambda document: document.path,
            )
        )
        if len(live) != manifest.document_count or [item.fact() for item in live] != [
            item.fact() for item in manifest.documents
        ]:
            raise ExternalGitRetirementError("external Git retirement manifest does not match live documents")

    async def _load_or_quarantine(
        self,
        manifest: AdoptionManifest,
        *,
        idempotency_key: uuid.UUID,
        requested_by: str,
    ) -> ExternalGitRetirementReceipt:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                record = await conn.fetchrow(
                    "SELECT * FROM external_git_retirements WHERE vault_id = $1 FOR UPDATE",
                    manifest.vault_id,
                )
                if record is not None:
                    receipt = _receipt(record)
                    self._require_same_binding(
                        receipt,
                        manifest,
                        idempotency_key=idempotency_key,
                        requested_by=requested_by,
                    )
                    if receipt.status == "retired":
                        return receipt
                    await self._locked_vault(conn, manifest)
                    await self._require_open_authority(conn)
                    sidecar = await self._locked_sidecar(conn, manifest.vault_id)
                    if (
                        sidecar["sync_state"] != "quarantined"
                        or sidecar["sync_state_reason"] != _RETIREMENT_PENDING_REASON
                    ):
                        raise ExternalGitRetirementError("external Git retirement quarantine intent is stale")
                    self._validate_sidecar(sidecar, manifest)
                    await self._validate_live_documents(conn, manifest)
                    return receipt

                key_record = await conn.fetchrow(
                    "SELECT * FROM external_git_retirements WHERE idempotency_key = $1 FOR UPDATE",
                    idempotency_key,
                )
                if key_record is not None:
                    raise ExternalGitRetirementConflict(
                        "external Git retirement idempotency key is already bound to another receipt"
                    )
                await self._locked_vault(conn, manifest)
                await self._require_open_authority(conn)
                sidecar = await self._locked_sidecar(conn, manifest.vault_id)
                if sidecar["sync_state"] not in {"active", "pending_preflight"}:
                    raise ExternalGitRetirementError("external Git retirement sidecar is not claimable for retirement")
                self._validate_sidecar(sidecar, manifest)
                await self._validate_live_documents(conn, manifest)

                try:
                    record = await conn.fetchrow(
                        """
                        INSERT INTO external_git_retirements (
                            vault_id, vault_name, manifest_digest, document_count,
                            remote_url, remote_branch, last_synced_sha,
                            idempotency_key, requested_by
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                        RETURNING *
                        """,
                        manifest.vault_id,
                        manifest.vault_name,
                        manifest.digest,
                        manifest.document_count,
                        manifest.remote_url,
                        manifest.remote_branch,
                        manifest.last_synced_sha,
                        idempotency_key,
                        requested_by,
                    )
                except asyncpg.UniqueViolationError as exc:
                    raise ExternalGitRetirementConflict(
                        "external Git retirement conflicts with an existing receipt"
                    ) from exc
                status = await conn.execute(
                    """
                    UPDATE vault_external_git
                       SET sync_state = 'quarantined',
                           sync_state_reason = $2,
                           sync_state_at = NOW(),
                           poll_next_at = 'infinity',
                           last_error = $3,
                           updated_at = NOW()
                     WHERE vault_id = $1
                       AND sync_state IN ('active', 'pending_preflight')
                    """,
                    manifest.vault_id,
                    _RETIREMENT_PENDING_REASON,
                    _RETIREMENT_PENDING_ERROR,
                )
                if not _command_updated_one(status) or record is None:
                    raise ExternalGitRetirementError("external Git retirement quarantine transition was superseded")
                return _receipt(record)

    async def _finalize(
        self,
        manifest: AdoptionManifest,
        *,
        idempotency_key: uuid.UUID,
        requested_by: str,
    ) -> ExternalGitRetirementReceipt:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                record = await conn.fetchrow(
                    "SELECT * FROM external_git_retirements WHERE vault_id = $1 FOR UPDATE",
                    manifest.vault_id,
                )
                if record is None:
                    raise ExternalGitRetirementError("external Git retirement intent disappeared")
                receipt = _receipt(record)
                self._require_same_binding(
                    receipt,
                    manifest,
                    idempotency_key=idempotency_key,
                    requested_by=requested_by,
                )
                if receipt.status == "retired":
                    return receipt
                await self._locked_vault(conn, manifest)
                await self._require_open_authority(conn)
                sidecar = await self._locked_sidecar(conn, manifest.vault_id)
                if (
                    sidecar["sync_state"] != "quarantined"
                    or sidecar["sync_state_reason"] != _RETIREMENT_PENDING_REASON
                ):
                    raise ExternalGitRetirementError("external Git retirement quarantine intent is stale")
                self._validate_sidecar(sidecar, manifest)
                await self._validate_live_documents(conn, manifest)

                reclassified = await conn.execute(
                    """
                    UPDATE documents
                       SET source = 'manual',
                           external_path = NULL,
                           external_blob = NULL,
                           llm_metadata_at = NULL,
                           llm_retry_count = 0,
                           llm_last_error = NULL,
                           llm_next_attempt_at = NULL
                     WHERE vault_id = $1 AND source = 'external_git'
                    """,
                    manifest.vault_id,
                )
                if _command_count(reclassified) != manifest.document_count:
                    raise ExternalGitRetirementError("external Git retirement document reclassification drifted")
                deleted = await conn.execute(
                    "DELETE FROM vault_external_git WHERE vault_id = $1",
                    manifest.vault_id,
                )
                if not _command_updated_one(deleted):
                    raise ExternalGitRetirementError("external Git retirement sidecar delete was superseded")
                row = await conn.fetchrow(
                    """
                    UPDATE external_git_retirements
                       SET status = 'retired', retired_at = NOW()
                     WHERE vault_id = $1 AND status = 'quarantined'
                    RETURNING *
                    """,
                    manifest.vault_id,
                )
                if row is None:
                    raise ExternalGitRetirementError("external Git retirement receipt transition was superseded")
                return _receipt(row)

    async def _finish_completed_marker(
        self,
        manifest: AdoptionManifest,
        *,
        idempotency_key: uuid.UUID,
        requested_by: str,
    ) -> None:
        vault_name: str | None = None
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                record = await conn.fetchrow(
                    "SELECT * FROM external_git_retirements WHERE vault_id = $1 FOR UPDATE",
                    manifest.vault_id,
                )
                if record is None:
                    raise ExternalGitRetirementError("external Git retirement receipt disappeared")
                receipt = _receipt(record)
                self._require_same_binding(
                    receipt,
                    manifest,
                    idempotency_key=idempotency_key,
                    requested_by=requested_by,
                )
                if receipt.status != "retired":
                    raise ExternalGitRetirementError("external Git retirement is not complete")
                vault = await conn.fetchrow(
                    "SELECT id, name FROM vaults WHERE id = $1 FOR UPDATE",
                    manifest.vault_id,
                )
                if vault is None:
                    return
                if vault["name"] != manifest.vault_name:
                    raise ExternalGitRetirementError("external Git retirement vault binding is stale")
                if await conn.fetchval(
                    "SELECT 1 FROM vault_external_git WHERE vault_id = $1",
                    manifest.vault_id,
                ):
                    raise ExternalGitRetirementError("external Git retirement sidecar unexpectedly remains")
                if await conn.fetchval(
                    "SELECT 1 FROM documents WHERE vault_id = $1 AND source = 'external_git'",
                    manifest.vault_id,
                ):
                    raise ExternalGitRetirementError("external Git retirement document reclassification is incomplete")
                vault_name = vault["name"]
        if vault_name is not None:
            try:
                await asyncio.to_thread(
                    self.git.finalize_external_mirror_retirement,
                    vault_name,
                    expected_ref=manifest.last_synced_sha,
                )
            except MirrorMarkerError as exc:
                raise ExternalGitRetirementError("external Git retirement marker finalization failed") from exc

    async def retire(
        self,
        *,
        manifest: AdoptionManifest,
        idempotency_key: uuid.UUID,
        requested_by: str,
    ) -> ExternalGitRetirementReceipt:
        """Retire one pre-adopted external-Git sidecar with exact replay only."""
        if not isinstance(manifest, AdoptionManifest):
            raise ExternalGitRetirementError("external Git retirement manifest is invalid")
        if not isinstance(idempotency_key, uuid.UUID):
            raise ExternalGitRetirementError("external Git retirement idempotency key is invalid")
        requested_by = _requested_by(requested_by)
        receipt = await self._load_or_quarantine(
            manifest,
            idempotency_key=idempotency_key,
            requested_by=requested_by,
        )
        if receipt.status == "retired":
            await self._finish_completed_marker(
                manifest,
                idempotency_key=idempotency_key,
                requested_by=requested_by,
            )
            return receipt
        try:
            await asyncio.to_thread(
                self.git.quarantine_external_mirror_marker,
                manifest.vault_name,
                expected_ref=manifest.last_synced_sha,
            )
        except MirrorMarkerError as exc:
            raise ExternalGitRetirementError("external Git retirement marker quarantine failed") from exc
        receipt = await self._finalize(
            manifest,
            idempotency_key=idempotency_key,
            requested_by=requested_by,
        )
        await self._finish_completed_marker(
            manifest,
            idempotency_key=idempotency_key,
            requested_by=requested_by,
        )
        return receipt
