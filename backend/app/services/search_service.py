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

import asyncio
import logging
import re
import uuid
from typing import Literal

from app.config import settings
from app.db.postgres import get_pool
from app.exceptions import ValidationError
from app.services.search_filters import ArchiveScope, collection_predicate, escape_like, metadata_matches, resolve_archive_scope
from app.models.document import SearchResponse, SearchResult
from app.repositories.vault_files_repo import confirmed_file_predicate
from app.services import sparse_encoder
from app.services.index_service import (
    OVERLAP,
    SOURCE_NATIVE_FILE,
    generate_embeddings,
)
from app.services.grep_replace import (
    DEFAULT_MAX_REPLACEMENTS,
    apply_grep_replacement,
    replacement_budget_error,
    replacement_failure_error,
    validate_max_replacements,
)
from app.services.vector_store import VectorHit, get_vector_store
from app.services.vector_store.base import VectorStoreUnavailable, supports_vault_filter
from app.services.rerank_service import RerankError, rerank
from app.services.uri_service import parse_uri

logger = logging.getLogger("akb.search")

LEGACY_DOCUMENT_SOURCE = "document"
NATIVE_DOCUMENT_SOURCE = "native_document"
NATIVE_MEASUREMENT_DATABASE = "akb_revision_m1_measurement"
NATIVE_SEARCH_MAX_CANDIDATE_RESOURCES = 10_000
NATIVE_SEARCH_MAX_BODY_BYTES = 128 * 1024 * 1024
# Legacy alias for the caller-supplied `source_uris` scope cap. The live limit
# is `settings.search_max_source_uris` (configurable, documented there); this
# constant stays so older imports keep resolving, but nothing reads it anymore.
NATIVE_SEARCH_MAX_SOURCE_URIS = 200
# Frontmatter slice for candidate filtering (workbench #1069, part 2): the
# filter decision needs only the leading frontmatter envelope (`type`/`tags`/
# `status`), never the body. Reading the first 8KiB bounds per-resource memory
# regardless of body size; a resource whose envelope does not close inside the
# slice is classified "unparseable" (counted, never silently dropped).
NATIVE_CANDIDATE_FRONTMATTER_SLICE_BYTES = 8 * 1024
# Candidate pagination: filter loop pages through scope rows keyset-ordered by
# resource_id, so peak memory is page-sized, not scope-sized. Page of 2,000 ×
# 8KiB slices ≈ 16MiB worst case per page, GC'd before the next page.
NATIVE_CANDIDATE_PAGE_SIZE = 2_000


def active_document_source_type(
    *,
    backend: str,
    measurement_only: bool,
    database: str,
) -> str:
    """Select one Document authority; fail closed on a partial native guard."""
    if backend in {"bare_git", "bare_git_current"}:
        return LEGACY_DOCUMENT_SOURCE
    if backend == "postgres_native":
        if measurement_only:
            raise RuntimeError("postgres_native rejects the measurement-only guard")
        if database == NATIVE_MEASUREMENT_DATABASE:
            raise RuntimeError("postgres_native rejects the reserved measurement database")
        return NATIVE_DOCUMENT_SOURCE
    if backend != "native_ledger_m1":
        raise RuntimeError(f"unsupported document revision backend: {backend}")
    if not measurement_only:
        raise RuntimeError("native ledger requires measurement-only guard")
    if database != NATIVE_MEASUREMENT_DATABASE:
        raise RuntimeError("native ledger requires dedicated measurement database")
    return NATIVE_DOCUMENT_SOURCE


def _configured_document_source_type() -> str:
    return active_document_source_type(
        backend=settings.document_revision_backend,
        measurement_only=settings.native_revision_m1_measurement_only,
        database=settings.db_name,
    )

# Strips the indexing-time enrichment block emitted by
# `build_doc_metadata_header` / `build_file_metadata_header`. It rides along
# with every body chunk so the BM25 and dense legs see resource-level signals
# during retrieval — but it is noise once the chunk content is shown to a
# human or an agent.
#
# The two patterns below TRANSCRIBE those two builders, key by key and in
# order, rather than accepting any run of key-shaped lines. The looser form
# is what lets a user paragraph be eaten: a document whose body opens
# `TITLE: …` and reaches something key-shaped before its first blank line
# would have that prose removed. Pinning the structure costs nothing —
# these are the only two shapes the indexer can write — and both require the
# `PATH:` line that every such header carries.
#
# `SUMMARY:` is the one value interpolated verbatim (`f"SUMMARY: {summary}"`),
# so it is the only line whose value can carry newlines of its own. Its group
# is therefore lazy and DOTALL: it runs to the first point where the rest of
# the header matches, which is the next `TAGS:` or `PATH:` line, and a blank
# line inside the summary does not end it.
#
# Table/file *catalogue* chunks (`build_table_chunk` / `build_file_chunk`) are
# pure metadata with no `\n\n` body separator and still do not match — there
# would be nothing left of them.
#
# Keep in step with index_service: a new key in either builder needs a new
# line here, or it starts leaking into drill_down / search / grep output.
_DOC_METADATA_HEADER = (
    r"TITLE:[^\n]*\n"
    r"(?:SUMMARY:.*?\n)?"
    r"(?:TAGS:[^\n]*\n)?"
    r"PATH:[^\n]*\n"
    r"(?:TYPE:[^\n]*\n)?"
)
_FILE_METADATA_HEADER = (
    r"TITLE:[^\n]*\n"
    r"TYPE:[^\n]*\n"
    r"VAULT:[^\n]*\n"
    r"PATH:[^\n]*\n"
    r"URI:[^\n]*\n"
    r"(?:SIZE:[^\n]*\n)?"
)
_CHUNK_HEADER_RE = re.compile(
    rf"\A(?:{_DOC_METADATA_HEADER}|{_FILE_METADATA_HEADER})\n",
    re.DOTALL,
)


def strip_chunk_metadata_header(text: str | None) -> str | None:
    """Strip the indexing-time TITLE/SUMMARY/TAGS/PATH/TYPE/... block
    from a chunk's stored `content` before returning it to clients.
    Leaves the body untouched if no such block is present (e.g. older
    chunks indexed before the enrichment was added, or table/file
    chunks that are pure-metadata with no body)."""
    if not text:
        return text
    return _CHUNK_HEADER_RE.sub("", text, count=1)


def strip_chunk_context_line(text: str | None, section_path: str | None) -> str | None:
    """Strip the `[# A > ## B]` heading-context line the indexer writes as
    the first line of a section's first chunk.

    `chunk_markdown` prepends `f"[{section_path}]\n"` to every section so
    the retrieval legs see the heading path inside the embedded text. On
    the way out it is pure duplication: `drill_down` already returns the
    same value in the `section_path` field of the very same row, so the
    line costs the caller tokens and tells it nothing new.

    Removed only when the first line is *exactly* `[<section_path>]` for
    this chunk's own `section_path` — a body that legitimately opens with
    a bracketed line (a markdown link label, a citation key) never
    matches, and the `section_path` field itself is untouched.
    """
    if not text or not section_path:
        return text
    prefix = f"[{section_path}]"
    if not text.startswith(prefix):
        return text
    rest = text[len(prefix):]
    if rest.startswith("\r\n"):
        return rest[2:]
    if rest.startswith("\n"):
        return rest[1:]
    if rest == "":
        return rest
    # `[section]` ran into other text on the same line — not the context
    # line the indexer wrote.
    return text


def strip_chunk_overlap_prefix(previous: str | None, current: str | None) -> str | None:
    """Remove the leading run of `current` that is an exact duplicate of the
    tail of `previous`.

    `_split_large_chunk` carries `OVERLAP` characters of each chunk into the
    head of the next one so a sentence cut by the size cap is still embedded
    intact on both sides. Reading a long section back therefore pays for that
    window once per chunk boundary.

    Only an exact character-for-character match is removed, and never more
    than `OVERLAP` characters, so `previous + returned` reproduces
    `previous + current` byte for byte — nothing a caller reading the whole
    section can no longer see. When no prefix of `current` equals a suffix of
    `previous`, `current` is returned untouched.
    """
    if not previous or not current:
        return current
    limit = min(OVERLAP, len(previous), len(current))
    for size in range(limit, 0, -1):
        if current[:size] == previous[-size:]:
            return current[size:]
    return current


def canonical_chunk_id(value) -> str | None:
    """A chunk id in one canonical spelling, or None if it is not a uuid.

    Drivers return the same id in different shapes — lower-case, upper-case,
    brace- or urn-wrapped — because each store round-trips it through its own
    type. Matching a hit against a `chunks` row on the raw string therefore
    misses for anything but the spelling PostgreSQL happens to emit. Both
    sides of that lookup go through here instead.
    """
    if value is None:
        return None
    try:
        return str(uuid.UUID(str(value)))
    except (AttributeError, TypeError, ValueError):
        return None


def _chunk_index_of(hit, chunk_indexes: dict[str, int]) -> int | None:
    """The ordinal of the chunk a hit matched, or None when it is unknown —
    an id the driver did not spell as a uuid, or a chunk row that is gone."""
    canonical = canonical_chunk_id(hit.chunk_id)
    if canonical is None:
        return None
    return chunk_indexes.get(canonical)


