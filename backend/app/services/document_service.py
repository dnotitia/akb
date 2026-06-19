"""Document service — orchestrates Put/Get/Update/Delete.

Coordinates Git, DB repositories, and indexing pipeline.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import urlsplit


VAULT_SKILL_SEED_TEMPLATE = """# {vault} Guide

> Edit this document to describe how agents should write into this vault.
> Until you do, it acts as the AKB-default template — agents fall back to
> general AKB conventions (browse before write, no inline secrets, etc.).

## Purpose

(Describe what this vault is for and what it is not for. One paragraph.)

## Document types

Use these types when writing documents. Skip the rest unless the body explicitly calls for them.

- note — lightweight record
- report — synthesized analysis
- decision — durable decision with rationale
- spec — technical or product specification
- plan — future work
- session — agent session record
- task — assignment
- reference — stable reference material
- skill — vault-level conventions (owner-maintained; one per vault, this very doc)

## Tag conventions

- topic:<slug> — concept grouping
- source:<system> — imported source family
- area:<slug> — organizational area

## Collections

(Optional — list collections and their write policy here. Vault owner can
free-form this section. Agents read it as context, not a hard schema.)

## Relation rules

- depends_on — one resource cannot be understood without another
- implements — code/spec realizes a designed behavior
- references — background citation
- related_to — soft association, no directional dependency
- attached_to — file or table belongs to its document
- derived_from — generated/curated work depends on source material

## Document Template

When creating a new document, use this as a starting structure:

```markdown
---
title: <Document Title>
type: note|report|decision|spec|plan|session|reference
tags: [topic:<slug>, source:<system>]
---

# <Document Title>

## Purpose
<Why this exists. 1-2 sentences.>

## Background
<Context, constraints.>

## Decision / Result
<Core content.>

## Verification
<How it was checked.>

## Related
<Links to other docs via akb_link or inline markdown.>
```

## Do not

