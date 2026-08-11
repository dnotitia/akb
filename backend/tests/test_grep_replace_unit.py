from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.exceptions import WriteBusyError
from app.services import search_service
from app.services.search_service import SearchService


def _rows(count: int, pattern: str = "TODO") -> list[dict]:
    return [
        {
            "doc_id": str(uuid.uuid4()),
            "vault": "test-vault",
            "path": f"docs/{index:03d}.md",
            "title": f"Document {index}",
            "metadata": {},
            "section_path": None,
            "content": f"{pattern} item {index}",
            "chunk_index": 0,
        }
        for index in range(count)
    ]


class _Connection:
    def __init__(self, rows: list[dict]):
        self.rows = rows

    async def fetch(self, *_args):
        return self.rows


class _Acquire:
    def __init__(self, connection: _Connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return None


class _Pool:
    def __init__(self, rows: list[dict]):
        self.connection = _Connection(rows)

    def acquire(self):
        return _Acquire(self.connection)


class _Documents:
    def __init__(self, rows: list[dict], *, fail_path: str | None = None):
        self.contents = {row["path"]: row["content"] for row in rows}
        self.fail_path = fail_path
        self.get_calls: list[str] = []
        self.update_calls: list[tuple[str, object]] = []

    async def get(self, _vault: str, path: str):
        self.get_calls.append(path)
        return SimpleNamespace(
            content=self.contents[path],
            current_commit=f"parent-{path}",
        )

    async def update(self, _vault: str, path: str, request, *, agent_id=None):
        self.update_calls.append((path, request))
        if path == self.fail_path:
            raise WriteBusyError("test-vault", 1.0)
        self.contents[path] = request.content
        return SimpleNamespace(
            commit_hash=f"commit-{path}",
            previous_commit=f"parent-{path}",
        )


@pytest.fixture
def legacy_grep(monkeypatch):
    async def configure(rows: list[dict]):
        pool = _Pool(rows)

        async def get_pool():
            return pool

        monkeypatch.setattr(search_service, "get_pool", get_pool)
        monkeypatch.setattr(
            search_service,
            "_configured_document_source_type",
            lambda: search_service.LEGACY_DOCUMENT_SOURCE,
        )
        return SearchService()

    return configure


@pytest.mark.asyncio
async def test_replace_applies_to_full_scope_while_limit_only_bounds_preview(legacy_grep):
    # Exercise the original hard ceiling directly: response limit cannot exceed
    # 50, but an explicitly budgeted write may cover a larger matching scope.
    rows = _rows(51)
    service = await legacy_grep(rows)
    documents = _Documents(rows)

    result = await service.grep(
        "TODO",
        vault="test-vault",
        replace="TODO(owner)",
        doc_service=documents,
        agent_id="tester",
        limit=1,
        max_replacements=51,
    )

    assert result["returned_docs"] == 1
    assert result["total_docs"] == 51
    assert result["truncated"] is True
    assert result["replaced_docs"] == 51
    assert result["replacement_complete"] is True
    assert len(result["replacements"]) == 51
    assert all(row["previous_commit"] for row in result["replacements"])
    assert all(content.startswith("TODO(owner)") for content in documents.contents.values())
    assert all(request.expected_commit == f"parent-{path}" for path, request in documents.update_calls)


@pytest.mark.asyncio
async def test_replace_budget_rejects_full_scope_before_reading_or_writing(legacy_grep):
    rows = _rows(3)
    service = await legacy_grep(rows)
    documents = _Documents(rows)

    result = await service.grep(
        "TODO",
        vault="test-vault",
        replace="done",
        doc_service=documents,
        agent_id="tester",
        limit=1,
        max_replacements=2,
    )

    assert result["code"] == "bulk_too_large"
    assert result["details"] == {
        "total_docs": 3,
        "max_replacements": 2,
        "writes_applied": 0,
    }
    assert result["replacement_complete"] is False
    assert result["replacements"] == []
    assert documents.get_calls == []
    assert documents.update_calls == []


@pytest.mark.asyncio
async def test_replace_failure_returns_committed_receipts_and_stops(legacy_grep):
    rows = _rows(3)
    service = await legacy_grep(rows)
    documents = _Documents(rows, fail_path=rows[1]["path"])

    result = await service.grep(
        "TODO",
        vault="test-vault",
        replace="done",
        doc_service=documents,
        agent_id="tester",
        limit=1,
        max_replacements=3,
    )

    assert result["code"] == "write_busy"
    assert result["replacement_complete"] is False
    assert result["replaced_docs"] == 1
    assert result["replacements"][0]["previous_commit"] == f"parent-{rows[0]['path']}"
    assert result["details"]["failed_uri"].endswith("/doc/001.md")
    assert result["details"]["committed_replacements"] == 1
    assert [path for path, _request in documents.update_calls] == [
        rows[0]["path"],
        rows[1]["path"],
    ]