def _row_value(row, key, default=None):
    """`row[key]` for asyncpg Records and plain dicts alike, tolerating a
    projection that did not select `key`."""
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def clean_section_rows(rows) -> list[dict]:
    """Build the `drill_down` section payload from stored chunk rows.

    Every transform here removes bytes the caller cannot use: index-side
    metadata (`strip_chunk_metadata_header`), the heading-context line that
    duplicates the row's own `section_path` (`strip_chunk_context_line`), and
    the indexing overlap window a continuation chunk repeats from its
    predecessor (`strip_chunk_overlap_prefix`). Keys are never removed —
    `section_path`, `content` and `chunk_index` are returned for every
    surviving row.

    The overlap strip is deliberately narrow. It fires only between rows the
    writer could actually have overlapped: same document, consecutive
    `chunk_index`, same `section_path`, and a `content` that carried neither a
    metadata header nor a context line (both mark the *first* chunk of a
    section, which `_split_large_chunk` never prefixes with an overlap). That
    keeps it from nibbling a character off the start of a new section just
    because the previous section happened to end with the same one.

    A chunk is also emitted once. `chunks` carries no uniqueness constraint on
    `(source_id, chunk_index)`, so a re-index that inserted before its delete
    landed leaves the same body sitting at the same position more than once
    and every `drill_down` — the `pattern` filter especially, which matches on
    body text — hands the agent the same paragraph several times over. Rows
    are collapsed on `(document, chunk_index, section_path, stored content)`:
    an identical row is a duplicate and goes, while two rows that differ in
    any part of that identity are both kept. Dropping one of *those* would
    pick a winner the query has no tiebreaker for, so the response would stop
    being deterministic in exactly the case where the difference matters.

    A position that does carry two different bodies also suspends the overlap
    strip across it. Two generations of the same chunk mean the neighbouring
    row's text may belong to the other generation, and an exact match against
    the wrong generation is still the wrong cut. Where the index is in that
    state the bodies are returned whole.

    `rows` must be ordered by `chunk_index`, as both SQL paths in
    `drill_down` are.
    """
    rows = list(rows)
    # Positions this call sees more than one distinct body for. Computed up
    # front because the decision for chunk n depends on a row that has not
    # been reached yet.
    bodies_at: dict[tuple, set] = {}
    for r in rows:
        bodies_at.setdefault(
            (_row_value(r, "doc_id"), r["chunk_index"]), set()
        ).add(r["content"])
    contested = {key for key, bodies in bodies_at.items() if len(bodies) > 1}

    sections: list[dict] = []
    seen: set[tuple] = set()
    # doc id -> (chunk_index, cleaned content, section_path) of the row this
    # document last contributed. Keyed by document because the same call can
    # (in principle) surface chunks from more than one row of `documents`.
    previous: dict[object, tuple[int, str, object]] = {}
    for r in rows:
        section_path = r["section_path"]
        chunk_index = r["chunk_index"]
        doc_key = _row_value(r, "doc_id")
        stored = r["content"]

        identity = (doc_key, chunk_index, section_path, stored)
        if identity in seen:
            continue
        seen.add(identity)

        content = strip_chunk_metadata_header(stored)
        content = strip_chunk_context_line(content, section_path)
        section_first_chunk = content != stored

        prior = previous.get(doc_key)
        if (
            prior is not None
            and not section_first_chunk
            and isinstance(chunk_index, int)
            and prior[0] + 1 == chunk_index
            and prior[2] == section_path
            and (doc_key, prior[0]) not in contested
            and (doc_key, chunk_index) not in contested
        ):
            content = strip_chunk_overlap_prefix(prior[1], content)

        if isinstance(chunk_index, int) and content is not None:
            previous[doc_key] = (chunk_index, content, section_path)

        sections.append({
            "section_path": section_path,
            "content": content,
            "chunk_index": chunk_index,
        })
    return sections


def fuse_original_and_reranked_hits(
    hits: list[VectorHit],
    ranked: list[tuple[int, float]],
    fusion_k: int,
) -> list[VectorHit]:
    """Fuse first-stage and cross-encoder ranks with RRF.

    The reranker is strongest at judging close semantic matches, but on
    noisy long-context corpora it can also overrule a high-confidence
    lexical/vector hit. Fusing ranks keeps rerank ON while preserving a
    vote from the first-stage retriever. Each hit's `score` is mutated
    in-place to the fused score; hits are returned in fused order.
    """
    if not hits:
        return []

    fused_scores: dict[int, float] = {
        i: 1.0 / (fusion_k + i + 1) for i in range(len(hits))
    }
    seen: set[int] = set()
    for rank, (idx, _score) in enumerate(ranked, start=1):
        if idx < 0 or idx >= len(hits) or idx in seen:
            continue
        seen.add(idx)
        fused_scores[idx] += 1.0 / (fusion_k + rank)

    ordered = sorted(fused_scores, key=lambda idx: (-fused_scores[idx], idx))
    for idx in ordered:
        hits[idx].score = fused_scores[idx]
    return [hits[idx] for idx in ordered]


def vault_path_eligible(
    *,
    collection: str | None,
    doc_type: str | None,
    tags: list[str] | None,
    source_uris: list[str] | None,
) -> bool:
    """Whether a search should use the VAULT-granularity ACL path (issue #189
    Phase 2) instead of enumerating source ids. Requires: the flag on, a driver
    whose `vault_filter_supported` is True (it stores vault_id and filters on it),
    and NO doc-level narrowing filter (those still need per-resource source_ids).
    When False, the existing source_ids path runs unchanged.

    The vector-store vault filter now ALSO constrains source_type (workbench
    #1069: the caller passes `source_types` alongside `vault_ids`), so the
    native arm is eligible too — stale legacy Document points are excluded
    driver-side before the top-K is cut and can no longer suppress valid
    native hits. `_hydrate_hits` keeps its arm-mismatch skip as defense in
    depth."""
    return (
        settings.vault_filter_enabled
        and supports_vault_filter(get_vector_store())
        and not (collection or doc_type or tags or source_uris)
    )


def clamp_search_limit(limit: int) -> int:
    """Clamp a user-supplied search/grep ``limit`` to ``[1, search_limit_max]``.

    The MCP tool schema advertises ``maximum: 50`` but that is client-side only;
    a direct REST call or a non-validating client can pass any value, which
    propagates into the vector-store prefetch (issue #189). Applied at the
    service entry so every caller (MCP, REST, internal) is bounded uniformly.
    """
    return max(1, min(limit, settings.search_limit_max))


def resolve_first_stage_unique_limit(
    *,
    limit: int,
    rerank_enabled: bool,
    rerank_prefetch: int,
    search_prefetch: int,
) -> int:
    """How many deduped sources to keep before final response truncation.

    Rerank already needs a larger candidate pool. Rerank-off search benefits
    from the same headroom because chunk-level dense/BM25 hits can contain
    multiple chunks from the same source before source-level dedup.
    """
    configured = max(search_prefetch, 0)
    if rerank_enabled:
        configured = max(configured, rerank_prefetch)
    return max(configured, limit)


def _normalize_vault_scope(vault: str | list[str] | None) -> list[str] | None:
    """Canonicalize the `vault` arg to a list of names, or None for "no scope".

    `vault` accepts a single name (MCP / legacy callers) or a list (the REST
    multi-vault scope picker). Blank/empty entries are dropped, and an empty
    result collapses to None ("search every vault the user can read") — so a
    stray `?vault=&vault=` can't be read as a real (empty-named) vault.
    """
    if isinstance(vault, str):
        vault = [vault]
    return [v for v in (vault or []) if v] or None


def _verified_native_metadata(row) -> dict:
    """Verify, decode, and parse one native body on a worker thread."""
    from app.services.document_service import _parse_markdown
    from app.services.native_payload_verification import verify_native_head_body

    canonical = verify_native_head_body(row)
    metadata, _ = _parse_markdown(canonical.decode("utf-8", errors="strict"))
    return metadata


