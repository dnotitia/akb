"""`akb_drill_down(mode='outline')` lists headings, not chunks.

A section longer than `MAX_CHUNK_SIZE` is stored as several chunks that
all carry the same `section_path`. The outline used to emit one row per
chunk, so a long section appeared three or four times and the 50-heading
cap in `mcp_server.server` was spent on repeats instead of on headings
the agent had not seen yet.
"""

from __future__ import annotations

import pytest

from app.services import search_service
from app.services.search_service import SearchService

pytestmark = pytest.mark.asyncio


class _Connection:
    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.sql: str | None = None
        self.params: tuple = ()

    async def fetch(self, sql: str, *params):
        self.sql = sql
        self.params = params
        return self.rows


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


@pytest.fixture
def outline(monkeypatch):
    def configure(rows: list[dict]):
        connection = _Connection(rows)

        async def get_pool():
            return _Pool(connection)

        monkeypatch.setattr(search_service, "get_pool", get_pool)
        return SearchService(), connection

    return configure


async def test_a_three_chunk_section_is_one_outline_row(outline):
    service, _ = outline([
        {"section_path": "# Guide > ## Long section", "first_chunk_index": 0},
        {"section_path": "# Guide > ## Long section", "first_chunk_index": 1},
        {"section_path": "# Guide > ## Long section", "first_chunk_index": 2},
    ])

    assert await service.list_section_headings("v", "d-1") == [
        "# Guide > ## Long section",
    ]


async def test_headings_keep_first_occurrence_order(outline):
    service, _ = outline([
        {"section_path": "# Guide"},
        {"section_path": "# Guide > ## A"},
        {"section_path": "# Guide > ## A"},
        {"section_path": "# Guide > ## B"},
        {"section_path": "# Guide > ## A"},
    ])

    assert await service.list_section_headings("v", "d-1") == [
        "# Guide",
        "# Guide > ## A",
        "# Guide > ## B",
    ]


async def test_blank_section_paths_are_dropped(outline):
    service, _ = outline([
        {"section_path": None},
        {"section_path": ""},
        {"section_path": "# Guide"},
    ])

    assert await service.list_section_headings("v", "d-1") == ["# Guide"]


async def test_the_limit_bounds_headings_not_chunks(outline):
    service, connection = outline([{"section_path": "# Guide"}])

    await service.list_section_headings("v", "d-1", limit=51)

    sql = connection.sql or ""
    # The cap can only count headings if the collapse happens before it.
    assert "GROUP BY c.section_path" in sql
    assert sql.index("GROUP BY c.section_path") < sql.index("LIMIT 51")
    # Blank headings must not consume a slot either.
    assert "c.section_path <> ''" in sql


async def test_no_limit_emits_no_limit_clause(outline):
    service, connection = outline([{"section_path": "# Guide"}])

    await service.list_section_headings("v", "d-1")

    assert "LIMIT" not in (connection.sql or "")
