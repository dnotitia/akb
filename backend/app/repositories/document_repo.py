"""Repository for document operations."""

from __future__ import annotations

import uuid
from datetime import datetime

import asyncpg

from app.exceptions import ConflictError
from app.util.text import like_escape
from app.utils import dumps_jsonb


async def acquire_path_lock(conn, vault_id: uuid.UUID, path: str) -> None:
    """Block until exclusive access to ``(vault_id, path)`` is granted.

    Uses ``pg_advisory_xact_lock`` so the lock is released automatically
    when the calling transaction commits or rolls back. The caller MUST
    be inside a transaction — call it as the FIRST step after opening
    the TX, before any git mutation or row read used to gate writes.

    Lock key: two 32-bit ints derived from ``hashtext(vault_id)`` and
    ``hashtext(path)`` so two distinct ``(vault, path)`` tuples cannot
    collide unless ``hashtext`` itself collides (negligible).

    Concurrent puts/updates/edits/deletes for the same ``(vault, path)``
    serialize on this lock, eliminating the check-then-act race where
    git HEAD ends up pointing at a different writer's commit than
    ``documents.current_commit``.
    """
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext($1::text), hashtext($2))",
        str(vault_id),
        path,
    )


# ── Resource aliases (rename/move redirects) ─────────────────────
# Polymorphic across document/table/file, so these are module-level helpers
# rather than methods on the document repo. The invariant is old_ref →
# resource_id (a UUID), never old_ref → new path (that would chain).

async def add_resource_alias(
    conn, vault_id: uuid.UUID, resource_type: str, old_ref: str, resource_id: uuid.UUID
) -> None:
    """Record that ``old_ref`` used to name this resource. Upserts so a path
    that has been the old name more than once re-points to the latest id."""
    await conn.execute(
        """
        INSERT INTO resource_aliases (vault_id, resource_type, old_ref, resource_id)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (vault_id, resource_type, old_ref)
        DO UPDATE SET resource_id = EXCLUDED.resource_id, created_at = NOW()
        """,
        vault_id, resource_type, old_ref, resource_id,
    )


async def drop_resource_alias(
    conn, vault_id: uuid.UUID, resource_type: str, old_ref: str
) -> None:
    """Remove any redirect sitting on ``old_ref`` — called when a real resource
    is (re)created at that path, so the live resource wins over a stale alias."""
    await conn.execute(
        "DELETE FROM resource_aliases WHERE vault_id = $1 AND resource_type = $2 AND old_ref = $3",
        vault_id, resource_type, old_ref,
    )


async def drop_aliases_for_resource(
    conn, vault_id: uuid.UUID, resource_type: str, resource_id: uuid.UUID
) -> None:
    """Remove every redirect pointing at a resource — called on its deletion so
    no alias outlives the row it targets."""
    await conn.execute(
        "DELETE FROM resource_aliases WHERE vault_id = $1 AND resource_type = $2 AND resource_id = $3",
        vault_id, resource_type, resource_id,
    )


