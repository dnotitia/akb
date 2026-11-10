"""The timestamps used for discovery follow the current document read contract."""
import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
import uuid

import asyncpg
from pgvector.asyncpg import register_vector
import pytest

from app.exceptions import ValidationError
from app.models.document import DocumentPutRequest, DocumentUpdateRequest
from app.repositories.document_repo import DocumentRepository
from app.services import search_service as search_module
from app.services.document_service import DocumentService
from app.services.git_service import GitService
from app.services.index_service import chunk_markdown, write_source_chunks
from app.services.knowledge_io import import_bundle
from app.services.native_derived_worker import NativeDerivedWorker
from app.services.native_document_service import NativeDocumentService
from app.services.native_revision_service import NativeRevisionService
from app.services.search_filters import DocumentTimeRange
from app.services.vector_store import VectorHit, VectorStoreUnavailable
from app.services.vector_store.pgvector import PgvectorStore
from tests.test_event_tail_postgres import _database_dsn, _fresh_database, _make_vault

pytestmark = pytest.mark.asyncio


def _document_service(native, pool, tmp_path, *vault_names):
    if native:
        return NativeDocumentService(pool=pool)
    git = GitService(storage_path=str(tmp_path / "vaults"))
    for name in vault_names:
        git.init_vault(name)
    return DocumentService(git=git)


@pytest.mark.parametrize("native", [False, True], ids=["bare-git", "native"])
async def test_document_timestamps_across_lifecycle(native, tmp_path):
    vault = "time-contract"
    async with _fresh_database() as pool:
        vault_id = await _make_vault(pool, vault)
        service = _document_service(native, pool, tmp_path, vault)

        start = datetime.now(UTC)
        # OKF carries a source timestamp; importing creates an AKB document.
        bundle = {"notes/proof.md": "---\ntitle: Import\ntimestamp: 2001-02-03T00:00:00Z\n---\nfirst"}
        imported = await import_bundle(vault, bundle, actor_id="writer", doc_service=service)
        assert imported["created"] == 1, imported
        first = await service.get(vault, "notes/proof.md")
        assert start <= first.created_at <= first.updated_at <= datetime.now(UTC)
        body = await service.update(vault, first.path, DocumentUpdateRequest(
            content="second", expected_commit=first.current_commit,
        ), agent_id="writer")
        edited = await service.get(vault, body.path)
        await service.update(vault, edited.path, DocumentUpdateRequest(tags=["tag"]), agent_id="writer")
        tagged = await service.get(vault, edited.path)
        move = await service.move(vault, tagged.path, slug="renamed", agent_id="writer")
        moved = await service.get(vault, move.path)
        assert first.created_at == edited.created_at == tagged.created_at == moved.created_at
        assert first.updated_at < edited.updated_at < tagged.updated_at < moved.updated_at

        if native:
            await NativeDerivedWorker(pool).settle(namespace_id=vault_id, timeout_seconds=10)
            row = await pool.fetchrow("SELECT created_at, updated_at FROM native_resources WHERE namespace_id=$1 AND current_path=$2",
                                      vault_id, move.path)
            assert moved.updated_at == row["updated_at"]
        else:
            row = await pool.fetchrow("SELECT id, created_at, updated_at FROM documents WHERE vault_id=$1 AND path=$2",
                                      vault_id, move.path)
            assert (moved.created_at, moved.updated_at) == (row["created_at"], row["updated_at"])
            async with pool.acquire() as conn, conn.transaction():
                await write_source_chunks(conn, "document", str(row["id"]), vault_id=vault_id, chunks=chunk_markdown(moved.content))
        refreshed = await service.get(vault, move.path)
        assert (refreshed.created_at, refreshed.updated_at, refreshed.current_commit) == (
            moved.created_at, moved.updated_at, moved.current_commit,
        )


