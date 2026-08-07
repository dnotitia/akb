"""Measurement-only Document compatibility facade over the native ledger.

The public Document models intentionally keep their historical commit-shaped
field names.  Values are native opaque Revision tokens; this service neither
constructs a Git service nor writes the legacy document projection.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import NoReturn

import asyncpg

from app.db.postgres import get_pool
from app.exceptions import AKBError, ConflictError, NotFoundError, ValidationError
from app.models.document import (
    DOC_STATUSES,
    BrowseResponse,
    DocumentPutRequest,
    DocumentPutResponse,
    DocumentResponse,
    DocumentUpdateRequest,
)
from app.repositories.native_revision_repo import NativeRevisionRepository
from app.repositories.vault_repo import VaultRepository
from app.services.document_service import (
    EditError,
    DocumentService,
    _body_content_hash,
    _build_frontmatter,
    _certified_content_hash,
    _compose_markdown,
    _parse_markdown,
    newest_public_slug,
    validate_vault_name,
)
from app.services.m1_pg_body_store import M1PgBodyStore
from app.services.native_revision_service import (
    Failpoint,
    NativeRevisionService,
    NativeRevisionSnapshot,
)
from app.services.resource_hash import HASH_ALGORITHM
from app.services.role_sync import get_role_sync
from app.services.uri_service import doc_uri
from app.util.text import (
    doc_path,
    normalize_collection_path,
    slugify,
    split_doc_path,
    strip_own_suffix,
    to_nfc,
)


class NativeRevisionUnsupportedSurfaceError(AKBError):
    """A non-revision surface was reached in the isolated measurement arm."""

    def __init__(self, surface: str):
        super().__init__(
            f"{surface} is unavailable in the native-ledger M1 measurement arm",
            status_code=501,
            code="native_revision_surface_unsupported",
        )


class NativeDocumentService(DocumentService):
    """Document lifecycle adapter preserving existing request/response models."""

    def __init__(
        self,
        *,
        pool: asyncpg.Pool | None = None,
        failpoint: Failpoint | None = None,
    ):
        # Deliberately do not call DocumentService.__init__: that would create
        # the legacy Git adapter before a request is even served.
        self._injected_pool = pool
        # ``failpoint`` carries the native service's deterministic test-only
        # hook down to the substrate this facade composes; production
        # composition must leave it unset.  Left unset, ``_native`` builds
        # exactly the service it built before this seam existed.
        self._failpoint = failpoint

    async def _pool(self) -> asyncpg.Pool:
        return self._injected_pool or await get_pool()

    async def _vault_id(self, vault: str) -> uuid.UUID:
        pool = await self._pool()
        async with pool.acquire() as conn:
            vault_id = await conn.fetchval("SELECT id FROM vaults WHERE name = $1", vault)
        if vault_id is None:
            raise NotFoundError("Vault", vault)
        return vault_id

    async def _native(self) -> NativeRevisionService:
        """Compose the substrate on the frozen P1 searchable-body placement.

        This method is the composition root for every Document written through
        the public facade, so it is where P1 selects ``pg-bodystore-v1`` — the
        same placement ``m1_native_text_file_bridge`` already injects for
        native text Files. Documents and Files therefore land on one body
        store instead of two.

        The injection deliberately does **not** move into
        ``NativeRevisionService``'s own default. The M1 measurement adapters
        (``backend/scripts/native_revision_m1_adapter.py`` and its siblings)
        construct ``NativeRevisionService(pool)`` directly and must keep
        reproducing the historical ``m1-reference-payload-v1`` behaviour their
        recorded runs were measured against; changing the default would
        silently re-label every replay of an already-published measurement.

        Historical Revisions keep the placement recorded in their immutable
        manifest, so a namespace is mixed by design during the transition.
        Readers dispatch on ``selected_placement`` rather than assuming one.
        """
        pool = await self._pool()
        return NativeRevisionService(
            pool, payload_store=M1PgBodyStore(pool), failpoint=self._failpoint
        )

    @staticmethod
    async def _yield_after_head_race(race_count: int) -> None:
        """Cooperate under sustained writers without inventing a 409 limit.

        ``asyncio.sleep`` is cancellation-safe: request cancellation interrupts
        the retry instead of leaving a detached publication loop behind.
        """
        delay = min(0.001 * (2 ** min(race_count, 5)), 0.032)
        await asyncio.sleep(delay)

    async def _current(
        self,
        vault: str,
        reference: str,
    ) -> tuple[uuid.UUID, NativeRevisionSnapshot]:
        vault_id = await self._vault_id(vault)
        snapshot = await (await self._native()).get_current_reference(
            namespace_id=vault_id,
            surface="document",
            reference=to_nfc(reference),
        )
        return vault_id, snapshot

    async def _path_is_owned(self, vault_id: uuid.UUID, path: str) -> bool:
        repository = NativeRevisionRepository(await self._pool())
        return (
            await repository.resolve_live_reference(
                namespace_id=vault_id,
                surface="document",
                reference=path,
            )
            is not None
        )

    async def _current_path_is_owned(self, vault_id: uuid.UUID, path: str) -> bool:
        repository = NativeRevisionRepository(await self._pool())
        pool = await self._pool()
        async with pool.acquire() as conn:
            return (
                await repository.find_live_path(
                    conn,
                    vault_id,
                    "document",
                    path,
                )
                is not None
            )

    async def _resolve_native_free_path(
        self,
        vault_id: uuid.UUID,
        base_path: str,
        path_identity: uuid.UUID,
        *,
        aliases_own_path: bool = True,
    ) -> str:
        path_is_owned = self._path_is_owned if aliases_own_path else self._current_path_is_owned
        if not await path_is_owned(vault_id, base_path):
            return base_path
        stem = base_path[:-3] if base_path.endswith(".md") else base_path
        for width in (8, 12, 16, len(path_identity.hex)):
            candidate = f"{stem}-{path_identity.hex[:width]}.md"
            if not await path_is_owned(vault_id, candidate):
                return candidate
        ordinal = 2
        while True:
            candidate = f"{stem}-{path_identity.hex}-{ordinal}.md"
            if not await path_is_owned(vault_id, candidate):
                return candidate
            ordinal += 1

    async def _created_by_name(self, created_by: str | None) -> str | None:
        if not created_by:
            return None
        pool = await self._pool()
        async with pool.acquire() as conn:
            return await conn.fetchval(
                """
                SELECT COALESCE(display_name, username)
                  FROM users
                 WHERE id::text = $1 OR username = $1
                 LIMIT 1
                """,
                created_by,
            )

    async def _public_slug(
        self, vault_id: uuid.UUID, vault: str, path: str
    ) -> str | None:
        """Newest publication slug for the document at ``path``, or None.

        Shares one query with the legacy arm (``newest_public_slug``) instead
        of keeping a second copy — the two copies had drifted into the same
        defect, a ``resource_uri`` match with no ``vault_id`` predicate, which
        told a reader that another vault carries a publication for the same
        path and handed over its slug.

        ``vault_id`` is passed in rather than resolved here: every caller has
        already resolved it through ``_current``, and ``_vault_id`` is an
        uncached pool checkout plus a query.

        ``document_id=None`` is passed deliberately. This arm does not write
        the legacy ``documents`` projection, so there is no id to name here;
        the vault-scoped ``resource_uri`` fallback is what answers, and it is
        scoped either way.
        """
        pool = await self._pool()
        async with pool.acquire() as conn:
            return await newest_public_slug(
                conn,
                vault_id=vault_id,
                document_id=None,
                resource_uri=doc_uri(vault, path),
            )

    async def _response(
        self,
        *,
        vault: str,
        vault_id: uuid.UUID,
        current: NativeRevisionSnapshot,
        selected: NativeRevisionSnapshot | None = None,
    ) -> DocumentResponse:
        selected = selected or current
        current_fm, _ = _parse_markdown(current.text)
        _, selected_body = _parse_markdown(selected.text)
        created_by = current_fm.get("created_by")
        # Sequential on purpose. These two reads are independent and a
        # `gather` would save one round trip, but each takes its own pool
        # connection, so overlapping them doubles this path's peak checkouts
        # per document read to buy a saving that is worth nothing here — this
        # arm is measurement-only and is not the default backend. It also
        # changes failure behaviour: under `gather` a raise in one no longer
        # stops the other, which runs on to completion with its result (or its
        # own exception) discarded. Not worth either, for zero live gain.
        public_slug = await self._public_slug(vault_id, vault, current.path)
        created_by_name = await self._created_by_name(created_by)
        return DocumentResponse(
            uri=doc_uri(vault, current.path),
            vault=vault,
            path=current.path,
            title=current_fm.get("title") or current.path.rsplit("/", 1)[-1],
            type=current_fm.get("type") or "note",
            status=current_fm.get("status") or "draft",
            summary=current_fm.get("summary"),
            domain=current_fm.get("domain"),
            created_by=created_by,
            created_by_name=created_by_name,
            created_at=current_fm.get("created_at") or current.resource_created_at,
            updated_at=current.resource_updated_at,
            current_commit=selected.revision_id,
            content_hash=_body_content_hash(selected_body),
            hash_algorithm=HASH_ALGORITHM,
            tags=list(current_fm.get("tags") or []),
            content=selected_body,
            is_public=public_slug is not None,
            public_slug=public_slug,
            metadata_is_current=selected.revision_id != current.revision_id,
        )

    async def put(
        self,
        req: DocumentPutRequest,
        agent_id: str | None = None,
    ) -> DocumentPutResponse:
        if req.status not in DOC_STATUSES:
            raise ValidationError(f"status must be one of {list(DOC_STATUSES)}, got {req.status!r}")
        vault_id = await self._vault_id(req.vault)
        now = datetime.now(UTC)
        base_slug = slugify(req.slug) if req.slug else slugify(req.title)
        collection = normalize_collection_path(req.collection)
        base_path = doc_path(collection, base_slug)
        path_identity = uuid.uuid4()
        if req.slug and await self._current_path_is_owned(vault_id, base_path):
            raise ConflictError(f"Document already exists at path: {base_path}")
        final_path = (
            base_path
            if req.slug
            else await self._resolve_native_free_path(
                vault_id,
                base_path,
                path_identity,
                aliases_own_path=False,
            )
        )

        frontmatter = _build_frontmatter(req, now)
        if agent_id:
            frontmatter["created_by"] = agent_id
        raw = _compose_markdown(frontmatter, req.content)
        actor = agent_id or "unknown"
        mutation_id = uuid.uuid4()
        race_count = 0
        while True:
            message = f"[put] {final_path}\n\nagent: {actor}\naction: create\nsummary: {req.title}"
            try:
                result = await (await self._native()).create_text(
                    namespace_id=vault_id,
                    surface="document",
                    path=final_path,
                    payload=raw,
                    actor=actor,
                    mutation_id=mutation_id,
                    resource_id=path_identity,
                    message=message,
                    subject=f"[put] {final_path}",
                    summary=req.title,
                )
                break
            except ConflictError as exc:
                if req.slug or not str(exc).startswith("Native Resource already exists at path:"):
                    raise
                await self._yield_after_head_race(race_count)
                race_count += 1
                final_path = await self._resolve_native_free_path(
                    vault_id,
                    base_path,
                    path_identity,
                    aliases_own_path=False,
                )
        content_hash = _certified_content_hash(raw)
        return DocumentPutResponse(
            uri=doc_uri(req.vault, final_path),
            vault=req.vault,
            path=final_path,
            commit_hash=result.revision_id,
            current_commit=result.revision_id,
            content_hash=content_hash,
            hash_algorithm=HASH_ALGORITHM,
            action="created",
            chunks_indexed=0,
            entities_found=0,
        )

    async def get(self, vault: str, doc_ref: str) -> DocumentResponse:
        vault_id, current = await self._current(vault, doc_ref)
        return await self._response(vault=vault, vault_id=vault_id, current=current)

    async def get_at_commit(
        self,
        vault: str,
        doc_ref: str,
        version: str,
    ) -> DocumentResponse:
        vault_id, current = await self._current(vault, doc_ref)
        if len(version) != 40 or any(ch not in "0123456789abcdef" for ch in version):
            raise NotFoundError("Document version", f"{current.path}@{version[:8]}")
        selected = await (await self._native()).get_revision(
            namespace_id=vault_id,
            surface="document",
            reference=current.path,
            revision_id=version,
        )
        return await self._response(
            vault=vault, vault_id=vault_id, current=current, selected=selected,
        )

    async def update(
        self,
        vault: str,
        doc_ref: str,
        req: DocumentUpdateRequest,
        agent_id: str | None = None,
    ) -> DocumentPutResponse:
        if req.status is not None and req.status not in DOC_STATUSES:
            raise ValidationError(f"status must be one of {list(DOC_STATUSES)}, got {req.status!r}")
        vault_id, current = await self._current(vault, doc_ref)
        return await self._update_from_snapshot(
            vault=vault,
            vault_id=vault_id,
            current=current,
            req=req,
            agent_id=agent_id,
        )

    async def _update_from_snapshot(
        self,
        *,
        vault: str,
        vault_id: uuid.UUID,
        current: NativeRevisionSnapshot,
        req: DocumentUpdateRequest,
        agent_id: str | None,
    ) -> DocumentPutResponse:
        exact_head_pinned = req.expected_commit is not None
        resource_id = current.resource_id
        native = await self._native()
        race_count = 0
        while True:
            if req.expected_commit and req.expected_commit != current.revision_id:
                raise ConflictError(
                    f"current_commit moved: expected {req.expected_commit}, actual {current.revision_id}"
                )
            frontmatter, current_body = _parse_markdown(current.text)
            previous_hash = _body_content_hash(current_body)
            if req.expected_content_hash and req.expected_content_hash != previous_hash:
                raise ConflictError(f"content_hash moved: expected {req.expected_content_hash}, actual {previous_hash}")
            if req.title:
                frontmatter["title"] = req.title
            if req.type:
                frontmatter["type"] = req.type
            if req.status:
                frontmatter["status"] = req.status
            if req.tags is not None:
                frontmatter["tags"] = req.tags
            if req.domain is not None:
                frontmatter["domain"] = req.domain
            if req.summary is not None:
                frontmatter["summary"] = req.summary
            if req.depends_on is not None:
                frontmatter["depends_on"] = req.depends_on
            if req.related_to is not None:
                frontmatter["related_to"] = req.related_to
            frontmatter["updated_at"] = datetime.now(UTC).isoformat()
            new_body = req.content if req.content is not None else current_body
            raw = _compose_markdown(frontmatter, new_body)
            actor = agent_id or "unknown"
            summary = req.message or f"Update {current.path}"
            message = f"[update] {current.path}\n\nagent: {actor}\naction: update\nsummary: {summary}"
            try:
                result = await native.replace_text(
                    namespace_id=vault_id,
                    surface="document",
                    path=current.path,
                    payload=raw,
                    actor=actor,
                    mutation_id=uuid.uuid4(),
                    # This internal compare protects the read/merge/write
                    # attempt. Unpinned callers are transparently recomputed
                    # against the new Head; explicit predicates fail closed.
                    expected_revision_id=current.revision_id,
                    expected_resource_id=resource_id,
                    message=message,
                    subject=f"[update] {current.path}",
                    summary=summary,
                )
            except ConflictError as exc:
                if exact_head_pinned or not str(exc).startswith(
                    ("Native Revision conflict:", "Native Resource conflict:")
                ):
                    raise
                await self._yield_after_head_race(race_count)
                race_count += 1
                current = await native.get_current_resource(
                    namespace_id=vault_id,
                    surface="document",
                    resource_id=resource_id,
                )
                continue
            return DocumentPutResponse(
                uri=doc_uri(vault, result.path),
                vault=vault,
                path=result.path,
                commit_hash=result.revision_id,
                current_commit=result.revision_id,
                previous_commit=result.parent_revision_id,
                previous_content_hash=previous_hash,
                content_hash=_certified_content_hash(raw),
                hash_algorithm=HASH_ALGORITHM,
                action="updated",
                chunks_indexed=0,
                entities_found=0,
            )

    async def move(
        self,
        vault: str,
        doc_ref: str,
        *,
        collection: str | None = None,
        slug: str | None = None,
        message: str | None = None,
        agent_id: str | None = None,
    ) -> DocumentPutResponse:
        if collection is None and slug is None:
            raise ValidationError("move requires at least one of collection or slug")
        requested_collection = normalize_collection_path(collection) if collection is not None else None
        requested_slug = slugify(slug) if slug else None
        actor = agent_id or "unknown"
        vault_id, current = await self._current(vault, doc_ref)
        resource_id = current.resource_id
        native = await self._native()
        race_count = 0
        while True:
            current_collection, current_slug = split_doc_path(current.path)
            next_collection = requested_collection if requested_collection is not None else current_collection
            next_slug = (
                requested_slug
                if requested_slug is not None
                else strip_own_suffix(
                    current_slug,
                    current.resource_id,
                )
            )
            base_path = doc_path(next_collection, next_slug)
            if base_path == current.path:
                raise ValidationError("move is a no-op: the target path equals the current path")
            next_path = await self._resolve_native_free_path(vault_id, base_path, current.resource_id)
            if requested_slug is not None and next_path != base_path:
                raise ConflictError(f"Document already exists at path: {base_path}")
            summary = message or f"{current.path} -> {next_path}"
            history_message = (
                f"[move] {current.path} -> {next_path}\n\n"
                f"agent: {actor}\naction: move\nsummary: {summary}"
            )
            try:
                result = await native.move_text(
                    namespace_id=vault_id,
                    surface="document",
                    path=current.path,
                    path_to=next_path,
                    actor=actor,
                    mutation_id=uuid.uuid4(),
                    expected_revision_id=current.revision_id,
                    expected_resource_id=resource_id,
                    message=history_message,
                    subject=f"[move] {current.path} -> {next_path}",
                    summary=summary,
                )
            except ConflictError as exc:
                retryable_authority_race = str(exc).startswith(
                    ("Native Revision conflict:", "Native Resource conflict:")
                )
                retryable_allocation_race = requested_slug is None and str(exc).startswith(
                    ("Native Resource already exists at path:", "Native Resource alias already owns path:")
                )
                if not (retryable_authority_race or retryable_allocation_race):
                    raise
                await self._yield_after_head_race(race_count)
                race_count += 1
                current = await native.get_current_resource(
                    namespace_id=vault_id,
                    surface="document",
                    resource_id=resource_id,
                )
                continue
            committed = await native.get_resource_revision(
                namespace_id=vault_id,
                surface="document",
                resource_id=result.resource_id,
                revision_id=result.revision_id,
            )
            _, body = _parse_markdown(committed.text)
            break
        return DocumentPutResponse(
            uri=doc_uri(vault, result.path),
            vault=vault,
            path=result.path,
            commit_hash=result.revision_id,
            current_commit=result.revision_id,
            previous_commit=result.parent_revision_id,
            content_hash=_body_content_hash(body),
            hash_algorithm=HASH_ALGORITHM,
            action="moved",
            chunks_indexed=0,
            entities_found=0,
        )

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
        if not old_string:
            raise EditError("old_string cannot be empty")
        vault_id, current = await self._current(vault, doc_ref)
        resource_id = current.resource_id
        native = await self._native()
        race_count = 0
        while True:
            if base_commit and base_commit != current.revision_id:
                raise ConflictError(f"current_commit moved: expected {base_commit}, actual {current.revision_id}")
            _, body = _parse_markdown(current.text)
            occurrences = body.count(old_string)
            if occurrences == 0:
                raise EditError("old_string not found in document body. Use akb_get to verify current content.")
            if occurrences > 1 and not replace_all:
                raise EditError(
                    f"old_string appears {occurrences} times in document. "
                    "Add more surrounding context to make it unique, or set replace_all=true."
                )
            new_body = (
                body.replace(old_string, new_string)
                if replace_all
                else body.replace(
                    old_string,
                    new_string,
                    1,
                )
            )
            if new_body == body:
                content_hash = _body_content_hash(body)
                return DocumentPutResponse(
                    uri=doc_uri(vault, current.path),
                    vault=vault,
                    path=current.path,
                    commit_hash=current.revision_id,
                    current_commit=current.revision_id,
                    content_hash=content_hash,
                    hash_algorithm=HASH_ALGORITHM,
                    action="unchanged",
                    chunks_indexed=0,
                    entities_found=0,
                )
            try:
                return await self._update_from_snapshot(
                    vault=vault,
                    vault_id=vault_id,
                    current=current,
                    req=DocumentUpdateRequest(
                        content=new_body,
                        message=message,
                        # Pin each derived body to the snapshot it edited.
                        # An unpinned edit recomputes on a moved Head below.
                        expected_commit=current.revision_id,
                    ),
                    agent_id=agent_id,
                )
            except ConflictError as exc:
                if base_commit is not None:
                    raise
                if not (
                    str(exc).startswith(("Native Revision conflict:", "Native Resource conflict:"))
                    or str(exc).startswith("current_commit moved:")
                ):
                    raise
                await self._yield_after_head_race(race_count)
                race_count += 1
                current = await native.get_current_resource(
                    namespace_id=vault_id,
                    surface="document",
                    resource_id=resource_id,
                )

    async def delete(self, vault: str, doc_ref: str, agent_id: str | None = None) -> bool:
        vault_id, current = await self._current(vault, doc_ref)
        resource_id = current.resource_id
        native = await self._native()
        actor = agent_id or "unknown"
        race_count = 0
        while True:
            try:
                await native.delete_resource(
                    namespace_id=vault_id,
                    surface="document",
                    path=current.path,
                    actor=actor,
                    mutation_id=uuid.uuid4(),
                    expected_revision_id=current.revision_id,
                    expected_resource_id=resource_id,
                    message=f"[delete] {current.path}\n\nagent: {actor}\naction: delete",
                    subject=f"[delete] {current.path}",
                )
                return True
            except ConflictError as exc:
                if not str(exc).startswith(("Native Revision conflict:", "Native Resource conflict:")):
                    raise
                await self._yield_after_head_race(race_count)
                race_count += 1
                current = await native.get_current_resource(
                    namespace_id=vault_id,
                    surface="document",
                    resource_id=resource_id,
                )

    async def history(self, vault: str, doc_ref: str, limit: int = 20) -> dict:
        vault_id = await self._vault_id(vault)
        resource, rows = await (await self._native()).list_history(
            namespace_id=vault_id,
            surface="document",
            reference=to_nfc(doc_ref),
            limit=limit,
        )
        history = [
            {
                "hash": row["revision_id"],
                "message": row["message"] or row["subject"] or row["action"],
                "author": row["actor"],
                "date": row["occurred_at"].isoformat(),
            }
            for row in rows
        ]
        return {"uri": doc_uri(vault, resource["current_path"]), "history": history}

    @staticmethod
    def _unsupported(surface: str) -> NoReturn:
        raise NativeRevisionUnsupportedSurfaceError(surface)

    async def browse(self, *args, **kwargs) -> BrowseResponse:
        self._unsupported("document browse")

    async def create_vault(
        self,
        name: str,
        description: str = "",
        owner_id: str | None = None,
        template: str | None = None,
        public_access: str = "none",
        external_git: dict | None = None,
    ) -> str:
        """Create a PG-native vault without materialising legacy Git state.

        ``vaults.git_path`` remains a required legacy catalog field, so the
        native arm records an explicitly non-filesystem sentinel.  The native
        ledger takes the vault UUID as its namespace authority; it neither
        initializes a bare repository nor creates a linked worktree.
        """
        validate_vault_name(name)
        if template is not None:
            self._unsupported("vault templates")
        if external_git is not None:
            self._unsupported("external git vaults")

        from app.services.access_service import validate_public_access

        public_access = validate_public_access(public_access)
        pool = await self._pool()
        vault_repo = VaultRepository(pool)
        if await vault_repo.get_by_name(name):
            raise ConflictError(f"Vault already exists: {name}")
        uid = uuid.UUID(owner_id) if owner_id else None
        vault_id: uuid.UUID | None = None
        # These strict same-connection hooks observe the uncommitted catalog
        # row while scoped PAT memberships are computed, and PostgreSQL rolls
        # back both the row and role DDL if either hook fails or is cancelled.
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    vault_id = await vault_repo.create(
                        name=name,
                        description=description,
                        git_path=f"native-ledger://{name}",
                        owner_id=uid,
                        public_access=public_access,
                        conn=conn,
                    )
                    role_sync = get_role_sync()
                    await role_sync.on_vault_create_in_conn(conn, vault_id, uid)
                    await role_sync.on_public_access_change_in_conn(
                        conn, vault_id, public_access,
                    )
        except asyncpg.UniqueViolationError as exc:
            if exc.constraint_name == "vaults_name_key":
                raise ConflictError(f"Vault already exists: {name}") from exc
            raise
        assert vault_id is not None
        return str(vault_id)

    async def list_vaults(self) -> list[dict]:
        self._unsupported("vault listing")
