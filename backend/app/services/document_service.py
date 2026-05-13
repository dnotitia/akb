"""Document service — orchestrates Put/Get/Update/Delete.

Coordinates Git, DB repositories, and indexing pipeline.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit


def _safe_remote_host(url: str) -> str:
    """Redact userinfo (potential PAT) before logging. Only the hostname
    is retained since the rest of the URL can leak credentials when the
    caller passes a `https://token@host/path` form."""
    try:
        return urlsplit(url).hostname or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"

import frontmatter

from app.db.postgres import get_pool
from app.exceptions import AKBError, ConflictError, NotFoundError, ValidationError
from app.models.document import (
    BrowseItem,
    BrowseResponse,
    DocumentPutRequest,
    DocumentPutResponse,
    DocumentResponse,
    DocumentUpdateRequest,
)
from app.repositories.document_repo import CollectionRepository, DocumentRepository
from app.repositories.events_repo import emit_event
from app.repositories.vault_external_git_repo import VaultExternalGitRepository
from app.repositories.vault_repo import VaultRepository
from app.services.git_service import GitService
from app.services.index_service import (
    build_doc_metadata_header,
    chunk_markdown,
    write_source_chunks,
    delete_document_chunks,
)
from app.services.kg_service import delete_document_relations, store_document_relations
from app.services.uri_service import doc_uri, table_uri, file_uri
from app.repositories import table_data_repo
from app.utils import ensure_dict, ensure_list

logger = logging.getLogger("akb.documents")


class EditError(AKBError):
    """Raised when an edit operation cannot be performed."""

    def __init__(self, message: str):
        super().__init__(message, status_code=400)


def _normalize_collection(collection: str) -> str:
    """Normalize and validate a collection path.

    - Strips leading/trailing slashes
    - Collapses multiple slashes
    - Rejects '..' segments (path traversal)
    - Rejects absolute paths
    - Returns "" for empty/root
    """
    if not collection:
        return ""
    # Strip leading/trailing slashes
    normalized = collection.strip().strip("/")
    if not normalized:
        return ""
    # Reject path traversal
    parts = [p for p in normalized.split("/") if p]
    for part in parts:
        if part == ".." or part == ".":
            raise ValueError(
                f"Invalid collection path: '{collection}'. "
                "Path traversal segments ('.', '..') are not allowed."
            )
        if "\x00" in part or "/" in part or "\\" in part:
            raise ValueError(f"Invalid character in collection path: '{collection}'")
    return "/".join(parts)


def _slugify(title: str) -> str:
    slug = title.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[-\s]+", "-", slug)
    return slug[:80]


def _build_frontmatter(req: DocumentPutRequest, doc_id: str, now: datetime) -> dict:
    fm = {
        "id": doc_id,
        "title": req.title,
        "type": req.type,
        "status": "draft",
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "tags": req.tags,
    }
    if req.domain:
        fm["domain"] = req.domain
    if req.summary:
        fm["summary"] = req.summary
    else:
        # Auto-generate summary from content (first non-heading paragraph, max 200 chars)
        for line in req.content.split("\n"):
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("---") or stripped.startswith("|") or stripped.startswith("```"):
                continue
            fm["summary"] = stripped[:200]
            break
    if req.depends_on:
        fm["depends_on"] = req.depends_on
    if req.related_to:
        fm["related_to"] = req.related_to
    if req.metadata:
        fm.update(req.metadata)
    return fm


def _compose_markdown(fm_dict: dict, body: str) -> str:
    post = frontmatter.Post(body, **fm_dict)
    return frontmatter.dumps(post)


def _parse_markdown(content: str) -> tuple[dict, str]:
    post = frontmatter.loads(content)
    return dict(post.metadata), post.content


class DocumentService:
    def __init__(self, git: GitService | None = None):
        self.git = git or GitService()

    async def _repos(self):
        pool = await get_pool()
        return VaultRepository(pool), DocumentRepository(pool), CollectionRepository(pool)

    # ── Put ───────────────────────────────────────────────────

    async def put(self, req: DocumentPutRequest, agent_id: str | None = None) -> DocumentPutResponse:
        vault_repo, doc_repo, coll_repo = await self._repos()

        vault_id = await vault_repo.get_id_by_name(req.vault)
        if not vault_id:
            raise NotFoundError("Vault", req.vault)

        doc_id = f"d-{uuid.uuid4().hex[:8]}"
        now = datetime.now(timezone.utc)
        slug = _slugify(req.title)
        normalized_collection = _normalize_collection(req.collection)
        file_path = f"{normalized_collection}/{slug}.md" if normalized_collection else f"{slug}.md"

        fm_dict = _build_frontmatter(req, doc_id, now)
        if agent_id:
            fm_dict["created_by"] = agent_id
        md_content = _compose_markdown(fm_dict, req.content)

        # Git commit
        commit_msg = f"[put] {file_path}\n\nagent: {agent_id or 'unknown'}\naction: create\nsummary: {req.title}"
        commit_hash = await asyncio.to_thread(
            self.git.commit_file,
            vault_name=req.vault, file_path=file_path,
            content=md_content, message=commit_msg,
            author_name=agent_id or "AKB System",
        )
        logger.info("Document created: %s (commit: %s)", file_path, commit_hash[:8])

        # DB
        collection_id = await coll_repo.get_or_create(vault_id, req.collection)
        metadata = {**(req.metadata or {}), "id": doc_id}
        pg_doc_id = await doc_repo.create(
            vault_id=vault_id, collection_id=collection_id, path=file_path,
            title=req.title, doc_type=req.type, status="draft",
            summary=fm_dict.get("summary") or req.summary, domain=req.domain, created_by=agent_id,
            now=now, commit_hash=commit_hash, tags=req.tags, metadata=metadata,
        )

        # Index: write chunks into PG (truth) + best-effort vector-store upsert.
        # Prepend a doc-level metadata header to every chunk so BM25 and
        # dense both see the title/summary/tags regardless of which body
        # section matched.
        meta_header = build_doc_metadata_header(
            vault_name=req.vault, path=file_path, title=req.title,
            summary=fm_dict.get("summary") or req.summary,
            tags=req.tags, doc_type=req.type,
        )
        chunks = chunk_markdown(req.content, metadata_header=meta_header)

        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                chunks_indexed = await write_source_chunks(
                    conn, "document", str(pg_doc_id),
                    vault_id=vault_id,
                    chunks=chunks,
                )
                await store_document_relations(
                    conn, vault_id, req.vault, file_path,
                    req.depends_on, req.related_to, [],
                    req.content,
                )
                await emit_event(
                    conn, "document.put",
                    vault_id=vault_id, ref_type="document", ref_id=doc_id,
                    actor_id=agent_id,
                    payload={
                        "vault": req.vault,
                        "path": file_path,
                        "title": req.title,
                        "doc_type": req.type,
                        "commit_hash": commit_hash,
                        "collection": normalized_collection,
                    },
                )

        await coll_repo.increment_count(collection_id, now)

        return DocumentPutResponse(
            doc_id=doc_id, vault=req.vault, path=file_path,
            commit_hash=commit_hash, chunks_indexed=chunks_indexed, entities_found=0,
        )

    # ── Get ───────────────────────────────────────────────────

    async def get(self, vault: str, doc_ref: str) -> DocumentResponse:
        vault_repo, doc_repo, _ = await self._repos()

        vault_id = await vault_repo.get_id_by_name(vault)
        if not vault_id:
            raise NotFoundError("Vault", vault)

        row = await doc_repo.find_by_ref(vault_id, doc_ref)
        if not row:
            raise NotFoundError("Document", doc_ref)

        content = await asyncio.to_thread(self.git.read_file, vault, row["path"])
        body = ""
        if content:
            _, body = _parse_markdown(content)

        # Derive published state from the publications table. We pick the
        # newest matching publication so the UI is consistent with publishDoc()
        # which reuses the first entry returned by listPublications (DESC order).
        public_slug = await self._get_public_slug(row["id"])

        return DocumentResponse(
            id=str(row["id"]), vault=row["vault_name"], path=row["path"],
            title=row["title"], type=row["doc_type"] or "note", status=row["status"],
            summary=row["summary"], domain=row["domain"], created_by=row["created_by"],
            created_at=row["created_at"], updated_at=row["updated_at"],
            current_commit=row["current_commit"],
            tags=list(row["tags"]) if row["tags"] else [],
            content=body,
            is_public=public_slug is not None,
            public_slug=public_slug,
        )

    async def _get_public_slug(self, doc_id: uuid.UUID) -> str | None:
        """Return the newest publication slug for a document, or None."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval(
                """
                SELECT slug FROM publications
                WHERE document_id = $1 AND resource_type = 'document'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                doc_id,
            )

    # ── Update ────────────────────────────────────────────────

    async def update(self, vault: str, doc_ref: str, req: DocumentUpdateRequest, agent_id: str | None = None) -> DocumentPutResponse:
        vault_repo, doc_repo, _ = await self._repos()

        vault_id = await vault_repo.get_id_by_name(vault)
        if not vault_id:
            raise NotFoundError("Vault", vault)

        row = await doc_repo.find_by_ref(vault_id, doc_ref)
        if not row:
            raise NotFoundError("Document", doc_ref)

        now = datetime.now(timezone.utc)
        pg_doc_id = row["id"]
        file_path = row["path"]

        current_content = await asyncio.to_thread(self.git.read_file, vault, file_path)
        if not current_content:
            raise NotFoundError("Document file", file_path)

        current_fm, current_body = _parse_markdown(current_content)

        # Merge updates
        if req.title:
            current_fm["title"] = req.title
        if req.type:
            current_fm["type"] = req.type
        if req.status:
            current_fm["status"] = req.status
        if req.tags is not None:
            current_fm["tags"] = req.tags
        if req.domain is not None:
            current_fm["domain"] = req.domain
        if req.summary is not None:
            current_fm["summary"] = req.summary
        if req.depends_on is not None:
            current_fm["depends_on"] = req.depends_on
        if req.related_to is not None:
            current_fm["related_to"] = req.related_to
        if req.metadata:
            current_fm.update(req.metadata)
        current_fm["updated_at"] = now.isoformat()

        new_body = req.content if req.content is not None else current_body
        new_md = _compose_markdown(current_fm, new_body)

        message = req.message or f"Update {file_path}"
        commit_msg = f"[update] {file_path}\n\nagent: {agent_id or 'unknown'}\naction: update\nsummary: {message}"
        commit_hash = await asyncio.to_thread(
            self.git.commit_file,
            vault_name=vault, file_path=file_path,
            content=new_md, message=commit_msg,
            author_name=agent_id or "AKB System",
        )
        logger.info("Document updated: %s (commit: %s)", file_path, commit_hash[:8])

        await doc_repo.update(
            pg_doc_id, title=req.title, doc_type=req.type, status=req.status,
            summary=req.summary, domain=req.domain, now=now,
            commit_hash=commit_hash, tags=req.tags,
        )

        chunks_indexed = 0
        if req.content is not None:
            # Use the values that were actually persisted to DB, not the
            # raw request — e.g. req.title may be None when caller kept
            # the title unchanged.
            meta_header = build_doc_metadata_header(
                vault_name=vault, path=file_path,
                title=req.title or row["title"],
                summary=req.summary if req.summary is not None else row["summary"],
                tags=req.tags if req.tags is not None else (list(row["tags"]) if row["tags"] else []),
                doc_type=req.type or row["doc_type"],
            )
            chunks = chunk_markdown(new_body, metadata_header=meta_header)
            pool = await get_pool()
            async with pool.acquire() as conn:
                chunks_indexed = await write_source_chunks(
                    conn, "document", str(pg_doc_id),
                    vault_id=vault_id,
                    chunks=chunks,
                )

        # Re-extract edges when content or relations changed
        if req.content is not None or req.depends_on is not None or req.related_to is not None:
            depends = current_fm.get("depends_on", []) or []
            related = current_fm.get("related_to", []) or []
            implements = current_fm.get("implements", []) or []
            pool = await get_pool()
            async with pool.acquire() as conn:
                await store_document_relations(
                    conn, vault_id, vault, file_path,
                    depends, related, implements,
                    new_body,
                )

        # ref_id uses the user-facing d-prefixed id from metadata when
        # available (subscribers reference docs by that, not the PG UUID).
        # Fall back to the PG UUID — same shape as delete() — instead of
        # `doc_ref` which is whatever string the caller happened to pass.
        meta = ensure_dict(row.get("metadata"))
        public_doc_id = meta.get("id") or str(pg_doc_id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            await emit_event(
                conn, "document.update",
                vault_id=vault_id, ref_type="document", ref_id=public_doc_id,
                actor_id=agent_id,
                payload={
                    "vault": vault,
                    "path": file_path,
                    "commit_hash": commit_hash,
                    "content_changed": req.content is not None,
                },
            )

        return DocumentPutResponse(
            doc_id=doc_ref, vault=vault, path=file_path,
            commit_hash=commit_hash, chunks_indexed=chunks_indexed, entities_found=0,
        )

    # ── Edit ──────────────────────────────────────────────────

    async def edit(
        self,
        vault: str,
        doc_ref: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
        message: str | None = None,
        agent_id: str | None = None,
    ) -> DocumentPutResponse:
        """Edit a document by replacing exact text in its body.

        Args:
            old_string: Exact text to find. Must be unique unless replace_all=True.
            new_string: Replacement text. Can be empty to delete.
            replace_all: If True, replace all occurrences. If False, old_string must be unique.

        Raises:
            EditError: If old_string is empty, not found, or not unique (and replace_all=False).
        """
        vault_repo, doc_repo, _ = await self._repos()

        vault_id = await vault_repo.get_id_by_name(vault)
        if not vault_id:
            raise NotFoundError("Vault", vault)

        row = await doc_repo.find_by_ref(vault_id, doc_ref)
        if not row:
            raise NotFoundError("Document", doc_ref)

        now = datetime.now(timezone.utc)
        pg_doc_id = row["id"]
        file_path = row["path"]

        current_content = await asyncio.to_thread(self.git.read_file, vault, file_path)
        if not current_content:
            raise NotFoundError("Document file", file_path)

        current_fm, current_body = _parse_markdown(current_content)

        # Apply edit — validate old_string and find occurrences
        if not old_string:
            raise EditError("old_string cannot be empty")

        occurrences = current_body.count(old_string)
        if occurrences == 0:
            raise EditError(
                "old_string not found in document body. "
                "Use akb_get to verify current content."
            )
        if occurrences > 1 and not replace_all:
            raise EditError(
                f"old_string appears {occurrences} times in document. "
                f"Add more surrounding context to make it unique, or set replace_all=true."
            )

        if replace_all:
            new_body = current_body.replace(old_string, new_string)
        else:
            new_body = current_body.replace(old_string, new_string, 1)

        if new_body == current_body:
            meta = ensure_dict(row["metadata"])
            return DocumentPutResponse(
                doc_id=meta.get("id", str(pg_doc_id)),
                vault=vault, path=file_path,
                commit_hash=row.get("current_commit") or "",
                chunks_indexed=0, entities_found=0,
            )

        current_fm["updated_at"] = now.isoformat()
        new_md = _compose_markdown(current_fm, new_body)

        msg = message or f"Edit {file_path}"
        commit_msg = f"[edit] {file_path}\n\nagent: {agent_id or 'unknown'}\naction: edit\nsummary: {msg}"
        commit_hash = await asyncio.to_thread(
            self.git.commit_file,
            vault_name=vault, file_path=file_path,
            content=new_md, message=commit_msg,
            author_name=agent_id or "AKB System",
        )
        logger.info("Document edited: %s (commit: %s)", file_path, commit_hash[:8])

        await doc_repo.update(
            pg_doc_id, title=None, doc_type=None, status=None,
            summary=None, domain=None, now=now,
            commit_hash=commit_hash, tags=None,
        )

        # Re-chunk and re-embed (full pipeline — mirrors update())
        meta_header = build_doc_metadata_header(
            vault_name=vault, path=file_path,
            title=row["title"], summary=row["summary"],
            tags=list(row["tags"]) if row["tags"] else [],
            doc_type=row["doc_type"],
        )
        chunks = chunk_markdown(new_body, metadata_header=meta_header)
        pool = await get_pool()
        async with pool.acquire() as conn:
            chunks_indexed = await write_source_chunks(
                conn, "document", str(pg_doc_id),
                vault_id=vault_id,
                chunks=chunks,
            )

        # Re-extract edges from updated content
        depends = current_fm.get("depends_on", []) or []
        related = current_fm.get("related_to", []) or []
        implements = current_fm.get("implements", []) or []
        pool = await get_pool()
        async with pool.acquire() as conn:
            await store_document_relations(
                conn, vault_id, vault, file_path,
                depends, related, implements,
                new_body,
            )

        return DocumentPutResponse(
            doc_id=ensure_dict(row["metadata"]).get("id", str(pg_doc_id)),
            vault=vault, path=file_path,
            commit_hash=commit_hash, chunks_indexed=chunks_indexed, entities_found=0,
        )

    # ── Delete ────────────────────────────────────────────────

    async def delete(self, vault: str, doc_ref: str, agent_id: str | None = None) -> bool:
        vault_repo, doc_repo, coll_repo = await self._repos()

        vault_id = await vault_repo.get_id_by_name(vault)
        if not vault_id:
            raise NotFoundError("Vault", vault)

        row = await doc_repo.find_by_ref(vault_id, doc_ref)
        if not row:
            raise NotFoundError("Document", doc_ref)

        pg_doc_id = row["id"]
        file_path = row["path"]
        collection_id = row["collection_id"]

        commit_msg = f"[delete] {file_path}\n\nagent: {agent_id or 'unknown'}\naction: delete"
        # Idempotent: if the git file is already gone (crash-recovery
        # state, manual cleanup, etc.) the DB cleanup below still runs.
        # Without this, a partial-delete leaves an undeletable document
        # row that needs operator intervention.
        try:
            await asyncio.to_thread(
                self.git.delete_file, vault_name=vault, file_path=file_path, message=commit_msg,
            )
        except FileNotFoundError:
            logger.warning(
                "Document %s/%s already absent from git — proceeding with DB-only cleanup",
                vault, file_path,
            )

        # Capture the public d-id BEFORE the row is gone so subscribers
        # see the same identifier they'd have used to fetch the doc.
        meta = ensure_dict(row.get("metadata"))
        public_doc_id = meta.get("id") or str(pg_doc_id)

        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await delete_document_chunks(conn, str(pg_doc_id))
                await delete_document_relations(conn, vault, file_path)
                await emit_event(
                    conn, "document.delete",
                    vault_id=vault_id, ref_type="document", ref_id=public_doc_id,
                    actor_id=agent_id,
                    payload={
                        "vault": vault,
                        "path": file_path,
                    },
                )

        await doc_repo.delete(pg_doc_id)

        if collection_id:
            await coll_repo.decrement_count(collection_id, datetime.now(timezone.utc))

        logger.info("Document deleted: %s", file_path)
        return True

    # ── Browse ────────────────────────────────────────────────

    async def browse(self, vault: str, collection: str | None = None, depth: int = 1, content_type: str = "all") -> BrowseResponse:
        vault_repo, doc_repo, coll_repo = await self._repos()

        vault_id = await vault_repo.get_id_by_name(vault)
        browse_path = collection or ""

        if not vault_id:
            return BrowseResponse(vault=vault, path=browse_path, items=[])

        show_docs = content_type in ("all", "documents")
        show_tables = content_type in ("all", "tables")
        show_files = content_type in ("all", "files")

        items: list[BrowseItem] = []

        if collection:
            if show_docs:
                items.extend(await self._browse_docs_in_collection(doc_repo, vault, vault_id, collection))
            if show_files:
                items.extend(await self._browse_files(vault, vault_id, collection=collection))
        else:
            if show_docs:
                items.extend(await self._browse_collections(doc_repo, coll_repo, vault, vault_id, depth))
            if show_tables:
                items.extend(await self._browse_tables(vault, vault_id))
            if show_files:
                items.extend(await self._browse_files(vault, vault_id))

        hint = self._browse_hint(vault, collection, items)
        return BrowseResponse(vault=vault, path=browse_path, items=items, hint=hint)

    async def _browse_docs_in_collection(self, doc_repo, vault: str, vault_id, collection: str) -> list[BrowseItem]:
        rows = await doc_repo.list_by_collection(vault_id, collection)
        return [
            BrowseItem(
                name=r["title"], path=r["path"], type="document",
                uri=doc_uri(vault, r["path"]),
                summary=r["summary"], doc_type=r["doc_type"], status=r["status"],
                tags=list(r["tags"]) if r["tags"] else [],
                last_updated=r["updated_at"],
            )
            for r in rows
        ]

    async def _browse_collections(self, doc_repo, coll_repo, vault: str, vault_id, depth: int) -> list[BrowseItem]:
        items: list[BrowseItem] = []
        coll_rows = await coll_repo.list_by_vault(vault_id)
        for r in coll_rows:
            items.append(BrowseItem(
                name=r["name"], path=r["path"], type="collection",
                summary=r["summary"], doc_count=r["doc_count"],
                last_updated=r["last_updated"],
            ))
        if depth >= 2:
            doc_rows = await doc_repo.list_by_vault(vault_id)
            for r in doc_rows:
                items.append(BrowseItem(
                    name=r["title"], path=r["path"], type="document",
                    uri=doc_uri(vault, r["path"]),
                    summary=r["summary"], doc_type=r["doc_type"], status=r["status"],
                    tags=list(r["tags"]) if r["tags"] else [],
                    last_updated=r["updated_at"],
                ))
        return items

    async def _browse_tables(self, vault: str, vault_id) -> list[BrowseItem]:
        import json as _json
        items: list[BrowseItem] = []
        pool = await get_pool()
        async with pool.acquire() as conn:
            vault_row = await conn.fetchrow("SELECT name FROM vaults WHERE id = $1", vault_id)
            table_rows = await conn.fetch(
                "SELECT id, name, description, columns, created_at FROM vault_tables WHERE vault_id = $1 ORDER BY name",
                vault_id,
            )
            for r in table_rows:
                pg_name = table_data_repo.pg_table_name(vault_row["name"], r["name"])
                try:
                    row_count = await conn.fetchval(f"SELECT COUNT(*) FROM {pg_name}")
                except Exception:
                    row_count = 0
                cols = ensure_list(r["columns"]) if isinstance(r["columns"], str) else r["columns"]
                items.append(BrowseItem(
                    name=r["name"], path=f"_tables/{r['name']}", type="table",
                    uri=table_uri(vault, r["name"]),
                    summary=r["description"], row_count=row_count,
                    columns=cols,
                    last_updated=r["created_at"],
                ))
        return items

    async def _browse_files(self, vault: str, vault_id, collection: str | None = None) -> list[BrowseItem]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            if collection:
                rows = await conn.fetch(
                    "SELECT id, name, mime_type, size_bytes, description, created_at FROM vault_files WHERE vault_id = $1 AND collection = $2 ORDER BY created_at DESC",
                    vault_id, collection,
                )
            else:
                rows = await conn.fetch(
                    "SELECT id, collection, name, mime_type, size_bytes, description, created_at FROM vault_files WHERE vault_id = $1 ORDER BY collection, created_at DESC",
                    vault_id,
                )
        return [
            BrowseItem(
                name=r["name"], path=f"_files/{r['id']}", type="file",
                uri=file_uri(vault, str(r["id"])),
                file_id=str(r["id"]), mime_type=r["mime_type"],
                size_bytes=r["size_bytes"], summary=r["description"],
                last_updated=r["created_at"],
            )
            for r in rows
        ]

    @staticmethod
    def _browse_hint(vault: str, collection: str | None, items: list[BrowseItem]) -> str:
        if collection:
            return f'Use akb_drill_down(vault="{vault}", doc_id="<doc_id>") to read sections, or akb_get() for full content.'
        if items:
            type_counts: dict[str, int] = {}
            for i in items:
                type_counts[i.type] = type_counts.get(i.type, 0) + 1
            parts = []
            if type_counts.get("collection"):
                parts.append(f'{type_counts["collection"]} collections')
            if type_counts.get("table"):
                parts.append(f'{type_counts["table"]} tables')
            if type_counts.get("file"):
                parts.append(f'{type_counts["file"]} files')
            summary_str = ", ".join(parts) if parts else "empty"
            return f'Vault contains: {summary_str}. Use akb_browse(vault="{vault}", collection="...") to drill into a collection, or content_type to filter.'
        return 'Vault is empty. Use akb_put() to add documents, akb_create_table() for tables, or akb_put_file() for files.'

    # ── Vault management ──────────────────────────────────────

    async def create_vault(
        self,
        name: str,
        description: str = "",
        owner_id: str | None = None,
        template: str | None = None,
        public_access: str = "none",
        external_git: dict | None = None,
    ) -> str:
        vault_repo, _, coll_repo = await self._repos()

        # Validate vault name: lowercase, hyphens, digits only, non-empty.
        # Raise ValidationError (422) rather than bare ValueError (500).
        import re as _re
        if not name or not _re.match(r'^[a-z0-9][a-z0-9-]*$', name):
            raise ValidationError(
                f"Invalid vault name: '{name}'. "
                "Use lowercase letters, digits, and hyphens only. Must start with a letter or digit."
            )

        from app.services.access_service import validate_public_access
        public_access = validate_public_access(public_access)

        if await vault_repo.get_by_name(name):
            raise ConflictError(f"Vault already exists: {name}")

        uid = uuid.UUID(owner_id) if owner_id else None

        if external_git:
            if not external_git.get("url"):
                raise ValidationError("external_git.url is required")
            # Mirror vaults defer the clone to the external_git_poller so
            # the MCP/HTTP caller doesn't block on multi-hundred-MB
            # network I/O. `git_path` still has to be set — store the
            # expected on-disk location; the poller's first reconcile
            # materialises it. Nothing is written to disk here, so no
            # rollback is needed: the two-row insert is atomic via the
            # PG transaction below, and a failure leaves zero side
            # effects.
            git_path = str(self.git._bare_path(name))
            pool = await get_pool()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    vault_id = await vault_repo.create(
                        name, description, git_path,
                        owner_id=uid, public_access=public_access, conn=conn,
                    )
                    await VaultExternalGitRepository(pool).create(
                        vault_id=vault_id,
                        remote_url=external_git["url"],
                        remote_branch=external_git.get("branch") or "main",
                        auth_token=external_git.get("auth_token"),
                        poll_interval_secs=int(external_git.get("poll_interval_secs") or 300),
                        conn=conn,
                    )
            logger.info(
                "Vault created (external_git mirror, pending clone): %s host=%s branch=%s",
                name, _safe_remote_host(external_git["url"]),
                external_git.get("branch") or "main",
            )
            return str(vault_id)

        # Standard path: init_vault writes a bare directory to disk
        # *before* the DB INSERT. If anything fails between the two
        # (commit_file crashes, request gets cancelled mid-flight, DB
        # write hits a constraint), the bare directory orphans and
        # init_vault's existence check permanently blocks the same
        # name. Wrap as a transaction; cleanup_vault_dirs is the
        # rollback. BaseException so SIGTERM and KeyboardInterrupt
        # also unwind cleanly.
        git_path = await asyncio.to_thread(self.git.init_vault, name)
        try:
            vault_yaml = f"name: {name}\ndescription: {description}\n"
            if template:
                vault_yaml += f"template: {template}\n"
            await asyncio.to_thread(
                self.git.commit_file,
                vault_name=name, file_path=".vault.yaml",
                content=vault_yaml,
                message=f"[init] Initialize vault: {name}",
            )
            vault_id = await vault_repo.create(
                name, description, git_path, owner_id=uid, public_access=public_access,
            )
            if template:
                await self._apply_template(name, vault_id, template, coll_repo)
        except BaseException:
            try:
                await asyncio.to_thread(self.git.cleanup_vault_dirs, name)
            except Exception as cleanup_err:  # noqa: BLE001
                logger.warning(
                    "create_vault rollback cleanup failed for %s: %s — operator must "
                    "rm -rf the orphan bare/worktree dir manually before retrying",
                    name, cleanup_err,
                )
            raise
        logger.info("Vault created: %s (owner: %s, template: %s)", name, owner_id, template)
        return str(vault_id)

    async def _apply_template(self, vault_name: str, vault_id: uuid.UUID, template: str, coll_repo) -> None:
        """Apply a vault template via the shared TemplateRegistry."""
        from app.services import template_registry

        tmpl = template_registry.get(template)
        if tmpl is None:
            logger.warning("Template not found: %s", template)
            return

        now = datetime.now(timezone.utc)
        for coll in tmpl.get("collections", []):
            path = coll["path"]
            coll_name = coll.get("name", path)
            guide = coll.get("guide", "")

            # Create collection
            coll_id = await coll_repo.get_or_create(vault_id, path)

            # Create _guide.md in collection
            if guide:
                guide_content = f"# {coll_name}\n\n{guide.strip()}"
                suggested = coll.get("suggested_types", [])
                if suggested:
                    guide_content += f"\n\nSuggested document types: {', '.join(suggested)}"

                await asyncio.to_thread(
                    self.git.commit_file,
                    vault_name=vault_name,
                    file_path=f"{path}/_guide.md",
                    content=guide_content,
                    message=f"[init] Add guide for {path}",
                )

        logger.info("Applied template '%s' to vault '%s' (%d collections)", template, vault_name, len(tmpl.get("collections", [])))

    async def list_vaults(self) -> list[dict]:
        vault_repo, _, _ = await self._repos()
        return await vault_repo.list_all()