@pytest.mark.parametrize("native", [False, True], ids=["bare-git", "native"])
async def test_time_candidates_precede_pgvector_prefetch_and_intersect_acl(native, tmp_path, monkeypatch):
    vault = "time-ranking"
    async with _fresh_database() as pool:
        visible = await _make_vault(pool, vault)
        private = await _make_vault(pool, "private")
        reader = await pool.fetchval(
            "INSERT INTO users(username,email,password_hash) VALUES ('reader','reader@example.invalid','disabled') RETURNING id"
        )
        await pool.execute("UPDATE vaults SET owner_id=$1 WHERE id=$2", reader, visible)
        service = _document_service(native, pool, tmp_path, vault, "private")
        source = "native_document" if native else "document"
        monkeypatch.setattr(search_module, "_configured_document_source_type", lambda: source)
        monkeypatch.setattr(search_module.settings, "search_prefetch", 0)
        paths = []
        for i in range(53):  # More better-scoring documents than the driver's prefetch of 50.
            result = await service.put(DocumentPutRequest(
                vault=vault, collection="", title=f"Old {i}", content="older match",
            ))
            paths.append(result.uri)
        selected = await service.put(DocumentPutRequest(vault=vault, collection="", title="In range", content="weaker"))
        current = await service.get(vault, selected.path)
        excluded = await service.put(DocumentPutRequest(vault=vault, collection="", title="Not requested", content="weaker"))
        hidden = await service.put(DocumentPutRequest(vault="private", collection="", title="Private", content="weaker"))
        if native:
            worker = NativeDerivedWorker(pool)
            for vid in (visible, private):
                await worker.settle(namespace_id=vid, timeout_seconds=30)

        # Use the real pgvector driver; only query feature generation is deterministic.
        await pool.execute("CREATE EXTENSION IF NOT EXISTS vector")
        database = await pool.fetchval("SELECT current_database()")
        index_pool = await asyncpg.create_pool(_database_dsn(database), min_size=2, max_size=4, init=register_vector)

        async def vector_pool():
            return index_pool

        store = PgvectorStore(dsn=None, schema="date_vectors", dense_dim=2, sparse_shape="arrays", get_main_pool=vector_pool)
        try:
            monkeypatch.setattr(search_module, "get_vector_store", lambda: store)
            monkeypatch.setattr(search_module, "generate_embeddings", AsyncMock(return_value=[[1.0, 0.0]]))
            monkeypatch.setattr(search_module.sparse_encoder, "encode_query", AsyncMock(return_value=([1], [1.0])))
            chunks = await pool.fetch("SELECT * FROM chunks WHERE source_type=$1", source)
            assert len(chunks) == 56
            for chunk in chunks:
                old = "older match" in chunk["content"]
                await store.upsert_one(
                    chunk_id=str(chunk["id"]), source_type=source, source_id=str(chunk["source_id"]),
                    vault_id=str(chunk["vault_id"]), content=chunk["content"], section_path=chunk["section_path"],
                    chunk_index=chunk["chunk_index"], dense=[1, 0] if old else [0, 1],
                    sparse_indices=[1] if old else [], sparse_values=[1.0] if old else [],
                )
            search = search_module.SearchService()
            args = dict(query="match", source_type="document", user_id=str(reader), limit=1, rerank=False)
            all_uris = [*paths, selected.uri, hidden.uri]
            baseline = await search.search(**args, source_uris=all_uris)
            assert baseline.results[0].uri in paths
            for field in ("created_at", "updated_at"):
                start = getattr(current, field)
                window = DocumentTimeRange(field=field, gte=start)
                page = await search.search(**args, source_uris=all_uris, time_range=window)
                assert [hit.uri for hit in page.results] == [selected.uri]
                assert page.total_matches == 1 and not page.degraded
                before = await search.search(**args, source_uris=[selected.uri], time_range=DocumentTimeRange(field=field, lt=start))
                assert before.results == []
                forbidden = await search.search(**args, source_uris=[hidden.uri], time_range=window)
                assert forbidden.total_matches == 0 and not forbidden.results
                visible_page = await search.search(**{**args, "limit": 10}, time_range=window)
                assert {hit.uri for hit in visible_page.results} == {selected.uri, excluded.uri}

            with monkeypatch.context() as small:
                small.setattr(search_module, "NATIVE_SEARCH_MAX_CANDIDATE_RESOURCES", 1)
                with pytest.raises(ValidationError, match="bounded candidate corpus"):
                    await search.search(**args, time_range=window)
            if native:
                with monkeypatch.context() as small:
                    small.setattr(search_module, "NATIVE_SEARCH_MAX_BODY_BYTES", 1)
                    with pytest.raises(ValidationError, match="bounded candidate corpus"):
                        await search.search(**args, source_uris=[selected.uri], time_range=window)

            with monkeypatch.context() as broken:
                unexpected = VectorHit(chunk_id=str(uuid.uuid4()), source_id=str(uuid.uuid4()), source_type=source,
                                       content="outside scope", section_path="", score=1)
                broken.setattr(search, "_run_vector_search", AsyncMock(return_value=([unexpected], None)))
                with pytest.raises(VectorStoreUnavailable, match="candidate scope"):
                    await search.search(**args, source_uris=[selected.uri], time_range=window)

            original_retrieval = search._run_vector_search

            async def concurrent_edit(**options):
                hits = await original_retrieval(**options)
                assert hits[0]
                await service.update(vault, selected.path, DocumentUpdateRequest(summary="changed"), agent_id="writer")
                return hits

            monkeypatch.setattr(search, "_run_vector_search", concurrent_edit)
            narrow = DocumentTimeRange(field="updated_at", gte=current.updated_at, lt=current.updated_at + timedelta(microseconds=1))
            changed = await search.search(**args, source_uris=[selected.uri], time_range=narrow)
            assert changed.results == []
            if not native:
                assert changed.excluded == {"time_range": 1}
        finally:
            await index_pool.close()


