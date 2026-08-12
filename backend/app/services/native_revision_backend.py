"""Complete native RevisionBackend facade for the guarded M1 arm."""

from __future__ import annotations

import asyncio
import difflib
import uuid
from datetime import UTC, datetime
from typing import Any

import asyncpg

from app.db.postgres import get_pool
from app.exceptions import NotFoundError
from app.repositories.native_revision_repo import NativeRevisionRepository
from app.services.document_service import _parse_markdown
from app.services.native_document_service import NativeDocumentService
from app.services.native_payload_verification import (
    payload_store_for_placement,
    verify_native_head_body,
)
from app.services.native_revision_service import NativeRevisionService


class NativeRevisionBackend:
    """Native implementation of every revision method selected by A1."""

    def __init__(
        self,
        *,
        pool: asyncpg.Pool | None = None,
        document_service: NativeDocumentService | None = None,
    ):
        self._injected_pool = pool
        self.document_service = document_service or NativeDocumentService(pool=pool)

    async def _pool(self) -> asyncpg.Pool:
        return self._injected_pool or await get_pool()

    async def _vault_id(self, vault: str) -> uuid.UUID | None:
        pool = await self._pool()
        async with pool.acquire() as conn:
            return await conn.fetchval("SELECT id FROM vaults WHERE name = $1", vault)

    @staticmethod
    def _public_action(action: str) -> str:
        return "update" if action == "replace" else action

    @staticmethod
    def _activity_file(row: dict) -> dict[str, str]:
        action = row["action"]
        if action == "create":
            change = "added"
        elif action == "delete":
            change = "deleted"
        else:
            change = "modified"
        return {"path": row["path_at_revision"], "change": change}

    @staticmethod
    def _parse_since(since: str | None) -> datetime | None:
        if not since:
            return None
        try:
            parsed = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed

    async def vault_activity(
        self,
        vault: str,
        *,
        max_count: int,
        since: str | None,
        path: str | None,
    ) -> list[dict[str, Any]]:
        vault_id = await self._vault_id(vault)
        if vault_id is None:
            return []
        repository = NativeRevisionRepository(await self._pool())
        rows = await repository.list_activity(
            namespace_id=vault_id,
            # This facade is the Document-only feed selected by A1; text-File
            # mutations still land activity rows (C3 provenance) but must
            # never surface in the Document envelope C6 froze.
            surface="document",
            limit=max_count,
            since=self._parse_since(since),
            path=path,
        )
        return [
            {
                "hash": row["revision_id"],
                "subject": row["subject"] or "",
                "author": row["actor"],
                "date": row["occurred_at"].isoformat(),
                "action": self._public_action(row["action"]),
                "summary": row["summary"] or "",
                "agent": row["actor"],
                "files": [self._activity_file(row)],
            }
            for row in rows
        ]

    async def recent_changes(
        self,
        user_id: str,
        *,
        vault: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        try:
            principal = uuid.UUID(user_id)
        except ValueError:
            principal = None
        repository = NativeRevisionRepository(await self._pool())
        rows = await repository.list_recent_document_heads(
            user_id=principal,
            vault=vault,
            limit=limit,
        )
        return await asyncio.to_thread(self._verified_changes, rows)

    @staticmethod
    def _verified_changes(rows: list[dict]) -> list[dict[str, Any]]:
        """Bind every listed body to its manifest before reading a title from it.

        This list used to decode ``canonical_bytes`` with no verification at
        all, which trusted the payload table more than every other native read
        path (grep, search, and the derived worker all verify). It also could
        not survive P1's mixed corpus, because a title has to be parsed out of
        bodies stored under either placement. Verification is fail-closed and
        runs off the event loop: SHA-256 over the whole listed corpus is not
        request-thread work.
        """
        changes: list[dict[str, Any]] = []
        for row in rows:
            canonical = verify_native_head_body(row)
            metadata, _ = _parse_markdown(canonical.decode("utf-8", errors="strict"))
            changes.append(
                {
                    "doc_id": str(row["resource_id"]),
                    "vault": row["vault_name"],
                    "path": row["path"],
                    "title": metadata.get("title") or row["path"].rsplit("/", 1)[-1],
                    "type": metadata.get("type") or "note",
                    "commit": row["revision_id"],
                    "changed_at": row["updated_at"].isoformat(),
                }
            )
        return changes

    async def _resolve_resource(
        self,
        vault: str,
        doc_ref: str,
    ) -> tuple[uuid.UUID, dict] | None:
        vault_id = await self._vault_id(vault)
        if vault_id is None:
            return None
        repository = NativeRevisionRepository(await self._pool())
        resource = await repository.resolve_live_reference(
            namespace_id=vault_id,
            surface="document",
            reference=doc_ref,
        )
        if resource is None:
            return None
        return vault_id, resource

    async def _revision_bytes(self, row: dict) -> bytes | None:
        """Open one historical Revision body through its own placement.

        ``selected_placement`` is an immutable per-Revision manifest fact. A
        document created before P1 and replaced after it has a
        ``m1-reference-payload-v1`` parent and a ``pg-bodystore-v1`` child, so
        hardcoding either adapter makes native diff fail closed on one side of
        the pair.
        """
        locator = row.get("private_locator")
        if locator is None:
            return None
        store = payload_store_for_placement(await self._pool(), row["selected_placement"])
        return await store.open_verified(locator)

    async def document_diff(
        self,
        vault: str,
        doc_ref: str,
        commit: str,
    ) -> dict[str, Any] | None:
        resolved = await self._resolve_resource(vault, doc_ref)
        if resolved is None:
            return None
        _, resource = resolved
        repository = NativeRevisionRepository(await self._pool())
        selected = await repository.get_revision(
            resource_id=resource["resource_id"],
            revision_id=commit,
        )
        if selected is None:
            return {
                "file": resource["current_path"],
                "commit": commit,
                "type": "unknown",
                "diff": "",
                "error": "commit not found",
            }
        selected_bytes = await self._revision_bytes(selected)
        parent_id = selected["parent_revision_id"]
        parent = (
            await repository.get_revision(
                resource_id=resource["resource_id"],
                revision_id=parent_id,
            )
            if parent_id
            else None
        )
        parent_bytes = await self._revision_bytes(parent) if parent is not None else None
        if selected["action"] == "create":
            text = (selected_bytes or b"").decode("utf-8")
            return {
                "file": resource["current_path"],
                "commit": commit,
                "type": "added",
                "diff": "\n".join(f"+{line}" for line in text.split("\n")),
            }
        if selected["action"] == "delete":
            text = (parent_bytes or b"").decode("utf-8")
            return {
                "file": resource["current_path"],
                "commit": commit,
                "type": "deleted",
                "diff": "\n".join(f"-{line}" for line in text.split("\n")),
            }
        before = (parent_bytes or b"").decode("utf-8").splitlines()
        after = (selected_bytes or b"").decode("utf-8").splitlines()
        patch = "\n".join(
            difflib.unified_diff(
                before,
                after,
                fromfile=f"a/{selected['path_at_revision']}",
                tofile=f"b/{selected['path_at_revision']}",
                lineterm="",
            )
        )
        return {
            "file": resource["current_path"],
            "commit": commit,
            "type": "modified" if patch else "unchanged",
            "diff": patch,
        }

    async def document_version(
        self,
        vault: str,
        doc_ref: str,
        version: str,
    ) -> tuple[dict[str, Any], str] | None:
        current = await self.document_service.get(vault, doc_ref)
        if not 7 <= len(version) <= 40 or any(ch not in "0123456789abcdef" for ch in version):
            return None
        try:
            vault_id = await self._vault_id(vault)
            assert vault_id is not None
            selected = await NativeRevisionService(await self._pool()).get_revision(
                namespace_id=vault_id,
                surface="document",
                reference=current.path,
                revision_id=version,
            )
        except NotFoundError:
            return None
        return current.model_dump(), selected.text

    async def document_history(
        self,
        vault: str,
        doc_ref: str,
        *,
        limit: int,
    ) -> dict[str, Any]:
        return await self.document_service.history(vault, doc_ref, limit=limit)


def native_revision_backend_factory() -> NativeRevisionBackend:
    """Synchronous process factory; the selected pool remains async/lazy."""
    return NativeRevisionBackend()
