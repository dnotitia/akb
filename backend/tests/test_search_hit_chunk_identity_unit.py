"""A search hit says which chunk matched, not just what it said.

`matched_section` is the chunk body. Without the chunk's own address a
caller that wants the surrounding section has to search again or read the
whole document; `section_path` + `chunk_index` are the two values that
turn a hit into an `akb_drill_down(section=...)` call. Both are additive —
every field a hit carried before is asserted here too.
"""

from __future__ import annotations

import uuid

import pytest

from app.services import search_service
from app.services.search_service import SearchService
from app.services.vector_store import VectorHit

pytestmark = pytest.mark.asyncio


class _Connection:
    def __init__(self, doc_id: uuid.UUID, chunk_rows: list[dict]):
        self.doc_id = doc_id
        self.chunk_rows = chunk_rows
        self.queries: list[str] = []

    async def fetch(self, sql: str, *_params):
        self.queries.append(sql)
        if "FROM documents d" in sql:
            return [{
                "id": self.doc_id,
                "vault_name": "project-akb",
                "path": "product/pipeline/modules/collector.md",
                "title": "Collector module",
                "collection": "product/pipeline/modules",
                "doc_type": "reference",
                "summary": "The Product-API seam.",
                "tags": ["topic:pipeline"],
                "status": "active",
            }]
        if "c.path = ANY" in sql:
            return [{
                "vault_name": "project-akb",
                "vault_description": "AKB family knowledge",
                "collection_path": "product/pipeline/modules",
                "collection_summary": "Pipeline modules",
            }]
        if "FROM chunks c" in sql:
            return self.chunk_rows
        return []


class _Acquire:
    def __init__(self, connection: _Connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return None


class _Pool:
    def __init__(self, connection: _Connection):
        self.connection = connection

    def acquire(self):
        return _Acquire(self.connection)


def _configure(monkeypatch, connection: _Connection) -> SearchService:
    async def get_pool():
        return _Pool(connection)

    monkeypatch.setattr(search_service, "get_pool", get_pool)
    monkeypatch.setattr(
        search_service, "_configured_document_source_type", lambda: "document"
    )
    return SearchService()


def _hit(chunk_id: str, doc_id: uuid.UUID) -> VectorHit:
    return VectorHit(
        chunk_id=chunk_id,
        source_type="document",
        source_id=str(doc_id),
        section_path="# Collector > ## Product-API seam",
        content=(
            "TITLE: Collector module\n"
            "PATH: project-akb/product/pipeline/modules/collector.md\n"
            "\n"
            "Requests carry an Idempotency-Key."
        ),
        score=0.87,
    )


async def test_hit_carries_the_matched_chunk_section_and_ordinal(monkeypatch):
    doc_id = uuid.uuid4()
    chunk_id = str(uuid.uuid4())
    connection = _Connection(doc_id, [{"chunk_id": chunk_id, "chunk_index": 4}])
    service = _configure(monkeypatch, connection)

    (result,) = await service._hydrate_hits([_hit(chunk_id, doc_id)])

    assert result.section_path == "# Collector > ## Product-API seam"
    assert result.chunk_index == 4
    # Everything the hit carried before is still there and unchanged.
    assert result.source_type == "document"
    assert result.uri == "akb://project-akb/coll/product/pipeline/modules/doc/collector.md"
    assert result.vault == "project-akb"
    assert result.path == "product/pipeline/modules/collector.md"
    assert result.title == "Collector module"
    assert result.status == "active"
    assert result.collection == "product/pipeline/modules"
    assert result.collection_summary == "Pipeline modules"
    assert result.vault_description == "AKB family knowledge"
    assert result.doc_type == "reference"
    assert result.summary == "The Product-API seam."
    assert result.tags == ["topic:pipeline"]
    assert result.score == 0.87
    assert result.matched_section == "Requests carry an Idempotency-Key."


async def test_a_missing_chunk_row_leaves_the_ordinal_null(monkeypatch):
    # The chunk was deleted between the vector-store read and hydration.
    doc_id = uuid.uuid4()
    chunk_id = str(uuid.uuid4())
    connection = _Connection(doc_id, [])
    service = _configure(monkeypatch, connection)

    (result,) = await service._hydrate_hits([_hit(chunk_id, doc_id)])

    assert result.chunk_index is None
    assert result.section_path == "# Collector > ## Product-API seam"
    assert result.matched_section == "Requests carry an Idempotency-Key."


async def test_a_driver_chunk_id_that_is_not_a_uuid_is_skipped(monkeypatch):
    doc_id = uuid.uuid4()
    connection = _Connection(doc_id, [])
    service = _configure(monkeypatch, connection)

    (result,) = await service._hydrate_hits([_hit("not-a-uuid", doc_id)])

    assert result.chunk_index is None
    # No chunk lookup was attempted for an id the query could not cast.
    assert not any("FROM chunks c" in q for q in connection.queries)


async def test_the_excerpt_drops_the_heading_context_line(monkeypatch):
    # The chunk body opens with `[<section_path>]`, which is the value the
    # hit already carries in `section_path`. Removing it before the 500-char
    # clip also means the excerpt is 500 characters of body.
    doc_id = uuid.uuid4()
    chunk_id = str(uuid.uuid4())
    connection = _Connection(doc_id, [{"chunk_id": chunk_id, "chunk_index": 4}])
    service = _configure(monkeypatch, connection)

    hit = _hit(chunk_id, doc_id)
    body = "Requests carry an Idempotency-Key. " + "x" * 600
    hit.content = (
        "TITLE: Collector module\n"
        "PATH: project-akb/product/pipeline/modules/collector.md\n"
        "\n"
        f"[{hit.section_path}]\n{body}"
    )

    (result,) = await service._hydrate_hits([hit])

    assert result.matched_section == body[:500]
    assert result.section_path == "# Collector > ## Product-API seam"


async def test_a_bracketed_body_line_survives_in_the_excerpt(monkeypatch):
    doc_id = uuid.uuid4()
    chunk_id = str(uuid.uuid4())
    connection = _Connection(doc_id, [{"chunk_id": chunk_id, "chunk_index": 4}])
    service = _configure(monkeypatch, connection)

    hit = _hit(chunk_id, doc_id)
    hit.content = "[# Some other heading]\nBody text."

    (result,) = await service._hydrate_hits([hit])

    assert result.matched_section == "[# Some other heading]\nBody text."


async def test_an_empty_section_path_is_reported_as_null(monkeypatch):
    doc_id = uuid.uuid4()
    chunk_id = str(uuid.uuid4())
    connection = _Connection(doc_id, [{"chunk_id": chunk_id, "chunk_index": 0}])
    service = _configure(monkeypatch, connection)

    hit = _hit(chunk_id, doc_id)
    hit.section_path = ""

    (result,) = await service._hydrate_hits([hit])

    assert result.section_path is None
    assert result.chunk_index == 0
