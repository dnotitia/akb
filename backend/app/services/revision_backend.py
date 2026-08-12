"""Process-scoped selection of the document revision implementation.

The stable bare-Git document service remains the default. Native is an
explicit process-scoped choice, while the historical M1 selector remains a
guarded compatibility path. Selection happens once at a composition root,
never from request or vault data.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from threading import Lock
from typing import Any, Protocol, cast

from app.config import NATIVE_REVISION_M1_MEASUREMENT_DATABASE_NAME, settings
from app.db.postgres import get_pool
from app.repositories.document_repo import DocumentRepository
from app.services.document_service import DocumentService
from app.services.git_service import GitService


class NativeRevisionBackendUnavailableError(RuntimeError):
    """A guarded native backend was selected but is not installed."""


class RevisionBackend(Protocol):
    """The complete revision surface consumed by document composition roots."""

    document_service: DocumentService

    async def vault_activity(
        self, vault: str, *, max_count: int, since: str | None, path: str | None,
    ) -> list[dict[str, Any]]: ...

    async def recent_changes(
        self, user_id: str, *, vault: str | None, limit: int,
    ) -> list[dict[str, Any]]: ...

    async def document_diff(
        self, vault: str, doc_ref: str, commit: str,
    ) -> dict[str, Any] | None: ...

    async def document_version(
        self, vault: str, doc_ref: str, version: str,
    ) -> tuple[dict[str, Any], str] | None: ...

    async def document_history(
        self, vault: str, doc_ref: str, *, limit: int,
    ) -> dict[str, Any]: ...


class LegacyRevisionBackend:
    """Bare-Git revision adapter retaining the pre-M1 route behavior."""

    def __init__(self) -> None:
        self.document_service = DocumentService()
        self._git = GitService()

    async def vault_activity(
        self, vault: str, *, max_count: int, since: str | None, path: str | None,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._git.vault_log, vault, max_count=max_count, since=since, path=path,
        )

    async def recent_changes(
        self, user_id: str, *, vault: str | None, limit: int,
    ) -> list[dict[str, Any]]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            if vault:
                rows = await conn.fetch(
                    """
                    SELECT d.id, d.title, d.path, d.doc_type, d.current_commit,
                           d.updated_at, v.name AS vault_name, d.metadata
                    FROM documents d
                    JOIN vaults v ON d.vault_id = v.id
                    WHERE v.name = $1
                    ORDER BY d.updated_at DESC
                    LIMIT $2
                    """,
                    vault, limit,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT d.id, d.title, d.path, d.doc_type, d.current_commit,
                           d.updated_at, v.name AS vault_name, d.metadata
                    FROM documents d
                    JOIN vaults v ON d.vault_id = v.id
                    LEFT JOIN vault_access va
                        ON va.vault_id = v.id AND va.user_id = $1
                    WHERE v.owner_id = $1
                       OR va.user_id = $1
                       OR v.public_access IN ('reader', 'writer')
                    ORDER BY d.updated_at DESC
                    LIMIT $2
                    """,
                    user_id, limit,
                )

        changes = []
        for row in rows:
            metadata = row["metadata"] or {}
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except json.JSONDecodeError:
                    metadata = {}
            changes.append({
                "doc_id": metadata.get("id") or str(row["id"]),
                "vault": row["vault_name"],
                "path": row["path"],
                "title": row["title"],
                "type": row["doc_type"] or "note",
                "commit": row["current_commit"],
                "changed_at": row["updated_at"].isoformat() if row["updated_at"] else None,
            })
        return changes

    async def document_diff(
        self, vault: str, doc_ref: str, commit: str,
    ) -> dict[str, Any] | None:
        document = await self._find_document(vault, doc_ref)
        if not document:
            return None
        return await asyncio.to_thread(self._git.file_diff, vault, document["path"], commit)

    async def document_version(
        self, vault: str, doc_ref: str, version: str,
    ) -> tuple[dict[str, Any], str] | None:
        document = await self._find_document(vault, doc_ref)
        if not document:
            from app.exceptions import NotFoundError

            raise NotFoundError("Document", doc_ref)
        raw = await asyncio.to_thread(self._git.read_file, vault, document["path"], version)
        if raw is None:
            return None
        return document, raw

    async def _find_document(self, vault: str, doc_ref: str) -> dict[str, Any] | None:
        pool = await get_pool()
        doc_repo = DocumentRepository(pool)
        async with pool.acquire() as conn:
            vault_row = await conn.fetchrow("SELECT id FROM vaults WHERE name = $1", vault)
            if not vault_row:
                return None
            document = await doc_repo.find_by_ref_with_conn(conn, vault_row["id"], doc_ref)
            if not document:
                return None
        return document

    async def document_history(
        self, vault: str, doc_ref: str, *, limit: int,
    ) -> dict[str, Any]:
        return await self.document_service.history(vault, doc_ref, limit=limit)


