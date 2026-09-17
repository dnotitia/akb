"""Frozen response contracts: read surfaces may gain keys, never lose one.

The payload-hygiene work removes *bytes* from `search` / `drill_down`
bodies and adds two identifying fields to a search hit. Nothing about it
is allowed to drop a key an existing consumer reads — the collector's
`extra="forbid"` models, the console, and the akb-client SDK types all
sit downstream. The baselines below were captured from
`9dbe627` (`main`, before this branch) and are the contract: every key
listed must still be produced, additions are fine, and a removal fails
here rather than at a consumer.
"""

from __future__ import annotations

import tempfile
import uuid

import pytest

from app.config import settings

# Importing the MCP server selects the process-scoped legacy DocumentService,
# which creates its git storage directory at module load.
settings.git_storage_path = tempfile.mkdtemp(prefix="akb-contract-snapshot-vaults-")

from app.models.document import (  # noqa: E402
    BrowseContext,
    BrowseItem,
    BrowseResponse,
    DocumentResponse,
    DrillDownResponse,
    DrillDownSection,
    GrepMatch,
    GrepReplacement,
    GrepResponse,
    GrepResult,
    SearchResponse,
    SearchResult,
)
from app.services import search_service as search_service_module  # noqa: E402
from app.services.search_service import SearchService  # noqa: E402
from mcp_server import server as mcp_server  # noqa: E402

# `asyncio_mode = "auto"` (backend/pyproject.toml) runs the async tests here;
# a module-level asyncio mark would also land on the sync model checks.


# ── models (REST + MCP share these shapes) ──────────────────────────

BASELINE_MODEL_FIELDS: dict[str, set[str]] = {
    "SearchResult": {
        "source_type", "uri", "vault", "path", "title", "status", "collection",
        "collection_summary", "vault_description", "doc_type", "summary",
        "tags", "score", "matched_section",
    },
    "SearchResponse": {
        "kind", "archive_scope", "query", "total", "returned", "total_matches",
        "truncated", "hint", "degraded", "degradation_reason", "results",
    },
    "BrowseItem": {
        "name", "path", "type", "uri", "summary", "collection", "doc_count",
        "doc_type", "status", "tags", "last_updated", "current_commit",
        "content_hash", "hash_algorithm", "row_count", "columns", "sql_name",
        "mime_type", "size_bytes", "etag", "storage_version", "version",
    },
    "BrowseContext": {"type", "uri", "name", "path", "summary", "description"},
    "BrowseResponse": {
        "kind", "vault", "archive_scope", "path", "context", "items", "hint",
    },
    "DrillDownSection": {"section_path", "content", "chunk_index"},
    "DrillDownResponse": {"kind", "uri", "sections"},
    "GrepMatch": {"section", "text"},
    "GrepResult": {
        "uri", "vault", "path", "title", "status", "resource_type", "revision",
        "content_hash", "payload_placement", "matches",
    },
    "GrepReplacement": {"uri", "path", "title", "commit", "error"},
    "GrepResponse": {
        "kind", "archive_scope", "pattern", "regex", "error", "returned_docs",
        "returned_matches", "total_docs", "total_matches", "truncated",
        "truncation", "hint", "results", "by_doc", "n_files", "files",
        "replace", "replaced_docs", "replacements",
    },
    "DocumentResponse": {
        "kind", "uri", "vault", "path", "title", "type", "status", "summary",
        "domain", "created_by", "created_by_name", "created_at", "updated_at",
        "current_commit", "content_hash", "hash_algorithm", "tags", "content",
        "is_public", "public_slug", "metadata_is_current",
    },
}

MODELS = {
    "SearchResult": SearchResult,
    "SearchResponse": SearchResponse,
    "BrowseItem": BrowseItem,
    "BrowseContext": BrowseContext,
    "BrowseResponse": BrowseResponse,
    "DrillDownSection": DrillDownSection,
    "DrillDownResponse": DrillDownResponse,
    "GrepMatch": GrepMatch,
    "GrepReplacement": GrepReplacement,
    "GrepResult": GrepResult,
    "GrepResponse": GrepResponse,
    "DocumentResponse": DocumentResponse,
}


@pytest.mark.parametrize("name", sorted(BASELINE_MODEL_FIELDS))
def test_response_model_keeps_every_baseline_field(name):
    missing = BASELINE_MODEL_FIELDS[name] - set(MODELS[name].model_fields)
    assert not missing, f"{name} dropped {sorted(missing)}"


def test_search_hit_additions_are_the_only_change():
    added = set(SearchResult.model_fields) - BASELINE_MODEL_FIELDS["SearchResult"]
    assert added == {"section_path", "chunk_index"}


def test_a_hit_serialises_every_baseline_key_even_when_the_new_ones_are_null():
    # Declaring a field is not the same as emitting it. A hit whose chunk row
    # is gone carries `section_path=None, chunk_index=None`, and those keys
    # must still appear — a consumer reading `"chunk_index" in hit` should not
    # get a different answer depending on whether the lookup succeeded.
    dumped = SearchResult(
        source_type="document",
        uri="akb://v/doc/a.md",
        vault="v",
        path="a.md",
        title="A",
        score=0.5,
        section_path=None,
        chunk_index=None,
    ).model_dump()

    assert BASELINE_MODEL_FIELDS["SearchResult"] <= set(dumped)
    assert dumped["section_path"] is None
    assert dumped["chunk_index"] is None
    assert set(dumped) == set(SearchResult.model_fields)


