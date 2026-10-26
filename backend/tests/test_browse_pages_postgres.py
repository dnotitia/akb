"""Bounded browse selects identities before reading verified Native bodies."""

import asyncio
import base64
import json
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import asyncpg
import pytest

from app.exceptions import ValidationError
from app.services.native_document_service import NativeDocumentService
from app.services.native_revision_service import NativeRevisionService
from tests.test_event_tail_postgres import _database_dsn, _fresh_database, _make_vault

pytestmark = pytest.mark.asyncio


async def _seed(pool, vault, paths):
    native = NativeRevisionService(pool)
    for index, path in enumerate(paths):
        status = "archived" if index % 3 == 0 else "active"
        await native.create_text(
            namespace_id=vault, surface="document", actor="writer", path=path,
            payload=f"---\ntitle: Item {index}\nstatus: {status}\n---\nbody {index}",
            mutation_id=uuid.uuid4(),
        )


async def test_pages_are_stable_bounded_and_do_not_hydrate_the_whole_vault():
    async with _fresh_database() as pool:
        vault = await _make_vault(pool, "browse-pages")
        await _seed(pool, vault, [f"guides/{i:03}.md" for i in range(13)])
        facade = NativeDocumentService(pool=pool)
        native = await facade._native()
        sizes = []
        verify = NativeRevisionService._verify_head_rows

        def observe(rows, stores):
            sizes.append(len(rows))
            return verify(rows, stores)

        items, cursor = [], None
        with patch.object(facade, "_native", return_value=native), patch.object(
            NativeRevisionService, "_verify_head_rows", side_effect=observe,
        ):
            for _ in range(10):
                page = await facade.browse(
                    "browse-pages", depth=-1, page_size=4, cursor=cursor,
                    archive_scope="all", include_hashes=True,
                )
                assert page.scanned <= 4
                assert len(page.items) <= 4
                items.extend(page.items)
                cursor = page.next_cursor
                assert page.complete is (cursor is None)
                if cursor is None:
                    break
        assert len(items) == 13
        assert [item.path for item in items] == [f"guides/{i:03}.md" for i in range(13)]
        assert all(item.content_hash and item.current_commit for item in items)
        assert max(sizes) <= 4


async def test_empty_filtered_page_still_has_a_continuation():
    async with _fresh_database() as pool:
        vault = await _make_vault(pool, "browse-filtered")
        await _seed(pool, vault, ["a.md", "b.md", "c.md", "d.md"])
        facade = NativeDocumentService(pool=pool)
        first = await facade.browse("browse-filtered", page_size=1, archive_scope="unarchived")
        assert first.items == []  # archived, not a real empty Vault
        assert first.complete is False and first.next_cursor
        second = await facade.browse(
            "browse-filtered", page_size=1, cursor=first.next_cursor,
            archive_scope="unarchived",
        )
        assert [item.path for item in second.items] == ["b.md"]


async def test_cursor_is_bound_to_vault_scope_and_options():
    async with _fresh_database() as pool:
        vault = await _make_vault(pool, "browse-cursor")
        await _make_vault(pool, "browse-other")
        await _seed(pool, vault, ["a.md", "b.md"])
        facade = NativeDocumentService(pool=pool)
        page = await facade.browse("browse-cursor", page_size=1)
        for options in (
            {"vault": "browse-other"}, {"vault": "browse-cursor", "depth": -1},
            {"vault": "browse-cursor", "archive_scope": "all"},
        ):
            with pytest.raises(ValidationError, match="cursor"):
                await facade.browse(**options, page_size=1, cursor=page.next_cursor)


async def test_depth_zero_scopes_collections_too_and_escapes_prefixes():
    async with _fresh_database() as pool:
        vault = await _make_vault(pool, "browse-depth")
        async with pool.acquire() as conn:
            for path in ("a_b", "a_b/child", "a_b/child/deep", "axb/other"):
                await conn.execute(
                    "INSERT INTO collections(vault_id,path,name) VALUES($1,$2,$2)", vault, path,
                )
        await _seed(pool, vault, ["a_b/root.md", "a_b/child/deep.md", "axb/root.md"])
        page = await NativeDocumentService(pool=pool).browse(
            "browse-depth", collection="a_b", depth=0, page_size=10, archive_scope="all",
        )
        assert [(item.type, item.path) for item in page.items] == [
            ("collection", "a_b/child"), ("document", "a_b/root.md"),
        ]
        assert page.complete is True


async def test_page_rejects_malformed_cursor_and_invalid_sizes():
    async with _fresh_database() as pool:
        await _make_vault(pool, "browse-invalid")
        facade = NativeDocumentService(pool=pool)
        for size in (0, -1, 201, True):
            with pytest.raises(ValidationError):
                await facade.browse("browse-invalid", page_size=size)
        with pytest.raises(ValidationError, match="cursor"):
            await facade.browse("browse-invalid", page_size=1, cursor="not-a-cursor")