RevisionBackendFactory = Callable[[], RevisionBackend]

_lock = Lock()
_revision_backend: RevisionBackend | None = None
_selected_backend: str | None = None
_native_revision_backend_factory: RevisionBackendFactory | None = None


def canonical_document_revision_backend(backend: str) -> str:
    """Return the public canonical name for a configured selector."""
    if backend in {"bare_git", "bare_git_current"}:
        return "bare_git"
    if backend == "postgres_native":
        return "postgres_native"
    if backend == "native_ledger_m1":
        # Diagnostics expose only the stable implementation family.  The raw
        # measurement selector remains visible in configuration and receipts.
        return "postgres_native"
    raise RuntimeError(f"unsupported document revision backend: {backend!r}")


def register_native_revision_backend(factory: RevisionBackendFactory) -> None:
    """Register the native facade before the process selects a backend.

    A3 owns the native implementation and calls this while composing the
    process. Replacing an already-selected backend is forbidden so a process
    cannot switch revision semantics after serving requests.
    """
    global _native_revision_backend_factory
    with _lock:
        if _revision_backend is not None:
            raise RuntimeError("document revision backend is already selected")
        _native_revision_backend_factory = factory


def _assert_native_measurement_safety() -> None:
    """Defence in depth for callers that mutate settings in-process."""
    if not settings.native_revision_m1_measurement_only:
        raise RuntimeError(
            "native_ledger_m1 requires native_revision_m1_measurement_only=true"
        )
    if settings.db_name != NATIVE_REVISION_M1_MEASUREMENT_DATABASE_NAME:
        raise RuntimeError(
            "native_ledger_m1 requires dedicated measurement database "
            f"{NATIVE_REVISION_M1_MEASUREMENT_DATABASE_NAME!r}"
        )


def get_revision_backend() -> RevisionBackend:
    """Return the complete revision facade selected once for this process."""
    global _revision_backend, _selected_backend, _native_revision_backend_factory
    with _lock:
        if _revision_backend is not None:
            return _revision_backend

        backend = settings.document_revision_backend
        revision_backend: RevisionBackend
        if backend in {"bare_git", "bare_git_current"}:
            revision_backend = LegacyRevisionBackend()
        elif backend == "native_ledger_m1":
            _assert_native_measurement_safety()
            if _native_revision_backend_factory is None:
                # Routes and the stdio MCP server select their facade from
                # module globals during import.  Loading the native factory at
                # this exact composition seam guarantees it is available
                # before either caller receives a service, without importing
                # any measurement-only code during default legacy startup.
                from app.services.native_revision_backend import (
                    native_revision_backend_factory,
                )

                _native_revision_backend_factory = cast(
                    RevisionBackendFactory,
                    native_revision_backend_factory,
                )
            native_factory = _native_revision_backend_factory
            assert native_factory is not None
            revision_backend = native_factory()
        elif backend == "postgres_native":
            if _native_revision_backend_factory is None:
                from app.services.native_revision_backend import (
                    native_revision_backend_factory,
                )

                _native_revision_backend_factory = cast(
                    RevisionBackendFactory,
                    native_revision_backend_factory,
                )
            native_factory = _native_revision_backend_factory
            assert native_factory is not None
            revision_backend = native_factory()
        else:  # Settings validates this, but fail closed for in-process mutation.
            raise RuntimeError(f"unsupported document revision backend: {backend!r}")

        _revision_backend = revision_backend
        _selected_backend = canonical_document_revision_backend(backend)
        return revision_backend


def get_document_service() -> DocumentService:
    """Return the document-service member of the selected revision facade."""
    return get_revision_backend().document_service


def selected_document_revision_backend() -> str | None:
    """Return the process-selected backend, or ``None`` before composition."""
    return _selected_backend


def reset_document_service_for_tests() -> None:
    """Clear process state for isolated unit tests only."""
    global _revision_backend, _selected_backend, _native_revision_backend_factory
    with _lock:
        _revision_backend = None
        _selected_backend = None
        _native_revision_backend_factory = None