# ── service payloads assembled in code, not by a model ──────────────

BASELINE_DRILL_DOWN_SECTION = {"section_path", "content", "chunk_index"}
BASELINE_GREP_RESPONSE = {
    "pattern", "regex", "returned_docs", "returned_matches", "total_docs",
    "total_matches", "truncated", "results",
}
BASELINE_GREP_RESULT = {"uri", "vault", "path", "title", "matches"}
BASELINE_OUTLINE_RESPONSE = {"uri", "outline", "returned", "total"}
BASELINE_SECTIONS_RESPONSE = {"uri", "sections", "returned", "hint"}
BASELINE_EMPTY_SECTIONS_RESPONSE = {
    "uri", "sections", "returned", "outline", "truncated", "hint",
}


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


def _with_rows(monkeypatch, rows: list[dict]) -> SearchService:
    pool = _Pool(rows)

    async def get_pool():
        return pool

    monkeypatch.setattr(search_service_module, "get_pool", get_pool)
    monkeypatch.setattr(
        search_service_module,
        "_configured_document_source_type",
        lambda: search_service_module.LEGACY_DOCUMENT_SOURCE,
    )
    return SearchService()


async def test_drill_down_section_rows_keep_their_baseline_keys(monkeypatch):
    service = _with_rows(monkeypatch, [{
        "section_path": "# Guide > ## Section",
        "content": "[# Guide > ## Section]\nBody.",
        "chunk_index": 0,
        "doc_id": uuid.uuid4(),
    }])

    (section,) = await service.drill_down("project-akb", "meta/welcome.md")

    assert BASELINE_DRILL_DOWN_SECTION <= set(section)


async def test_grep_response_keeps_its_baseline_keys(monkeypatch):
    service = _with_rows(monkeypatch, [{
        "doc_id": str(uuid.uuid4()),
        "vault": "project-akb",
        "path": "meta/welcome.md",
        "title": "Welcome",
        "metadata": {},
        "section_path": None,
        "content": "TODO: route the intent",
        "chunk_index": 0,
    }])

    response = await service.grep("TODO", vault="project-akb")

    assert BASELINE_GREP_RESPONSE <= set(response)
    assert BASELINE_GREP_RESULT <= set(response["results"][0])


# ── MCP akb_drill_down envelopes ────────────────────────────────────


@pytest.fixture
def drill_down_handler(monkeypatch):
    async def allow(*_args, **_kwargs):
        return None

    monkeypatch.setattr(mcp_server, "check_vault_access", allow)

    def configure(*, sections: list[dict], headings: list[str]):
        async def fake_drill_down(_vault, _doc, section=None):
            return list(sections)

        async def fake_headings(_vault, _doc, limit=None):
            return list(headings if limit is None else headings[:limit])

        monkeypatch.setattr(
            mcp_server.search_service, "drill_down", fake_drill_down
        )
        monkeypatch.setattr(
            mcp_server.search_service, "list_section_headings", fake_headings
        )
        return mcp_server._handle_drill_down

    return configure


URI = "akb://project-akb/coll/meta/doc/welcome.md"


async def test_outline_envelope_keeps_its_baseline_keys(drill_down_handler):
    handler = drill_down_handler(sections=[], headings=["# A", "# B"])

    response = await handler({"uri": URI, "mode": "outline"}, "user-1", None)

    assert BASELINE_OUTLINE_RESPONSE <= set(response)
    assert response["outline"] == ["# A", "# B"]
    assert response["returned"] == 2


async def test_sections_envelope_keeps_its_baseline_keys(drill_down_handler):
    handler = drill_down_handler(
        sections=[{
            "section_path": "# A",
            "content": "Body.",
            "chunk_index": 0,
        }],
        headings=["# A"],
    )

    response = await handler({"uri": URI}, "user-1", None)

    assert BASELINE_SECTIONS_RESPONSE <= set(response)
    assert BASELINE_DRILL_DOWN_SECTION <= set(response["sections"][0])


async def test_empty_match_envelope_keeps_its_baseline_keys(drill_down_handler):
    handler = drill_down_handler(sections=[], headings=["# A", "# B"])

    response = await handler({"uri": URI, "section": "Nope"}, "user-1", None)

    assert BASELINE_EMPTY_SECTIONS_RESPONSE <= set(response)
    assert response["sections"] == []


async def test_the_outline_cap_still_lines_up_with_the_probe(drill_down_handler):
    # The handler asks for OUTLINE_CAP + 1 headings to learn whether the
    # outline was truncated without paying for a full count. Now that the
    # service collapses duplicates before the limit, that probe has to keep
    # meaning "more than 50 distinct headings exist".
    headings = [f"# H{i:03d}" for i in range(51)]
    handler = drill_down_handler(sections=[], headings=headings)

    response = await handler({"uri": URI, "mode": "outline"}, "user-1", None)

    assert len(response["outline"]) == 50
    assert response["returned"] == 50
    assert response["truncated"] is True
    assert "total" not in response  # unknown once truncated
    assert response["outline"] == headings[:50]


async def test_exactly_the_cap_is_not_reported_as_truncated(drill_down_handler):
    headings = [f"# H{i:03d}" for i in range(50)]
    handler = drill_down_handler(sections=[], headings=headings)

    response = await handler({"uri": URI, "mode": "outline"}, "user-1", None)

    assert len(response["outline"]) == 50
    assert response["total"] == 50
    assert "truncated" not in response
