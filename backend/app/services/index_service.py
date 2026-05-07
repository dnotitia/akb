"""Chunking and embedding pipeline.

Handles:
- Markdown document chunking (heading-based hierarchical splitting)
- Embedding generation via external API
- Chunk storage in PostgreSQL (with pgvector)
- Cleanup on document update/delete
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from typing import Literal

import httpx

from app.config import settings
from app.services import http_pool

logger = logging.getLogger("akb.index")

# Discriminator for chunks / vector_delete_outbox / vector-store payload. Must stay
# in sync with the DB CHECK constraint in migration 006. Keep both the
# tuple (for runtime set-membership) and the Literal (for type checking).
SOURCE_DOCUMENT: Literal["document"] = "document"
SOURCE_TABLE: Literal["table"] = "table"
SOURCE_FILE: Literal["file"] = "file"
SOURCE_TYPES: tuple[str, ...] = (SOURCE_DOCUMENT, SOURCE_TABLE, SOURCE_FILE)
SourceType = Literal["document", "table", "file"]


@dataclass
class Chunk:
    section_path: str
    content: str
    chunk_index: int
    char_start: int
    char_end: int


# ── Chunker ──────────────────────────────────────────────────

MAX_CHUNK_SIZE = 1500
OVERLAP = 200

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


def build_doc_metadata_header(
    *,
    vault_name: str,
    path: str,
    title: str,
    summary: str | None = None,
    tags: list[str] | None = None,
    doc_type: str | None = None,
) -> str:
    """Top block prepended to every chunk of a document so the BM25 and
    dense legs both see doc-level identifiers (title/summary/tags) on
    every chunk. Without this, a chunk from deep inside the body carries
    zero doc-level signal and gets outweighed by shorter chunks from
    other docs that happen to repeat the query terms."""
    lines = [f"TITLE: {title}"]
    if summary:
        lines.append(f"SUMMARY: {summary}")
    if tags:
        lines.append(f"TAGS: {', '.join(tags)}")
    lines.append(f"PATH: {vault_name}/{path}")
    if doc_type:
        lines.append(f"TYPE: {doc_type}")
    return "\n".join(lines) + "\n\n"


def build_table_chunk(
    *,
    vault_name: str,
    name: str,
    description: str | None,
    columns: list[dict],
) -> "Chunk":
    """Single chunk representing a table's metadata + column schema.
    Tables are not markdown; we emit one metadata chunk so hybrid search
    can find them alongside documents."""
    col_lines = []
    for col in columns or []:
        line = f"  - {col.get('name')}: {col.get('type', 'text')}"
        if col.get("description"):
            line += f" — {col['description']}"
        col_lines.append(line)
    parts = [
        f"TITLE: {name}",
        "TYPE: table",
        f"VAULT: {vault_name}",
    ]
    if description:
        parts.append(f"DESCRIPTION: {description}")
    if col_lines:
        parts.append("COLUMNS:")
        parts.extend(col_lines)
    content = "\n".join(parts)
    return Chunk(
        section_path="",
        content=content,
        chunk_index=0,
        char_start=0,
        char_end=len(content),
    )


def build_file_chunk(
    *,
    vault_name: str,
    collection: str,
    name: str,
    mime_type: str | None,
    size_bytes: int | None,
    description: str | None,
) -> "Chunk":
    """Single chunk representing a file's metadata. Binary content is
    not indexed; searchability comes from filename + description + mime."""
    parts = [
        f"TITLE: {name}",
        "TYPE: file",
        f"VAULT: {vault_name}",
        f"PATH: {collection}/{name}" if collection else f"PATH: {name}",
    ]
    if mime_type:
        parts.append(f"MIME: {mime_type}")
    if size_bytes is not None:
        parts.append(f"SIZE: {size_bytes}")
    if description:
        parts.append(f"DESCRIPTION: {description}")
    content = "\n".join(parts)
    return Chunk(
        section_path="",
        content=content,
        chunk_index=0,
        char_start=0,
        char_end=len(content),
    )


def chunk_markdown(content: str, metadata_header: str = "") -> list[Chunk]:
    """Split markdown into chunks based on headings.

    Each chunk preserves its heading hierarchy as section_path. When
    `metadata_header` is provided, it is prepended to every chunk so
    doc-level signals (title/summary/tags) ride along with each chunk.
    Chunks exceeding MAX_CHUNK_SIZE are split at paragraph boundaries.
    """
    # Parse heading positions
    headings: list[tuple[int, int, str]] = []  # (pos, level, title)
    for m in _HEADING_RE.finditer(content):
        level = len(m.group(1))
        title = m.group(2).strip()
        headings.append((m.start(), level, title))

    if not headings:
        # No headings — treat entire content as one chunk
        return _split_large_chunk("", metadata_header + content, 0)

    chunks: list[Chunk] = []
    heading_stack: list[tuple[int, str]] = []  # (level, display_string)

    for i, (pos, level, title) in enumerate(headings):
        # Determine section end
        next_pos = headings[i + 1][0] if i + 1 < len(headings) else len(content)
        section_content = content[pos:next_pos].strip()

        # Update heading stack: pop entries at same or deeper level
        while heading_stack and heading_stack[-1][0] >= level:
            heading_stack.pop()
        heading_stack.append((level, f"{'#' * level} {title}"))

        section_path = " > ".join(h[1] for h in heading_stack)

        # Remove the heading line itself from content for the chunk body
        first_newline = section_content.find("\n")
        if first_newline >= 0:
            body = section_content[first_newline + 1:].strip()
        else:
            body = ""

        if not body:
            continue

        # Add heading context as prefix for search accuracy. When present,
        # the doc metadata header is placed first so every chunk carries
        # the document-level signals.
        context_prefix = f"[{section_path}]\n"
        full_content = metadata_header + context_prefix + body

        sub_chunks = _split_large_chunk(section_path, full_content, pos)
        chunks.extend(sub_chunks)

    # Re-index
    for i, chunk in enumerate(chunks):
        chunk.chunk_index = i

    return chunks


def _split_large_chunk(section_path: str, content: str, char_offset: int) -> list[Chunk]:
    """Split content exceeding MAX_CHUNK_SIZE at paragraph boundaries."""
    if len(content) <= MAX_CHUNK_SIZE:
        return [
            Chunk(
                section_path=section_path,
                content=content,
                chunk_index=0,
                char_start=char_offset,
                char_end=char_offset + len(content),
            )
        ]

    paragraphs = content.split("\n\n")
    chunks: list[Chunk] = []
    current = ""
    current_start = char_offset

    for para in paragraphs:
        if len(current) + len(para) + 2 > MAX_CHUNK_SIZE and current:
            chunks.append(
                Chunk(
                    section_path=section_path,
                    content=current.strip(),
                    chunk_index=len(chunks),
                    char_start=current_start,
                    char_end=current_start + len(current),
                )
            )
            # Overlap: keep tail of previous chunk
            overlap_text = current[-OVERLAP:] if len(current) > OVERLAP else current
            current_start = current_start + len(current) - len(overlap_text)
            current = overlap_text + "\n\n" + para
        else:
            if current:
                current += "\n\n" + para
            else:
                current = para

    if current.strip():
        chunks.append(
            Chunk(
                section_path=section_path,
                content=current.strip(),
                chunk_index=len(chunks),
                char_start=current_start,
                char_end=current_start + len(current),
            )
        )

    return chunks


# ── Embedding ────────────────────────────────────────────────

async def generate_embeddings(
    texts: list[str],
    *,
    timeout: float = 60.0,
) -> list[list[float]]:
    """Call embedding API to generate vectors. Batches up to 32 inputs.

    `timeout` caps each HTTP call. Indexing paths use the default (60s —
    bulk batches with large payloads); the query path in SearchService
    passes a short value so a hung embedding API doesn't stall every
    interactive search.

    Returns empty list per text if embedding API is unavailable (graceful degradation).
    """
    if not texts:
        return []

    all_embeddings: list[list[float]] = []
    batch_size = 32

    client = http_pool.get_client()
    try:
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            resp = await client.post(
                f"{settings.embed_base_url}/embeddings",
                json={
                    "model": settings.embed_model,
                    "input": batch,
                },
                headers={"Authorization": f"Bearer {settings.embed_api_key}"} if settings.embed_api_key else {},
                timeout=timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            batch_embeddings = [item["embedding"] for item in data["data"]]
            all_embeddings.extend(batch_embeddings)
    except (httpx.ConnectError, httpx.HTTPStatusError, httpx.TimeoutException, httpx.UnsupportedProtocol) as e:
        logger.warning("Embedding API unavailable (%s). Chunks stored without vectors.", e)
        return []

    return all_embeddings


# ── DB operations ────────────────────────────────────────────


async def _drop_source_chunks_with_outbox(conn, source_type: str, source_id: str) -> None:
    """Remove chunks for any indexable source from PG; enqueue the
    per-chunk ids into the outbox so delete_worker drains them from
    the vector store asynchronously."""
    from app.services import delete_worker
    try:
        await delete_worker.enqueue_source_deletes(source_type, source_id, conn=conn)
    except Exception as e:  # noqa: BLE001
        logger.warning("vector-store outbox enqueue failed: %s", e)
    await conn.execute(
        "DELETE FROM chunks WHERE source_type = $1 AND source_id = $2",
        source_type, uuid.UUID(source_id),
    )


async def delete_document_chunks(conn, document_id: str) -> None:
    """Remove all chunks for a document (public helper for document_service)."""
    await _drop_source_chunks_with_outbox(conn, "document", document_id)


async def delete_table_chunks(conn, table_id: str) -> None:
    """Remove metadata chunks for a table (called on drop/alter)."""
    await _drop_source_chunks_with_outbox(conn, "table", table_id)


async def delete_file_chunks(conn, file_id: str) -> None:
    """Remove metadata chunks for a file (called on file delete/replace)."""
    await _drop_source_chunks_with_outbox(conn, "file", file_id)


async def delete_vault_chunks(conn, vault_id) -> None:
    """Remove every chunk under a vault, enqueuing vector-store deletes in bulk.

    Scoped to source_type='document' because tables/files CASCADE through
    their own vault_tables / vault_files FKs at vault-drop time — their
    chunk cleanup is handled in the service delete hooks.
    """
    try:
        await conn.execute(
            """
            INSERT INTO vector_delete_outbox
                (chunk_id, source_type, source_id, next_attempt_at)
            SELECT c.id, c.source_type, c.source_id, NOW()
              FROM chunks c
              JOIN documents d ON d.id = c.source_id
             WHERE c.source_type = 'document'
               AND d.vault_id = $1
            """,
            vault_id,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to enqueue vector-store deletes for vault %s: %s", vault_id, e)
    await conn.execute(
        """
        DELETE FROM chunks
         WHERE source_type = 'document'
           AND source_id IN (SELECT id FROM documents WHERE vault_id = $1)
        """,
        vault_id,
    )


async def write_source_chunks(
    conn,
    source_type: SourceType,
    source_id: str,
    *,
    vault_id: uuid.UUID,
    chunks: list[Chunk],
) -> int:
    """Replace chunks for any indexable source (document/table/file).

    Post-Phase-4 the dense vector lives exclusively in the configured
    vector store, not on the chunks row. This function just lays down
    the text + metadata; `embed_worker` picks the row up via
    `vector_indexed_at IS NULL`, embeds + sparse-encodes + upserts to
    the vector store atomically.

    `vault_id` is denormalized onto every chunk so `pending_stats(vault_id)`
    can be a single indexed COUNT instead of a polymorphic JOIN through
    the parent table. Caller MUST pass the vault that owns `source_id`
    — there is no consistency check here.

    Crash-safe: rows go in with NULL flags; a crash anywhere after this
    call leaves them catchable by the worker.
    """
    await _drop_source_chunks_with_outbox(conn, source_type, source_id)
    if not chunks:
        return 0

    src_uuid = uuid.UUID(source_id)
    for chunk in chunks:
        chunk_id = uuid.uuid4()
        await conn.execute(
            """
            INSERT INTO chunks (id, source_type, source_id, vault_id,
                                section_path, content,
                                chunk_index, char_start, char_end)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            chunk_id, source_type, src_uuid, vault_id,
            chunk.section_path, chunk.content,
            chunk.chunk_index, chunk.char_start, chunk.char_end,
        )
    return len(chunks)


