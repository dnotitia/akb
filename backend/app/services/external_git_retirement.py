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
import re
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

import asyncpg

from app.config import settings
from app.exceptions import MirrorMarkerError
from app.services.external_git_validation import ExternalGitPolicyError, validate
from app.services.git_service import GitService
from app.services.uri_service import doc_uri
from app.util.text import to_nfc


_COLLECTOR_MANIFEST_SCHEMA = "akb-collector.git-adoption-manifest"
_COLLECTOR_MANIFEST_VERSION = 1
_COLLECTOR_MANIFEST_PURPOSE = "legacy-external-git-retirement"
_OID_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_MANIFEST_KEYS = frozenset(
    {
        "schema",
        "version",
        "purpose",
        "binding",
        "source",
        "documents",
    }
)
_BINDING_KEYS = frozenset({"name", "source_scope", "target_vault", "target_collection"})
_SOURCE_KEYS = frozenset({"remote_url", "branch", "snapshot_commit", "path_prefix"})
_DOCUMENT_KEYS = frozenset(
    {
        "origin_key",
        "path",
        "resource_uri",
        "source_version",
        "blob_sha",
        "akb_content_sha256",
        "akb_current_version",
        "managed_metadata",
    }
)
_MANAGED_METADATA_KEYS = frozenset({"managed", "title", "type", "tags", "summary", "domain"})
# Large repositories can legitimately need tens of thousands of body-free
# document facts. Keep the operator input bounded while leaving enough room
# for that inventory and its canonical metadata.
_MAX_MANIFEST_BYTES = 32 * 1024 * 1024


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

    origin_key: str
    path: str
    resource_uri: str
    source_version: str
    blob_sha: str
    akb_content_sha256: str
    akb_current_version: str
    managed_metadata: dict[str, Any]

    def fact(self) -> dict[str, Any]:
        return {
            "origin_key": self.origin_key,
            "path": self.path,
            "resource_uri": self.resource_uri,
            "source_version": self.source_version,
            "blob_sha": self.blob_sha,
            "akb_content_sha256": self.akb_content_sha256,
            "akb_current_version": self.akb_current_version,
            "managed_metadata": self.managed_metadata,
        }


@dataclass(frozen=True, slots=True)
class AdoptionManifest:
    """Canonical, body-free manifest accepted by the retirement command."""

    binding_name: str
    source_scope: str
    target_vault: str
    target_collection: str
    remote_url: str
    remote_branch: str
    snapshot_commit: str
    path_prefix: str | None
    documents: tuple[AdoptionDocument, ...]
    digest: str

    @property
    def document_count(self) -> int:
        return len(self.documents)

    @property
    def vault_name(self) -> str:
        """The existing receipt column names the Collector target Vault."""
        return self.target_vault

    @property
    def last_synced_sha(self) -> str:
        """The source snapshot is the mirror's only accepted fixed ref."""
        return self.snapshot_commit

    def fact(self) -> dict[str, Any]:
        return {
            "schema": _COLLECTOR_MANIFEST_SCHEMA,
            "version": _COLLECTOR_MANIFEST_VERSION,
            "purpose": _COLLECTOR_MANIFEST_PURPOSE,
            "binding": {
                "name": self.binding_name,
                "source_scope": self.source_scope,
                "target_vault": self.target_vault,
                "target_collection": self.target_collection,
            },
            "source": {
                "remote_url": self.remote_url,
                "branch": self.remote_branch,
                "snapshot_commit": self.snapshot_commit,
                "path_prefix": self.path_prefix,
            },
            "documents": [document.fact() for document in self.documents],
        }


def _manifest_shape_error() -> ExternalGitRetirementError:
    return ExternalGitRetirementError("external Git adoption manifest shape is invalid")


def _require_exact_keys(value: Mapping[str, Any], expected: frozenset[str]) -> None:
    if set(value) != expected:
        raise _manifest_shape_error()


