"""Search service — hybrid (dense + BM25) retrieval.

Flow:
1. Metadata pre-filter in PostgreSQL (vault, collection, type, tags, ACL)
   → candidate source ids (documents + tables + files).
2. Query embedding via external API.
3. vector_store.hybrid_search (dense + sparse BM25, RRF fusion) over candidates.
4. Optional cross-encoder rerank over the prefetch pool.
5. Hydrate hits with source metadata from PostgreSQL.
"""

from __future__ import annotations

import logging
import uuid

from app.config import settings
from app.db.postgres import get_pool
from app.models.document import SearchResponse, SearchResult
from app.services import sparse_encoder
from app.services.index_service import generate_embeddings
from app.services.vector_store import get_vector_store
from app.services.rerank_service import RerankError, rerank
from app.utils import ensure_dict

logger = logging.getLogger("akb.search")


class SearchService:

    async def search(
        self,
        query: str,
        vault: str | None = None,
        collection: str | None = None,
        doc_type: str | None = None,
        tags: list[str] | None = None,
        limit: int = 10,
        user_id: str | None = None,
    ) -> SearchResponse:
        """Hybrid search across documents. See module docstring for flow."""
        pool = await get_pool()

        # Generate query embedding. When the embedding API is down we still
        # try to proceed — the hybrid path can fall back to sparse-only, and the
        # short-circuit happens later once both legs are known to be empty.
        # Short timeout: a slow/hung embedding API must not stall interactive
        # search for the full 60s indexing budget.
        try:
            embeddings = await generate_embeddings([query], timeout=5.0)
        except Exception as e:  # noqa: BLE001
            logger.warning("query embedding failed: %s", e)
            embeddings = []
        query_embedding = embeddings[0] if embeddings else None

        # Always pre-filter when user_id is provided so we never leak
        # documents from vaults the user can't read.
        has_filters = any([vault, collection, doc_type, tags, user_id])
        candidate_source_ids: list[str] | None = None

        if has_filters:
            async with pool.acquire() as conn:
                # Resolve user access once — used in all three candidate
                # queries below. Previously this repeated the lookup per
                # source type (3x round-trip) and pasted the predicate
                # three times.
                user_uuid = uuid.UUID(user_id) if user_id else None
                is_admin = False
                if user_uuid is not None:
                    is_admin = bool(await conn.fetchval(
                        "SELECT is_admin FROM users WHERE id = $1", user_uuid,
                    ))

                def _vault_acl(param_idx: int) -> tuple[str | None, list]:
                    """Returns (sql_fragment, params) for the
                    owner/grant/public_access predicate, or (None, []) if
                    no filter is needed (admin or anon)."""
                    if user_uuid is None or is_admin:
                        return None, []
                    return (
                        f"(v.id IN (SELECT vault_id FROM vault_access WHERE user_id = ${param_idx}) "
                        f"OR v.owner_id = ${param_idx} "
                        f"OR v.public_access IN ('reader', 'writer'))",
                        [user_uuid],
                    )

                conditions = []
                params: list = []
                idx = 1
                if vault:
                    conditions.append(f"v.name = ${idx}")
                    params.append(vault); idx += 1
                if collection:
                    conditions.append(f"d.path LIKE ${idx} || '%'")
                    params.append(collection); idx += 1
                if doc_type:
                    conditions.append(f"d.doc_type = ${idx}")
                    params.append(doc_type); idx += 1
                if tags:
                    conditions.append(f"d.tags && ${idx}")
                    params.append(tags); idx += 1
                acl_sql, acl_params = _vault_acl(idx)
                if acl_sql:
                    conditions.append(acl_sql)
                    params.extend(acl_params); idx += 1

                where_sql = " AND ".join(conditions) if conditions else "TRUE"
                rows = await conn.fetch(
                    f"""
                    SELECT d.id FROM documents d
                    JOIN vaults v ON d.vault_id = v.id
                    WHERE {where_sql}
                    """,
                    *params,
                )
                candidate_source_ids = [str(r["id"]) for r in rows]

                # Tables (skip when doc_type explicitly constrains to a
                # non-table source). Tags/collection apply to documents
                # only.
                if not doc_type or doc_type == "table":
                    t_params: list = []
                    t_conds: list[str] = []
                    if vault:
                        t_conds.append("v.name = $1")
                        t_params.append(vault)
                    acl_sql, acl_params = _vault_acl(len(t_params) + 1)
                    if acl_sql:
                        t_conds.append(acl_sql)
                        t_params.extend(acl_params)
                    q = "SELECT t.id FROM vault_tables t JOIN vaults v ON t.vault_id = v.id"
                    if t_conds:
                        q += " WHERE " + " AND ".join(t_conds)
                    trows = await conn.fetch(q, *t_params)
                    candidate_source_ids.extend(str(r["id"]) for r in trows)

                if not doc_type or doc_type == "file":
                    f_params: list = []
                    f_conds: list[str] = []
                    if vault:
                        f_conds.append("v.name = $1")
                        f_params.append(vault)
                    if collection:
                        f_conds.append(f"f.collection = ${len(f_params) + 1}")
                        f_params.append(collection)
                    acl_sql, acl_params = _vault_acl(len(f_params) + 1)
                    if acl_sql:
                        f_conds.append(acl_sql)
                        f_params.extend(acl_params)
                    q = "SELECT f.id FROM vault_files f JOIN vaults v ON f.vault_id = v.id"
                    if f_conds:
                        q += " WHERE " + " AND ".join(f_conds)
                    frows = await conn.fetch(q, *f_params)
                    candidate_source_ids.extend(str(r["id"]) for r in frows)

                if not candidate_source_ids:
                    return SearchResponse(query=query, total=0, results=[])

        target_unique = (
            max(settings.rerank_prefetch, limit) if settings.rerank_enabled else limit
        )

        # Hybrid (dense + BM25 sparse) via the configured driver. Returns [] on any vector-store
        # failure — PG is the source of truth, the index is rebuildable.
        hits = await self._run_vector_search(
            query_text=query,
            query_embedding=query_embedding,
            candidate_source_ids=candidate_source_ids,
            limit=target_unique * 3,
        )

        if not hits:
            return SearchResponse(query=query, total=0, results=[])

        # Dedup at the source level — one hit per (source_type, source_id).
        # Previously dedup was by document_id only; generalizing keeps
        # tables and files first-class in the dedup pool.
        seen: set[tuple[str, str]] = set()
        unique_hits = []
        for hit in hits:
            key = (hit.source_type, hit.source_id)
            if key in seen:
                continue
            seen.add(key)
            unique_hits.append(hit)
            if len(unique_hits) >= target_unique:
                break

        if settings.rerank_enabled and len(unique_hits) > 1:
            unique_hits = await self._apply_rerank(query, unique_hits)

        unique_hits = unique_hits[:limit]

        # Post-search metadata join — one fetch per source_type, merged back
        # in the driver-returned order. Keeps document results fully
        # backward-compatible (doc_id == source_id) while adding table/file.
        results = await self._hydrate_hits(unique_hits)
        return SearchResponse(query=query, total=len(results), results=results)

    async def _hydrate_hits(self, hits: list) -> list[SearchResult]:
        from app.services.index_service import SOURCE_TYPES
        by_type: dict[str, list[str]] = {t: [] for t in SOURCE_TYPES}
        unknown_types: set[str] = set()
        for h in hits:
            if h.source_type not in by_type:
                unknown_types.add(h.source_type)
                continue
            if h.source_id:
                by_type[h.source_type].append(h.source_id)
        if unknown_types:
            logger.warning("hydrate: unknown source_type(s) skipped: %s", unknown_types)

        pool = await get_pool()
        meta: dict[tuple[str, str], dict] = {}
        async with pool.acquire() as conn:
            if by_type["document"]:
                rows = await conn.fetch(
                    """
                    SELECT d.id, v.name AS vault_name, d.path, d.title,
                           d.doc_type, d.summary, d.tags
                      FROM documents d
                      JOIN vaults v ON d.vault_id = v.id
                     WHERE d.id = ANY($1)
                    """,
                    [uuid.UUID(x) for x in by_type["document"]],
                )
                for r in rows:
                    meta[("document", str(r["id"]))] = {
                        "vault": r["vault_name"], "path": r["path"],
                        "title": r["title"], "doc_type": r["doc_type"],
                        "summary": r["summary"],
                        "tags": list(r["tags"]) if r["tags"] else [],
                    }
            if by_type["table"]:
                rows = await conn.fetch(
                    """
                    SELECT t.id, v.name AS vault_name, t.name, t.description
                      FROM vault_tables t
                      JOIN vaults v ON t.vault_id = v.id
                     WHERE t.id = ANY($1)
                    """,
                    [uuid.UUID(x) for x in by_type["table"]],
                )
                for r in rows:
                    meta[("table", str(r["id"]))] = {
                        "vault": r["vault_name"],
                        "path": f"_tables/{r['name']}",
                        "title": r["name"],
                        "doc_type": "table",
                        "summary": r["description"],
                        "tags": [],
                    }
            if by_type["file"]:
                rows = await conn.fetch(
                    """
                    SELECT f.id, v.name AS vault_name, c.path AS collection,
                           f.name, f.description, f.mime_type
                      FROM vault_files f
                      JOIN vaults v ON f.vault_id = v.id
                      LEFT JOIN collections c ON c.id = f.collection_id
                     WHERE f.id = ANY($1)
                    """,
                    [uuid.UUID(x) for x in by_type["file"]],
                )
                for r in rows:
                    path = f"{r['collection']}/{r['name']}" if r["collection"] else r["name"]
                    meta[("file", str(r["id"]))] = {
                        "vault": r["vault_name"],
                        "path": path,
                        "title": r["name"],
                        "doc_type": "file",
                        "summary": r["description"] or r["mime_type"],
                        "tags": [],
                    }

        from app.services.uri_service import doc_uri, table_uri, file_uri

        results: list[SearchResult] = []
        for h in hits:
            key = (h.source_type, h.source_id)
            m = meta.get(key)
            if not m:
                continue
            # Build the canonical URI per resource type. `source_id` is
            # the internal handle we use to fetch metadata; it never
            # leaves this function — the client only sees `uri`.
            if h.source_type == "document":
                uri = doc_uri(m["vault"], m["path"])
            elif h.source_type == "table":
                uri = table_uri(m["vault"], m["title"])
            elif h.source_type == "file":
                uri = file_uri(m["vault"], h.source_id)
            else:
                continue
            results.append(
                SearchResult(
                    source_type=h.source_type,
                    uri=uri,
                    vault=m["vault"], path=m["path"], title=m["title"],
                    doc_type=m["doc_type"], summary=m["summary"],
                    tags=m["tags"], score=h.score,
                    matched_section=h.content[:500] if h.content else None,
                )
            )
        return results

    async def _apply_rerank(self, query: str, hits: list) -> list:
        """Rescore `hits` with the configured reranker. On any rerank
        failure log a warning and fall back to the input (RRF) order —
        search must never go dark on a reranker outage."""
        docs = [(h.content or "")[:512] for h in hits]
        try:
            ranked = await rerank(query, docs, top_n=len(docs))
        except RerankError as e:
            logger.warning("rerank failed (%s); keeping RRF order", e)
            return hits

        reordered = []
        for idx, score in ranked:
            if 0 <= idx < len(hits):
                hit = hits[idx]
                hit.score = score
                reordered.append(hit)
        return reordered

    async def _run_vector_search(
        self,
        *,
        query_text: str,
        query_embedding: list[float] | None,
        candidate_source_ids: list[str] | None,
        limit: int,
    ):
        """Hybrid search over the vector store. Returns [] on any failure —
        the store is a derived view; outages surface as 'no results' while
        PG truth is untouched."""
        try:
            sparse_idx, sparse_vals = await sparse_encoder.encode_query(query_text)
        except Exception as e:  # noqa: BLE001
            logger.warning("sparse encode_query failed (%s); dense-only path", e)
            sparse_idx, sparse_vals = [], []

        # max(limit*3, 50): same heuristic the legacy native-fusion path used.
        # Driver-agnostic now, but the value transfers cleanly — RRF
        # fusion benefits from generous prefetch in either driver.
        prefetch_per_leg = max(limit * 3, 50)

        try:
            return await get_vector_store().hybrid_search(
                query_text=query_text,
                query_dense=query_embedding,
                query_sparse_indices=sparse_idx,
                query_sparse_values=sparse_vals,
                source_ids=candidate_source_ids,
                limit=limit,
                prefetch_per_leg=prefetch_per_leg,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("vector hybrid_search failed (%s); returning empty", e)
            return []

    async def grep(
        self,
        pattern: str,
        vault: str | None = None,
        collection: str | None = None,
        regex: bool = False,
        case_sensitive: bool = False,
        replace: str | None = None,
        doc_service=None,
        agent_id: str | None = None,
        user_id: str | None = None,
        limit: int = 20,
    ) -> dict:
        """Exact text / regex search (and optional replace) across document content.

        Searches chunk content in PostgreSQL. Returns matching documents
        with matched lines. If `replace` is provided, performs find-and-replace
        on matching documents' bodies and commits via the standard pipeline.
        """
        import re as _re

        # Validate regex pattern early to give a clear error
        if regex:
            try:
                _re.compile(pattern)
            except _re.error as e:
                return {
                    "error": f"Invalid regex pattern: {e}",
                    "pattern": pattern,
                    "total_docs": 0,
                    "total_matches": 0,
                    "results": [],
                }

        pool = await get_pool()
        async with pool.acquire() as conn:
            conditions = []
            params: list = []
            idx = 1

            # Text match condition
            if regex:
                op = "~" if case_sensitive else "~*"
                conditions.append(f"c.content {op} ${idx}")
                params.append(pattern)
            else:
                if case_sensitive:
                    conditions.append(f"c.content LIKE '%' || ${idx} || '%'")
                else:
                    conditions.append(f"c.content ILIKE '%' || ${idx} || '%'")
                params.append(pattern)
            idx += 1

            if vault:
                conditions.append(f"v.name = ${idx}")
                params.append(vault)
                idx += 1
            elif user_id:
                # No vault specified: restrict to vaults the user can access
                # (vault_access for explicit grants OR owner OR public_access)
                conditions.append(
                    f"(v.id IN (SELECT vault_id FROM vault_access WHERE user_id = ${idx}) "
                    f"OR v.owner_id = ${idx} "
                    f"OR v.public_access IN ('reader', 'writer'))"
                )
                params.append(user_id)
                idx += 1

            if collection:
                conditions.append(f"d.path LIKE ${idx} || '%'")
                params.append(collection)
                idx += 1

            where_sql = " AND ".join(conditions)
            rows = await conn.fetch(
                f"""
                SELECT d.id::text as doc_id, v.name as vault, d.path, d.title,
                       d.metadata,
                       c.section_path, c.content, c.chunk_index
                FROM chunks c
                JOIN documents d ON c.source_id = d.id AND c.source_type = 'document'
                JOIN vaults v ON d.vault_id = v.id
                WHERE {where_sql}
                ORDER BY v.name, d.path, c.chunk_index
                LIMIT {int(limit) * 5}
                """,
                *params,
            )

        # Group by document and extract matching lines. `_doc_pk` is
        # the internal PG UUID — kept only as a dedup key while building
        # results; stripped before the response leaves this function.
        from app.services.uri_service import doc_uri as _doc_uri
        docs: dict[str, dict] = {}
        for r in rows:
            doc_key = r["doc_id"]
            if doc_key not in docs:
                docs[doc_key] = {
                    "_doc_pk": r["doc_id"],
                    "uri": _doc_uri(r["vault"], r["path"]),
                    "vault": r["vault"],
                    "path": r["path"],
                    "title": r["title"],
                    "metadata": r["metadata"],
                    "matches": [],
                }

            # Extract individual matching lines from chunk
            chunk_lines = r["content"].split("\n")
            for i, line in enumerate(chunk_lines):
                if regex:
                    matched = bool(_re.search(pattern, line, 0 if case_sensitive else _re.IGNORECASE))
                else:
                    if case_sensitive:
                        matched = pattern in line
                    else:
                        matched = pattern.lower() in line.lower()
                if matched:
                    docs[doc_key]["matches"].append({
                        "section": r["section_path"],
                        "text": line.strip(),
                    })

        # Apply document limit
        result_docs = list(docs.values())[:limit]
        total_matches = sum(len(d["matches"]) for d in result_docs)

        # Replace mode: apply find-and-replace on each matching document.
        # Service-layer `doc_service.update` still wants the doc path
        # (which find_by_ref accepts) — we pass `path` rather than re-
        # parsing the URI we just built.
        replaced: list[dict] = []
        if replace is not None and result_docs and doc_service:
            re_flags = 0 if case_sensitive else _re.IGNORECASE

            for doc_info in result_docs:
                doc_vault = doc_info["vault"]
                doc_path = doc_info["path"]
                doc_uri_str = doc_info["uri"]

                try:
                    doc = await doc_service.get(doc_vault, doc_path)
                except Exception:
                    replaced.append({"uri": doc_uri_str, "error": "not found"})
                    continue

                body = doc.content or ""

                if regex:
                    new_body = _re.sub(pattern, replace, body, flags=re_flags)
                else:
                    if case_sensitive:
                        new_body = body.replace(pattern, replace)
                    else:
                        # Case-insensitive non-regex replace
                        new_body = _re.sub(_re.escape(pattern), replace, body, flags=_re.IGNORECASE)

                if new_body == body:
                    continue  # no actual change

                from app.models.document import DocumentUpdateRequest
                req = DocumentUpdateRequest(
                    content=new_body,
                    message=f"grep replace: '{pattern}' → '{replace}'",
                )
                result = await doc_service.update(doc_vault, doc_path, req, agent_id=agent_id)
                replaced.append({
                    "uri": doc_uri_str,
                    "path": doc_path,
                    "title": doc_info["title"],
                    "commit": result.commit_hash,
                })

        # Build response — strip internal handles (`_doc_pk`, `metadata`)
        # so the client only sees `uri`.
        clean_results = [
            {k: v for k, v in d.items() if k not in ("_doc_pk", "metadata")}
            for d in result_docs
        ]

        resp = {
            "pattern": pattern,
            "regex": regex,
            "total_docs": len(clean_results),
            "total_matches": total_matches,
            "results": clean_results,
        }

        if total_matches == 0 and not regex:
            metachars = set("|.*+?()[]{}^$\\")
            found_meta = sorted({c for c in pattern if c in metachars})
            if found_meta:
                resp["hint"] = (
                    f"Pattern contains regex metacharacter(s) {found_meta} but regex=false, "
                    f"so they were matched literally. If you intended an OR/wildcard match, "
                    f"retry with regex=true."
                )

        if replace is not None:
            resp["replace"] = replace
            resp["replaced_docs"] = len(replaced)
            resp["replacements"] = replaced
        return resp

    async def drill_down(self, vault: str, doc_id: str, section: str | None = None) -> list[dict]:
        """Get L3 section-level content for a document."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            # Match by UUID, metadata.id (d- prefix), or path substring
            doc_match = "(d.id::text = $2 OR d.metadata->>'id' = $2 OR d.path LIKE '%' || $2 || '%')"
            if section:
                rows = await conn.fetch(
                    f"""
                    SELECT c.section_path, c.content, c.chunk_index
                    FROM chunks c
                    JOIN documents d ON c.source_id = d.id AND c.source_type = 'document'
                    JOIN vaults v ON d.vault_id = v.id
                    WHERE v.name = $1
                      AND {doc_match}
                      AND c.section_path ILIKE '%' || $3 || '%'
                    ORDER BY c.chunk_index
                    """,
                    vault, doc_id, section,
                )
            else:
                rows = await conn.fetch(
                    f"""
                    SELECT c.section_path, c.content, c.chunk_index
                    FROM chunks c
                    JOIN documents d ON c.source_id = d.id AND c.source_type = 'document'
                    JOIN vaults v ON d.vault_id = v.id
                    WHERE v.name = $1 AND {doc_match}
                    ORDER BY c.chunk_index
                    """,
                    vault, doc_id,
                )

            return [
                {
                    "section_path": r["section_path"],
                    "content": r["content"],
                    "chunk_index": r["chunk_index"],
                }
                for r in rows
            ]
