"""Filtered legacy outlines use stored paths and bound the result by headings."""
from unittest.mock import AsyncMock

import pytest

from app.services import search_service
from tests.test_event_tail_postgres import _fresh_database, _make_vault


@pytest.mark.asyncio
async def test_legacy_outline_filters_literal_paths_before_grouping_and_limit(monkeypatch):
    async with _fresh_database() as pool:
        vault_id = await _make_vault(pool, "outline")
        async with pool.acquire() as conn:
            doc_id = await conn.fetchval(
                "INSERT INTO documents (vault_id, path, title) VALUES ($1, 'doc.md', 'Guide') RETURNING id",
                vault_id,
            )
            paths = [f"# Other {i}" for i in range(60)] + [
                "설치_%", "설치_%/설정", "설치_%/설정", "설치_%/설정/값", "설치_%2", "설치ab/설정",
                "# API", "# API > ## Setup", "# API > ## Setup v2", "# API/SDK",
            ]
            await conn.executemany(
                """INSERT INTO chunks (source_type, source_id, vault_id, content, chunk_index, section_path)
                   VALUES ('document', $1, $2, 'body', $3, $4)""",
                [(doc_id, vault_id, index, path) for index, path in enumerate(paths)],
            )
        monkeypatch.setattr(search_service, "get_pool", AsyncMock(return_value=pool))
        monkeypatch.setattr(search_service, "_configured_document_source_type", lambda: "document")
        headings = search_service.SearchService().list_section_headings
        assert await headings("outline", "doc.md", section="설치_%", limit=2) == ["설치_%", "설치_%/설정"]
        assert await headings("outline", "doc.md", section="설치_%/설정") == ["설치_%/설정", "설치_%/설정/값"]
        assert await headings("outline", "doc.md", section="# API") == [
            "# API", "# API > ## Setup", "# API > ## Setup v2",
        ]
        assert await headings("outline", "doc.md", section="# API > ## Setup") == ["# API > ## Setup"]
        assert await headings("outline", "doc.md", section="missing") == []