def _canonical_text(value: object, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise _manifest_shape_error()
    if value != value.strip() or (not allow_empty and not value):
        raise _manifest_shape_error()
    return value


def _canonical_path(value: object) -> str:
    path = _canonical_text(value)
    pure = PurePosixPath(path)
    if (
        pure.is_absolute()
        or path.endswith("/")
        or any(component in {"", ".", ".."} for component in path.split("/"))
    ):
        raise ExternalGitRetirementError("external Git adoption manifest document path is invalid")
    return path


def _collector_path_prefix(value: object) -> str:
    """Keep Collector's canonical selection fact without giving it AKB meaning."""
    prefix = _canonical_text(value)
    if prefix != prefix.strip("/"):
        raise _manifest_shape_error()
    return prefix


def _managed_metadata(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _manifest_shape_error()
    _require_exact_keys(value, _MANAGED_METADATA_KEYS)

    managed = value["managed"]
    title = _canonical_text(value["title"], allow_empty=True)
    doc_type = _canonical_text(value["type"])
    domain = _canonical_text(value["domain"], allow_empty=True)
    summary = _canonical_text(value["summary"], allow_empty=True)
    tags = value["tags"]
    if type(managed) is not bool or not isinstance(tags, list):
        raise _manifest_shape_error()
    normalized_tags: list[str] = []
    for tag in tags:
        normalized_tags.append(_canonical_text(tag))
    if not managed and (doc_type != "reference" or normalized_tags or summary or domain):
        raise ExternalGitRetirementError("external Git adoption manifest unmanaged metadata is invalid")
    return {
        "managed": managed,
        "title": title,
        "type": doc_type,
        "tags": normalized_tags,
        "domain": domain,
        "summary": summary,
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
    """Parse the exact strict credential-free Collector v1 manifest.

    Unknown keys are refused rather than ignored.  That makes a future
    credential/body field a hard compatibility break instead of something that
    could be silently accepted, logged, or persisted by an older AKB image.
    """
    if not isinstance(value, Mapping):
        raise _manifest_shape_error()
    _require_exact_keys(value, _MANIFEST_KEYS)
    if (
        value["schema"] != _COLLECTOR_MANIFEST_SCHEMA
        or isinstance(value["version"], bool)
        or value["version"] != _COLLECTOR_MANIFEST_VERSION
        or value["purpose"] != _COLLECTOR_MANIFEST_PURPOSE
        or not isinstance(value["binding"], Mapping)
        or not isinstance(value["source"], Mapping)
    ):
        raise _manifest_shape_error()
    binding = value["binding"]
    source = value["source"]
    _require_exact_keys(binding, _BINDING_KEYS)
    _require_exact_keys(source, _SOURCE_KEYS)
    binding_name = _canonical_text(binding["name"])
    source_scope = _canonical_text(binding["source_scope"])
    target_vault = _canonical_text(binding["target_vault"])
    target_collection = _canonical_text(binding["target_collection"])
    remote_url = _canonical_text(source["remote_url"])
    remote_branch = _canonical_text(source["branch"])
    snapshot_commit = _canonical_text(source["snapshot_commit"])
    raw_path_prefix = source["path_prefix"]
    path_prefix = None if raw_path_prefix is None else _collector_path_prefix(raw_path_prefix)
    documents = value["documents"]
    if not isinstance(documents, list) or _OID_RE.fullmatch(snapshot_commit) is None:
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
    resource_uris: set[str] = set()
    origin_keys: set[str] = set()
    for document in documents:
        if not isinstance(document, Mapping):
            raise _manifest_shape_error()
        _require_exact_keys(document, _DOCUMENT_KEYS)
        origin_key = _canonical_text(document["origin_key"])
        path = _canonical_path(document["path"])
        resource_uri = _canonical_text(document["resource_uri"])
        source_version = _canonical_text(document["source_version"])
        blob_sha = _canonical_text(document["blob_sha"])
        akb_content_sha256 = _canonical_text(document["akb_content_sha256"])
        akb_current_version = _canonical_text(document["akb_current_version"])
        if (
            _OID_RE.fullmatch(source_version) is None
            or _OID_RE.fullmatch(blob_sha) is None
            or _DIGEST_RE.fullmatch(akb_content_sha256) is None
            or _OID_RE.fullmatch(akb_current_version) is None
        ):
            raise _manifest_shape_error()
        if source_version != blob_sha or origin_key != f"git://{source_scope}/{path}":
            raise ExternalGitRetirementError("external Git adoption manifest source identity is invalid")
        if resource_uri != doc_uri(target_vault, path):
            raise ExternalGitRetirementError("external Git adoption manifest document URI is invalid")
        if path in paths or resource_uri in resource_uris or origin_key in origin_keys:
            raise ExternalGitRetirementError("external Git adoption manifest contains duplicate documents")
        paths.add(path)
        resource_uris.add(resource_uri)
        origin_keys.add(origin_key)
        parsed_documents.append(
            AdoptionDocument(
                origin_key=origin_key,
                path=path,
                resource_uri=resource_uri,
                source_version=source_version,
                blob_sha=blob_sha,
                akb_content_sha256=akb_content_sha256,
                akb_current_version=akb_current_version,
                managed_metadata=_managed_metadata(document["managed_metadata"]),
            )
        )

    ordered = tuple(sorted(parsed_documents, key=lambda document: (document.path, document.origin_key)))
    preliminary = AdoptionManifest(
        binding_name=binding_name,
        source_scope=source_scope,
        target_vault=target_vault,
        target_collection=target_collection,
        remote_url=remote.canonical_url,
        remote_branch=remote.branch,
        snapshot_commit=snapshot_commit,
        path_prefix=path_prefix,
        documents=ordered,
        digest="",
    )
    return AdoptionManifest(
        binding_name=preliminary.binding_name,
        source_scope=preliminary.source_scope,
        target_vault=preliminary.target_vault,
        target_collection=preliminary.target_collection,
        remote_url=preliminary.remote_url,
        remote_branch=preliminary.remote_branch,
        snapshot_commit=preliminary.snapshot_commit,
        path_prefix=preliminary.path_prefix,
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


def _normalized_live_metadata_text(value: object, *, default: str) -> str:
    """Use the same blank/default rules as Collector's public AKB proof."""
    if value is None:
        return default
    if not isinstance(value, str):
        raise ExternalGitRetirementError("external Git retirement document facts are invalid")
    return value.strip() or default


def _normalized_live_metadata_tags(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ExternalGitRetirementError("external Git retirement document facts are invalid")
    normalized: list[str] = []
    for tag in value:
        if not isinstance(tag, str):
            raise ExternalGitRetirementError("external Git retirement document facts are invalid")
        trimmed = tag.strip()
        if trimmed:
            normalized.append(trimmed)
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
        expected_vault_id: uuid.UUID,
        idempotency_key: uuid.UUID,
        requested_by: str,
    ) -> bool:
        return (
            receipt.vault_id == expected_vault_id
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
        expected_vault_id: uuid.UUID,
        idempotency_key: uuid.UUID,
        requested_by: str,
    ) -> None:
        if not cls._same_binding(
            receipt,
            manifest,
            expected_vault_id=expected_vault_id,
            idempotency_key=idempotency_key,
            requested_by=requested_by,
        ):
            raise ExternalGitRetirementConflict("external Git retirement replay conflicts with its durable receipt")

    @staticmethod
    async def _locked_vault(
        conn: asyncpg.Connection,
        manifest: AdoptionManifest,
        *,
        expected_vault_id: uuid.UUID,
    ) -> asyncpg.Record:
        row = await conn.fetchrow(
            "SELECT id, name, status FROM vaults WHERE name = $1 FOR UPDATE",
            manifest.target_vault,
        )
        if row is None or row["id"] != expected_vault_id:
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
    def _require_live_document_matches(
        manifest: AdoptionManifest,
        document: AdoptionDocument,
        row: Mapping[str, Any],
    ) -> None:
        """Prove every Collector fact against the active AKB mirror row."""
        if (
            row["source"] != "external_git"
            or row["status"] != "active"
            or row["external_path"] != document.path
            or row["external_blob"] != document.blob_sha
            or row["content_hash"] != document.akb_content_sha256
            or row["hash_algorithm"] != "sha256"
            or row["current_commit"] != document.akb_current_version
            or document.source_version != document.blob_sha
            or document.origin_key != f"git://{manifest.source_scope}/{document.path}"
            or document.resource_uri != doc_uri(manifest.target_vault, document.path)
        ):
            raise ExternalGitRetirementError("external Git retirement manifest does not match live documents")
        if not document.managed_metadata["managed"]:
            return
        expected_metadata = {
            "title": document.managed_metadata["title"],
            "type": document.managed_metadata["type"],
            "tags": document.managed_metadata["tags"],
            "summary": document.managed_metadata["summary"],
            "domain": document.managed_metadata["domain"],
        }
        try:
            live_metadata = {
                "title": _normalized_live_metadata_text(row["title"], default=""),
                "type": _normalized_live_metadata_text(row["doc_type"], default="reference"),
                "tags": _normalized_live_metadata_tags(row["tags"]),
                "summary": _normalized_live_metadata_text(row["summary"], default=""),
                "domain": _normalized_live_metadata_text(row["domain"], default=""),
            }
        except ExternalGitRetirementError:
            raise ExternalGitRetirementError(
                "external Git retirement manifest does not match live documents"
            ) from None
        if live_metadata != expected_metadata:
            raise ExternalGitRetirementError("external Git retirement manifest does not match live documents")

    async def _validate_live_documents(
        self,
        conn: asyncpg.Connection,
        manifest: AdoptionManifest,
        *,
        vault_id: uuid.UUID,
    ) -> None:
        rows = await conn.fetch(
            """
            SELECT id, path, title, doc_type, status, tags, domain, summary,
                   source, external_path, external_blob, content_hash,
                   hash_algorithm, current_commit
              FROM documents
             WHERE vault_id = $1 AND source = 'external_git'
             ORDER BY path
             FOR UPDATE
            """,
            vault_id,
        )
        if len(rows) != manifest.document_count:
            raise ExternalGitRetirementError("external Git retirement manifest does not match live documents")
        expected_by_path = {document.path: document for document in manifest.documents}
        seen_paths: set[str] = set()
        for row in rows:
            path = row["path"]
            if not isinstance(path, str) or path in seen_paths:
                raise ExternalGitRetirementError("external Git retirement manifest does not match live documents")
            document = expected_by_path.get(path)
            if document is None:
                raise ExternalGitRetirementError("external Git retirement manifest does not match live documents")
            seen_paths.add(path)
            self._require_live_document_matches(manifest, document, row)
        if len(seen_paths) != manifest.document_count:
            raise ExternalGitRetirementError("external Git retirement manifest does not match live documents")

    async def _load_or_quarantine(
        self,
        manifest: AdoptionManifest,
        *,
        expected_vault_id: uuid.UUID,
        idempotency_key: uuid.UUID,
        requested_by: str,
    ) -> ExternalGitRetirementReceipt:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                record = await conn.fetchrow(
                    "SELECT * FROM external_git_retirements WHERE vault_id = $1 FOR UPDATE",
                    expected_vault_id,
                )
                if record is not None:
                    receipt = _receipt(record)
                    self._require_same_binding(
                        receipt,
                        manifest,
                        expected_vault_id=expected_vault_id,
                        idempotency_key=idempotency_key,
                        requested_by=requested_by,
                    )
                    if receipt.status == "retired":
                        return receipt
                    vault = await self._locked_vault(
                        conn,
                        manifest,
                        expected_vault_id=expected_vault_id,
                    )
                    await self._require_open_authority(conn)
                    sidecar = await self._locked_sidecar(conn, vault["id"])
                    if (
                        sidecar["sync_state"] != "quarantined"
                        or sidecar["sync_state_reason"] != _RETIREMENT_PENDING_REASON
                    ):
                        raise ExternalGitRetirementError("external Git retirement quarantine intent is stale")
                    self._validate_sidecar(sidecar, manifest)
                    await self._validate_live_documents(conn, manifest, vault_id=vault["id"])
                    return receipt

                vault = await self._locked_vault(
                    conn,
                    manifest,
                    expected_vault_id=expected_vault_id,
                )
                key_record = await conn.fetchrow(
                    "SELECT * FROM external_git_retirements WHERE idempotency_key = $1 FOR UPDATE",
                    idempotency_key,
                )
                if key_record is not None:
                    raise ExternalGitRetirementConflict(
                        "external Git retirement idempotency key is already bound to another receipt"
                    )
                await self._require_open_authority(conn)
                sidecar = await self._locked_sidecar(conn, vault["id"])
                if sidecar["sync_state"] not in {"active", "pending_preflight"}:
                    raise ExternalGitRetirementError("external Git retirement sidecar is not claimable for retirement")
                self._validate_sidecar(sidecar, manifest)
                await self._validate_live_documents(conn, manifest, vault_id=vault["id"])

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
                        vault["id"],
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
                    vault["id"],
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
        expected_vault_id: uuid.UUID,
        idempotency_key: uuid.UUID,
        requested_by: str,
    ) -> ExternalGitRetirementReceipt:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                record = await conn.fetchrow(
                    "SELECT * FROM external_git_retirements WHERE vault_id = $1 FOR UPDATE",
                    expected_vault_id,
                )
                if record is None:
                    raise ExternalGitRetirementError("external Git retirement intent disappeared")
                receipt = _receipt(record)
                self._require_same_binding(
                    receipt,
                    manifest,
                    expected_vault_id=expected_vault_id,
                    idempotency_key=idempotency_key,
                    requested_by=requested_by,
                )
                if receipt.status == "retired":
                    return receipt
                vault = await self._locked_vault(
                    conn,
                    manifest,
                    expected_vault_id=expected_vault_id,
                )
                await self._require_open_authority(conn)
                sidecar = await self._locked_sidecar(conn, vault["id"])
                if (
                    sidecar["sync_state"] != "quarantined"
                    or sidecar["sync_state_reason"] != _RETIREMENT_PENDING_REASON
                ):
                    raise ExternalGitRetirementError("external Git retirement quarantine intent is stale")
                self._validate_sidecar(sidecar, manifest)
                await self._validate_live_documents(conn, manifest, vault_id=vault["id"])

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
                    vault["id"],
                )
                if _command_count(reclassified) != manifest.document_count:
                    raise ExternalGitRetirementError("external Git retirement document reclassification drifted")
                deleted = await conn.execute(
                    "DELETE FROM vault_external_git WHERE vault_id = $1",
                    vault["id"],
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
                    vault["id"],
                )
                if row is None:
                    raise ExternalGitRetirementError("external Git retirement receipt transition was superseded")
                return _receipt(row)

    async def _finish_completed_marker(
        self,
        manifest: AdoptionManifest,
        *,
        expected_vault_id: uuid.UUID,
        idempotency_key: uuid.UUID,
        requested_by: str,
    ) -> None:
        vault_name: str | None = None
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                record = await conn.fetchrow(
                    "SELECT * FROM external_git_retirements WHERE vault_id = $1 FOR UPDATE",
                    expected_vault_id,
                )
                if record is None:
                    raise ExternalGitRetirementError("external Git retirement receipt disappeared")
                receipt = _receipt(record)
                self._require_same_binding(
                    receipt,
                    manifest,
                    expected_vault_id=expected_vault_id,
                    idempotency_key=idempotency_key,
                    requested_by=requested_by,
                )
                if receipt.status != "retired":
                    raise ExternalGitRetirementError("external Git retirement is not complete")
                vault = await conn.fetchrow(
                    "SELECT id, name FROM vaults WHERE id = $1 FOR UPDATE",
                    expected_vault_id,
                )
                if vault is None:
                    return
                if vault["name"] != manifest.vault_name:
                    raise ExternalGitRetirementError("external Git retirement vault binding is stale")
                if await conn.fetchval(
                    "SELECT 1 FROM vault_external_git WHERE vault_id = $1",
                    expected_vault_id,
                ):
                    raise ExternalGitRetirementError("external Git retirement sidecar unexpectedly remains")
                if await conn.fetchval(
                    "SELECT 1 FROM documents WHERE vault_id = $1 AND source = 'external_git'",
                    expected_vault_id,
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
        expected_vault_id: uuid.UUID,
        idempotency_key: uuid.UUID,
        requested_by: str,
    ) -> ExternalGitRetirementReceipt:
        """Retire one pre-adopted external-Git sidecar with exact replay only."""
        if not isinstance(manifest, AdoptionManifest):
            raise ExternalGitRetirementError("external Git retirement manifest is invalid")
        if not isinstance(expected_vault_id, uuid.UUID):
            raise ExternalGitRetirementError("external Git retirement vault id is invalid")
        if not isinstance(idempotency_key, uuid.UUID):
            raise ExternalGitRetirementError("external Git retirement idempotency key is invalid")
        try:
            canonical_manifest = parse_adoption_manifest(manifest.fact())
        except ExternalGitRetirementError:
            raise ExternalGitRetirementError("external Git retirement manifest is invalid") from None
        if canonical_manifest != manifest:
            raise ExternalGitRetirementError("external Git retirement manifest is invalid")
        requested_by = _requested_by(requested_by)
        receipt = await self._load_or_quarantine(
            manifest,
            expected_vault_id=expected_vault_id,
            idempotency_key=idempotency_key,
            requested_by=requested_by,
        )
        if receipt.status == "retired":
            await self._finish_completed_marker(
                manifest,
                expected_vault_id=expected_vault_id,
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
            expected_vault_id=expected_vault_id,
            idempotency_key=idempotency_key,
            requested_by=requested_by,
        )
        await self._finish_completed_marker(
            manifest,
            expected_vault_id=expected_vault_id,
            idempotency_key=idempotency_key,
            requested_by=requested_by,
        )
        return receipt