- Inline secrets in bodies; use ${{secrets.X}} placeholders
- Edit auto-generated docs without checking provenance
"""


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
    DOC_STATUSES,
    BrowseItem,
    BrowseResponse,
    DocumentPutRequest,
    DocumentPutResponse,
    DocumentResponse,
    DocumentUpdateRequest,
)
from app.repositories.document_repo import (
    CollectionRepository,
    DocumentRepository,
    acquire_path_lock,
)
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
from app.services.resource_hash import HASH_ALGORITHM, compute_text_content_hash
from app.services.role_sync import get_role_sync
from app.services.uri_service import coll_uri, doc_uri, file_uri, table_uri
from app.repositories import table_data_repo
from app.utils import ensure_list

logger = logging.getLogger("akb.documents")


class EditError(AKBError):
    """Raised when an edit operation cannot be performed."""

    def __init__(self, message: str):
        super().__init__(message, status_code=400)


from app.util.text import normalize_collection_path as _normalize_collection
from app.util.text import slugify
from app.util.text import to_nfc
from app.util.text import doc_path as _doc_path
from app.util.text import split_doc_path as _split_doc_path
from app.util.text import strip_own_suffix as _strip_own_suffix


def _build_frontmatter(req: DocumentPutRequest, now: datetime) -> dict:
    # Frontmatter no longer carries a `id:` line — the canonical handle
    # is the akb:// URI (vault + path). Path is captured in the .md
    # filename inside the vault's git tree, so a standalone clone of
    # the repo still resolves which doc each file represents.
    fm = {
        "title": req.title,
        "type": req.type,
        "status": req.status,
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
    return fm


def _compose_markdown(fm_dict: dict, body: str) -> str:
    post = frontmatter.Post(body, **fm_dict)
    return frontmatter.dumps(post)


def _parse_markdown(content: str) -> tuple[dict, str]:
    post = frontmatter.loads(content)
    return dict(post.metadata), post.content


def _body_content_hash(body: str) -> str:
    return compute_text_content_hash(body)


def _certified_content_hash(md_content: str) -> str:
    """Hash of the body exactly as a later ``get`` will serve it.

    ``get`` parses the stored markdown (``frontmatter.loads``) and hashes
    the parsed body — and python-frontmatter strips surrounding
    whitespace on load. Hashing the raw request body instead certified a
    write-response hash that no later read would ever confirm whenever
    the body had leading/trailing whitespace (issue #181). Parsing the
    composed markdown back makes write-response, DB row, and get agree
    by construction, and turns the read-side ``_ensure_document_hash``
    self-heal into a no-op for fresh writes.
    """
    _, canonical_body = _parse_markdown(md_content)
    return _body_content_hash(canonical_body)


class DocumentService:
    def __init__(self, git: GitService | None = None):
        self.git = git or GitService()

    async def _repos(self):
        pool = await get_pool()
        return VaultRepository(pool), DocumentRepository(pool), CollectionRepository(pool)

    async def _resolve_author_name(self, created_by: str | None) -> str | None:
        """Resolve a `created_by` user id to a human display name for the UI.

        App-authored docs store a user UUID; external-git imports store a
        non-UUID author string (left for the caller to show as-is). Returns the
        user's display_name (falling back to username), or None when the id
        isn't a UUID or doesn't match a user.
        """
        if not created_by:
            return None
        try:
            uuid.UUID(str(created_by))
        except (ValueError, AttributeError, TypeError):
            return None
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT display_name, username FROM users WHERE id::text = $1",
                str(created_by),
            )
        if not row:
            return None
        return row["display_name"] or row["username"]

    async def _ensure_document_hash(
        self,
        doc_repo,
        row: dict,
        body: str,
        *,
        persist: bool = True,
        conn=None,
    ) -> tuple[str, str]:
        content_hash = _body_content_hash(body)
        current_commit = row.get("current_commit")
        if (
            persist
            and (
                row.get("content_hash") != content_hash
                or row.get("hash_algorithm") != HASH_ALGORITHM
                or row.get("content_hash_commit") != current_commit
            )
        ):
            await doc_repo.update_hash(
                row["id"],
                content_hash=content_hash,
                hash_algorithm=HASH_ALGORITHM,
                content_hash_commit=current_commit,
                conn=conn,
            )
        return content_hash, HASH_ALGORITHM

    @asynccontextmanager
    async def _path_lock(self, vault_id: uuid.UUID, file_path: str):
        """Hold an exclusive (vault_id, path) advisory lock for the duration
        of the with-block. Serializes concurrent put/update/edit/delete on
        the same logical document path so git HEAD never diverges from
        ``documents.current_commit`` under a race.

        Yields the locked connection (already inside a transaction). Callers
        MUST run every DB statement of their critical section on this one
        connection — create, chunks, relations, events, counts — instead of
        acquiring a second pool connection. Holding this connection while
        acquiring another is what deadlocked the pool under a write burst:
        once ``pool_size`` writers each held a lock connection and then
        waited for a second connection, none could free one, so every write
        (and any read needing the pool) stalled until PG's 60s
        ``idle_in_transaction_session_timeout`` killed the lock transactions.
        One connection per writer makes the pool a clean backpressure queue.
        """
        pool = await get_pool()
        async with pool.acquire() as lock_conn:
            async with lock_conn.transaction():
                await acquire_path_lock(lock_conn, vault_id, file_path)
                yield lock_conn

    @asynccontextmanager
    async def _move_lock(self, vault_id: uuid.UUID, path_a: str, path_b: str):
        """Lock BOTH the source and destination paths for a move, in a stable
        (sorted) order so two concurrent moves can't deadlock on each other."""
        pool = await get_pool()
        async with pool.acquire() as lock_conn:
            async with lock_conn.transaction():
                for p in sorted({path_a, path_b}):
                    await acquire_path_lock(lock_conn, vault_id, p)
                yield lock_conn

    async def _resolve_free_path(
        self, doc_repo, vault_id: uuid.UUID, base_path: str,
        doc_id: uuid.UUID, conn, *, self_id: uuid.UUID | None = None,
    ) -> str:
        """Return ``base_path`` if it is free, else disambiguate with
        progressively longer slices of the doc's OWN uuid hex (8→12→16→full).

        The clean path is preferred (predictable). On collision the doc's uuid
        is the disambiguator: distinct docs have distinct uuids, and the full
        hex is globally unique, so this always terminates on a free path. This
        covers the deep cases — a same-title doc already at the target (move
        into a colliding collection), AND the (adversarial/astronomical) case
        where the 8-hex suffix is itself taken. ``self_id`` (move) treats a row
        that IS this doc as free, so re-running a move is a clean no-op rather
        than a self-collision. The advisory lock on the base path + the final
        UNIQUE(vault_id, path) constraint guard the residual concurrent race.
        """
        def _is_free(row) -> bool:
            return row is None or (self_id is not None and row["id"] == self_id)

        if _is_free(await doc_repo.find_by_path(vault_id, base_path, conn=conn)):
            return base_path
        stem = base_path[:-3] if base_path.endswith(".md") else base_path
        hexs = doc_id.hex
        for n in (8, 12, 16, len(hexs)):
            cand = f"{stem}-{hexs[:n]}.md"
            if _is_free(await doc_repo.find_by_path(vault_id, cand, conn=conn)):
                return cand
        # Unreachable in practice (full uuid is unique); fall through to the
        # full-hex form and let the UNIQUE constraint surface any true clash.
        return f"{stem}-{hexs}.md"

    @staticmethod
    async def _relink_edges(conn, vault_id, column: str, old_uri: str, new_uri: str) -> None:
        """Repoint edges referencing ``old_uri`` to ``new_uri`` on ``column``
        ('source_uri' or 'target_uri') during a move. Rows that would duplicate
        an edge already present at ``new_uri`` are skipped by the UPDATE (the
        UNIQUE(source_uri, target_uri, relation_type) guard) and then dropped,
        since they ARE such duplicates. ``column`` comes from a fixed internal
        set, so the f-string interpolation is not an injection surface."""
        other = "target_uri" if column == "source_uri" else "source_uri"
        await conn.execute(
            f"""
            UPDATE edges e SET {column} = $1
             WHERE e.vault_id = $2 AND e.{column} = $3
               AND NOT EXISTS (
                 SELECT 1 FROM edges x
                  WHERE x.vault_id = $2 AND x.{column} = $1
                    AND x.{other} = e.{other}
                    AND x.relation_type = e.relation_type)
            """,
            new_uri, vault_id, old_uri,
        )
        await conn.execute(
            f"DELETE FROM edges WHERE vault_id = $1 AND {column} = $2",
            vault_id, old_uri,
        )

    # ── Put ───────────────────────────────────────────────────

    async def put(self, req: DocumentPutRequest, agent_id: str | None = None) -> DocumentPutResponse:
        if req.status not in DOC_STATUSES:
            raise ValidationError(
                f"status must be one of {list(DOC_STATUSES)}, got {req.status!r}"
            )
        vault_repo, doc_repo, coll_repo = await self._repos()

        vault_id = await vault_repo.get_id_by_name(req.vault)
        if not vault_id:
            raise NotFoundError("Vault", req.vault)

        now = datetime.now(timezone.utc)
        # Generate the doc id up front so a collision can disambiguate the path
        # with the doc's short form. The clean path is used when the slug is
        # free; a `-{shortid}` suffix is appended ONLY on collision — the
        # industry-standard pattern for identity-bearing paths (WordPress /
        # Drupal / Rails / Ghost all suffix only on collision, never always).
        # A caller-pinned `req.slug` (e.g. the vault-skill seed) must land at
        # its exact path, so a collision there is a real conflict (no suffix).
        # See docs/designs/doc-identity-slug/00-overview.md.
        doc_id = uuid.uuid4()
        base_slug = slugify(req.slug) if req.slug else slugify(req.title)
        normalized_collection = _normalize_collection(req.collection)
        base_path = _doc_path(normalized_collection, base_slug)

        async with self._path_lock(vault_id, base_path) as conn:
            return await self._put_locked(
                req=req, agent_id=agent_id, vault_id=vault_id, doc_id=doc_id,
                base_path=base_path, base_slug=base_slug,
                explicit_slug=bool(req.slug), now=now,
                normalized_collection=normalized_collection,
                doc_repo=doc_repo, coll_repo=coll_repo, conn=conn,
            )

    async def _put_locked(
        self, *, req, agent_id, vault_id, doc_id, base_path, base_slug,
        explicit_slug, now, normalized_collection, doc_repo, coll_repo, conn,
    ) -> DocumentPutResponse:
        # Resolve the final path under the (vault, base_path) advisory lock,
        # which serializes writers racing on the same base slug. If the clean
        # path is free, use it (predictable, human-readable). If taken: a
        # caller-pinned slug is a true conflict (409), but a title-derived slug
        # disambiguates with the doc's short id so two distinct titles that
        # normalize to the same slug both persist. The short id makes the
        # suffixed path unique by construction; create()'s UNIQUE(vault_id,
        # path) is the final guard against the astronomically rare hex clash.
        # Every DB call below reuses `conn` (the lock connection, already in a
        # transaction) so the whole put holds exactly one pool connection.
        from app.exceptions import ConflictError
        from app.repositories.document_repo import drop_resource_alias
        if await doc_repo.find_by_path(vault_id, base_path, conn=conn):
            if explicit_slug:
                # A caller-pinned slug must land at its exact path.
                raise ConflictError(f"Document already exists at path: {base_path}")
            # Title-derived: disambiguate with this doc's own uuid (robust even
            # if the 8-hex form is itself taken).
            file_path = await self._resolve_free_path(
                doc_repo, vault_id, base_path, doc_id, conn
            )
        else:
            file_path = base_path
        # A real doc now owns this path → clear any stale rename redirect that
        # pointed elsewhere (a reused path's history resets to the live doc).
        await drop_resource_alias(conn, vault_id, "document", file_path)

        fm_dict = _build_frontmatter(req, now)
        if agent_id:
            fm_dict["created_by"] = agent_id
        md_content = _compose_markdown(fm_dict, req.content)
        content_hash = _certified_content_hash(md_content)

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
        # Use the *normalized* path so the `collections` row's `path`
        # matches the document's stored `path`. Passing the raw
        # `req.collection` here would create rows with leading/trailing
        # slashes or whitespace, diverging from the doc path under it.
        # Empty (== vault root) maps to NULL FK, matching the convention
        # used by file_service / table_service / external_git_service —
        # never insert a `path=""` phantom collection row.
        collection_id = (
            await coll_repo.get_or_create(vault_id, normalized_collection, conn=conn)
            if normalized_collection
            else None
        )
        # No `id` key — canonical handle is the akb:// URI built from
        # (vault, path), not a short hash. The `metadata` JSONB column is
        # reserved for internal writers (external-git import, LLM auto-tagging)
        # — user document writes never populate it.
        pg_doc_id = await doc_repo.create(
            vault_id=vault_id, collection_id=collection_id, path=file_path,
            title=req.title, doc_type=req.type, status=req.status,
            summary=fm_dict.get("summary") or req.summary, domain=req.domain, created_by=agent_id,
            now=now, commit_hash=commit_hash, content_hash=content_hash,
            hash_algorithm=HASH_ALGORITHM, tags=req.tags, metadata={}, conn=conn,
            doc_id=doc_id,
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

        # chunks + relations + event run on the lock connection's existing
        # transaction (opened in `_path_lock`). No second pool.acquire().
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
            vault_id=vault_id,
            resource_uri=doc_uri(req.vault, file_path),
            actor_id=agent_id,
            payload={
                "vault": req.vault,
                "path": file_path,
                "title": req.title,
                "doc_type": req.type,
                "commit_hash": commit_hash,
                "content_hash": content_hash,
                "hash_algorithm": HASH_ALGORITHM,
                "collection": normalized_collection,
            },
        )

        await coll_repo.increment_count(collection_id, now, conn=conn)

        return DocumentPutResponse(
            uri=doc_uri(req.vault, file_path),
            vault=req.vault, path=file_path,
            commit_hash=commit_hash, current_commit=commit_hash,
            content_hash=content_hash, hash_algorithm=HASH_ALGORITHM,
            action="created", chunks_indexed=chunks_indexed, entities_found=0,
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

        # Read the body at the row's recorded commit, NOT the floating vault
        # HEAD. This GET assembles `content` (from git) and `current_commit`
        # (from the DB row) in two separate reads; reading the body at HEAD
        # lets a concurrent writer advance git/DB between them, so a single
        # response could carry a body and a current_commit from *different*
        # writers (E03). Pinning the read to row["current_commit"] makes the
        # (content, current_commit) pair consistent by construction. A NULL
        # current_commit (legacy rows) falls back to HEAD inside read_file.
        content = await asyncio.to_thread(
            self.git.read_file, vault, row["path"], row["current_commit"]
        )
        body = ""
        if content:
            _, body = _parse_markdown(content)
        content_hash, hash_algorithm = await self._ensure_document_hash(doc_repo, row, body)

        # Derive published state from the publications table. We pick the
        # newest matching publication so the UI is consistent with publishDoc()
        # which reuses the first entry returned by listPublications (DESC order).
        public_slug = await self._get_public_slug(row["vault_name"], row["path"])

        return DocumentResponse(
            uri=doc_uri(row["vault_name"], row["path"]),
            vault=row["vault_name"], path=row["path"],
            title=row["title"], type=row["doc_type"] or "note", status=row["status"],
            summary=row["summary"], domain=row["domain"], created_by=row["created_by"],
            created_by_name=await self._resolve_author_name(row["created_by"]),
            created_at=row["created_at"], updated_at=row["updated_at"],
            current_commit=row["current_commit"],
            content_hash=content_hash, hash_algorithm=hash_algorithm,
            tags=list(row["tags"]) if row["tags"] else [],
            content=body,
            is_public=public_slug is not None,
            public_slug=public_slug,
        )

    async def get_at_commit(self, vault: str, doc_ref: str, version: str) -> DocumentResponse:
        """Return a document's metadata + body as of a specific git commit.

        The metadata (title, type, tags, summary, dates) is read from the
        current PG row — historical metadata is not tracked here. The
        content body is read from git at the requested commit. If the commit
        doesn't have the file at this path, NotFoundError is raised.
        """
        vault_repo, doc_repo, _ = await self._repos()

        vault_id = await vault_repo.get_id_by_name(vault)
        if not vault_id:
            raise NotFoundError("Vault", vault)

        row = await doc_repo.find_by_ref(vault_id, doc_ref)
        if not row:
            raise NotFoundError("Document", doc_ref)

        raw = await asyncio.to_thread(
            self.git.read_file, vault, row["path"], commit=version,
        )
        if raw is None:
            raise NotFoundError("Document version", f"{row['path']}@{version[:8]}")

        # Strip frontmatter. Historical commits may carry malformed YAML
        # or legacy id fields, so prefer the python-frontmatter parser
        # which mirrors MCP's _handle_get behavior. Fall back to a regex
        # strip if the parser chokes — never leak the raw frontmatter
        # header (which can contain legacy d-prefix ids).
        try:
            import frontmatter as _fm
            body = _fm.loads(raw).content
        except Exception:
            import re as _re
            body = _re.sub(
                r"\A---\r?\n.*?\r?\n---\r?\n",
                "",
                raw,
                count=1,
                flags=_re.DOTALL,
            )

        public_slug = await self._get_public_slug(row["vault_name"], row["path"])
        content_hash = _body_content_hash(body)

        return DocumentResponse(
            uri=doc_uri(row["vault_name"], row["path"]),
            vault=row["vault_name"], path=row["path"],
            title=row["title"], type=row["doc_type"] or "note", status=row["status"],
            summary=row["summary"], domain=row["domain"], created_by=row["created_by"],
            created_by_name=await self._resolve_author_name(row["created_by"]),
            created_at=row["created_at"], updated_at=row["updated_at"],
            current_commit=version,    # report the requested version, not HEAD
            content_hash=content_hash, hash_algorithm=HASH_ALGORITHM,
            tags=list(row["tags"]) if row["tags"] else [],
            content=body,
            is_public=public_slug is not None,
            public_slug=public_slug,
            # Metadata (title/type/tags/...) is read from the live PG row,
            # NOT from frontmatter at the requested commit. Flag this so
            # the UI can render a "metadata may not reflect this version"
            # banner alongside the historical body.
            metadata_is_current=True,
        )

    async def history(self, vault: str, doc_ref: str, limit: int = 20) -> dict:
        """Version history of a document — who changed it, when, and why.

        Single source of truth behind both the ``akb_history`` MCP tool and
        ``GET /api/v1/history/{vault}/{doc}``. Each entry is a git commit.

        The doc's ``created_at`` is passed to git as a lineage boundary so
        commits from a *previous* document at the same path (deleted-and-
        recreated) don't leak into this doc's history — git keys by path,
        not by document identity. Each entry is annotated with a human
        ``author_name`` resolved from the git author.

        Raises ``NotFoundError`` for a missing vault or document; callers
        leave the HTTP/MCP mapping to the global error handlers.
        """
        doc_ref = to_nfc(doc_ref)
        vault_repo, doc_repo, _ = await self._repos()

        vault_id = await vault_repo.get_id_by_name(vault)
        if not vault_id:
            raise NotFoundError("Vault", vault)

        row = await doc_repo.find_by_ref(vault_id, doc_ref)
        if not row:
            raise NotFoundError("Document", doc_ref)

        since_epoch = None
        created_at = row.get("created_at")
        if created_at is not None:
            since_epoch = int(created_at.timestamp())

        entries = await asyncio.to_thread(
            self.git.file_log,
            vault, row["path"],
            max_count=limit, since_epoch=since_epoch,
        )
        entries = await self._annotate_history_authors(entries)
        return {"uri": doc_uri(row["vault_name"], row["path"]), "history": entries}

    async def _annotate_history_authors(self, entries: list[dict]) -> list[dict]:
        """Add a human ``author_name`` to each ``file_log`` entry.

        The write path sets the git author name to the actor's ``agent_id``,
        which is the user's ``username`` (see ``put``/``update``). Legacy or
        external-git rows may instead carry a user UUID. Resolve *either*
        form to ``display_name`` (falling back to username) in one batched
        query so the UI shows a real name. Authors that match no user (e.g.
        external-git imports) keep only the raw ``author``.
        """
        authors = {e["author"] for e in entries if e.get("author")}
        if not authors:
            return entries
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id::text AS id, username,
                       COALESCE(display_name, username) AS name
                  FROM users
                 WHERE id::text = ANY($1) OR username = ANY($1)
                """,
                list(authors),
            )
        name_by_key: dict[str, str] = {}
        for r in rows:
            name_by_key[r["id"]] = r["name"]
            name_by_key[r["username"]] = r["name"]
        for e in entries:
            name = name_by_key.get(e.get("author"))
            if name:
                e["author_name"] = name
        return entries

    async def _get_public_slug(self, vault_name: str, doc_path: str) -> str | None:
        """Return the newest publication slug for a document, or None.
        Looks up by the canonical resource_uri rather than the dropped
        `publications.document_id` FK column."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval(
                """
                SELECT slug FROM publications
                WHERE resource_uri = $1 AND resource_type = 'document'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                doc_uri(vault_name, doc_path),
            )

    # ── Update ────────────────────────────────────────────────

    async def update(self, vault: str, doc_ref: str, req: DocumentUpdateRequest, agent_id: str | None = None) -> DocumentPutResponse:
        if req.status is not None and req.status not in DOC_STATUSES:
            raise ValidationError(
                f"status must be one of {list(DOC_STATUSES)}, got {req.status!r}"
            )
        vault_repo, doc_repo, _ = await self._repos()

        vault_id = await vault_repo.get_id_by_name(vault)
        if not vault_id:
            raise NotFoundError("Vault", vault)

        # Resolve once to learn the path, then acquire the lock and re-read.
        row = await doc_repo.find_by_ref(vault_id, doc_ref)
        if not row:
            raise NotFoundError("Document", doc_ref)
        file_path = row["path"]

        async with self._path_lock(vault_id, file_path) as conn:
            # Re-read under the lock so we observe any commit that landed
            # between the initial resolution and lock acquisition. Uses the
            # lock connection so the whole update holds one pool connection.
            row = await doc_repo.find_by_ref_with_conn(conn, vault_id, doc_ref)
            if not row:
                raise NotFoundError("Document", doc_ref)

            # Optimistic concurrency: if caller pinned expected_commit, refuse
            # the write when the row has moved on. Caller should re-read and
            # retry against the new HEAD.
            if req.expected_commit and row["current_commit"] != req.expected_commit:
                raise ConflictError(
                    f"current_commit moved: expected {req.expected_commit}, "
                    f"actual {row['current_commit']}"
                )

            return await self._update_locked(
                req=req, agent_id=agent_id, vault=vault,
                vault_id=vault_id, doc_repo=doc_repo, row=row, conn=conn,
            )

    async def _update_locked(self, *, req, agent_id, vault, vault_id, doc_repo, row, conn) -> DocumentPutResponse:
        now = datetime.now(timezone.utc)
        pg_doc_id = row["id"]
        file_path = row["path"]

        current_content = await asyncio.to_thread(self.git.read_file, vault, file_path)
        if current_content is None:
            raise NotFoundError("Document file", file_path)

        current_fm, current_body = _parse_markdown(current_content)
        current_hash, _ = await self._ensure_document_hash(doc_repo, row, current_body, conn=conn)
        if req.expected_content_hash and req.expected_content_hash != current_hash:
            raise ConflictError(
                f"content_hash moved: expected {req.expected_content_hash}, "
                f"actual {current_hash}"
            )

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
        current_fm["updated_at"] = now.isoformat()

        new_body = req.content if req.content is not None else current_body
        new_md = _compose_markdown(current_fm, new_body)
        previous_hash = current_hash
        content_hash = _certified_content_hash(new_md)

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
            commit_hash=commit_hash, content_hash=content_hash,
            hash_algorithm=HASH_ALGORITHM, content_hash_commit=commit_hash,
            tags=req.tags, conn=conn,
        )

        chunks_indexed = 0
        # chunks + relations + event run on the lock connection's existing
        # transaction (opened in `_path_lock`), so partial failure rolls back
        # all three together and the whole update holds one pool connection.
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
            chunks_indexed = await write_source_chunks(
                conn, "document", str(pg_doc_id),
                vault_id=vault_id,
                chunks=chunks,
            )

        if req.content is not None or req.depends_on is not None or req.related_to is not None:
            depends = current_fm.get("depends_on", []) or []
            related = current_fm.get("related_to", []) or []
            implements = current_fm.get("implements", []) or []
            await store_document_relations(
                conn, vault_id, vault, file_path,
                depends, related, implements,
                new_body,
            )

        await emit_event(
            conn, "document.update",
            vault_id=vault_id,
            resource_uri=doc_uri(vault, file_path),
            actor_id=agent_id,
            payload={
                "vault": vault,
                "path": file_path,
                "commit_hash": commit_hash,
                "content_hash": content_hash,
                "hash_algorithm": HASH_ALGORITHM,
                "content_changed": req.content is not None,
            },
        )

        return DocumentPutResponse(
            uri=doc_uri(vault, file_path),
            vault=vault, path=file_path,
            commit_hash=commit_hash, current_commit=commit_hash,
            previous_commit=row.get("current_commit"),
            previous_content_hash=previous_hash,
            content_hash=content_hash, hash_algorithm=HASH_ALGORITHM,
            action="updated", chunks_indexed=chunks_indexed, entities_found=0,
        )

    # ── Move / rename ─────────────────────────────────────────

    async def move(
        self, vault: str, doc_ref: str, *,
        collection: str | None = None, slug: str | None = None,
        message: str | None = None, agent_id: str | None = None,
    ) -> DocumentPutResponse:
        """Move/rename a document: change its collection and/or slug while
        keeping its identity (id). The move is a ``git mv``, so ``git log
        --follow`` traces the file's history across it later. The old path is
        recorded in ``resource_aliases`` so old akb:// URIs keep resolving;
        graph edges + publications referencing the old URI are rewritten. At
        least one of ``collection``/``slug`` must be provided, and the resulting
        path must differ from the current one (else it is rejected as a no-op).
        """
        from app.repositories.document_repo import (
            add_resource_alias,
            drop_resource_alias,
        )

        vault_repo, doc_repo, coll_repo = await self._repos()
        vault_id = await vault_repo.get_id_by_name(vault)
        if not vault_id:
            raise NotFoundError("Vault", vault)

        if collection is None and slug is None:
            raise ValidationError("move requires at least one of collection or slug")
        # An explicitly-requested slug is a precise rename target — like create,
        # a collision on it rejects rather than silently suffixing.
        explicit_slug = bool(slug)

        row = await doc_repo.find_by_ref(vault_id, doc_ref)
        if not row:
            raise NotFoundError("Document", doc_ref)
        old_path = row["path"]

        # Best-effort target estimate, used only to pick the lock. The
        # AUTHORITATIVE target is recomputed from the fresh row after the lock,
        # since a concurrent move could change this doc's path between this read
        # and lock acquisition (the recompute + UNIQUE(vault_id, path) make the
        # stale-read race safe). When the slug is kept (not re-specified), strip
        # any collision suffix THIS doc added before so a move doesn't nest them.
        def _target(base_path: str, doc_id: uuid.UUID) -> tuple[str, str, str]:
            cur_coll, cur_base = _split_doc_path(base_path)
            nc = _normalize_collection(collection) if collection is not None else cur_coll
            ns = slugify(slug) if slug else _strip_own_suffix(cur_base, doc_id)
            return nc, ns, _doc_path(nc, ns)

        _, _, est_new_path = _target(old_path, row["id"])

        async with self._move_lock(vault_id, old_path, est_new_path) as conn:
            # Re-read under the lock and recompute the target from fresh state.
            row = await doc_repo.find_by_ref_with_conn(conn, vault_id, doc_ref)
            if not row:
                raise NotFoundError("Document", doc_ref)
            old_path = row["path"]
            pg_doc_id = row["id"]
            old_collection_id = row["collection_id"]

            new_coll, new_base_slug, base_new_path = _target(old_path, pg_doc_id)
            if base_new_path == old_path:
                raise ValidationError(
                    "move is a no-op: the target path equals the current path"
                )

            # On-collision suffix (same robust rule as create): if the clean
            # target is taken by a DIFFERENT doc, disambiguate with this doc's
            # own uuid. self_id lets a target already held by THIS doc count as
            # free, so a redundant re-move collapses to the no-op check below.
            new_path = await self._resolve_free_path(
                doc_repo, vault_id, base_new_path, pg_doc_id, conn, self_id=pg_doc_id
            )
            if new_path == old_path:
                raise ValidationError("move is a no-op: the target path equals the current path")
            # A suffix was forced (collision) but the caller pinned this slug →
            # reject instead of silently renaming to something they didn't ask for.
            if explicit_slug and new_path != base_new_path:
                raise ConflictError(f"Document already exists at path: {base_new_path}")

            new_collection_id = (
                await coll_repo.get_or_create(vault_id, new_coll, conn=conn)
                if new_coll else None
            )
            now = datetime.now(timezone.utc)
            summary = message or f"{old_path} -> {new_path}"
            commit_msg = (
                f"[move] {old_path} -> {new_path}\n\n"
                f"agent: {agent_id or 'unknown'}\naction: move\nsummary: {summary}"
            )
            try:
                commit_hash = await asyncio.to_thread(
                    self.git.move_file,
                    vault_name=vault, old_path=old_path, new_path=new_path,
                    message=commit_msg, author_name=agent_id or "AKB System",
                )
            except FileNotFoundError:
                # Crash-recovery idempotence: a prior move committed the git mv
                # but its DB tx didn't commit, so on retry the source is already
                # gone. If the file now lives at new_path, adopt the current HEAD
                # and finish the DB side; otherwise it's a genuine missing file.
                head = await asyncio.to_thread(self.git.current_commit, vault)
                if head is None or await asyncio.to_thread(self.git.read_file, vault, new_path) is None:
                    raise
                logger.warning(
                    "Move recovery: %s already at %s in git — reconciling DB to HEAD %s",
                    old_path, new_path, head[:8],
                )
                commit_hash = head
            logger.info("Document moved: %s -> %s (commit: %s)", old_path, new_path, commit_hash[:8])

            await doc_repo.update_path(
                pg_doc_id, new_path, collection_id=new_collection_id,
                now=now, commit_hash=commit_hash, conn=conn,
            )

            # Keep collection doc counts honest when the folder changed.
            if old_collection_id != new_collection_id:
                if old_collection_id:
                    await coll_repo.decrement_count(old_collection_id, now, conn=conn)
                if new_collection_id:
                    await coll_repo.increment_count(new_collection_id, now, conn=conn)

            # Redirect old_path -> this id; clear any stale redirect now shadowed
            # by a real doc landing on new_path.
            await add_resource_alias(conn, vault_id, "document", old_path, pg_doc_id)
            await drop_resource_alias(conn, vault_id, "document", new_path)

            # Rewrite graph edges + publications that referenced the old URI so
            # the live graph stays correct (old links also resolve via alias).
            # Guard edges' UNIQUE(source_uri, target_uri, relation_type): move
            # only rows that won't duplicate an edge already present at the new
            # URI, then drop the leftovers (they ARE such duplicates). Without
            # this, a move onto a path that previously held an equivalent edge
            # would throw a UniqueViolation and abort the whole move.
            old_uri = doc_uri(vault, old_path)
            new_uri = doc_uri(vault, new_path)
            await self._relink_edges(conn, vault_id, "source_uri", old_uri, new_uri)
            await self._relink_edges(conn, vault_id, "target_uri", old_uri, new_uri)
            await conn.execute(
                "UPDATE publications SET resource_uri = $1 WHERE vault_id = $2 AND resource_uri = $3",
                new_uri, vault_id, old_uri,
            )

            # Re-chunk so the doc-metadata header carries the new path.
            chunks_indexed = 0
            content = await asyncio.to_thread(self.git.read_file, vault, new_path)
            if content is not None:
                _, body = _parse_markdown(content)
                meta_header = build_doc_metadata_header(
                    vault_name=vault, path=new_path, title=row["title"],
                    summary=row["summary"],
                    tags=list(row["tags"]) if row["tags"] else [],
                    doc_type=row["doc_type"],
                )
                chunks = chunk_markdown(body, metadata_header=meta_header)
                chunks_indexed = await write_source_chunks(
                    conn, "document", str(pg_doc_id), vault_id=vault_id, chunks=chunks,
                )
            else:
                # Invariant violation: the file we just `git mv`-ed is unreadable.
                # Don't abort (that would diverge git from DB — the path move is
                # already committed). But DROP the now-orphaned chunks: the
                # re-chunk above (skipped here) would have *replaced* them, so
                # leaving them serves a stale, wrong-path embedding under the new
                # path. Dropping makes the doc honestly UNSEARCHABLE until its
                # next edit/reindex — the correct failure mode here.
                await delete_document_chunks(conn, str(pg_doc_id))
                logger.error(
                    "Move re-index FAILED: %s unreadable right after git mv "
                    "(doc_id=%s, commit=%s) — dropped stale chunks; doc is "
                    "UNSEARCHABLE until next reindex",
                    new_path, pg_doc_id, commit_hash[:8],
                )

            await emit_event(
                conn, "document.move",
                vault_id=vault_id,
                resource_uri=new_uri,
                actor_id=agent_id,
                payload={
                    "vault": vault, "path": new_path, "old_path": old_path,
                    "old_uri": old_uri, "commit_hash": commit_hash,
                },
            )

            return DocumentPutResponse(
                uri=new_uri, vault=vault, path=new_path,
                commit_hash=commit_hash, current_commit=commit_hash,
                previous_commit=row.get("current_commit"),
                content_hash=row.get("content_hash"),
                hash_algorithm=row.get("hash_algorithm"),
                action="moved", chunks_indexed=chunks_indexed, entities_found=0,
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
        base_commit: str | None = None,
    ) -> DocumentPutResponse:
        """Edit a document by replacing exact text in its body.

        Args:
            old_string: Exact text to find. Must be unique unless replace_all=True.
            new_string: Replacement text. Can be empty to delete.
            replace_all: If True, replace all occurrences. If False, old_string must be unique.
            base_commit: Optional OCC pin — reject with 409 if the doc's
                current_commit doesn't match. Use to detect a concurrent
                writer between the agent's read and edit submission.

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
        file_path = row["path"]

        async with self._path_lock(vault_id, file_path) as conn:
            row = await doc_repo.find_by_ref_with_conn(conn, vault_id, doc_ref)
            if not row:
                raise NotFoundError("Document", doc_ref)
            if base_commit and row["current_commit"] != base_commit:
                raise ConflictError(
                    f"current_commit moved: expected {base_commit}, "
                    f"actual {row['current_commit']}"
                )
            return await self._edit_locked(
                vault=vault, vault_id=vault_id, row=row, doc_repo=doc_repo,
                old_string=old_string, new_string=new_string,
                replace_all=replace_all, message=message, agent_id=agent_id,
                conn=conn,
            )

    async def _edit_locked(
        self, *, vault, vault_id, row, doc_repo,
        old_string, new_string, replace_all, message, agent_id, conn,
    ) -> DocumentPutResponse:
        now = datetime.now(timezone.utc)
        pg_doc_id = row["id"]
        file_path = row["path"]

        current_content = await asyncio.to_thread(self.git.read_file, vault, file_path)
        if current_content is None:
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
            content_hash, hash_algorithm = await self._ensure_document_hash(
                doc_repo, row, current_body, conn=conn,
            )
            return DocumentPutResponse(
                uri=doc_uri(vault, file_path),
                vault=vault, path=file_path,
                commit_hash=row.get("current_commit") or "",
                current_commit=row.get("current_commit"),
                content_hash=content_hash, hash_algorithm=hash_algorithm,
                action="unchanged", chunks_indexed=0, entities_found=0,
            )

        current_fm["updated_at"] = now.isoformat()
        new_md = _compose_markdown(current_fm, new_body)
        previous_hash = row.get("content_hash") or _body_content_hash(current_body)
        content_hash = _certified_content_hash(new_md)

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
            commit_hash=commit_hash, content_hash=content_hash,
            hash_algorithm=HASH_ALGORITHM, content_hash_commit=commit_hash,
            tags=None, conn=conn,
        )

        # Re-chunk and re-embed (full pipeline — mirrors update())
        meta_header = build_doc_metadata_header(
            vault_name=vault, path=file_path,
            title=row["title"], summary=row["summary"],
            tags=list(row["tags"]) if row["tags"] else [],
            doc_type=row["doc_type"],
        )
        chunks = chunk_markdown(new_body, metadata_header=meta_header)
        depends = current_fm.get("depends_on", []) or []
        related = current_fm.get("related_to", []) or []
        implements = current_fm.get("implements", []) or []

        # chunks + relations + event run on the lock connection's existing
        # transaction (opened in `_path_lock`) so a crash between them can't
        # leave the chunk index, the edge graph, and the event stream in
        # inconsistent states — and the whole edit holds one pool connection.
        chunks_indexed = await write_source_chunks(
            conn, "document", str(pg_doc_id),
            vault_id=vault_id,
            chunks=chunks,
        )
        await store_document_relations(
            conn, vault_id, vault, file_path,
            depends, related, implements,
            new_body,
        )
        await emit_event(
            conn, "document.update",
            vault_id=vault_id,
            resource_uri=doc_uri(vault, file_path),
            actor_id=agent_id,
            payload={
                "vault": vault,
                "path": file_path,
                "commit_hash": commit_hash,
                "content_hash": content_hash,
                "hash_algorithm": HASH_ALGORITHM,
                "content_changed": True,
                "source": "edit",
            },
        )

        return DocumentPutResponse(
            uri=doc_uri(vault, file_path),
            vault=vault, path=file_path,
            commit_hash=commit_hash, current_commit=commit_hash,
            previous_commit=row.get("current_commit"),
            previous_content_hash=previous_hash,
            content_hash=content_hash, hash_algorithm=HASH_ALGORITHM,
            action="updated", chunks_indexed=chunks_indexed, entities_found=0,
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
        file_path = row["path"]

        async with self._path_lock(vault_id, file_path) as conn:
            # Re-resolve under the lock — a concurrent delete may have run.
            row = await doc_repo.find_by_ref_with_conn(conn, vault_id, doc_ref)
            if not row:
                raise NotFoundError("Document", doc_ref)
            return await self._delete_locked(
                vault=vault, vault_id=vault_id, row=row, agent_id=agent_id,
                doc_repo=doc_repo, coll_repo=coll_repo, conn=conn,
            )

    async def _delete_locked(self, *, vault, vault_id, row, agent_id, doc_repo, coll_repo, conn) -> bool:
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

        # All cascade statements run on the lock connection's existing
        # transaction (opened in `_path_lock`): a crash between any two of
        # them can't leave an orphan `documents` row with no
        # chunks/edges/publications, and the whole delete holds one
        # pool connection.
        await delete_document_chunks(conn, str(pg_doc_id))
        await delete_document_relations(conn, vault, file_path)
        # App-level publication cascade. Previously this rode
        # on `publications.document_id` ON DELETE CASCADE; that
        # FK column is gone after migration 022, so we wipe
        # publications by canonical URI before the doc row
        # itself goes.
        await conn.execute(
            "DELETE FROM publications WHERE resource_uri = $1",
            doc_uri(vault, file_path),
        )
        # Drop any rename/move redirects that pointed at this doc so no alias
        # outlives the row it targets (a later doc reusing the old path then
        # resolves to itself, not a tombstone).
        from app.repositories.document_repo import drop_aliases_for_resource
        await drop_aliases_for_resource(conn, vault_id, "document", pg_doc_id)
        await emit_event(
            conn, "document.delete",
            vault_id=vault_id,
            resource_uri=doc_uri(vault, file_path),
            actor_id=agent_id,
            payload={
                "vault": vault,
                "path": file_path,
            },
        )
        await doc_repo.delete(pg_doc_id, conn=conn)

        if collection_id:
            await coll_repo.decrement_count(collection_id, datetime.now(timezone.utc), conn=conn)

        logger.info("Document deleted: %s", file_path)
        return True

    # ── Browse ────────────────────────────────────────────────

    async def browse(
        self,
        vault: str,
        collection: str | None = None,
        depth: int = 1,
        content_type: str = "all",
        include_hashes: bool = False,
        include_archived: bool = False,
    ) -> BrowseResponse:
        """Unified vault browse.

        ``depth`` is **tree-depth from the browse root**, mirroring the
        ``tree -L N`` convention:

          * ``depth=0`` — only direct children of the browse root;
            no descent into any collection.
          * ``depth=N`` (N ≥ 1) — additionally descend ``N`` levels
            of collections.
          * ``depth=-1`` — unbounded; the entire subtree of the
            browse root.

        Browse root is the vault root when ``collection`` is omitted,
        otherwise it is that collection. ``content_type`` lets callers
        narrow to ``documents`` / ``tables`` / ``files`` only.

        Collection rows themselves are always emitted (they are
        navigation aids — the response would be useless without them),
        with ``path`` scoped to the requested subtree when
        ``collection`` is provided. ``doc`` / ``table`` / ``file`` rows
        are the ones gated by depth.
        """
        vault_repo, doc_repo, coll_repo = await self._repos()

        vault_id = await vault_repo.get_id_by_name(vault)
        browse_path = collection or ""

        if not vault_id:
            return BrowseResponse(vault=vault, path=browse_path, items=[])

        show_docs = content_type in ("all", "documents")
        show_tables = content_type in ("all", "tables")
        show_files = content_type in ("all", "files")

        items: list[BrowseItem] = []
        prefix = collection or ""

        if show_docs:
            # Collections are conceptually navigation aids for the
            # *document* tree (file/table also live under collections,
            # but a `content_type="tables"` caller is asking for tables
            # specifically — they don't want the nav rows). Gating on
            # show_docs keeps the response narrow when content_type
            # excludes documents.
            items.extend(await self._browse_collections(coll_repo, vault, vault_id, prefix))
            items.extend(await self._browse_docs(
                doc_repo, vault, vault_id, prefix=prefix, max_depth=depth,
                include_hashes=include_hashes, include_archived=include_archived,
            ))
        if show_tables:
            items.extend(await self._browse_tables_by_depth(
                vault, vault_id, prefix=prefix, max_depth=depth,
            ))
        if show_files:
            items.extend(await self._browse_files_by_depth(
                vault, vault_id, prefix=prefix, max_depth=depth,
                include_hashes=include_hashes,
            ))

        hint = self._browse_hint(vault, collection, items)
        return BrowseResponse(vault=vault, path=browse_path, items=items, hint=hint)

    async def _browse_collections(self, coll_repo, vault: str, vault_id, prefix: str) -> list[BrowseItem]:
        """Emit collection rows. With ``prefix`` empty, emits every
        collection in the vault. With a non-empty prefix, restricts to
        collections strictly under that subtree (``prefix/X``,
        ``prefix/X/Y``, …) so a scoped browse only shows the relevant
        navigation slice. The collection at ``prefix`` itself is
        excluded — clients already know they are inside it.

        Each emitted row carries the canonical ``akb://V/coll/X`` URI
        so callers can paste it back into ``akb_browse(uri=...)`` to
        drill in — collections are now URI-citizens like docs / tables
        / files (closing the long-standing gap from pre-0.3.0)."""
        all_rows = await coll_repo.list_by_vault(vault_id)
        items: list[BrowseItem] = []
        for r in all_rows:
            if prefix:
                if not r["path"].startswith(prefix + "/"):
                    continue
            items.append(BrowseItem(
                name=r["name"], path=r["path"], type="collection",
                uri=coll_uri(vault, r["path"]),
                summary=r["summary"], doc_count=r["doc_count"],
                last_updated=r["last_updated"],
            ))
        return items

    async def _browse_docs(
        self,
        doc_repo,
        vault: str,
        vault_id,
        *,
        prefix: str,
        max_depth: int,
        include_hashes: bool = False,
        include_archived: bool = False,
    ) -> list[BrowseItem]:
        """Documents under ``prefix`` whose depth (from inside the
        prefix) is ≤ ``max_depth``. ``max_depth < 0`` is unbounded."""
        rows = await doc_repo.list_docs_by_depth(
            vault_id, max_depth, prefix, include_archived=include_archived,
        )
        items: list[BrowseItem] = []
        for r in rows:
            content_hash = r.get("content_hash")
            hash_algorithm = r.get("hash_algorithm")
            if include_hashes and (
                not content_hash
                or hash_algorithm != HASH_ALGORITHM
                or r.get("content_hash_commit") != r.get("current_commit")
            ):
                raw = await asyncio.to_thread(self.git.read_file, vault, r["path"])
                if raw is not None:
                    _, body = _parse_markdown(raw)
                    content_hash, hash_algorithm = await self._ensure_document_hash(
                        doc_repo, r, body,
                    )
            items.append(
                BrowseItem(
                    name=r["title"], path=r["path"], type="document",
                    uri=doc_uri(vault, r["path"]),
                    summary=r["summary"], doc_type=r["doc_type"], status=r["status"],
                    tags=list(r["tags"]) if r["tags"] else [],
                    last_updated=r["updated_at"],
                    current_commit=r.get("current_commit") if include_hashes else None,
                    content_hash=content_hash if include_hashes else None,
                    hash_algorithm=hash_algorithm if include_hashes else None,
                )
            )
        return items

    async def _browse_tables_by_depth(
        self,
        vault: str,
        vault_id,
        *,
        prefix: str,
        max_depth: int,
        include_hashes: bool = False,
    ) -> list[BrowseItem]:
        """Tables under ``prefix`` whose containing-collection depth
        (relative to the prefix) is ≤ ``max_depth``. ``max_depth < 0``
        is unbounded. Mirrors `_browse_docs`'s semantics so the four
        item types share one rule."""
        from app.repositories import table_registry_repo
        items: list[BrowseItem] = []
        pool = await get_pool()
        async with pool.acquire() as conn:
            vault_row = await conn.fetchrow("SELECT name FROM vaults WHERE id = $1", vault_id)
            table_rows = await table_registry_repo.list_for_vault(
                conn, vault_id, max_depth=max_depth, prefix=prefix,
            )
            for r in table_rows:
                pg_name = table_data_repo.pg_table_name(vault_row["name"], r["name"])
                try:
                    row_count = await conn.fetchval(f"SELECT COUNT(*) FROM {pg_name}")
                except Exception:
                    row_count = 0
                cols = ensure_list(r["columns"]) if isinstance(r["columns"], str) else r["columns"]
                items.append(BrowseItem(
                    # `path` is the table name. Pre-0.3.0 it was a
                    # synthetic `_tables/<name>` string, which made
                    # sense before tables had URIs — the prefix
                    # substituted for "what kind of resource is this".
                    # Now `type="table"` + `uri` (which encodes both
                    # location and kind) carry that signal, so the
                    # synthetic prefix is pure noise.
                    name=r["name"], path=r["name"], type="table",
                    uri=table_uri(vault, r["name"], collection=r.get("collection")),
                    summary=r["description"], row_count=row_count,
                    columns=cols,
                    sql_name=table_data_repo.pg_short_name(r["name"]),
                    collection=r.get("collection"),
                    last_updated=r["created_at"],
                ))
        return items

    async def _browse_files_by_depth(
        self,
        vault: str,
        vault_id,
        *,
        prefix: str,
        max_depth: int,
        include_hashes: bool = False,
    ) -> list[BrowseItem]:
        from app.repositories import vault_files_repo
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await vault_files_repo.list_for_vault(
                conn, vault_id,
                max_depth=max_depth, prefix=prefix,
                # Browse renders the full list — don't apply a 50-row
                # cap silently. If this turns into a performance issue
                # we can paginate at the route layer.
                limit=10_000,
            )
        return [
            BrowseItem(
                name=r["name"],
                # Build the human path from collection + filename. The
                # canonical handle is `uri`; `path` is purely a
                # display string and must not embed the file UUID.
                path=(f"{r.get('collection')}/{r['name']}" if r.get("collection") else r["name"]),
                type="file",
                # Pass collection so the URI takes 0.3.0 canonical form
                # akb://V/coll/<path>/file/<uuid> instead of root-form
                # akb://V/file/<uuid>. Without this, browse → akb_link
                # round-trips re-pollute the edges table with non-canonical
                # URIs that migration 026 already cleaned up.
                uri=file_uri(vault, str(r["id"]), collection=r.get("collection")),
                mime_type=r["mime_type"],
                size_bytes=r["size_bytes"], summary=r["description"],
                collection=r.get("collection"),
                last_updated=r["created_at"],
                content_hash=r.get("content_hash") if include_hashes else None,
                hash_algorithm=r.get("hash_algorithm") if include_hashes else None,
                etag=r.get("etag") if include_hashes else None,
                storage_version=r.get("storage_version") if include_hashes else None,
            )
            for r in rows
        ]

    @staticmethod
    def _browse_hint(vault: str, collection: str | None, items: list[BrowseItem]) -> str:
        if collection:
            return 'Use akb_drill_down(uri=...) to read sections, or akb_get(uri=...) for full content. Pass the canonical `uri` from any item above.'
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
        vault_repo, doc_repo, coll_repo = await self._repos()

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
            # PG-native RBAC: create vault group roles + grant admin to owner,
            # then mirror public_access (no-op if 'none').
            rs = get_role_sync()
            await rs.on_vault_create(vault_id, uid)
            await rs.on_public_access_change(vault_id, public_access)
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
            # PG-native RBAC: create vault group roles + grant admin to owner,
            # then mirror public_access (no-op if 'none'). Done before template
            # application so any tables the template creates inherit grants
            # from the proper group roles.
            rs = get_role_sync()
            await rs.on_vault_create(vault_id, uid)
            await rs.on_public_access_change(vault_id, public_access)
            if template:
                await self._apply_template(name, vault_id, template, coll_repo)
            # Seed overview/vault-skill.md so every non-mirror vault carries a starter
            # skill doc. The vault owner edits this later via akb_edit/akb_update; agents
            # read it via akb_help(topic="vault-skill", vault=...).
            #
            # Unlike `_apply_template` which writes only git (the existing collection-level
            # `_guide.md` files are not reachable via akb_get because no DB row is created),
            # the vault-skill seed writes BOTH git AND a documents row so akb_get /
            # akb_browse / akb_search can find it.
            if not external_git:  # mirror vaults are read-only
                skill_body = VAULT_SKILL_SEED_TEMPLATE.replace("{vault}", name)
                # Route through the canonical put() so chunks/BM25 indexing,
                # frontmatter composition, collection-count increment, and the
                # document.put event all run — fixing I1–I4.
                # put() calls coll_repo.get_or_create internally, so no
                # separate create_empty is needed.
                seed_req = DocumentPutRequest(
                    vault=name,
                    collection="overview",
                    title=f"{name} Guide",
                    slug="vault-skill",
                    content=skill_body,
                    type="skill",
                    tags=["akb:skill"],
                )
                await self.put(seed_req, agent_id=str(owner_id) if owner_id else None)
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

        for coll in tmpl.get("collections", []):
            path = coll["path"]
            coll_name = coll.get("name", path)
            guide = coll.get("guide", "")

            # Create collection. Defensive: never call get_or_create
            # with an empty path — that would re-introduce the phantom
            # path='' row that issues #81/#82 fixed. Today every shipped
            # template has a non-empty path; the guard exists so a
            # future template typo can't quietly resurrect the bug.
            if not path:
                logger.warning(
                    "Template %s collection skipped: empty path", template,
                )
                continue
            await coll_repo.get_or_create(vault_id, path)

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