# Matches a complete leading frontmatter envelope: an opening `---` line, then
# a closing `---` line. `re.DOTALL` so the envelope may span lines; `\Z`-safe
# via `$` with MULTILINE. Only the envelope presence is tested here — full
# YAML parsing still goes through `_parse_markdown` on the slice.
_FRONTMATTER_ENVELOPE_RE = re.compile(r"\A---[ \t]*\r?\n.*?\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)


def _slice_has_complete_frontmatter(slice_text: str) -> bool:
    """Whether a body slice contains a complete leading frontmatter envelope.

    A body without a leading `---` line has no envelope to complete (plain
    Markdown: metadata defaults apply). Only a body that OPENS an envelope
    but never closes it inside the slice is "incomplete" — its filter fields
    may lie beyond the slice, so the caller must count it as unparseable
    rather than filter it on defaults.
    """
    if not slice_text.startswith("---"):
        return True
    return _FRONTMATTER_ENVELOPE_RE.match(slice_text) is not None


def _verify_native_body(row) -> None:
    """Verify one native body without interpreting File bytes as Markdown."""
    from app.services.native_payload_verification import verify_native_head_body

    verify_native_head_body(row)


def _filtered_native_metadata(row) -> dict | None:
    """Parse filter metadata from a frontmatter slice on a worker thread.

    Returns the metadata dict, or None when the slice holds an INCOMPLETE
    envelope (opens `---` but never closes inside the slice): the filter
    fields may lie beyond the slice, so the caller must exclude + count the
    resource rather than filter it on defaults. A body with no leading `---`
    parses normally (plain Markdown: defaults apply).

    The slice is byte-cut (`substring(bytes ...)`), so it can end mid-codepoint
    on multibyte text. The trailing incomplete sequence (at most 3 bytes for
    UTF-8) is trimmed before decoding — only a cut inside the first 8KiB+1
    bytes triggers this, and dropping ≤3 tail bytes cannot hide a complete
    envelope close. A body that is genuinely non-UTF-8 still returns None.
    """
    from app.services.document_service import _parse_markdown

    raw = bytes(row["body_slice"])
    text = _decode_slice_prefix(raw)
    if text is None:
        return None
    if not _slice_has_complete_frontmatter(text):
        return None
    metadata, _ = _parse_markdown(text)
    return metadata


def _decode_slice_prefix(raw: bytes) -> str | None:
    """Decode a byte-cut slice, trimming a trailing partial codepoint.

    Tries strict decode first (the common case: cut landed on a character
    boundary). On failure, drops up to 3 trailing bytes (the max length of an
    incomplete UTF-8 sequence) and retries — progressively, so a body ending
    in genuinely invalid bytes still returns None instead of silently
    decoding past the corruption.
    """
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        pass
    for cut in (1, 2, 3):
        try:
            return raw[:-cut].decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            continue
    return None


class SearchService:

    async def _native_document_candidates(
        self,
        conn,
        *,
        user_uuid: uuid.UUID | None,
        is_admin: bool,
        vaults: list[str] | None,
        collection: str | None,
        doc_type: str | None,
        tags: list[str] | None,
        include_archived: bool,
        archive_scope: ArchiveScope | None = None,
        source_uris: list[str] | None,
        doc_types: list[str] | None = None,
    ) -> tuple[list[str], dict[str, int]]:
        conditions = ["r.surface = 'document'", "r.lifecycle = 'live'"]
        params: list = []
        if vaults:
            params.append(vaults)
            conditions.append(f"v.name = ANY(${len(params)})")
        if collection:
            prefix = collection.strip("/")
            escaped_prefix = (
                prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            params.extend([prefix, f"{escaped_prefix}/%"])
            conditions.append(
                f"(r.current_path = ${len(params) - 1} OR "
                f"r.current_path LIKE ${len(params)} ESCAPE '\\')"
            )
        if user_uuid is not None and not is_admin:
            params.append(user_uuid)
            index = len(params)
            conditions.append(
                f"(v.id IN (SELECT vault_id FROM vault_access WHERE user_id = ${index}) "
                f"OR v.owner_id = ${index} OR v.public_access IN ('reader', 'writer'))"
            )
        if source_uris:
            max_uris = settings.search_max_source_uris
            if len(source_uris) > max_uris:
                raise ValidationError(
                    f"native search accepts at most {max_uris} source URIs "
                    f"(got {len(source_uris)}); split the request or use a "
                    "vault scope instead"
                )
            # Pairwise scope match via unnested (vault, identifier) rows: the SQL
            # text stays constant-size no matter how many URIs arrive (only the
            # bind arrays grow). Each pair matches exactly the way the old
            # per-URI OR expansion did — vault AND (path OR id) PER PAIR — so
            # no cross-pairing: vault A can never match vault B's identifier.
            # (A naive `v.name = ANY($1) AND ident = ANY($2)` WOULD cross-match
            # and pollute the scope across vaults.) Non-doc URIs are skipped,
            # so every identifier here is a doc path-or-id by construction.
            uri_pairs: list[tuple[str, str]] = []
            for uri in source_uris:
                parsed = parse_uri(uri)
                if parsed is None or parsed.kind != "doc" or not parsed.identifier:
                    continue
                uri_pairs.append((parsed.vault, parsed.identifier))
            if not uri_pairs:
                return [], {}
            params.extend([
                [v for v, _ in uri_pairs],
                [i for _, i in uri_pairs],
            ])
            pair_idx = len(params) - 1
            ident_idx = len(params)
            conditions.append(
                f"((v.name, r.current_path) IN ("
                f"SELECT * FROM unnest(${pair_idx}::text[], ${ident_idx}::text[])) OR "
                f"(v.name, r.resource_id::text) IN ("
                f"SELECT * FROM unnest(${pair_idx}::text[], ${ident_idx}::text[])))"
            )

        joins = """
              FROM native_resources r
              JOIN vaults v ON v.id = r.namespace_id
              JOIN native_revisions nr
                ON nr.resource_id = r.resource_id
                AND nr.revision_id = r.head_revision_id
              JOIN native_payload_manifests pm
                ON pm.payload_manifest_id = nr.payload_manifest_id
              JOIN m1_reference_payloads p ON p.payload_id = pm.private_locator
        """
        where_sql = " AND ".join(conditions)
        # The aggregate guard reads manifest numbers only — no body bytes are
        # touched, so an oversized scope is rejected before asyncpg
        # materializes anything (same shape as the grep guard).
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            scope = await conn.fetchrow(
                f"""
                SELECT COUNT(*)::bigint AS resource_count,
                       COALESCE(SUM(p.byte_size), 0)::bigint AS body_bytes
                  {joins}
                 WHERE {where_sql}
                """,
                *params,
            )
            if (
                scope["resource_count"] > NATIVE_SEARCH_MAX_CANDIDATE_RESOURCES
                or scope["body_bytes"] > NATIVE_SEARCH_MAX_BODY_BYTES
            ):
                raise ValidationError(
                    "native search scope exceeds the bounded candidate corpus"
                )
            # Slice + paginate (workbench #1069, part 2): filter 판정 needs only
            # the leading frontmatter envelope, so fetch an 8KiB prefix per row
            # instead of the full body, keyset-paged by resource_id so peak
            # memory is page-sized rather than scope-sized. `byte_size` still
            # comes along so the per-row slice can be sanity-checked.
            candidates: list[str] = []
            filter_stats: dict[str, int] = {}
            last_seen: str | None = None
            while True:
                page_params = list(params)
                page_where = where_sql
                if last_seen is not None:
                    page_params.append(last_seen)
                    page_where = f"{where_sql} AND r.resource_id > ${len(page_params)}::uuid"
                rows = await conn.fetch(
                    f"""
                SELECT r.resource_id, r.current_path, v.name AS vault_name,
                       p.byte_size, p.digest, p.encoding,
                       p.selected_placement, p.verification_profile,
                       substring(
                           p.canonical_bytes FROM 1
                           FOR {NATIVE_CANDIDATE_FRONTMATTER_SLICE_BYTES + 1}
                       ) AS body_slice
                  {joins}
                 WHERE {page_where}
                 ORDER BY r.resource_id
                 LIMIT {NATIVE_CANDIDATE_PAGE_SIZE}
                    """,
                    *page_params,
                )
                if not rows:
                    break
                for row in rows:
                    last_seen = str(row["resource_id"])
                    metadata = await asyncio.to_thread(
                        _filtered_native_metadata, row
                    )
                    if metadata is None:
                        # Envelope opens but never closes inside the slice:
                        # filter fields may lie beyond it. Exclude + count,
                        # never filter on defaults.
                        filter_stats["unparseable_envelope"] = (
                            filter_stats.get("unparseable_envelope", 0) + 1
                        )
                        continue
                    if doc_type and (metadata.get("type") or "note") != doc_type:
                        continue
                    if not metadata_matches(metadata, doc_types, tags, include_archived, archive_scope):
                        continue
                    candidates.append(str(row["resource_id"]))
                if len(rows) < NATIVE_CANDIDATE_PAGE_SIZE:
                    break
        if filter_stats:
            logger.warning(
                "native candidates: %d unparseable envelope(s) excluded: %s",
                sum(filter_stats.values()), filter_stats,
            )
        return candidates, filter_stats

    async def search(
        self,
        query: str,
        vault: str | list[str] | None = None,
        mode: Literal["hybrid"] = "hybrid",
        rerank: bool | None = None,
        collection: str | None = None,
        doc_type: str | None = None,
        tags: list[str] | None = None,
        limit: int = 10,
        user_id: str | None = None,
        include_archived: bool = False,
        archive_scope: ArchiveScope | None = None,
        source_uris: list[str] | None = None,
        doc_types: list[str] | None = None,
        source_type: Literal["document", "file", "table"] | None = None,
    ) -> SearchResponse:
        """Hybrid search across documents. See module docstring for flow.

        ``source_uris`` (optional) restricts the search to a specific set of
        resources — each is resolved to its (ACL-checked) source id and
        intersected with the other filters, so retrieval runs only inside that
        set. An empty/omitted list means no restriction (default behaviour).
        """
        scope = resolve_archive_scope(archive_scope, include_archived)
        include_archived = scope != "unarchived"
        if mode != "hybrid":
            raise ValidationError("unsupported search mode")
        if source_uris and len(source_uris) > settings.search_max_source_uris:
            raise ValidationError(
                f"search accepts at most {settings.search_max_source_uris} source URIs "
                f"(got {len(source_uris)}); split the request or use a "
                "vault scope instead"
            )

        document_source = _configured_document_source_type()

        rerank_enabled = settings.rerank_enabled if rerank is None else settings.rerank_enabled and rerank
        vaults = _normalize_vault_scope(vault)  # str | list | None → canonical list | None
        # ACL guard mirroring `grep` below: when neither vault nor
        # user_id scopes the query, the prefilter block ends up
        # skipped (has_filters=False) and `_run_vector_search` runs
        # unscoped — a cross-vault scan. The MCP and REST handlers
        # both forward user_id today (see issue #66 / PR #67), but
        # this self-defends against any future caller that forgets.
        if not vaults and user_id is None:
            raise ValidationError("vault or user_id required")

        # Server-side limit clamp (issue #189): the MCP schema caps `limit` at
        # 50 but that is client-side only — a direct REST call or non-validating
        # client could pass an arbitrary value that propagates into the vector
        # store prefetch (target_unique * 3, prefetch_per_leg). Clamp here, the
        # single entry point for every caller.
        limit = clamp_search_limit(limit)

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
        # A None embedding here is intentionally NOT surfaced as `degraded`:
        # sparse-only is a legitimate by-design mode (a deployment may leave
        # `embed_base_url` unset), and we can't cheaply tell "configured but
        # transiently down" from "intentionally absent" at this point. The
        # symmetric sparse-leg failure IS flagged degraded in _run_vector_search
        # because the BM25 vocab is always present when the feature is on.

        # Always pre-filter when user_id is provided so we never leak
        # documents from vaults the user can't read.
        has_filters = any([vaults, collection, doc_type, tags, user_id, source_uris])
        candidate_source_ids: list[str] | None = None
        candidate_vault_ids: list[str] | None = None

        # VAULT path (issue #189 Phase 2): when there's no doc-level narrowing
        # filter, the flag is on, and the driver is pgvector, filter the vector
        # search by the user's accessible VAULT ids (a small set) instead of
        # enumerating every accessible source id (O(accessible corpus)). ACL in
        # AKB is purely per-vault, so this is correctness-equivalent. Otherwise
        # the existing SOURCE_IDS path runs UNCHANGED (flag off / other driver /
        # any doc-level filter present).
        # `is_ready()` additionally gates on the auto-backfill: until every
        # pre-upgrade pgvector point carries its vault_id, fall back to the
        # source-id path so a user can't miss their own un-backfilled docs.
        from app.services import vault_backfill
        use_vault_path = not (doc_types or source_type or scope != "all") and vault_path_eligible(
            collection=collection, doc_type=doc_type, tags=tags, source_uris=source_uris,
        ) and vault_backfill.is_ready()

        if use_vault_path:
            async with pool.acquire() as conn:
                user_uuid = uuid.UUID(user_id) if user_id else None
                is_admin = bool(await conn.fetchval(
                    "SELECT is_admin FROM users WHERE id = $1", user_uuid,
                )) if user_uuid is not None else False
                candidate_vault_ids = await self._accessible_vault_ids(
                    conn, user_uuid=user_uuid, is_admin=is_admin, vaults=vaults,
                )
            # None  → admin / anon with NO named vault → unscoped (mirrors the
            #         source path's no-ACL admin behavior; admin sees all). A
            #         named scope always resolves to ids (a list), never None.
            # []    → the named vaults are all unreadable → no results.
            if candidate_vault_ids is not None and not candidate_vault_ids:
                return SearchResponse(
                    query=query, total=0, returned=0, total_matches=0, results=[], archive_scope=scope,
                )
        elif has_filters:
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

                # Resolve an explicit `source_uris` scope to per-kind source
                # ids. ACL is NOT enforced here — the candidate queries below
                # carry the owner/grant/public predicate, so any resolved id
                # the user can't read is dropped from the final candidate set.
                src_doc_ids: list[str] = []
                src_table_ids: list[str] = []
                src_file_ids: list[str] = []
                if source_uris:
                    src_doc_ids, src_table_ids, src_file_ids = (
                        await self._resolve_source_uris(conn, source_uris)
                    )

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
                if vaults:
                    conditions.append(f"v.name = ANY(${idx})")
                    params.append(vaults); idx += 1
                if collection:
                    conditions.append(collection_predicate("d.path", collection, params))
                    idx = len(params) + 1
                if doc_type:
                    conditions.append(f"d.doc_type = ${idx}")
                    params.append(doc_type); idx += 1
                if tags:
                    conditions.append(f"d.tags && ${idx}")
                    params.append(tags); idx += 1
                if doc_types:
                    conditions.append(f"d.doc_type = ANY(${idx}::text[])")
                    params.append(doc_types); idx += 1
                acl_sql, acl_params = _vault_acl(idx)
                if acl_sql:
                    conditions.append(acl_sql)
                    params.extend(acl_params); idx += 1

                if source_uris:
                    conditions.append(f"d.id = ANY(${idx}::uuid[])")
                    params.append(src_doc_ids); idx += 1

                # Default-hide archived documents from discovery; opt back in
                # with include_archived=true. (Status literal — no bind param;
                # only the document candidate query carries doc status.)
                if not include_archived:
                    conditions.append("d.status != 'archived'")
                elif scope == "archived":
                    conditions.append("d.status = 'archived'")

                where_sql = " AND ".join(conditions) if conditions else "TRUE"
                if source_type in {"file", "table"}:
                    candidate_source_ids = []
                elif document_source == NATIVE_DOCUMENT_SOURCE:
                    candidate_source_ids, _filter_stats = await self._native_document_candidates(
                        conn,
                        user_uuid=user_uuid,
                        is_admin=is_admin,
                        vaults=vaults,
                        collection=collection,
                        doc_type=doc_type,
                        tags=tags,
                        include_archived=include_archived,
                        archive_scope=scope,
                        source_uris=source_uris,
                        doc_types=doc_types,
                    )
                else:
                    rows = await conn.fetch(
                        f"""
                        SELECT d.id FROM documents d
                        JOIN vaults v ON d.vault_id = v.id
                        WHERE {where_sql}
                        """,
                        *params,
                    )
                    candidate_source_ids = [str(r["id"]) for r in rows]

                # Document metadata filters never admit unrelated files/tables.
                if scope != "archived" and (not doc_type or doc_type == "table") and not (doc_types or tags) and source_type in {None, "table"}:
                    t_params: list = []
                    t_conds: list[str] = []
                    if vaults:
                        t_conds.append("v.name = ANY($1)")
                        t_params.append(vaults)
                    acl_sql, acl_params = _vault_acl(len(t_params) + 1)
                    if acl_sql:
                        t_conds.append(acl_sql)
                        t_params.extend(acl_params)
                    if source_uris:
                        t_conds.append(f"t.id = ANY(${len(t_params) + 1}::uuid[])")
                        t_params.append(src_table_ids)
                    if collection:
                        t_conds.append(collection_predicate("col.path", collection, t_params))
                    q = "SELECT t.id FROM vault_tables t JOIN vaults v ON t.vault_id = v.id LEFT JOIN collections col ON col.id = t.collection_id"
                    if t_conds:
                        q += " WHERE " + " AND ".join(t_conds)
                    trows = await conn.fetch(q, *t_params)
                    candidate_source_ids.extend(str(r["id"]) for r in trows)

                if scope != "archived" and (not doc_type or doc_type == "file") and not (doc_types or tags) and source_type in {None, "file"}:
                    f_params: list = []
                    # Editor attachments are storage implementation details,
                    # never standalone searchable File resources.
                    f_conds: list[str] = [confirmed_file_predicate("f")]
                    if vaults:
                        f_conds.append("v.name = ANY($1)")
                        f_params.append(vaults)
                    if collection:
                        # vault_files.collection (TEXT) was dropped in
                        # migration 020 → collection_id FK. Filter via the
                        # joined collections.path with a prefix match, same
                        # semantics as the documents branch above.
                        f_conds.append(collection_predicate("c.path", collection, f_params))
                    acl_sql, acl_params = _vault_acl(len(f_params) + 1)
                    if acl_sql:
                        f_conds.append(acl_sql)
                        f_params.extend(acl_params)
                    if source_uris:
                        f_conds.append(f"f.id = ANY(${len(f_params) + 1}::uuid[])")
                        f_params.append(src_file_ids)
                    q = (
                        "SELECT f.id FROM vault_files f "
                        "JOIN vaults v ON f.vault_id = v.id "
                        "LEFT JOIN collections c ON c.id = f.collection_id"
                    )
                    if f_conds:
                        q += " WHERE " + " AND ".join(f_conds)
                    frows = await conn.fetch(q, *f_params)
                    candidate_source_ids.extend(str(r["id"]) for r in frows)

                if not candidate_source_ids:
                    return SearchResponse(query=query, total=0, returned=0, total_matches=0, results=[], archive_scope=scope)

        target_unique = resolve_first_stage_unique_limit(
            limit=limit,
            rerank_enabled=rerank_enabled,
            rerank_prefetch=settings.rerank_prefetch,
            search_prefetch=settings.search_prefetch,
        )

        # Hybrid (dense + BM25 sparse) via the configured driver. Returns [] on any vector-store
        # failure — PG is the source of truth, the index is rebuildable.
        #
        # source_types (workbench #1069): constrain the driver-side pre-filter
        # to the active Document arm (+ table/file, which have no second arm)
        # so stale points from the non-active arm can never consume the top-K.
        # `_hydrate_hits` keeps its arm-mismatch skip as defense in depth.
        hits, degraded_reason = await self._run_vector_search(
            query_text=query,
            query_embedding=query_embedding,
            candidate_source_ids=candidate_source_ids,
            candidate_vault_ids=candidate_vault_ids,
            source_types=[document_source, "table", "file", SOURCE_NATIVE_FILE],
            limit=target_unique * 3,
        )

        if not hits:
            return SearchResponse(
                archive_scope=scope,
                query=query, total=0, returned=0, total_matches=0, results=[],
                degraded=degraded_reason is not None,
                degradation_reason=degraded_reason,
            )

        # Dedup at the public source level — one hit per public resource.
        # Previously dedup was by document_id only; generalizing keeps
        # tables and files first-class in the dedup pool. A text File has both
        # its ordinary S3 metadata chunk (``file``) and its native body chunks
        # (``native_file``); those are two projections of the same public File,
        # not two search results.
        seen: set[tuple[str, str]] = set()
        unique_hits = []
        for hit in hits:
            public_source_type = (
                "file" if hit.source_type == SOURCE_NATIVE_FILE else hit.source_type
            )
            key = (public_source_type, hit.source_id)
            if key in seen:
                continue
            seen.add(key)
            unique_hits.append(hit)
            if len(unique_hits) >= target_unique:
                break

        # `total_matches` here is the size of the *prefetch pool* after
        # source-level dedup, NOT a corpus-wide hit count — vector ANN is
        # fundamentally top-K. When the pool fills to `target_unique` the
        # corpus may contain many more hits than we ever fetched, and the
        # caller deserves an explicit signal (the limit-as-count confusion
        # this guards against was observed in the KISA RAG PoC, issue #35).
        total_matches = len(unique_hits)
        prefetch_capped = total_matches >= target_unique

        if rerank_enabled and len(unique_hits) > 1:
            unique_hits = await self._apply_rerank(query, unique_hits)

        unique_hits = unique_hits[:limit]

        # Post-search metadata join — one fetch per source_type, merged back
        # in the driver-returned order. Keeps document results fully
        # backward-compatible (doc_id == source_id) while adding table/file.
        # `dropped` counts hits lost between retrieval and hydration by cause
        # (workbench #1069 G3): any non-empty drop set marks the response
        # degraded so `total_matches > 0, returned == 0` can never again read
        # as a silent zero-match.
        results, dropped = await self._hydrate_hits(unique_hits)
        hydrate_reason = (
            f"hydration_dropped:{','.join(f'{k}={v}' for k, v in sorted(dropped.items()))}" if dropped else None
        )
        if hydrate_reason is not None and degraded_reason is None:
            degraded_reason = hydrate_reason
        returned = len(results)
        hint = (
            "Prefetch pool was capped; the corpus may contain more matches than reported. "
            "For an exact corpus-wide count of a literal substring use akb_grep with "
            "count_only=true. Semantic queries are inherently top-K and cannot be "
            "exhaustively enumerated."
        ) if prefetch_capped else None
        return SearchResponse(
            archive_scope=scope,
            query=query,
            total=returned,  # deprecated alias of `returned`
            returned=returned,
            total_matches=total_matches,
            truncated=prefetch_capped,
            hint=hint,
            degraded=degraded_reason is not None,
            degradation_reason=degraded_reason,
            results=results,
        )

    async def _accessible_vault_ids(
        self, conn, *, user_uuid: uuid.UUID | None, is_admin: bool, vaults: list[str] | None,
    ) -> list[str] | None:
        """The vault ids the user may read — the VAULT-path ACL (issue #189
        Phase 2). Mirrors the per-vault `_vault_acl` predicate exactly so the
        flag is a pure performance change, never a security change.

        Returns:
          - ``None``  → no ACL filter needed: admin / anon with NO named scope
            → an unscoped vector search (same as the source path's no-ACL admin
            behavior). A named scope ALWAYS resolves to ids (never None).
          - ``[]``    → the named vaults are all unreadable (or don't exist) →
            caller returns [].
          - ``[id…]`` → the accessible vault id(s) to filter on.
        """
        # admin / anon mirror `_vault_acl` returning (None, []): no predicate.
        if user_uuid is None or is_admin:
            if vaults:
                rows = await conn.fetch(
                    "SELECT id FROM vaults WHERE name = ANY($1)", vaults,
                )
                return [str(r["id"]) for r in rows]
            return None  # admin, no named vault → unscoped
        # Authenticated non-admin: owner / explicit grant / public.
        acl = (
            "v.id IN (SELECT vault_id FROM vault_access WHERE user_id = $1) "
            "OR v.owner_id = $1 OR v.public_access IN ('reader', 'writer')"
        )
        if vaults:
            # Intersect the chosen vault names with the user's accessible set —
            # a name the user can't read simply drops out (no leak).
            rows = await conn.fetch(
                f"SELECT v.id FROM vaults v WHERE v.name = ANY($2) AND ({acl})",
                user_uuid, vaults,
            )
            return [str(r["id"]) for r in rows]
        rows = await conn.fetch(f"SELECT v.id FROM vaults v WHERE {acl}", user_uuid)
        return [str(r["id"]) for r in rows]

    async def _resolve_source_uris(
        self, conn, source_uris: list[str]
    ) -> tuple[list[str], list[str], list[str]]:
        """Resolve a list of ``akb://`` resource URIs into per-kind source ids
        ``(doc_ids, table_ids, file_ids)`` for the candidate prefilter.

        - doc   → ``documents.id`` matched by path OR id (canonical doc URIs
          carry the full vault-relative path; ``find_by_ref`` semantics)
        - table → ``vault_tables.id`` matched by ``name`` (UNIQUE per vault)
        - file  → the URI identifier IS the file uuid; validated, used as-is

        Unparseable / non-resource (coll/vault) / unknown URIs are skipped.
        ACL is applied later by the candidate queries, so this resolution is
        purely structural.
        """
        doc_refs_by_vault: dict[str, list[str]] = {}
        table_names_by_vault: dict[str, list[str]] = {}
        file_ids: list[str] = []
        for u in source_uris:
            p = parse_uri(u)
            if p is None or p.identifier is None:
                continue
            if p.kind == "doc":
                doc_refs_by_vault.setdefault(p.vault, []).append(p.identifier)
            elif p.kind == "table":
                table_names_by_vault.setdefault(p.vault, []).append(p.identifier)
            elif p.kind == "file":
                try:
                    uuid.UUID(p.identifier)
                except ValueError:
                    continue
                file_ids.append(p.identifier)

        vault_names = set(doc_refs_by_vault) | set(table_names_by_vault)
        vault_id_by_name: dict[str, uuid.UUID] = {}
        if vault_names:
            rows = await conn.fetch(
                "SELECT id, name FROM vaults WHERE name = ANY($1)", list(vault_names)
            )
            vault_id_by_name = {r["name"]: r["id"] for r in rows}

        doc_ids: list[str] = []
        for vname, refs in doc_refs_by_vault.items():
            vid = vault_id_by_name.get(vname)
            if vid is None:
                continue
            rows = await conn.fetch(
                "SELECT id FROM documents "
                "WHERE vault_id = $1 AND (path = ANY($2) OR id::text = ANY($2))",
                vid, refs,
            )
            doc_ids.extend(str(r["id"]) for r in rows)

        table_ids: list[str] = []
        for vname, names in table_names_by_vault.items():
            vid = vault_id_by_name.get(vname)
            if vid is None:
                continue
            rows = await conn.fetch(
                "SELECT id FROM vault_tables WHERE vault_id = $1 AND name = ANY($2)",
                vid, names,
            )
            table_ids.extend(str(r["id"]) for r in rows)

        return doc_ids, table_ids, file_ids

    async def _hydrate_hits(self, hits: list) -> tuple[list[SearchResult], dict[str, int]]:
        from app.services.index_service import SOURCE_TYPES
        by_type: dict[str, list[str]] = {t: [] for t in SOURCE_TYPES}
        document_source = _configured_document_source_type()
        unknown_types: set[str] = set()
        # Hydration-drop accounting (workbench #1069 G3): every hit that enters
        # this method but leaves as no result is counted by cause, so a
        # `total_matches > 0, returned == 0` response can say WHERE the hits
        # went instead of reading as a silent zero-match. Keys are stable
        # diagnostic strings (not user-facing copy).
        dropped: dict[str, int] = {}
        for h in hits:
            if h.source_type in {LEGACY_DOCUMENT_SOURCE, NATIVE_DOCUMENT_SOURCE} and h.source_type != document_source:
                # A selected backend has exactly one Document authority. Old
                # vector points from the other arm are never hydrated. With the
                # driver-side `source_types` predicate (workbench #1069) these
                # should no longer arrive; the skip stays as defense in depth
                # and the counter proves it (stays zero when the predicate
                # works, goes non-zero if a driver ignores it).
                dropped["stale_arm"] = dropped.get("stale_arm", 0) + 1
                continue
            if h.source_type not in by_type:
                dropped["unknown_source_type"] = dropped.get("unknown_source_type", 0) + 1
                unknown_types.add(h.source_type)
                continue
            if h.source_id:
                by_type[h.source_type].append(h.source_id)
        if unknown_types:
            logger.warning("hydrate: unknown source_type(s) skipped: %s", unknown_types)

        pool = await get_pool()
        meta: dict[tuple[str, str], dict] = {}
        chunk_indexes: dict[str, int] = {}
        async with pool.acquire() as conn:
            if by_type["document"]:
                rows = await conn.fetch(
                    """
                    SELECT d.id, v.name AS vault_name, d.path, d.title,
                           c.path AS collection,
                           d.doc_type, d.summary, d.tags, d.status
                      FROM documents d
                      JOIN vaults v ON d.vault_id = v.id
                      LEFT JOIN collections c ON c.id = d.collection_id
                     WHERE d.id = ANY($1)
                    """,
                    [uuid.UUID(x) for x in by_type["document"]],
                )
                for r in rows:
                    meta[("document", str(r["id"]))] = {
                        "vault": r["vault_name"], "path": r["path"],
                        "title": r["title"], "doc_type": r["doc_type"],
                        "status": r.get("status") or "draft",
                        "summary": r["summary"],
                        "tags": list(r["tags"]) if r["tags"] else [],
                        "collection": r["collection"],
                    }
            if by_type[NATIVE_DOCUMENT_SOURCE]:
                native_hits = {
                    uuid.UUID(h.chunk_id): h
                    for h in hits
                    if h.source_type == NATIVE_DOCUMENT_SOURCE
                }
                native_body_bytes = await conn.fetchval(
                    """
                    SELECT COALESCE(SUM(p.byte_size), 0)::bigint
                      FROM chunks c
                      JOIN native_derived_chunks dc ON dc.chunk_id = c.id
                      JOIN native_resources r
                        ON r.resource_id = dc.resource_id
                       AND r.head_revision_id = dc.revision_id
                       AND r.lifecycle = 'live'
                      JOIN native_revisions nr
                        ON nr.resource_id = r.resource_id
                       AND nr.revision_id = r.head_revision_id
                      JOIN native_payload_manifests pm
                        ON pm.payload_manifest_id = nr.payload_manifest_id
                      JOIN m1_reference_payloads p
                        ON p.payload_id = pm.private_locator
                     WHERE c.id = ANY($1::uuid[])
                       AND c.source_type = 'native_document'
                    """,
                    list(native_hits),
                )
                if native_body_bytes > NATIVE_SEARCH_MAX_BODY_BYTES:
                    raise ValidationError(
                        "native search hydration exceeds the bounded body corpus"
                    )
                rows = await conn.fetch(
                    """
                    SELECT c.id AS chunk_id, r.resource_id, r.current_path,
                           r.head_revision_id, v.name AS vault_name,
                           p.payload_id, p.namespace_id, p.content_profile,
                           p.digest, p.byte_size, p.encoding,
                           p.selected_placement, p.verification_profile,
                           p.canonical_bytes
                      FROM chunks c
                      JOIN native_derived_chunks dc ON dc.chunk_id = c.id
                      JOIN native_derived_heads dh
                        ON dh.resource_id = dc.resource_id
                       AND dh.revision_id = dc.revision_id
                      JOIN native_resources r
                        ON r.resource_id = dc.resource_id
                       AND r.head_revision_id = dc.revision_id
                       AND r.lifecycle = 'live'
                      JOIN vaults v ON v.id = r.namespace_id
                      JOIN native_revisions nr
                        ON nr.resource_id = r.resource_id
                       AND nr.revision_id = r.head_revision_id
                      JOIN native_payload_manifests pm
                        ON pm.payload_manifest_id = nr.payload_manifest_id
                      JOIN m1_reference_payloads p
                        ON p.payload_id = pm.private_locator
                     WHERE c.id = ANY($1::uuid[])
                       AND c.source_type = 'native_document'
                    """,
                    list(native_hits),
                )
                for r in rows:
                    # Hydration independently verifies the current Head body;
                    # the derived chunk is only a candidate, never authority.
                    metadata = await asyncio.to_thread(_verified_native_metadata, r)
                    path = r["current_path"]
                    collection = path.rsplit("/", 1)[0] if "/" in path else None
                    meta[(NATIVE_DOCUMENT_SOURCE, str(r["resource_id"]))] = {
                        "vault": r["vault_name"],
                        "path": path,
                        "title": metadata.get("title") or path.rsplit("/", 1)[-1],
                        "status": metadata.get("status") or "draft",
                        "doc_type": metadata.get("type") or "note",
                        "summary": metadata.get("summary"),
                        "tags": list(metadata.get("tags") or []),
                        "collection": collection,
                        "revision": r["head_revision_id"],
                    }
            if by_type[SOURCE_NATIVE_FILE]:
                native_file_hits = {
                    uuid.UUID(h.chunk_id): h
                    for h in hits
                    if h.source_type == SOURCE_NATIVE_FILE
                }
                native_file_body_bytes = await conn.fetchval(
                    """
                    SELECT COALESCE(SUM(p.byte_size), 0)::bigint
                      FROM chunks c
                      JOIN native_derived_chunks dc ON dc.chunk_id = c.id
                      JOIN native_resources r
                        ON r.resource_id = dc.resource_id
                       AND r.head_revision_id = dc.revision_id
                       AND r.surface = 'file'
                       AND r.lifecycle = 'live'
                      JOIN native_revisions nr
                        ON nr.resource_id = r.resource_id
                       AND nr.revision_id = r.head_revision_id
                      JOIN native_payload_manifests pm
                        ON pm.payload_manifest_id = nr.payload_manifest_id
                      JOIN m1_reference_payloads p
                        ON p.payload_id = pm.private_locator
                     WHERE c.id = ANY($1::uuid[])
                       AND c.source_type = 'native_file'
                    """,
                    list(native_file_hits),
                )
                if native_file_body_bytes > NATIVE_SEARCH_MAX_BODY_BYTES:
                    raise ValidationError(
                        "native search hydration exceeds the bounded body corpus"
                    )
                rows = await conn.fetch(
                    f"""
                    SELECT c.id AS chunk_id, r.resource_id, r.current_path,
                           r.head_revision_id, v.name AS vault_name,
                           f.name, f.description, f.mime_type,
                           col.path AS collection,
                           p.payload_id, p.namespace_id, p.content_profile,
                           p.digest, p.byte_size, p.encoding,
                           p.selected_placement, p.verification_profile,
                           p.canonical_bytes
                      FROM chunks c
                      JOIN native_derived_chunks dc ON dc.chunk_id = c.id
                      JOIN native_derived_heads dh
                        ON dh.resource_id = dc.resource_id
                       AND dh.revision_id = dc.revision_id
                      JOIN native_resources r
                        ON r.resource_id = dc.resource_id
                       AND r.head_revision_id = dc.revision_id
                       AND r.surface = 'file'
                       AND r.lifecycle = 'live'
                      JOIN vaults v ON v.id = r.namespace_id
                      JOIN vault_files f
                        ON f.id = r.resource_id
                       AND f.vault_id = r.namespace_id
                      LEFT JOIN collections col ON col.id = f.collection_id
                      JOIN native_revisions nr
                        ON nr.resource_id = r.resource_id
                       AND nr.revision_id = r.head_revision_id
                      JOIN native_payload_manifests pm
                        ON pm.payload_manifest_id = nr.payload_manifest_id
                      JOIN m1_reference_payloads p
                        ON p.payload_id = pm.private_locator
                     WHERE c.id = ANY($1::uuid[])
                       AND c.source_type = 'native_file'
                       AND {confirmed_file_predicate("f")}
                    """,
                    list(native_file_hits),
                )
                for r in rows:
                    # File catalog/S3 remains public authority. The native body
                    # is a searchable projection and must still verify against
                    # its immutable Head before a derived hit is exposed.
                    await asyncio.to_thread(_verify_native_body, r)
                    catalog_path = (
                        f"{r['collection']}/{r['name']}"
                        if r["collection"]
                        else r["name"]
                    )
                    if r["current_path"] != catalog_path:
                        logger.warning(
                            "hydrate: stale native File path skipped for %s",
                            r["resource_id"],
                        )
                        dropped["stale_native_file_path"] = dropped.get("stale_native_file_path", 0) + 1
                        continue
                    meta[(SOURCE_NATIVE_FILE, str(r["resource_id"]))] = {
                        "vault": r["vault_name"],
                        "path": catalog_path,
                        "title": r["name"],
                        "doc_type": "file",
                        "summary": r["description"] or r["mime_type"],
                        "tags": [],
                        "collection": r["collection"],
                        "revision": r["head_revision_id"],
                    }
            if by_type["table"]:
                rows = await conn.fetch(
                    """
                    SELECT t.id, v.name AS vault_name, c.path AS collection,
                           t.name, t.description
                      FROM vault_tables t
                      JOIN vaults v ON t.vault_id = v.id
                      LEFT JOIN collections c ON c.id = t.collection_id
                     WHERE t.id = ANY($1)
                    """,
                    [uuid.UUID(x) for x in by_type["table"]],
                )
                for r in rows:
                    meta[("table", str(r["id"]))] = {
                        "vault": r["vault_name"],
                        # `path` is the table name — pre-0.3.0 was the
                        # synthetic `_tables/<name>` form. The URI now
                        # encodes kind + location, so the prefix is
                        # redundant noise. Matches the BrowseItem
                        # emit shape.
                        "path": r["name"],
                        "title": r["name"],
                        "doc_type": "table",
                        "summary": r["description"],
                        "tags": [],
                        "collection": r["collection"],
                    }
            if by_type["file"]:
                rows = await conn.fetch(
                    f"""
                    SELECT f.id, v.name AS vault_name, c.path AS collection,
                           f.name, f.description, f.mime_type
                      FROM vault_files f
                      JOIN vaults v ON f.vault_id = v.id
                      LEFT JOIN collections c ON c.id = f.collection_id
                     WHERE f.id = ANY($1)
                       AND {confirmed_file_predicate("f")}
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
                        "collection": r["collection"],
                    }

            # Parent descriptions are response context only. Fetch them from
            # the source-of-truth catalog after hit selection so they do not
            # create vector points or influence retrieval/rerank scores.
            vault_names = sorted({m["vault"] for m in meta.values()})
            collection_paths = sorted({
                m["collection"] for m in meta.values() if m.get("collection")
            })
            if vault_names:
                rows = await conn.fetch(
                    """
                    SELECT v.name AS vault_name,
                           v.description AS vault_description,
                           c.path AS collection_path,
                           c.summary AS collection_summary
                      FROM vaults v
                      LEFT JOIN collections c
                        ON c.vault_id = v.id
                       AND c.path = ANY($2::text[])
                     WHERE v.name = ANY($1::text[])
                    """,
                    vault_names,
                    collection_paths,
                )
                vault_descriptions: dict[str, str | None] = {}
                collection_summaries: dict[tuple[str, str], str | None] = {}
                for row in rows:
                    vault_descriptions[row["vault_name"]] = row["vault_description"]
                    if row["collection_path"] is not None:
                        collection_summaries[(row["vault_name"], row["collection_path"])] = row[
                            "collection_summary"
                        ]
                for item in meta.values():
                    item["vault_description"] = vault_descriptions.get(item["vault"])
                    collection_path = item.get("collection")
                    item["collection_summary"] = (
                        collection_summaries.get((item["vault"], collection_path))
                        if collection_path
                        else None
                    )

            # Chunk-level identity for the row that matched. `VectorHit`
            # carries `section_path` but no ordinal, and the drivers differ in
            # what they store, so read it from `chunks` — PG is the source of
            # truth for chunk rows, and this is one keyed lookup for the whole
            # result page. A hit whose chunk row has since been deleted simply
            # gets no ordinal; the hit itself is unaffected.
            chunk_uuids = sorted(
                {
                    canonical
                    for canonical in (canonical_chunk_id(h.chunk_id) for h in hits)
                    if canonical is not None
                }
            )
            if chunk_uuids:
                rows = await conn.fetch(
                    """
                    SELECT c.id::text AS chunk_id, c.chunk_index
                      FROM chunks c
                     WHERE c.id = ANY($1::uuid[])
                    """,
                    [uuid.UUID(x) for x in chunk_uuids],
                )
                for r in rows:
                    chunk_id = canonical_chunk_id(_row_value(r, "chunk_id"))
                    chunk_index = _row_value(r, "chunk_index")
                    if chunk_id is not None and isinstance(chunk_index, int):
                        chunk_indexes[chunk_id] = chunk_index

        from app.services.uri_service import doc_uri, table_uri, file_uri

        results: list[SearchResult] = []
        for h in hits:
            key = (h.source_type, h.source_id)
            m = meta.get(key)
            if not m:
                # The hit survived retrieval but its source row is gone or stale
                # (deleted between retrieval and hydration, or a derived chunk
                # whose head moved). Count it — this is the workbench #1069
                # `total_matches=30, returned=0` shape, and it must never again
                # read as a silent zero-match.
                dropped["hydration_miss"] = dropped.get("hydration_miss", 0) + 1
                continue
            # Build the canonical 0.3.0 URI per resource type. Doc URIs
            # derive the collection from `path` automatically (path
            # encodes it); table/file URIs need it passed in.
            if h.source_type in {"document", NATIVE_DOCUMENT_SOURCE}:
                uri = doc_uri(m["vault"], m["path"])
            elif h.source_type == "table":
                uri = table_uri(m["vault"], m["title"], collection=m.get("collection"))
            elif h.source_type in {"file", SOURCE_NATIVE_FILE}:
                uri = file_uri(m["vault"], h.source_id, collection=m.get("collection"))
            else:
                dropped["unuriable_source_type"] = dropped.get("unuriable_source_type", 0) + 1
                continue
            results.append(
                SearchResult(
                    source_type=(
                        "document"
                        if h.source_type == NATIVE_DOCUMENT_SOURCE
                        else "file"
                        if h.source_type == SOURCE_NATIVE_FILE
                        else h.source_type
                    ),
                    uri=uri,
                    vault=m["vault"], path=m["path"], title=m["title"],
                    collection=m.get("collection"),
                    collection_summary=m.get("collection_summary"),
                    vault_description=m.get("vault_description"),
                    doc_type=m["doc_type"], summary=m["summary"],
                    status=m.get("status"),
                    tags=m["tags"], score=h.score,
                    # Cleaned before the clip, not after: the heading-context
                    # line duplicates `section_path` on this very row, so
                    # leaving it in would spend the first ~40 characters of
                    # the excerpt restating the field beside it.
                    matched_section=(
                        strip_chunk_context_line(
                            strip_chunk_metadata_header(h.content),
                            h.section_path,
                        ) or ""
                    )[:500] or None,
                    section_path=(h.section_path or None),
                    chunk_index=_chunk_index_of(h, chunk_indexes),
                )
            )
        if dropped:
            logger.warning("hydrate: dropped %d hit(s): %s", sum(dropped.values()), dropped)
        return results, dropped

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

        return fuse_original_and_reranked_hits(
            hits,
            ranked,
            settings.rerank_fusion_k,
        )

    async def _run_vector_search(
        self,
        *,
        query_text: str,
        query_embedding: list[float] | None,
        candidate_source_ids: list[str] | None,
        candidate_vault_ids: list[str] | None = None,
        source_types: list[str] | None = None,
        limit: int,
    ) -> tuple[list, str | None]:
        """Hybrid search over the vector store.

        Returns ``(hits, degradation_reason)``. ``reason`` is None on success
        OR on a genuine OOV-empty; a non-None reason means the store FAILED
        (outage, or a filter-size overflow on the seahorse drivers) so the
        empty/partial result is degraded — not a true zero-match. The store is
        a derived view, so a failure never raises to the caller; PG truth is
        untouched. Previously every failure was swallowed into a silent ``[]``
        with no signal (issue #189)."""
        sparse_failed = False
        try:
            sparse_idx, sparse_vals = await sparse_encoder.encode_query(query_text)
        except Exception as e:  # noqa: BLE001
            logger.warning("sparse encode_query failed (%s); dense-only path", e)
            sparse_idx, sparse_vals = [], []
            sparse_failed = True

        # OOV guard — fires ONLY when encode_query SUCCEEDED but produced no
        # vocab terms (nonsense query) AND the embedding API succeeded: the
        # right answer is []. Dense-only is a degraded mode for an outage, not
        # for OOV — Qwen3-style embeddings sit at ~0.4-0.5 cosine for unrelated
        # text and would return plausible-looking distractor neighbours. This
        # must NOT fire on a sparse-encoder FAILURE (that's degraded, not OOV).
        if not sparse_failed and not sparse_idx and query_embedding is not None:
            return [], None

        # Sparse encoder DOWN with no dense leg either → we can't search at all.
        # Surface it as degraded (issue #189) instead of a silent zero-match.
        if sparse_failed and query_embedding is None:
            return [], "sparse_encoder_failed"

        # If the sparse leg failed but a dense leg is available, we proceed
        # dense-only — but the result is degraded (one retrieval leg missing),
        # so we flag it rather than passing it off as a complete result.
        sparse_reason = "sparse_encoder_degraded" if sparse_failed else None

        # max(limit*3, 50): same heuristic the legacy native-fusion path used.
        # Driver-agnostic now, but the value transfers cleanly — RRF
        # fusion benefits from generous prefetch in either driver.
        prefetch_per_leg = max(limit * 3, 50)

        try:
            hits = await get_vector_store().hybrid_search(
                query_text=query_text,
                query_dense=query_embedding,
                query_sparse_indices=sparse_idx,
                query_sparse_values=sparse_vals,
                source_ids=candidate_source_ids,
                vault_ids=candidate_vault_ids,
                source_types=source_types,
                limit=limit,
                prefetch_per_leg=prefetch_per_leg,
            )
            # `sparse_reason` is None on the normal path; set when the sparse leg
            # was down and we ran dense-only (degraded-but-has-results).
            return hits, sparse_reason
        except VectorStoreUnavailable as e:
            # Transient/expected: store outage. Search degrades to empty but the
            # caller is told WHY instead of seeing a silent zero-match.
            logger.warning("vector store unavailable (%s); degraded empty result", e)
            return [], "vector_store_unavailable"
        except Exception as e:  # noqa: BLE001
            # Unexpected (e.g. seahorse filter-size overflow from a giant IN list
            # — the #189 silent-failure mode). Log loudly + surface the signal.
            #
            # Log the exception CLASS, not just str(e). The classes that actually
            # show up here under load — asyncio/asyncpg timeouts, cancellations —
            # carry an empty message, so a bare "%s" renders the useless
            # "hybrid_search failed ()" and the operator learns nothing about a
            # search that silently returned zero rows. exc_info keeps the driver
            # frame that raised, which is what separates "query too slow" from
            # "filter payload too large".
            logger.error(
                "vector hybrid_search failed (%s: %s); degraded empty result",
                type(e).__name__, e, exc_info=True,
            )
            return [], "vector_store_error"

    async def grep(
        self,
        pattern: str,
        vault: str | list[str] | None = None,
        collection: str | None = None,
        regex: bool = False,
        case_sensitive: bool = False,
        replace: str | None = None,
        doc_service=None,
        agent_id: str | None = None,
        user_id: str | None = None,
        limit: int = 20,
        max_replacements: int = DEFAULT_MAX_REPLACEMENTS,
        count_only: bool = False,
        files_with_matches: bool = False,
        measurement_include_text_files: bool = False,
        doc_types: list[str] | None = None,
        tags: list[str] | None = None,
        include_archived: bool = True,
        archive_scope: ArchiveScope | None = None,
    ) -> dict:
        """Exact text / regex search across document content.

        Three response shapes (mutually exclusive):

        * default: matched lines + their containing docs.
        * ``count_only=True`` (``grep -c``): per-doc match count + total,
          no snippet payload.
        * ``files_with_matches=True`` (``grep -l``): just the doc URIs
          that contain the pattern, no per-line detail.

        If ``replace`` is provided, performs find-and-replace on matching
        documents' bodies and commits via the standard pipeline (only
        valid with the default response shape).
        """
        import re as _re

        scope = resolve_archive_scope(archive_scope, include_archived)
        include_archived = scope != "unarchived"

        if pattern == "":
            raise ValidationError("grep pattern must not be empty")

        # Mutual exclusion — issue #41.
        if count_only and files_with_matches:
            return {
                "error": "count_only and files_with_matches are mutually exclusive",
                "pattern": pattern,
            }
        if replace is not None and (count_only or files_with_matches):
            return {
                "error": "replace= is incompatible with count_only / files_with_matches",
                "pattern": pattern,
            }
        if replace is not None:
            max_replacements = validate_max_replacements(max_replacements)

        # Validate regex pattern early to give a clear error
        if regex:
            try:
                _re.compile(pattern)
            except _re.error as e:
                raise ValidationError(f"Invalid regex pattern: {e}") from e

        vaults = _normalize_vault_scope(vault)  # str | list | None → canonical list | None
        # ACL guard: when no vault is given we MUST have a user_id so the
        # SQL can scope to the vaults that user can access. A None user_id
        # in that branch would silently produce a cross-vault scan.
        if not vaults and user_id is None:
            raise ValidationError("vault or user_id required")

        # Server-side limit clamp (issue #189) — same ceiling as search().
        limit = clamp_search_limit(limit)

        document_source = _configured_document_source_type()
        if measurement_include_text_files and (
            settings.document_revision_backend
            not in {"postgres_native", "native_ledger_m1"}
            or document_source != NATIVE_DOCUMENT_SOURCE
        ):
            raise ValidationError(
                "measurement_include_text_files requires a native Document backend "
                "(postgres_native or guarded native_ledger_m1)"
            )
        if document_source == NATIVE_DOCUMENT_SOURCE:
            from app.services.m1_native_grep_service import M1NativeGrepService

            if user_id is None:
                raise ValidationError("user_id required for native grep")
            return await M1NativeGrepService(await get_pool()).grep_public(
                pattern,
                user_id=uuid.UUID(user_id) if isinstance(user_id, str) else user_id,
                vaults=vaults,
                collection=collection,
                regex=regex,
                case_sensitive=case_sensitive,
                replace=replace,
                actor=agent_id,
                limit=limit,
                max_replacements=max_replacements,
                count_only=count_only,
                files_with_matches=files_with_matches,
                include_text_files=measurement_include_text_files,
                doc_types=doc_types, tags=tags, include_archived=include_archived,
                archive_scope=scope,
            )

        if replace is not None and doc_service is None:
            raise ValidationError("doc_service is required for legacy grep replacement")

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
                params.append(escape_like(pattern))
            idx += 1

            if vaults:
                conditions.append(f"v.name = ANY(${idx})")
                params.append(vaults)
                idx += 1
            if user_id:
                # Restrict to vaults the user can access (vault_access grant OR
                # owner OR public). Applied even when `vaults` is given so the
                # named set is intersected with the accessible set (no leak).
                conditions.append(
                    f"(v.id IN (SELECT vault_id FROM vault_access WHERE user_id = ${idx}) "
                    f"OR v.owner_id = ${idx} "
                    f"OR v.public_access IN ('reader', 'writer'))"
                )
                # Cast to UUID so asyncpg binds the parameter as uuid
                # (vault_access.user_id / vaults.owner_id are uuid columns).
                params.append(uuid.UUID(user_id) if isinstance(user_id, str) else user_id)
                idx += 1

            if collection:
                conditions.append(collection_predicate("d.path", collection, params))
                idx = len(params) + 1
            if doc_types:
                conditions.append(f"d.doc_type = ANY(${idx}::text[])")
                params.append(doc_types)
                idx += 1
            if tags:
                conditions.append(f"d.tags && ${idx}")
                params.append(tags)
            if not include_archived:
                conditions.append("d.status != 'archived'")
            elif scope == "archived":
                conditions.append("d.status = 'archived'")

            where_sql = " AND ".join(conditions)
            # No prefetch cap. The old `LIMIT (limit * 5)` cap was inherited
            # from a score-ordered hybrid search path, but `grep` matches with
            # ILIKE — there is no score, so ORDER + LIMIT was just chopping the
            # corpus alphabetically. Symptoms reported by users:
            #   - vault filter gave 13 hits, no filter gave 11 (cap consumed
            #     by vaults sorted before the real one).
            #   - within a single vault, adding a `collection` filter raised
            #     the count (the tighter WHERE shrank the population below
            #     the cap, so all rows fit again).
            # Both are the same anti-pattern: a user-facing count (total_docs /
            # total_matches) that drifted with the WHERE clause because the
            # cap was tied to `limit`. ILIKE is a full scan either way; the
            # only thing the cap was buying was a memory safety net. The PG
            # planner happily streams millions of rows back through asyncpg,
            # and our largest vault has tens of thousands of chunks — well
            # within budget. Drop the cap; if a pathological corpus ever
            # becomes a real concern, gate it on `EXPLAIN` cost instead.
            rows = await conn.fetch(
                f"""
                SELECT d.id::text as doc_id, v.name as vault, d.path, d.title,
                       d.metadata, d.status,
                       c.section_path, c.content, c.chunk_index
                FROM chunks c
                JOIN documents d ON c.source_id = d.id AND c.source_type = 'document'
                JOIN vaults v ON d.vault_id = v.id
                WHERE {where_sql}
                ORDER BY v.name, d.path, c.chunk_index
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
                    **({"status": r["status"]} if r.get("status") is not None else {}),
                    "matches": [],
                }

            # Extract individual matching lines from chunk. Strip the
            # indexing-time TITLE/SUMMARY/... enrichment so a user
            # grepping for a real body word doesn't get phantom hits
            # against the doc-level signals that ride along with every
            # chunk.
            chunk_body = strip_chunk_metadata_header(r["content"]) or ""
            chunk_lines = chunk_body.split("\n")
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

        # ── count_only (grep -c) — issue #41 ─────────────────────────
        # `limit` is a *snippet-output* knob. For count/files modes we
        # want the populations the agent is really asking about ("X가 등장하는
        # 사고가 몇 건"), so we count across all docs that matched — capped only
        # by the SQL prefetch (`limit * 5`), which is generous and unconditional.
        if count_only:
            by_doc = {
                d["uri"]: len(d["matches"])
                for d in docs.values()
                if d["matches"]
            }
            return {
                "pattern": pattern,
                "regex": regex,
                "total_matches": sum(by_doc.values()),
                "total_docs": len(by_doc),
                "by_doc": by_doc,
            }

        # ── files_with_matches (grep -l) — issue #41 ─────────────────
        if files_with_matches:
            files = [d["uri"] for d in docs.values() if d["matches"]]
            return {
                "pattern": pattern,
                "regex": regex,
                "n_files": len(files),
                "files": files,
            }

        # Default response shape: separate "what fit under limit"
        # (`returned_*`) from "what the full scan actually matched"
        # (`total_*`). Aligning with the hybrid-search response shape
        # established by issue #35 (`total_matches` MUST always be ≥
        # `returned`). Filter out chunk-level ILIKE hits that produced
        # no line-level matches after `strip_chunk_metadata_header` —
        # those are not real grep hits.
        matched_docs = [d for d in docs.values() if d["matches"]]
        total_docs = len(matched_docs)
        total_matches = sum(len(d["matches"]) for d in matched_docs)
        result_docs = matched_docs[:limit]
        returned_matches = sum(len(d["matches"]) for d in result_docs)

        # Replace mode: apply find-and-replace on each matching document.  The
        # response ``limit`` remains a preview-only knob; writes are governed by
        # the independent, caller-visible ``max_replacements`` budget.
        # Service-layer `doc_service.update` still wants the doc path
        # (which find_by_ref accepts) — we pass `path` rather than re-
        # parsing the URI we just built.
        replaced: list[dict] = []
        unchanged_docs = 0
        replacement_error: dict | None = None
        if replace is not None and total_docs > max_replacements:
            replacement_error = replacement_budget_error(
                total_docs=total_docs,
                max_replacements=max_replacements,
            )
        elif replace is not None and matched_docs:
            for doc_info in matched_docs:
                doc_vault = doc_info["vault"]
                doc_path = doc_info["path"]
                doc_uri_str = doc_info["uri"]

                try:
                    doc = await doc_service.get(doc_vault, doc_path)
                    body = doc.content or ""

                    new_body = apply_grep_replacement(
                        body,
                        pattern,
                        replace,
                        regex=regex,
                        case_sensitive=case_sensitive,
                    )

                    if new_body == body:
                        unchanged_docs += 1
                        continue

                    from app.models.document import DocumentUpdateRequest
                    req = DocumentUpdateRequest(
                        content=new_body,
                        message=f"grep replace: '{pattern}' → '{replace}'",
                        # Do not overwrite a concurrent edit made after the
                        # replacement body was read.
                        expected_commit=doc.current_commit,
                    )
                    result = await doc_service.update(
                        doc_vault,
                        doc_path,
                        req,
                        agent_id=agent_id,
                    )
                except Exception as exc:  # noqa: BLE001 — return recovery receipt
                    replacement_error = replacement_failure_error(
                        exc,
                        failed_uri=doc_uri_str,
                        committed_replacements=len(replaced),
                    )
                    break

                replaced.append({
                    "uri": doc_uri_str,
                    "path": doc_path,
                    "title": doc_info["title"],
                    "commit": result.commit_hash,
                    "previous_commit": result.previous_commit,
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
            "returned_docs": len(clean_results),
            "returned_matches": returned_matches,
            "total_docs": total_docs,
            "total_matches": total_matches,
            "truncated": total_docs > len(clean_results),
            "results": clean_results,
        }
        if resp["truncated"]:
            resp["hint"] = (
                f"Showing {len(clean_results)} of {total_docs} matching docs "
                f"(limit={limit}, {returned_matches} of {total_matches} line matches). "
                f"For full counts use count_only=true; for the full URI list use "
                f"files_with_matches=true."
            )

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
            resp["max_replacements"] = max_replacements
            resp["replaced_docs"] = len(replaced)
            resp["unchanged_docs"] = unchanged_docs
            resp["replacement_complete"] = replacement_error is None
            resp["replacements"] = replaced
            if replacement_error is not None:
                resp.update(replacement_error)
        return resp

    async def drill_down(self, vault: str, doc_id: str, section: str | None = None) -> list[dict]:
        """Get L3 section-level content for a document."""
        from app.repositories.document_repo import DocumentRepository
        pool = await get_pool()
        async with pool.acquire() as conn:
            doc_match = DocumentRepository.match_clause(2)
            if section:
                rows = await conn.fetch(
                    f"""
                    SELECT c.section_path, c.content, c.chunk_index, d.id AS doc_id
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
                    SELECT c.section_path, c.content, c.chunk_index, d.id AS doc_id
                    FROM chunks c
                    JOIN documents d ON c.source_id = d.id AND c.source_type = 'document'
                    JOIN vaults v ON d.vault_id = v.id
                    WHERE v.name = $1 AND {doc_match}
                    ORDER BY c.chunk_index
                    """,
                    vault, doc_id,
                )

            return clean_section_rows(rows)

    async def list_section_headings(self, vault: str, doc_id: str, limit: int | None = None) -> list[str]:
        """Return the document's distinct section paths, without their bodies,
        in first-occurrence order.

        Used by `akb_drill_down`'s empty-match fallback to surface the
        available headings cheaply — pulling full content for a 1000-
        section doc just to extract heading strings is wasteful.

        One heading, one row. A section longer than `MAX_CHUNK_SIZE` is stored
        as several chunks that all carry the same `section_path`, so the
        row-per-chunk form repeated a heading once per chunk and the caller's
        outline cap was spent on duplicates instead of on headings it had not
        seen yet. `limit` therefore bounds *headings*: the grouping happens in
        SQL, before the LIMIT, and the Python pass keeps the contract true
        regardless of how the rows arrive.
        """
        from app.repositories.document_repo import DocumentRepository
        pool = await get_pool()
        async with pool.acquire() as conn:
            doc_match = DocumentRepository.match_clause(2)
            sql = f"""
                SELECT c.section_path, MIN(c.chunk_index) AS first_chunk_index
                FROM chunks c
                JOIN documents d ON c.source_id = d.id AND c.source_type = 'document'
                JOIN vaults v ON d.vault_id = v.id
                WHERE v.name = $1 AND {doc_match}
                  AND c.section_path IS NOT NULL
                  AND c.section_path <> ''
                GROUP BY c.section_path
                ORDER BY first_chunk_index, c.section_path
            """
            if isinstance(limit, int) and limit > 0:
                sql += f" LIMIT {int(limit)}"
            rows = await conn.fetch(sql, vault, doc_id)
            headings: list[str] = []
            seen: set[str] = set()
            for r in rows:
                section_path = r["section_path"]
                if not section_path or section_path in seen:
                    continue
                seen.add(section_path)
                headings.append(section_path)
            return headings
