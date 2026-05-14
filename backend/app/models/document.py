from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.util.text import NFCModel


class DocumentFrontmatter(NFCModel):
    """Canonical frontmatter schema parsed from YAML."""

    id: str | None = None
    title: str
    type: str = "note"  # note, report, decision, spec, plan, session, task, reference, skill
    status: str = "draft"  # draft, active, archived, superseded
    created_by: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    supersedes: str | None = None
    tags: list[str] = Field(default_factory=list)
    domain: str | None = None
    summary: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    related_to: list[str] = Field(default_factory=list)
    implements: list[str] = Field(default_factory=list)


class DocumentPutRequest(NFCModel):
    """Request body for akb_put."""

    vault: str
    collection: str
    title: str
    content: str
    type: str = "note"
    tags: list[str] = Field(default_factory=list)
    domain: str | None = None
    summary: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    related_to: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentUpdateRequest(NFCModel):
    """Request body for akb_update."""

    content: str | None = None
    title: str | None = None
    type: str | None = None
    status: str | None = None
    tags: list[str] | None = None
    domain: str | None = None
    summary: str | None = None
    depends_on: list[str] | None = None
    related_to: list[str] | None = None
    metadata: dict[str, Any] | None = None
    message: str | None = None  # commit message


class DocumentResponse(BaseModel):
    """Response for a single document."""

    id: str
    vault: str
    path: str
    title: str
    type: str
    status: str
    summary: str | None = None
    domain: str | None = None
    created_by: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    current_commit: str | None = None
    tags: list[str] = Field(default_factory=list)
    content: str | None = None  # included only for akb_get, not for browse/search
    is_public: bool = False
    public_slug: str | None = None


class DocumentPutResponse(BaseModel):
    """Response after akb_put."""

    doc_id: str
    vault: str
    path: str
    commit_hash: str
    chunks_indexed: int
    entities_found: int


class BrowseItem(BaseModel):
    """Single item in browse results — documents, collections, tables, or files."""

    name: str
    path: str
    type: str  # "collection", "document", "table", "file"
    uri: str | None = None  # akb:// URI for this resource
    summary: str | None = None
    # Document fields
    doc_count: int | None = None  # for collections
    doc_type: str | None = None  # for documents
    status: str | None = None  # for documents
    tags: list[str] = Field(default_factory=list)
    last_updated: datetime | None = None
    # Table fields
    row_count: int | None = None
    columns: list[dict] | None = None
    # File fields
    file_id: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None


class BrowseResponse(BaseModel):
    """Response for akb_browse."""

    vault: str
    path: str
    items: list[BrowseItem]
    hint: str | None = None


class SearchResult(BaseModel):
    """Single search result. Unifies document / table / file results;
    `source_type` discriminates how the frontend renders the row."""

    source_type: str                                  # 'document' | 'table' | 'file'
    source_id: str                                    # canonical id for the row
    vault: str
    path: str
    title: str
    doc_type: str | None = None
    summary: str | None = None
    tags: list[str] = Field(default_factory=list)
    score: float
    matched_section: str | None = None                # the chunk that matched


class SearchResponse(BaseModel):
    """Response for akb_search."""

    query: str
    total: int
    results: list[SearchResult]
