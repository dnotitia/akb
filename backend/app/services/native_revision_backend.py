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
from app.repositories.native_revision_cutover_repo import NativeRevisionCutoverRepository
from app.repositories.native_revision_migration_repo import (
    NativeRevisionMigrationRepository,
)
from app.repositories.native_revision_repo import (
    NativeRevisionRepository,
    NativeRevisionSelectorAmbiguousError,
)
from app.services.document_service import _parse_markdown
from app.services.git_service import FixedRefHistoryError, GitService
from app.services.native_document_service import NativeDocumentService
from app.services.native_payload_verification import (
    payload_store_for_placement,
    verify_native_head_body,
)
from app.services.native_revision_service import NativeRevisionService
from app.services.user_directory import resolve_display_names


class NativeRevisionBackend:
    """Native implementation of every revision method selected by A1."""

    def __init__(
        self,
        *,
        pool: asyncpg.Pool | None = None,
        document_service: NativeDocumentService | None = None,
        legacy_git: GitService | None = None,
    ):
        self._injected_pool = pool
        # Keep the retained Legacy reader out of current Native-only paths.
        # It is needed only when a completed migration mapping resolves to a
        # Legacy bridge; NativeDocumentService follows the same lazy boundary.
        self._legacy_git = legacy_git
        self._created_document_service = document_service is None
        self.document_service = document_service or NativeDocumentService(
            pool=pool,
            legacy_git=legacy_git,
        )

    def _legacy_git_reader(self) -> GitService:
        """Construct the retained Legacy reader only for a bridge operation."""
        if self._legacy_git is None:
            self._legacy_git = GitService()
            if self._created_document_service:
                self.document_service._legacy_git = self._legacy_git
        return self._legacy_git

    async def _pool(self) -> asyncpg.Pool:
        return self._injected_pool or await get_pool()

    async def _vault_id(self, vault: str) -> uuid.UUID | None:
        pool = await self._pool()
        async with pool.acquire() as conn:
            return await conn.fetchval("SELECT id FROM vaults WHERE name = $1", vault)

    async def _annotate_history_authors(
        self, entries: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Apply the same id-or-username display-name contract as Legacy."""

        authors = [entry.get("author") for entry in entries]
        names = await resolve_display_names(authors, pool=await self._pool())
        for entry in entries:
            author = entry.get("author")
            if isinstance(author, str) and author in names:
                entry["author_name"] = names[author]
        return entries

    async def _annotated_history_payload(
        self, payload: dict[str, Any],
    ) -> dict[str, Any]:
        payload["history"] = await self._annotate_history_authors(payload["history"])
        return payload

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
    def _path_matches(path: str, scope: str | None) -> bool:
        return scope is None or path == scope or path.startswith(f"{scope}/")

    @classmethod
    def _legacy_activity_files(cls, activity: dict[str, Any]) -> list[dict[str, str]]:
        files: list[dict[str, str]] = []
        changed_paths = activity.get("changed_paths")
        if not isinstance(changed_paths, (list, tuple)):
            return files
        for changed in changed_paths:
            if not isinstance(changed, dict):
                continue
            path = changed.get("path_to") or changed.get("path_from")
            if not isinstance(path, str):
                continue
            action = changed.get("change")
            files.append(
                {
                    "path": path,
                    "change": ("added" if action == "create" else "deleted" if action == "delete" else "modified"),
                }
            )
        return files

    @classmethod
    def _legacy_activity_matches_path(cls, activity: dict[str, Any], scope: str | None) -> bool:
        if scope is None:
            return True
        changed_paths = activity.get("changed_paths")
        if not isinstance(changed_paths, (list, tuple)):
            return False
        for changed in changed_paths:
            if not isinstance(changed, dict):
                continue
            for path in (changed.get("path_from"), changed.get("path_to")):
                if isinstance(path, str) and cls._path_matches(path, scope):
                    return True
        return False

    @classmethod
    def _legacy_activity_entry(cls, mapping, snapshot: dict[str, Any]) -> dict[str, Any] | None:
        activity = snapshot.get("activity")
        history = snapshot.get("history")
        if not isinstance(activity, dict) or not isinstance(history, list):
            return None
        committed_at = activity.get("committed_at")
        actor = activity.get("actor")
        subject = activity.get("subject")
        summary = activity.get("summary")
        action = activity.get("action")
        if not (
            isinstance(committed_at, datetime)
            and isinstance(actor, str)
            and isinstance(subject, str)
            and isinstance(summary, str)
            and isinstance(action, str)
        ):
            return None
        author = next(
            (
                row.get("author")
                for row in history
                if isinstance(row, dict)
                and row.get("legacy_git_oid") == mapping.legacy_git_oid
                and isinstance(row.get("author"), str)
            ),
            None,
        )
        if not isinstance(author, str):
            return None
        return {
            "hash": mapping.legacy_git_oid[:12],
            "subject": subject,
            "author": author,
            "date": committed_at.isoformat(),
            "action": action,
            "summary": summary,
            "agent": actor,
            "files": cls._legacy_activity_files(activity),
        }

    async def _bridged_legacy_activity(
        self,
        *,
        vault: str,
        vault_id: uuid.UUID,
        max_count: int,
        since: datetime | None,
        path: str | None,
    ) -> tuple[list[dict[str, Any]], set[str]]:
        """Re-open Legacy activity through the committed vault fixed ref.

        A completed existing-database cutover already persists one exact Git
        tip per vault. Reading that full graph keeps Legacy events for wholly
        deleted Resources and pre-recreate lifecycles, which cannot be
        reconstructed from only the surviving native Resource anchors. Before
        authority exists, retain the C9 per-resource bridge for the guarded
        migration path.
        """
        pool = await self._pool()
        migration = NativeRevisionMigrationRepository(pool)
        try:
            anchors = await migration.list_completed_lineage_anchors(
                namespace_id=vault_id,
            )
        except asyncpg.UndefinedTableError:
            return [], set()

        try:
            fixed_ref = await NativeRevisionCutoverRepository(pool).committed_vault_fixed_git_oid(vault_id)
        except asyncpg.UndefinedTableError:
            fixed_ref = None

        bridged_by_hash: dict[str, dict[str, Any]] = {}
        mapped_native_ids: set[str] = set()
        for anchor in anchors:
            mappings = await migration.list_resource_mappings(
                namespace_id=vault_id,
                resource_id=anchor.resource_id,
            )
            for mapping in mappings:
                if mapping.native_revision_id is not None:
                    mapped_native_ids.add(mapping.native_revision_id)
                if fixed_ref is not None:
                    continue
                try:
                    snapshot = await asyncio.to_thread(
                        self._legacy_git_reader().manual_fixed_ref_history,
                        vault,
                        mapping.fixed_git_oid,
                        mapping.path_at_revision,
                        current_commit=mapping.legacy_git_oid,
                    )
                except FixedRefHistoryError:
                    continue
                activity = snapshot.get("activity")
                if not isinstance(activity, dict) or not self._legacy_activity_matches_path(activity, path):
                    continue
                entry = self._legacy_activity_entry(mapping, snapshot)
                if entry is None:
                    continue
                occurred_at = activity.get("committed_at")
                if since is not None and isinstance(occurred_at, datetime) and occurred_at < since:
                    continue
                existing = bridged_by_hash.get(entry["hash"])
                if existing is None:
                    bridged_by_hash[entry["hash"]] = entry
                    continue
                if {key: existing[key] for key in existing if key != "files"} != {
                    key: entry[key] for key in entry if key != "files"
                }:
                    continue
                known_files = {(item["path"], item["change"]) for item in existing["files"]}
                for changed in entry["files"]:
                    signature = (changed["path"], changed["change"])
                    if signature not in known_files:
                        existing["files"].append(changed)
                        known_files.add(signature)
        if fixed_ref is not None:
            events = await asyncio.to_thread(
                self._legacy_git_reader().manual_fixed_ref_vault_log,
                vault,
                fixed_ref,
                max_count=max_count,
                since=None if since is None else since.isoformat(),
                path=path,
            )
            return events, mapped_native_ids
        return list(bridged_by_hash.values()), mapped_native_ids

    async def _legacy_mappings_for_selector(
        self,
        migration: NativeRevisionMigrationRepository,
        *,
        resource_id: uuid.UUID,
        selector: str,
    ) -> list:
        try:
            if len(selector) == 40:
                mapping = await migration.exact_mapping(
                    resource_id=resource_id,
                    legacy_git_oid=selector,
                )
                return [] if mapping is None else [mapping]
            if 7 <= len(selector) < 40:
                return await migration.prefix_mappings(
                    resource_id=resource_id,
                    legacy_git_prefix=selector,
                )
        except asyncpg.UndefinedTableError:
            return []
        return []

    async def _frozen_legacy_diff(self, vault: str, mapping, commit: str) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                self._legacy_git_reader().manual_fixed_ref_file_diff,
                vault,
                mapping.fixed_git_oid,
                mapping.path_at_revision,
                mapping.legacy_git_oid,
            )
        except FixedRefHistoryError:
            return {
                "file": mapping.path_at_revision,
                "commit": commit,
                "type": "unknown",
                "diff": "",
                "error": "frozen legacy diff is unavailable",
            }

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
        parsed_since = self._parse_since(since)
        rows = await repository.list_activity(
            namespace_id=vault_id,
            # This facade is the Document-only feed selected by A1; text-File
            # mutations still land activity rows (C3 provenance) but must
            # never surface in the Document envelope C6 froze.
            surface="document",
            limit=max_count,
            since=parsed_since,
            path=path,
        )
        legacy, mapped_native_ids = await self._bridged_legacy_activity(
            vault=vault,
            vault_id=vault_id,
            max_count=max_count,
            since=parsed_since,
            path=path,
        )
        native = [
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
            if row["revision_id"] not in mapped_native_ids
        ]
        events = [*native, *legacy]
        events.sort(key=lambda event: (event["date"], event["hash"]), reverse=True)
        return events[:max_count]

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
        vault_id, resource = resolved
        pool = await self._pool()
        repository = NativeRevisionRepository(pool)
        migration = NativeRevisionMigrationRepository(pool)
        selected = await repository.get_revision(
            resource_id=resource["resource_id"],
            revision_id=commit,
        )
        mapping = None
        if selected is not None:
            try:
                mapping = await migration.mapping_for_native_revision(
                    namespace_id=vault_id,
                    resource_id=resource["resource_id"],
                    native_revision_id=selected["revision_id"],
                )
            except asyncpg.UndefinedTableError:
                mapping = None
        else:
            matches = await self._legacy_mappings_for_selector(
                migration,
                resource_id=resource["resource_id"],
                selector=commit,
            )
            if len(matches) > 1:
                return {
                    "file": resource["current_path"],
                    "commit": commit,
                    "type": "unknown",
                    "diff": "",
                    "error": "commit is ambiguous",
                }
            mapping = matches[0] if matches else None
        if mapping is not None:
            return await self._frozen_legacy_diff(vault, mapping, commit)
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
        resolved = await self._resolve_resource(vault, doc_ref)
        if resolved is None:
            return None
        vault_id, resource = resolved
        pool = await self._pool()
        try:
            selected = await NativeRevisionService(pool).get_revision(
                namespace_id=vault_id,
                surface="document",
                reference=current.path,
                revision_id=version,
            )
        except NotFoundError:
            migration = NativeRevisionMigrationRepository(pool)
            matches = await self._legacy_mappings_for_selector(
                migration,
                resource_id=resource["resource_id"],
                selector=version,
            )
            if len(matches) > 1:
                raise NativeRevisionSelectorAmbiguousError(version)
            if not matches:
                return None
            mapping = matches[0]
            raw = await asyncio.to_thread(
                self._legacy_git_reader().read_file,
                vault,
                mapping.path_at_revision,
                mapping.legacy_git_oid,
            )
            if raw is None:
                return None
            return current.model_dump(), raw
        return current.model_dump(), selected.text

    async def document_history(
        self,
        vault: str,
        doc_ref: str,
        *,
        limit: int,
    ) -> dict[str, Any]:
        native = await self.document_service.history(vault, doc_ref, limit=limit)
        resolved = await self._resolve_resource(vault, doc_ref)
        if resolved is None:
            return await self._annotated_history_payload(native)
        vault_id, resource = resolved
        migration = NativeRevisionMigrationRepository(await self._pool())
        try:
            mappings = await migration.list_resource_mappings(
                namespace_id=vault_id,
                resource_id=resource["resource_id"],
            )
        except asyncpg.UndefinedTableError:
            return await self._annotated_history_payload(native)
        if not mappings:
            return await self._annotated_history_payload(native)

        fixed_refs = {mapping.fixed_git_oid for mapping in mappings}
        if len(fixed_refs) != 1:
            # Completed immutable mappings are expected to describe one
            # frozen lineage.  Do not synthesize history across authorities.
            return await self._annotated_history_payload(native)
        frozen_head = mappings[-1]
        try:
            snapshot = await asyncio.to_thread(
                self._legacy_git_reader().manual_fixed_ref_history,
                vault,
                frozen_head.fixed_git_oid,
                frozen_head.path_at_revision,
                current_commit=frozen_head.legacy_git_oid,
            )
        except FixedRefHistoryError:
            return await self._annotated_history_payload(native)
        rows = snapshot.get("history")
        if not isinstance(rows, list):
            return await self._annotated_history_payload(native)
        by_oid = {
            row.get("legacy_git_oid"): row
            for row in rows
            if isinstance(row, dict) and isinstance(row.get("legacy_git_oid"), str)
        }
        if any(mapping.legacy_git_oid not in by_oid for mapping in mappings):
            return await self._annotated_history_payload(native)

        # Native genesis represents the same head as the newest legacy
        # mapping. Replace that internal token with the public Git history,
        # while retaining any genuinely-new Native revisions before it.
        mapped_native_ids = {
            mapping.native_revision_id
            for mapping in mappings
            if mapping.native_revision_id is not None
        }
        native_only = [
            entry
            for entry in native["history"]
            if entry.get("hash") not in mapped_native_ids
        ]
        legacy = []
        for mapping in reversed(mappings):
            row = by_oid[mapping.legacy_git_oid]
            committed_at = row["committed_at"]
            legacy.append(
                {
                    "hash": mapping.legacy_git_oid[:12],
                    "message": row.get("message") or row.get("action") or "legacy revision",
                    "author": row.get("author") or "unknown",
                    "date": committed_at.isoformat(),
                }
            )
        combined = (native_only + legacy)[:limit]
        return {
            "uri": native["uri"],
            "history": await self._annotate_history_authors(combined),
        }


def native_revision_backend_factory() -> NativeRevisionBackend:
    """Synchronous process factory; the selected pool remains async/lazy."""
    return NativeRevisionBackend()
