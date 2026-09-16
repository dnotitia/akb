"""Native section reads use current authority without derived index dependence."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.exceptions import NotFoundError
from app.services import search_service
from app.services.native_document_service import NativeDocumentService


@pytest.fixture
def native_body(monkeypatch):
    monkeypatch.setattr(search_service, "_configured_document_source_type", lambda: "native_document")
    pool = object()
    monkeypatch.setattr(search_service, "get_pool", AsyncMock(return_value=pool))
    reader = AsyncMock()
    monkeypatch.setattr(NativeDocumentService, "get", reader)
    return reader


@pytest.mark.asyncio
async def test_native_prose_preserved_without_legacy_rows(native_body):
    body = "TITLE: Release notes\nURI: https://example.com/x\n\nactual body\n"
    native_body.return_value = SimpleNamespace(content=body)
    rows = await search_service.SearchService().drill_down("native", "path.md")
    assert rows == [{"section_path": "", "content": body, "chunk_index": 0}]
    native_body.assert_awaited_once_with("native", "path.md")


@pytest.mark.asyncio
async def test_native_header_shaped_prose_and_preamble_are_authoritative(native_body):
    body = "TITLE: user\nSUMMARY: own summary\nPATH: own path\n\npreamble\n\n# Heading\n\nbody"
    native_body.return_value = SimpleNamespace(content=body)
    rows = await search_service.SearchService().drill_down("native", "alias.md")
    assert rows[0]["content"] == body.split("# Heading")[0]
    assert rows[1] == {"section_path": "# Heading", "content": "\nbody", "chunk_index": 1}


@pytest.mark.asyncio
async def test_native_section_filter_outline_and_latest_head(native_body):
    native_body.return_value = SimpleNamespace(content="# Parent\n\nfirst\n\n## Child\n\nsecond\n\n# Other\n\nlast")
    service = search_service.SearchService()
    rows = await service.drill_down("native", "id", section="CHILD")
    assert rows == [{"section_path": "# Parent > ## Child", "content": "\nsecond\n\n", "chunk_index": 1}]
    assert await service.list_section_headings("native", "id", limit=2) == ["# Parent", "# Parent > ## Child"]
    native_body.return_value = SimpleNamespace(content="# Updated\n\nnew current body")
    assert await service.list_section_headings("native", "id") == ["# Updated"]
    assert await service.drill_down("native", "id", section="%") == []


@pytest.mark.asyncio
async def test_native_long_section_uses_bounded_chunks_and_unique_outline(native_body):
    from app.services.index_service import MAX_CHUNK_SIZE
    native_body.return_value = SimpleNamespace(content="# Long\n\n" + "\n\n".join(f"paragraph {i} " + "x" * 400 for i in range(35)))
    service = search_service.SearchService()
    rows = await service.drill_down("native", "id")
    assert len(rows) > 1
    assert all(len(row["content"]) <= MAX_CHUNK_SIZE for row in rows)
    assert [row["chunk_index"] for row in rows] == list(range(len(rows)))
    assert await service.list_section_headings("native", "id") == ["# Long"]


@pytest.mark.asyncio
async def test_native_missing_or_deleted_document_is_not_silent_empty(native_body):
    native_body.side_effect = NotFoundError("Native Resource", "gone")
    with pytest.raises(NotFoundError):
        await search_service.SearchService().drill_down("native", "gone")


@pytest.mark.asyncio
async def test_native_long_paragraph_reconstructs_exactly_without_nested_overlap(native_body):
    from app.services.index_service import MAX_CHUNK_SIZE
    body = "x" * 5000
    native_body.return_value = SimpleNamespace(content=body)
    rows = await search_service.SearchService().drill_down("native", "id")
    assert "".join(row["content"] for row in rows) == body
    assert all(len(row["content"]) <= MAX_CHUNK_SIZE for row in rows)


@pytest.mark.asyncio
async def test_native_authored_context_and_repeated_text_survive_chunk_boundaries(native_body):
    from app.services.index_service import MAX_CHUNK_SIZE
    authored = "a" * MAX_CHUNK_SIZE + "[# Heading]\n" + "repeat\n" * 700 + "\n\n  trailing  \n"
    native_body.return_value = SimpleNamespace(content="# Heading\n" + authored)
    rows = await search_service.SearchService().drill_down("native", "id")
    assert "".join(row["content"] for row in rows) == authored
    assert rows[1]["content"].startswith("[# Heading]\n")


@pytest.mark.asyncio
async def test_native_heading_only_outline_retains_hierarchy_and_order(native_body):
    native_body.return_value = SimpleNamespace(content="# Empty\n\n## Child\n# Last")
    service = search_service.SearchService()
    assert await service.list_section_headings("native", "id") == [
        "# Empty", "# Empty > ## Child", "# Last",
    ]
    assert await service.drill_down("native", "id", section="Last") == [
        {"section_path": "# Last", "content": "", "chunk_index": 2},
    ]