async def test_migrated_native_creation_uses_the_same_precedence_as_get():
    legacy_date = datetime(2003, 2, 1, tzinfo=UTC)
    authored_date = datetime(1998, 7, 6, tzinfo=UTC)
    async with _fresh_database() as pool:
        vault = await _make_vault(pool, "migration")
        native = NativeRevisionService(pool)
        facade = NativeDocumentService(pool=pool)
        for name, frontmatter in [("frozen", ""), ("authored", f"---\ncreated_at: {authored_date.isoformat()}\n---\n")]:
            doc = await native.create_text(namespace_id=vault, surface="document", path=f"{name}.md",
                                           payload=frontmatter + "body", actor="import", mutation_id=uuid.uuid4())
            await pool.execute(
                "INSERT INTO documents(id,vault_id,path,title,created_at) VALUES($1,$2,$3,$3,$4)",
                doc.resource_id, vault, doc.path, legacy_date,
            )
            current = await facade.get("migration", doc.path)
            assert current.created_at == (authored_date if name == "authored" else legacy_date)
            async with pool.acquire() as conn:
                for stamp, expected in [(legacy_date, name == "frozen"), (authored_date, name == "authored")]:
                    candidates, _ = await search_module.SearchService()._native_document_candidates(
                        conn, user_uuid=None, is_admin=True, vaults=["migration"], collection=None,
                        doc_type=None, tags=None, include_archived=True, source_uris=[current.uri],
                        time_range=DocumentTimeRange(field="created_at", gte=stamp, lt=stamp + timedelta(microseconds=1)),
                    )
                    assert candidates == ([str(doc.resource_id)] if expected else [])

        raw = await native.create_text(namespace_id=vault, surface="document", path="plain.md",
                                       payload="Plain body", actor="import", mutation_id=uuid.uuid4())
        plain = await facade.get("migration", raw.path)
        assert plain.created_at == await pool.fetchval(
            "SELECT created_at FROM native_resources WHERE resource_id=$1", raw.resource_id,
        )
        invalid = await native.create_text(namespace_id=vault, surface="document", path="local.md",
                                           payload="---\ncreated_at: '2000-01-01T12:00:00'\n---\nbody",
                                           actor="import", mutation_id=uuid.uuid4())
        async with pool.acquire() as conn:
            with pytest.raises(ValidationError, match="timezone-aware instant"):
                await search_module.SearchService()._native_document_candidates(
                    conn, user_uuid=None, is_admin=True, vaults=["migration"], collection=None, doc_type=None,
                    tags=None, include_archived=True, source_uris=[f"akb://migration/doc/{invalid.path}"],
                    time_range=DocumentTimeRange(field="created_at", gte=legacy_date),
                )


async def test_external_git_catalog_uses_first_and_last_sync_times():
    first = datetime(2004, 3, 1, tzinfo=UTC)
    second = first + timedelta(days=1)
    async with _fresh_database() as pool:
        vault = await _make_vault(pool, "mirror")
        repo = DocumentRepository(pool)
        args = dict(vault_id=vault, vault_name="mirror", collection_id=None, path="external.md",
                    external_path="external.md", title="Mirror", doc_type="note", tags=[], summary=None, domain=None,
                    metadata={"created_at": "1970-01-01"}, hash_algorithm="sha256")
        for index, now in enumerate((first, second)):
            identity, inserted = await repo.upsert_external(
                **args, now=now, external_blob=str(index) * 40, commit_hash=str(index) * 40, content_hash=str(index) * 64,
            )
            row = await repo.find_by_ref(vault, str(identity))
            assert inserted is (index == 0)
            assert (row["created_at"], row["updated_at"]) == (first, now)


async def test_date_candidate_deadline_and_cancellation_release_the_pool(monkeypatch):
    async with _fresh_database() as pool:
        await _make_vault(pool, "timeout")
        monkeypatch.setattr(search_module, "_configured_document_source_type", lambda: "document")
        monkeypatch.setattr(search_module, "generate_embeddings", AsyncMock(return_value=[]))
        monkeypatch.setattr(search_module.settings, "search_retrieval_timeout_secs", 0.2)
        args = dict(query="test", vault="timeout", source_type="document",
                    time_range=DocumentTimeRange(field="updated_at", gte=datetime.now(UTC)))
        service = search_module.SearchService()
        async with pool.acquire() as lock, lock.transaction():
            await lock.execute("LOCK TABLE documents IN ACCESS EXCLUSIVE MODE")
            with pytest.raises(TimeoutError):
                await service.search(**args)
            monkeypatch.setattr(search_module.settings, "search_retrieval_timeout_secs", 30)
            search = asyncio.create_task(service.search(**args))
            try:
                async with asyncio.timeout(5):
                    while not await lock.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE relation='documents'::regclass AND NOT granted)"
                    ):
                        await asyncio.sleep(0.01)
            finally:
                search.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await search
        assert (await asyncio.wait_for(service.search(**args), timeout=5)).results == []