async def test_wrong_json_cursor_field_types_are_validation_errors():
    async with _fresh_database() as pool:
        vault = await _make_vault(pool, "browse-cursor-types")
        await _seed(pool, vault, ["a.md", "b.md"])
        facade = NativeDocumentService(pool=pool)
        page = await facade.browse("browse-cursor-types", page_size=1, archive_scope="all")
        raw = json.loads(base64.urlsafe_b64decode(page.next_cursor + "=" * (-len(page.next_cursor) % 4)))
        for value in (1, {}, [], None):
            encoded = base64.urlsafe_b64encode(json.dumps([*raw[:-1], value]).encode()).decode()
            with pytest.raises(ValidationError, match="cursor"):
                await facade.browse("browse-cursor-types", page_size=1, cursor=encoded, archive_scope="all")
        for path in ("bad\x00path", "bad\ud800path"):
            encoded = base64.urlsafe_b64encode(json.dumps([*raw[:3], path, raw[-1]]).encode()).decode()
            with pytest.raises(ValidationError, match="cursor"):
                await facade.browse("browse-cursor-types", page_size=1, cursor=encoded, archive_scope="all")


async def test_body_byte_budget_pages_without_skipping_or_dropping_integrity(monkeypatch):
    from app.services import browse_pages

    async with _fresh_database() as pool:
        vault = await _make_vault(pool, "browse-byte-budget")
        await _seed(pool, vault, ["a.md", "b.md", "c.md"])
        # Each fixture fits, but two together exceed this small test budget.
        # Normal Documents have a 10 MiB admission ceiling.
        monkeypatch.setattr(browse_pages, "MAX_PAGE_BODY_BYTES", 60)
        facade = NativeDocumentService(pool=pool)
        items, cursor = [], None
        for _ in range(4):
            page = await facade.browse("browse-byte-budget", page_size=100, cursor=cursor, archive_scope="all")
            assert page.scanned == 1
            items.extend(item.path for item in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
        assert items == ["a.md", "b.md", "c.md"]
        monkeypatch.setattr(browse_pages, "MAX_PAGE_BODY_BYTES", 1)
        with pytest.raises(ValidationError, match="exceeds the browse body budget"):
            await facade.browse("browse-byte-budget", page_size=100, archive_scope="all")


async def test_legacy_hash_repair_uses_the_existing_connection(monkeypatch):
    from app.services import document_service

    async with _fresh_database() as pool:
        vault = await _make_vault(pool, "browse-legacy")
        async with pool.acquire() as conn:
            database = await conn.fetchval("SELECT current_database()")
            await conn.execute(
                "INSERT INTO documents(vault_id,path,title,current_commit) VALUES($1,'a.md','A',$2)", vault, "a" * 40,
            )
        single = await asyncpg.create_pool(_database_dsn(database), min_size=1, max_size=1)
        try:
            async def get_pool():
                return single

            monkeypatch.setattr(document_service, "get_pool", get_pool)
            facade = document_service.DocumentService(git=SimpleNamespace(read_file=lambda *args: "body"))
            async with asyncio.timeout(3):
                page = await facade.browse("browse-legacy", page_size=1, include_hashes=True)
            assert page.complete is True
            assert page.items[0].content_hash and page.items[0].current_commit == "a" * 40
        finally:
            await single.close()


async def test_byte_guard_and_hydration_pin_the_same_head(monkeypatch):
    from app.services import browse_pages

    async with _fresh_database() as pool:
        vault = await _make_vault(pool, "browse-head-snapshot")
        await _seed(pool, vault, ["a.md", "b.md"])
        native = NativeRevisionService(pool)
        old = next(s for s in await native.list_live_head_snapshots(namespace_id=vault, surface="document") if s.path == "a.md")
        original = browse_pages._hydrate
        new_head = None

        async def concurrent_replace(*args, **kwargs):
            nonlocal new_head
            new_head = await native.replace_text(
                namespace_id=vault, surface="document", actor="writer", path=old.path,
                payload="---\ntitle: Changed\n---\n" + "x" * 2_000,
                expected_revision_id=old.revision_id, expected_resource_id=old.resource_id,
                mutation_id=uuid.uuid4(),
            )
            return await original(*args, **kwargs)

        monkeypatch.setattr(browse_pages, "MAX_PAGE_BODY_BYTES", 200)
        monkeypatch.setattr(browse_pages, "_hydrate", concurrent_replace)
        page = await NativeDocumentService(pool=pool).browse(
            "browse-head-snapshot", page_size=10, archive_scope="all", include_hashes=True,
        )
        first = next(item for item in page.items if item.path == "a.md")
        assert first.current_commit == old.revision_id
        assert first.name == "Item 0"
        assert new_head.revision_id != old.revision_id


async def test_mixed_kind_pages_keep_duplicate_names_and_hide_unconfirmed_files():
    async with _fresh_database() as pool:
        vault = await _make_vault(pool, "browse-mixed")
        async with pool.acquire() as conn:
            collection = await conn.fetchval(
                "INSERT INTO collections(vault_id,path,name) VALUES($1,'guides','Guides') RETURNING id", vault,
            )
            await conn.execute(
                "INSERT INTO vault_tables(vault_id,collection_id,name) VALUES($1,$2,'same')", vault, collection,
            )
            for index, (kind, state) in enumerate((("file", "confirmed"), ("file", "confirmed"), ("file", "pending"), ("attachment", "confirmed"))):
                await conn.execute(
                    "INSERT INTO vault_files(vault_id,collection_id,kind,upload_state,name,s3_key) VALUES($1,$2,$3,$4,'same',$5)",
                    vault, collection, kind, state, f"key-{index}",
                )
        await _seed(pool, vault, ["guides/same.md"])
        facade = NativeDocumentService(pool=pool)
        cursor, seen = None, []
        for _ in range(8):
            page = await facade.browse("browse-mixed", depth=-1, page_size=1, cursor=cursor, archive_scope="all")
            seen.extend(page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
        assert [item.type for item in seen] == ["collection", "document", "table", "file", "file"]
        assert len({item.uri for item in seen}) == 5
        assert seen[0].doc_count is None  # no stale Native legacy counter
        assert seen[2].row_count is None  # no hidden exact COUNT or fabricated zero
        scoped = await facade.browse("browse-mixed", depth=-1, page_size=10, content_type="files")
        assert len(scoped.items) == 2 and all(item.type == "file" for item in scoped.items)


async def test_name_search_finds_current_native_titles_after_empty_pages():
    async with _fresh_database() as pool:
        vault = await _make_vault(pool, "browse-name")
        await _seed(pool, vault, ["a.md", "b.md", "c.md"])
        native = NativeRevisionService(pool)
        await native.replace_text(
            namespace_id=vault, surface="document", path="c.md", actor="writer",
            payload="---\ntitle: 마지막 Needle 제목\nstatus: active\n---\nbody",
            mutation_id=uuid.uuid4(), summary="An activity message, not the title",
        )
        facade = NativeDocumentService(pool=pool)
        cursor, seen, empty = None, [], 0
        for _ in range(4):
            page = await facade.browse(
                "browse-name", depth=-1, content_type="resources", page_size=1,
                name_query=" needle ", cursor=cursor, archive_scope="all",
            )
            assert page.name_query == "needle" and page.scanned <= 1
            empty += not page.items
            seen.extend(item.name for item in page.items)
            cursor = page.next_cursor
            if page.complete:
                assert cursor is None
                break
            assert cursor
        assert seen == ["마지막 Needle 제목"] and empty == 2
        old = await facade.browse("browse-name", content_type="resources", page_size=100, name_query="Item 2", archive_scope="all")
        assert old.complete and old.items == []


async def test_name_search_cursor_is_bound_and_resource_pages_exclude_collections():
    async with _fresh_database() as pool:
        vault = await _make_vault(pool, "browse-name-cursor")
        await _seed(pool, vault, ["a.md", "b.md"])
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO collections(vault_id,path,name) VALUES($1,'empty','Empty')", vault)
        facade = NativeDocumentService(pool=pool)
        page = await facade.browse("browse-name-cursor", content_type="resources", page_size=1, name_query="missing")
        assert page.items == [] and page.next_cursor
        with pytest.raises(ValidationError, match="cursor"):
            await facade.browse("browse-name-cursor", content_type="resources", page_size=1, name_query="different", cursor=page.next_cursor)
        all_resources = await facade.browse("browse-name-cursor", content_type="resources", page_size=10, archive_scope="all")
        assert [item.type for item in all_resources.items] == ["document", "document"]


async def test_catalog_name_search_filters_before_limit_and_escapes_wildcards(monkeypatch):
    from app.services import document_service

    async with _fresh_database() as pool:
        vault = await _make_vault(pool, "browse-literal-name")
        async with pool.acquire() as conn:
            for path, title in (("a.md", "Other"), ("b.md", "Literal 50%_\\ title"), ("c.md", "Literal 50abc title")):
                await conn.execute("INSERT INTO documents(vault_id,path,title) VALUES($1,$2,$3)", vault, path, title)
            await conn.execute("INSERT INTO collections(vault_id,path,name) VALUES($1,'slug','Human name')", vault)
        async def get_pool():
            return pool
        monkeypatch.setattr(document_service, "get_pool", get_pool)
        facade = document_service.DocumentService(git=SimpleNamespace())
        page = await facade.browse("browse-literal-name", content_type="resources", page_size=1, name_query="50%_\\")
        assert [item.path for item in page.items] == ["b.md"] and page.complete
        collection = await facade.browse("browse-literal-name", content_type="collections", page_size=1, name_query="human")
        assert [item.path for item in collection.items] == ["slug"] and collection.complete