class DocumentRepository:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def create(
        self,
        vault_id: uuid.UUID,
        collection_id: uuid.UUID | None,
        path: str,
        title: str,
        doc_type: str,
        status: str,
        summary: str | None,
        domain: str | None,
        created_by: str | None,
        now: datetime,
        commit_hash: str,
        content_hash: str,
        hash_algorithm: str,
        tags: list[str],
        metadata: dict,
        *,
        conn=None,
        doc_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        # Caller may supply the id so a path can embed its short form; else mint one.
        doc_id = doc_id or uuid.uuid4()

        async def _insert(c):
            try:
                await c.execute(
                    """
                    INSERT INTO documents
                        (id, vault_id, collection_id, path, title, doc_type, status,
                         summary, domain, created_by, created_at, updated_at,
                         current_commit, content_hash, hash_algorithm, content_hash_commit,
                         tags, metadata)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                            $13, $14, $15, $16, $17, $18)
                    """,
                    doc_id, vault_id, collection_id, path, title, doc_type, status,
                    summary, domain, created_by, now, now,
                    commit_hash, content_hash, hash_algorithm, commit_hash,
                    tags, dumps_jsonb(metadata),
                )
            except asyncpg.UniqueViolationError as e:
                # (vault_id, path) is the only UNIQUE constraint that callers
                # can collide on. Surface it as a 409 instead of a 500.
                raise ConflictError(f"Document already exists at path: {path}") from e
            except asyncpg.ForeignKeyViolationError as e:
                # The parent collection (or vault) was deleted out from under
                # this insert — a concurrent collection-retirement / vault-delete
                # race: get_or_create observed the collection, then a DELETE
                # removed it before this INSERT committed. `collection_id` is
                # ON DELETE SET NULL, so the *delete* side re-homes existing
                # docs, but a NEW insert that still references the vanished id
                # trips the FK. Surface it as a 409 (retryable) instead of an
                # unhandled 500.
                raise ConflictError(
                    f"Target collection or vault was concurrently deleted while creating: {path}"
                ) from e

        # When `conn` is supplied, run on the caller's connection/TX so a
        # put can hold ONE pool connection for its whole critical section
        # (advisory lock + create + chunks). Acquiring a *second* connection
        # here while the caller still holds its lock connection is what
        # deadlocked the pool under a write burst: ≥ pool_size concurrent
        # writers each held one conn and waited for a second that never freed.
        if conn is not None:
            await _insert(conn)
        else:
            async with self.pool.acquire() as c:
                await _insert(c)
        return doc_id

    # Document lookup keys: PG UUID or exact path. MCP / REST handlers
    # all split the URI into (vault, path) before calling here, so the
    # path is canonical. The earlier `path LIKE '%' || $2 || '%'` arm
    # was a substring match that, after the URI cutover, could silently
    # return the wrong doc when one path was a substring of another
    # (`api.md` matching `api-v2.md`) — turning a benign-looking
    # `akb_delete` into a wrong-resource delete. Exact match closes
    # that class of bug.
    _MATCH_WHERE = "(d.id::text = $2 OR d.path = $2)"

    @staticmethod
    def match_clause(param_index: int, alias: str = "d") -> str:
        """Doc lookup predicate against `<alias>.id` OR `<alias>.path`,
        parameter-positional so callers can compose the clause inside
        a larger query with its own placeholder numbering. Mirrors
        `_MATCH_WHERE` so a single source defines the substring-match
        ban — adding new lookup arms here propagates to every caller."""
        return f"({alias}.id::text = ${param_index} OR {alias}.path = ${param_index})"

    async def find_by_ref(self, vault_id: uuid.UUID, ref: str) -> dict | None:
        """Find document by UUID or exact path."""
        async with self.pool.acquire() as conn:
            return await self.find_by_ref_with_conn(conn, vault_id, ref)

    async def find_by_ref_with_conn(self, conn, vault_id: uuid.UUID, ref: str) -> dict | None:
        """Find document using an existing connection (no pool acquire).

        Resolution order: an exact id/path match wins; only on a miss do we
        consult ``resource_aliases`` so an OLD path (from a prior move/rename)
        still resolves to the current document. Exact-first means a real
        document that now occupies a reused path always beats a stale redirect.
        """
        row = await conn.fetchrow(
            f"""
            SELECT d.*, v.name as vault_name
            FROM documents d
            JOIN vaults v ON d.vault_id = v.id
            WHERE d.vault_id = $1
              AND {self._MATCH_WHERE}
            """,
            vault_id, ref,
        )
        if row:
            return dict(row)
        # Alias fallback — old_ref → current resource_id (never path→path).
        row = await conn.fetchrow(
            """
            SELECT d.*, v.name as vault_name
            FROM resource_aliases a
            JOIN documents d ON d.id = a.resource_id
            JOIN vaults v ON d.vault_id = v.id
            WHERE a.vault_id = $1 AND a.resource_type = 'document' AND a.old_ref = $2
              AND d.vault_id = $1
            LIMIT 1
            """,
            vault_id, ref,
        )
        return dict(row) if row else None

    async def find_by_path(self, vault_id: uuid.UUID, path: str, *, conn=None) -> dict | None:
        sql = """
            SELECT d.*, v.name as vault_name
            FROM documents d
            JOIN vaults v ON d.vault_id = v.id
            WHERE d.vault_id = $1 AND d.path = $2
        """
        if conn is not None:
            row = await conn.fetchrow(sql, vault_id, path)
            return dict(row) if row else None
        async with self.pool.acquire() as c:
            row = await c.fetchrow(sql, vault_id, path)
            return dict(row) if row else None

    async def update_path(
        self, doc_id: uuid.UUID, new_path: str, *, collection_id: uuid.UUID | None,
        now: datetime, commit_hash: str, conn,
    ) -> None:
        """Move a document to ``new_path`` (rename/move). Identity (id) is
        unchanged; only the path/collection/commit move. Surfaces a path
        collision as a 409 like create() does."""
        try:
            await conn.execute(
                """
                UPDATE documents
                   SET path = $2, collection_id = $3, updated_at = $4, current_commit = $5
                 WHERE id = $1
                """,
                doc_id, new_path, collection_id, now, commit_hash,
            )
        except asyncpg.UniqueViolationError as e:
            # The target was free when move() resolved it under the lock, so this
            # only fires when a concurrent writer claimed it in the meantime — a
            # retry re-resolves to a fresh free path.
            raise ConflictError(
                f"Document already exists at path: {new_path} "
                "(a concurrent write claimed it — retry the move)"
            ) from e

    async def update(
        self,
        doc_id: uuid.UUID,
        title: str | None = None,
        doc_type: str | None = None,
        status: str | None = None,
        summary: str | None = None,
        domain: str | None = None,
        now: datetime | None = None,
        commit_hash: str | None = None,
        content_hash: str | None = None,
        hash_algorithm: str | None = None,
        content_hash_commit: str | None = None,
        tags: list[str] | None = None,
        conn=None,
    ) -> None:
        sql = """
                UPDATE documents SET
                    title = COALESCE($1, title),
                    doc_type = COALESCE($2, doc_type),
                    status = COALESCE($3, status),
                    summary = COALESCE($4, summary),
                    domain = COALESCE($5, domain),
                    updated_at = COALESCE($6, updated_at),
                    current_commit = COALESCE($7, current_commit),
                    content_hash = COALESCE($8, content_hash),
                    hash_algorithm = COALESCE($9, hash_algorithm),
                    content_hash_commit = COALESCE($10, content_hash_commit),
                    tags = COALESCE($11, tags)
                WHERE id = $12
                """
        args = (title, doc_type, status, summary, domain, now, commit_hash,
                content_hash, hash_algorithm, content_hash_commit, tags, doc_id)
        if conn is not None:
            await conn.execute(sql, *args)
        else:
            async with self.pool.acquire() as own_conn:
                await own_conn.execute(sql, *args)

    async def update_hash(
        self,
        doc_id: uuid.UUID,
        *,
        content_hash: str,
        hash_algorithm: str,
        content_hash_commit: str | None,
        conn=None,
    ) -> None:
        sql = """
                UPDATE documents SET
                    content_hash = $1,
                    hash_algorithm = $2,
                    content_hash_commit = $3
                WHERE id = $4
                """
        args = (content_hash, hash_algorithm, content_hash_commit, doc_id)
        if conn is not None:
            await conn.execute(sql, *args)
        else:
            async with self.pool.acquire() as own_conn:
                await own_conn.execute(sql, *args)

    async def delete(self, doc_id: uuid.UUID, *, conn=None) -> None:
        """Delete the documents row. When `conn` is provided the DELETE
        runs on the caller's connection (so it joins the same TX as the
        cascade in `document_service.delete`)."""
        if conn is None:
            async with self.pool.acquire() as own_conn:
                await own_conn.execute("DELETE FROM documents WHERE id = $1", doc_id)
        else:
            await conn.execute("DELETE FROM documents WHERE id = $1", doc_id)

    async def list_by_collection(self, vault_id: uuid.UUID, collection_path: str) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT path, title, doc_type, status, summary, tags, updated_at,
                       current_commit, content_hash, hash_algorithm, content_hash_commit
                FROM documents
                WHERE vault_id = $1 AND collection_id = (
                    SELECT id FROM collections WHERE vault_id = $1 AND path = $2
                )
                ORDER BY updated_at DESC
                """,
                vault_id, collection_path,
            )
            return [dict(r) for r in rows]

    async def list_by_vault(self, vault_id: uuid.UUID) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT path, title, doc_type, status, summary, tags, updated_at,
                       current_commit, content_hash, hash_algorithm, content_hash_commit
                FROM documents WHERE vault_id = $1 ORDER BY updated_at DESC
                """,
                vault_id,
            )
            return [dict(r) for r in rows]

    async def list_docs_by_depth(
        self,
        vault_id: uuid.UUID,
        max_depth: int,
        prefix: str = "",
        *,
        include_archived: bool = False,
    ) -> list[dict]:
        """List documents under ``prefix`` (vault root if ``prefix=""``)
        whose containing-collection depth, measured from inside the
        prefix, is ≤ ``max_depth``. ``max_depth < 0`` disables the
        depth filter (entire subtree).

        Depth = number of path separators *between* the prefix boundary
        and the document filename:
          - prefix="", path="doc.md"        → depth 0 (vault root)
          - prefix="", path="X/doc.md"      → depth 1 (one collection in)
          - prefix="", path="X/Y/doc.md"    → depth 2 (nested)
          - prefix="X", path="X/doc.md"     → depth 0 (root of X)
          - prefix="X", path="X/Y/doc.md"   → depth 1 (one level inside X)

        Slashes are counted via ``length - length(replace(...,'/',''))``
        because ``string_to_array`` rejects empty input — this form
        handles vault-root docs uniformly.
        """
        base_select = (
            "SELECT id, path, title, doc_type, status, summary, tags, updated_at, "
            "current_commit, content_hash, hash_algorithm, content_hash_commit "
            "FROM documents WHERE vault_id = $1"
        )
        # Default-hide archived docs from browse; opt back in with
        # include_archived=true. Literal status — no bind param.
        if not include_archived:
            base_select += " AND status != 'archived'"
        params: list = [vault_id]

        if prefix:
            # Defend against LIKE metacharacters even though normalized
            # collection paths shouldn't contain them. Goes through
            # the shared helper so all four call sites agree on the
            # escape semantics.
            from app.util.text import like_escape
            params.append(like_escape(prefix) + "/%")
            prefix_clause = f" AND path LIKE ${len(params)} ESCAPE '\\'"
            # Slashes the prefix itself contributes to `path`: "X" → 1,
            # "X/Y" → 2 (the prefix separator plus its own internal slashes).
            depth_offset = prefix.count("/") + 1
        else:
            prefix_clause = ""
            depth_offset = 0

        if max_depth < 0:
            depth_clause = ""
        else:
            params.append(max_depth + depth_offset)
            depth_clause = (
                f" AND (length(path) - length(replace(path, '/', ''))) <= ${len(params)}"
            )

        sql = base_select + prefix_clause + depth_clause + " ORDER BY updated_at DESC"
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
            return [dict(r) for r in rows]

    # ── External-git mirror helpers ──────────────────────────

    async def list_external_blobs(self, vault_id: uuid.UUID) -> dict[str, dict]:
        """Return `{external_path: {id, external_blob}}` for every
        external_git document in a vault. Used by the reconciler to
        diff against the upstream tree without re-reading file content.
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, external_path, external_blob
                  FROM documents
                 WHERE vault_id = $1 AND source = 'external_git'
                """,
                vault_id,
            )
        return {
            r["external_path"]: {"id": r["id"], "external_blob": r["external_blob"]}
            for r in rows
        }

    async def find_by_external_path(self, vault_id: uuid.UUID, external_path: str) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM documents
                 WHERE vault_id = $1 AND source = 'external_git' AND external_path = $2
                """,
                vault_id, external_path,
            )
            return dict(row) if row else None

    async def upsert_external(
        self,
        *,
        vault_id: uuid.UUID,
        collection_id: uuid.UUID | None,
        path: str,
        external_path: str,
        external_blob: str,
        title: str,
        doc_type: str | None,
        summary: str | None,
        domain: str | None,
        tags: list[str],
        metadata: dict,
        now: datetime,
        commit_hash: str | None,
        content_hash: str,
        hash_algorithm: str,
        created_by: str | None = None,
        conn=None,
    ) -> tuple[uuid.UUID, bool]:
        """Insert or update an external_git document. Stable on
        (vault_id, external_path); content changes are detected via
        external_blob upstream so the row identity stays intact across
        re-syncs.

        Returns `(id, inserted)` where `inserted=True` means this was a
        fresh INSERT (i.e. caller should bump collections.doc_count).
        Uses the PG `xmax = 0` trick to distinguish INSERT vs UPDATE on
        a single `INSERT ... ON CONFLICT` statement.

        Accepts an optional `conn` so callers that already hold a
        connection (e.g. the reconcile loop doing upsert + chunks in
        one transaction) don't re-acquire.
        """
        sql = """
            INSERT INTO documents
                (id, vault_id, collection_id, path, title, doc_type, status,
                 summary, domain, created_by, created_at, updated_at,
                 current_commit, content_hash, hash_algorithm, content_hash_commit,
                 tags, metadata,
                 source, external_path, external_blob)
            VALUES ($1, $2, $3, $4, $5, $6, 'active',
                    $7, $8, $9, $10, $10, $11, $12, $13, $11,
                    $14, $15,
                    'external_git', $16, $17)
            ON CONFLICT (vault_id, path) DO UPDATE SET
                collection_id  = EXCLUDED.collection_id,
                title          = EXCLUDED.title,
                doc_type       = EXCLUDED.doc_type,
                summary        = EXCLUDED.summary,
                domain         = EXCLUDED.domain,
                updated_at     = EXCLUDED.updated_at,
                current_commit = EXCLUDED.current_commit,
                content_hash   = EXCLUDED.content_hash,
                hash_algorithm = EXCLUDED.hash_algorithm,
                content_hash_commit = EXCLUDED.content_hash_commit,
                tags           = EXCLUDED.tags,
                metadata       = EXCLUDED.metadata,
                external_blob  = EXCLUDED.external_blob,
                -- Re-trigger metadata_worker only when the blob actually
                -- changes; unchanged content keeps its prior LLM fill.
                llm_metadata_at = CASE
                    WHEN documents.external_blob IS DISTINCT FROM EXCLUDED.external_blob
                        THEN NULL
                    ELSE documents.llm_metadata_at
                END
            RETURNING id, (xmax = 0) AS inserted
        """
        args = (
            uuid.uuid4(), vault_id, collection_id, path, title, doc_type,
            summary, domain, created_by, now, commit_hash, content_hash,
            hash_algorithm, tags, dumps_jsonb(metadata),
            external_path, external_blob,
        )
        if conn is not None:
            row = await conn.fetchrow(sql, *args)
        else:
            async with self.pool.acquire() as acq:
                row = await acq.fetchrow(sql, *args)
        return row["id"], row["inserted"]

    async def mark_llm_metadata_filled(
        self,
        doc_id: uuid.UUID,
        summary: str | None,
        tags: list[str] | None,
        doc_type: str | None,
        domain: str | None,
        now: datetime,
        expected_blob: str | None = None,
    ) -> bool:
        """Apply LLM-generated metadata, but only into NULL/empty fields
        so that frontmatter-provided values always win.

        When `expected_blob` is passed, the UPDATE is gated on
        `external_blob = expected_blob`. The external_git reconciler can
        reindex a path between worker claim and worker write — without
        the predicate the worker would stamp stale LLM output onto a row
        whose body is already newer. Returns True iff the row matched.
        """
        async with self.pool.acquire() as conn:
            sql = """
                UPDATE documents SET
                    summary  = COALESCE(NULLIF(summary, ''), $2, summary),
                    tags     = CASE
                        WHEN tags IS NULL OR cardinality(tags) = 0 THEN COALESCE($3, tags)
                        ELSE tags
                    END,
                    doc_type = COALESCE(NULLIF(doc_type, ''), $4, doc_type),
                    domain   = COALESCE(NULLIF(domain, ''), $5, domain),
                    llm_metadata_at = $6,
                    updated_at = $6
                WHERE id = $1
            """
            args: list = [doc_id, summary, tags, doc_type, domain, now]
            if expected_blob is not None:
                sql += " AND external_blob = $7"
                args.append(expected_blob)
            status = await conn.execute(sql, *args)
        return status.endswith(" 1")


class CollectionRepository:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def get_or_create(self, vault_id: uuid.UUID, path: str, conn=None) -> uuid.UUID:
        async def _do(c):
            # ON CONFLICT handles the SELECT-then-INSERT race where two
            # concurrent PUTs both find no row and both INSERT.
            name = path.rstrip("/").split("/")[-1]
            row = await c.fetchrow(
                """
                INSERT INTO collections (id, vault_id, path, name)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (vault_id, path) DO NOTHING
                RETURNING id
                """,
                uuid.uuid4(), vault_id, path, name,
            )
            if row:
                return row["id"]
            existing = await c.fetchrow(
                "SELECT id FROM collections WHERE vault_id = $1 AND path = $2",
                vault_id, path,
            )
            if existing is None:
                raise RuntimeError(
                    f"collection {path!r} could not be created or found "
                    f"in vault {vault_id} (concurrent delete?)"
                )
            return existing["id"]
        if conn is not None:
            return await _do(conn)
        async with self.pool.acquire() as acq:
            return await _do(acq)

    async def list_by_vault(self, vault_id: uuid.UUID) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT path, name, summary, doc_count, last_updated FROM collections WHERE vault_id = $1 ORDER BY name",
                vault_id,
            )
            return [dict(r) for r in rows]

    async def increment_count(self, collection_id: uuid.UUID, now: datetime, conn=None) -> None:
        sql = "UPDATE collections SET doc_count = doc_count + 1, last_updated = $1 WHERE id = $2"
        if conn is not None:
            await conn.execute(sql, now, collection_id)
            return
        async with self.pool.acquire() as acq:
            await acq.execute(sql, now, collection_id)

    async def decrement_count(self, collection_id: uuid.UUID, now: datetime, conn=None) -> None:
        sql = "UPDATE collections SET doc_count = GREATEST(doc_count - 1, 0), last_updated = $1 WHERE id = $2"
        if conn is not None:
            await conn.execute(sql, now, collection_id)
            return
        async with self.pool.acquire() as acq:
            await acq.execute(sql, now, collection_id)

    # ── Lifecycle helpers (used by CollectionService) ────────
    #
    # All four take an optional `conn` so the caller can compose them
    # inside an outer transaction — same pattern as get_or_create /
    # increment_count above.

    async def create_empty(
        self,
        vault_id: uuid.UUID,
        path: str,
        summary: str | None = None,
        conn=None,
    ) -> tuple[uuid.UUID, bool, str, str | None, int]:
        """Idempotent insert. Returns
        `(collection_id, created, name, summary, doc_count)`.

        Both branches return the *current row state* — when the row
        already exists, `summary` and `doc_count` reflect what's in the
        DB, not what the caller passed (idempotent calls must not
        clobber stored state, and callers building a response envelope
        need the truth to surface). `created=False` lets callers
        distinguish a no-op from a fresh create (matters for git commit
        + event emission).
        """
        async def _do(c):
            cid = uuid.uuid4()
            name = path.rstrip("/").split("/")[-1]
            row = await c.fetchrow(
                """
                INSERT INTO collections (id, vault_id, path, name, summary, doc_count)
                VALUES ($1, $2, $3, $4, $5, 0)
                ON CONFLICT (vault_id, path) DO NOTHING
                RETURNING id, name, summary, doc_count
                """,
                cid, vault_id, path, name, summary,
            )
            if row is not None:
                return row["id"], True, row["name"], row["summary"], row["doc_count"]
            existing = await c.fetchrow(
                """
                SELECT id, name, summary, doc_count
                  FROM collections
                 WHERE vault_id = $1 AND path = $2
                """,
                vault_id, path,
            )
            return (
                existing["id"], False,
                existing["name"], existing["summary"], existing["doc_count"],
            )
        if conn is not None:
            return await _do(conn)
        async with self.pool.acquire() as acq:
            return await _do(acq)

    async def delete_by_id(self, collection_id: uuid.UUID, conn=None) -> None:
        sql = "DELETE FROM collections WHERE id = $1"
        if conn is not None:
            await conn.execute(sql, collection_id)
            return
        async with self.pool.acquire() as acq:
            await acq.execute(sql, collection_id)

    # ``_like_escape`` used to live here. The same triple-replace also
    # got copy-pasted into the inline prefix-filter inside
    # ``list_docs_by_depth`` (and into two other repos). Consolidated
    # at ``app.util.text.like_escape`` (imported at module top) — call
    # sites now go through that, and this alias keeps the existing
    # ``self._like_escape`` call-pattern working without churn.
    _like_escape = staticmethod(like_escape)

    async def list_docs_under(
        self,
        vault_id: uuid.UUID,
        path: str,
        conn=None,
    ) -> list[dict]:
        """Return documents whose path starts with `{path}/`. Used by
        cascade delete to find every doc beneath a collection root."""
        like = self._like_escape(path.rstrip("/")) + "/%"
        sql = (
            "SELECT id, path, collection_id, metadata "
            "FROM documents "
            "WHERE vault_id = $1 AND path LIKE $2 ESCAPE '\\'"
        )
        async def _do(c):
            rows = await c.fetch(sql, vault_id, like)
            return [dict(r) for r in rows]
        if conn is not None:
            return await _do(conn)
        async with self.pool.acquire() as acq:
            return await _do(acq)

    async def list_files_under(
        self,
        vault_id: uuid.UUID,
        path: str,
        conn=None,
    ) -> list[dict]:
        """Return vault_files whose collection path equals `path` exactly
        or starts with `{path}/`. Covers the folder itself plus every
        descendant — used by cascade delete to enqueue S3 cleanup.

        Implementation joins `collections` on the new `collection_id`
        FK (migration 020). Pre-migration callers that relied on the
        legacy `vault_files.collection` TEXT column are not supported.
        """
        bare = path.rstrip("/")
        like = self._like_escape(bare) + "/%"
        sql = (
            "SELECT vf.id, vf.vault_id, vf.collection_id, "
            "       c.path AS collection, vf.name, vf.s3_key, vf.mime_type, "
            "       vf.size_bytes, vf.description, vf.created_by, "
            "       vf.created_at, vf.updated_at "
            "  FROM vault_files vf "
            "  JOIN collections c ON c.id = vf.collection_id "
            " WHERE vf.vault_id = $1 "
            "   AND (c.path = $2 OR c.path LIKE $3 ESCAPE '\\')"
        )
        async def _do(c):
            rows = await c.fetch(sql, vault_id, bare, like)
            return [dict(r) for r in rows]
        if conn is not None:
            return await _do(conn)
        async with self.pool.acquire() as acq:
            return await _do(acq)

    async def list_tables_under(
        self,
        vault_id: uuid.UUID,
        path: str,
        conn=None,
    ) -> list[dict]:
        """Return vault_tables whose owning collection path equals `path`
        exactly or starts with `{path}/`. Covers the collection itself
        plus every descendant — used by cascade delete to tear down
        tables that live inside a collection (FK collection_id is
        ON DELETE SET NULL, so the collection delete would otherwise
        silently re-home them to vault root). Locked FOR UPDATE so a
        concurrent drop/alter on the same table serialises.
        """
        bare = path.rstrip("/")
        like = self._like_escape(bare) + "/%"
        sql = (
            "SELECT vt.id, vt.name, vt.collection_id, c.path AS collection "
            "  FROM vault_tables vt "
            "  JOIN collections c ON c.id = vt.collection_id "
            " WHERE vt.vault_id = $1 "
            "   AND (c.path = $2 OR c.path LIKE $3 ESCAPE '\\') "
            " FOR UPDATE OF vt"
        )
        async def _do(c):
            rows = await c.fetch(sql, vault_id, bare, like)
            return [dict(r) for r in rows]
        if conn is not None:
            return await _do(conn)
        async with self.pool.acquire() as acq:
            return await _do(acq)

    async def list_collections_under(
        self,
        vault_id: uuid.UUID,
        path: str,
        *,
        exclude_self: bool = False,
        conn=None,
    ) -> list[dict]:
        """Return collection rows whose `path` equals `P` exactly or
        starts with `P/`. Used by prefix-delete to discover all sub-
        collections beneath a target path (including paths where the
        target itself has no row, but descendants do — the nested-parent
        delete case).

        When `exclude_self=True`, the exact-match row at `path` is
        omitted from the result. LIKE metacharacters in the user-
        supplied path are escaped so a folder literally named `a_b`
        only matches `a_b` and `a_b/...`, not `aXb`.
        """
        bare = path.rstrip("/")
        like = self._like_escape(bare) + "/%"
        args: tuple
        if exclude_self:
            sql = (
                "SELECT id, path, name, summary, doc_count, last_updated "
                "  FROM collections "
                " WHERE vault_id = $1 "
                "   AND path LIKE $2 ESCAPE '\\'"
            )
            args = (vault_id, like)
        else:
            sql = (
                "SELECT id, path, name, summary, doc_count, last_updated "
                "  FROM collections "
                " WHERE vault_id = $1 "
                "   AND (path = $2 OR path LIKE $3 ESCAPE '\\')"
            )
            args = (vault_id, bare, like)
        async def _do(c):
            rows = await c.fetch(sql, *args)
            return [dict(r) for r in rows]
        if conn is not None:
            return await _do(conn)
        async with self.pool.acquire() as acq:
            return await _do(acq)
