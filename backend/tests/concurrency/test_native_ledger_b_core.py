"""Focused real-PostgreSQL contract for the M1 B-core native ledger.

The reference payload table exists only to exercise ledger publication in M1;
these tests deliberately do not approve it as the final searchable-body store.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import pytest

from app.exceptions import ConflictError, NotFoundError, ValidationError
from app.models.document import DocumentPutRequest, DocumentUpdateRequest
from app.repositories.native_revision_repo import NativeRevisionRepository
from app.services.m1_reference_payload_store import M1ReferencePayloadStore
from app.services.native_document_service import (
    NativeDocumentService,
    NativeRevisionUnsupportedSurfaceError,
)
from app.services.native_revision_backend import NativeRevisionBackend
from app.services.native_revision_service import NativeRevisionService


pytestmark = pytest.mark.asyncio

_BACKEND = Path(__file__).resolve().parents[2]
_INIT_SQL = (_BACKEND / "app" / "db" / "init.sql").read_text()
_MIGRATION = _BACKEND / "app" / "db" / "migrations" / "048_native_revision_core.py"
_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:5433/akb",  # pragma: allowlist secret
)


async def _can_connect() -> bool:
    try:
        conn = await asyncpg.connect(_DSN, timeout=2)
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


def _database_dsn(name: str) -> str:
    base, _ = _DSN.rsplit("/", 1)
    return f"{base}/{name}"


def _load_migration():
    spec = importlib.util.spec_from_file_location("native_revision_migration", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@asynccontextmanager
async def _fresh_database():
    if not await _can_connect():
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail(f"REQUIRE_REAL_PG=1 but Postgres is not reachable at {_DSN}")
        pytest.skip(f"Postgres not reachable at {_DSN}")
    admin = await asyncpg.connect(_DSN)
    name = f"akb_native_{uuid.uuid4().hex[:12]}"
    await admin.execute(f'CREATE DATABASE "{name}"')
    dsn = _database_dsn(name)
    conn = await asyncpg.connect(dsn)
    pool = None
    try:
        await conn.execute(_INIT_SQL)
        migration = _load_migration()
        await migration.migrate(conn=conn)
        await migration.migrate(conn=conn)  # idempotent startup/retry
        await conn.close()
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=8)
        async with pool.acquire() as seeded:
            vault_id = await seeded.fetchval(
                "INSERT INTO vaults (name, git_path) VALUES ($1, $2) RETURNING id",
                f"native-{uuid.uuid4().hex}",
                "/tmp/legacy-unused.git",
            )
        yield pool, vault_id
    finally:
        if pool is not None:
            await pool.close()
        elif not conn.is_closed():
            await conn.close()
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


def _create_args(vault_id: uuid.UUID, *, mutation_id: uuid.UUID | None = None) -> dict:
    return {
        "namespace_id": vault_id,
        "surface": "document",
        "path": "notes/native.md",
        "payload": "alpha\n",
        "actor": "m1-test",
        "mutation_id": mutation_id or uuid.uuid4(),
        "message": "create native note",
        "subject": "native note",
        "summary": "first revision",
    }


async def _authority_counts(pool: asyncpg.Pool) -> dict[str, int]:
    tables = (
        "native_resources",
        "native_revisions",
        "native_payload_manifests",
        "native_revision_activity",
        "native_invalidation_intents",
    )
    async with pool.acquire() as conn:
        return {table: await conn.fetchval(f"SELECT count(*) FROM {table}") for table in tables}


async def test_create_get_replace_are_native_atomic_and_leave_legacy_projection_untouched():
    async with _fresh_database() as (pool, vault_id):
        async with pool.acquire() as conn:
            legacy_id = await conn.fetchval(
                """
                INSERT INTO documents
                    (vault_id, path, title, current_commit, created_by)
                VALUES ($1, $2, 'legacy', $3, 'legacy-owner')
                RETURNING id
                """,
                vault_id,
                "notes/native.md",
                "0123456789abcdef0123456789abcdef01234567",  # pragma: allowlist secret
            )

        service = NativeRevisionService(pool)
        created = await service.create_text(**_create_args(vault_id))

        assert len(created.revision_id) == 40
        assert created.revision_id == created.revision_id.lower()
        assert all(ch in "0123456789abcdef" for ch in created.revision_id)
        assert created.parent_revision_id is None
        assert created.action == "create"

        current = await service.get_current(
            namespace_id=vault_id,
            surface="document",
            path="notes/native.md",
        )
        assert current.resource_id == created.resource_id
        assert current.revision_id == created.revision_id
        assert current.payload_bytes == b"alpha\n"
        assert current.text == "alpha\n"
        assert current.digest == hashlib.sha256(b"alpha\n").hexdigest()

        replaced = await service.replace_text(
            namespace_id=vault_id,
            surface="document",
            path="notes/native.md",
            payload="beta\n",
            actor="m1-test",
            mutation_id=uuid.uuid4(),
            expected_revision_id=created.revision_id,
            message="replace native note",
        )
        assert replaced.resource_id == created.resource_id
        assert replaced.parent_revision_id == created.revision_id
        assert replaced.revision_id != created.revision_id

        current = await service.get_current(
            namespace_id=vault_id,
            surface="document",
            path="notes/native.md",
        )
        assert current.revision_id == replaced.revision_id
        assert current.text == "beta\n"

        async with pool.acquire() as conn:
            assert (
                await conn.fetchval("SELECT current_commit FROM documents WHERE id = $1", legacy_id)
                == "0123456789abcdef0123456789abcdef01234567"  # pragma: allowlist secret
            )
            rows = await conn.fetch(
                """
                SELECT r.revision_id, r.parent_revision_id, r.action,
                       m.digest, m.byte_size, m.selected_placement,
                       a.revision_id AS activity_revision,
                       i.revision_id AS intent_revision
                  FROM native_revisions r
                  JOIN native_payload_manifests m ON m.payload_manifest_id = r.payload_manifest_id
                  JOIN native_revision_activity a ON a.activity_event_id = r.activity_event_id
                  JOIN native_invalidation_intents i ON i.intent_id = r.invalidation_intent_id
                 WHERE r.resource_id = $1
                 ORDER BY r.occurred_at, r.revision_id
                """,
                created.resource_id,
            )
        assert len(rows) == 2
        assert {row["selected_placement"] for row in rows} == {"m1-reference-payload-v1"}
        assert all(row["revision_id"] == row["activity_revision"] == row["intent_revision"] for row in rows)


async def test_same_base_replace_has_one_winner_without_vault_write_lane():
    async with _fresh_database() as (pool, vault_id):
        service = NativeRevisionService(pool)
        created = await service.create_text(**_create_args(vault_id))

        async def replace(body: str):
            return await service.replace_text(
                namespace_id=vault_id,
                surface="document",
                path="notes/native.md",
                payload=body,
                actor="racer",
                mutation_id=uuid.uuid4(),
                expected_revision_id=created.revision_id,
            )

        outcomes = await asyncio.gather(replace("winner-a"), replace("winner-b"), return_exceptions=True)
        successes = [item for item in outcomes if not isinstance(item, Exception)]
        conflicts = [item for item in outcomes if isinstance(item, ConflictError)]
        assert len(successes) == 1
        assert len(conflicts) == 1

        current = await service.get_current(
            namespace_id=vault_id,
            surface="document",
            path="notes/native.md",
        )
        assert current.revision_id == successes[0].revision_id
        assert await _authority_counts(pool) == {
            "native_resources": 1,
            "native_revisions": 2,
            "native_payload_manifests": 2,
            "native_revision_activity": 2,
            "native_invalidation_intents": 2,
        }


async def test_same_path_create_has_one_winner_and_distinct_paths_do_not_share_a_vault_lock():
    async with _fresh_database() as (pool, vault_id):
        service = NativeRevisionService(pool)

        same_path = await asyncio.gather(
            service.create_text(**_create_args(vault_id)),
            service.create_text(**_create_args(vault_id)),
            return_exceptions=True,
        )
        assert len([item for item in same_path if not isinstance(item, Exception)]) == 1
        assert len([item for item in same_path if isinstance(item, ConflictError)]) == 1

        left = _create_args(vault_id)
        left["path"] = "notes/left.md"
        right = _create_args(vault_id)
        right["path"] = "notes/right.md"
        extra = await asyncio.gather(service.create_text(**left), service.create_text(**right))
        assert {item.path for item in extra} == {"notes/left.md", "notes/right.md"}


async def test_document_and_file_cannot_own_the_same_namespace_path():
    async with _fresh_database() as (pool, vault_id):
        service = NativeRevisionService(pool)
        document = _create_args(vault_id)
        file = _create_args(vault_id)
        file["surface"] = "file"

        outcomes = await asyncio.gather(
            service.create_text(**document),
            service.create_text(**file),
            return_exceptions=True,
        )

        assert len([item for item in outcomes if not isinstance(item, Exception)]) == 1
        assert len([item for item in outcomes if isinstance(item, ConflictError)]) == 1
        async with pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    """
                SELECT count(*) FROM native_resources
                 WHERE namespace_id = $1 AND current_path = 'notes/native.md'
                   AND lifecycle = 'live'
                """,
                    vault_id,
                )
                == 1
            )


async def test_create_retires_a_cross_surface_alias_before_claiming_its_path():
    async with _fresh_database() as (pool, vault_id):
        service = NativeRevisionService(pool)
        file_args = _create_args(vault_id)
        file_args["surface"] = "file"
        file_resource = await service.create_text(**file_args)
        file_move = await service.move_text(
            namespace_id=vault_id,
            surface="file",
            path="notes/native.md",
            path_to="archive/native.bin",
            actor="file-mover",
            mutation_id=uuid.uuid4(),
            expected_revision_id=file_resource.revision_id,
        )

        document = await service.create_text(**_create_args(vault_id))

        assert document.resource_id != file_resource.resource_id
        assert (
            await service.get_current_reference(
                namespace_id=vault_id,
                surface="document",
                reference="notes/native.md",
            )
        ).resource_id == document.resource_id
        assert (
            await service.get_current_reference(
                namespace_id=vault_id,
                surface="file",
                reference="archive/native.bin",
            )
        ).revision_id == file_move.revision_id
        with pytest.raises(NotFoundError):
            await service.get_current_reference(
                namespace_id=vault_id,
                surface="file",
                reference="notes/native.md",
            )
        async with pool.acquire() as conn:
            retired = await conn.fetchrow(
                """
                SELECT retired_revision_id
                  FROM native_resource_path_aliases
                 WHERE namespace_id = $1 AND old_path = 'notes/native.md'
                """,
                vault_id,
            )
        assert retired["retired_revision_id"] == document.revision_id


async def test_move_rejects_a_destination_alias_owned_by_another_surface():
    async with _fresh_database() as (pool, vault_id):
        service = NativeRevisionService(pool)
        file_args = _create_args(vault_id)
        file_args["surface"] = "file"
        file_resource = await service.create_text(**file_args)
        await service.move_text(
            namespace_id=vault_id,
            surface="file",
            path="notes/native.md",
            path_to="archive/native.bin",
            actor="file-mover",
            mutation_id=uuid.uuid4(),
            expected_revision_id=file_resource.revision_id,
        )
        document_args = _create_args(vault_id)
        document_args["path"] = "notes/document.md"
        document = await service.create_text(**document_args)

        with pytest.raises(ConflictError, match="alias already owns path"):
            await service.move_text(
                namespace_id=vault_id,
                surface="document",
                path="notes/document.md",
                path_to="notes/native.md",
                actor="document-mover",
                mutation_id=uuid.uuid4(),
                expected_revision_id=document.revision_id,
            )

        current = await service.get_current(
            namespace_id=vault_id,
            surface="document",
            path="notes/document.md",
        )
        assert current.revision_id == document.revision_id


async def test_waiting_unpinned_replacements_publish_monotonic_lineage_time():
    async with _fresh_database() as (pool, vault_id):
        created = await NativeRevisionService(pool).create_text(**_create_args(vault_id))
        first_holds_lock = asyncio.Event()
        release_first = asyncio.Event()

        async def first_failpoint(name: str) -> None:
            if name == "authority.after_manifest":
                first_holds_lock.set()
                await release_first.wait()

        first_service = NativeRevisionService(pool, failpoint=first_failpoint)
        first_task = asyncio.create_task(
            first_service.replace_text(
                namespace_id=vault_id,
                surface="document",
                path="notes/native.md",
                payload="first waiter",
                actor="first",
                mutation_id=uuid.uuid4(),
            )
        )
        await first_holds_lock.wait()
        second_task = asyncio.create_task(
            NativeRevisionService(pool).replace_text(
                namespace_id=vault_id,
                surface="document",
                path="notes/native.md",
                payload="second waiter",
                actor="second",
                mutation_id=uuid.uuid4(),
            )
        )
        await asyncio.sleep(0)
        release_first.set()
        first, second = await asyncio.gather(first_task, second_task)

        rows = await NativeRevisionRepository(pool).list_history(
            resource_id=created.resource_id,
            limit=10,
        )
        chronological = list(reversed(rows))
        assert [row["revision_id"] for row in chronological] == [
            created.revision_id,
            first.revision_id,
            second.revision_id,
        ]
        assert [row["occurred_at"] for row in chronological] == sorted(row["occurred_at"] for row in chronological)


async def test_public_unpinned_metadata_and_body_updates_recompute_and_serialize():
    async with _fresh_database() as (pool, vault_id):
        async with pool.acquire() as conn:
            vault = await conn.fetchval("SELECT name FROM vaults WHERE id = $1", vault_id)

        document_service = NativeDocumentService(pool=pool)
        created = await document_service.put(
            DocumentPutRequest(
                vault=vault,
                collection="notes",
                slug="public-race",
                title="Public race",
                content="created",
            ),
            agent_id="creator",
        )

        prepared_count = 0
        both_prepared = asyncio.Event()

        async def hold_after_prepare(name: str) -> None:
            nonlocal prepared_count
            if name != "payload.after_prepare_before_tx":
                return
            prepared_count += 1
            if prepared_count == 2:
                both_prepared.set()
            await both_prepared.wait()

        native = NativeRevisionService(pool, failpoint=hold_after_prepare)

        async def injected_native() -> NativeRevisionService:
            return native

        document_service._native = injected_native  # type: ignore[method-assign]
        first, second = await asyncio.gather(
            document_service.update(
                vault,
                created.path,
                DocumentUpdateRequest(title="Updated title"),
                agent_id="first",
            ),
            document_service.update(
                vault,
                created.path,
                DocumentUpdateRequest(content="second"),
                agent_id="second",
            ),
        )

        rows = await NativeRevisionRepository(pool).list_history(
            resource_id=(
                await native.get_current(
                    namespace_id=vault_id,
                    surface="document",
                    path=created.path,
                )
            ).resource_id,
            limit=10,
        )
        chronological = list(reversed(rows))
        assert chronological[0]["revision_id"] == created.commit_hash
        assert {row["revision_id"] for row in chronological[1:]} == {
            first.commit_hash,
            second.commit_hash,
        }
        response_parents = {
            first.commit_hash: first.previous_commit,
            second.commit_hash: second.previous_commit,
        }
        assert response_parents == {row["revision_id"]: row["parent_revision_id"] for row in chronological[1:]}
        current = await document_service.get(vault, created.path)
        assert current.title == "Updated title"
        assert current.content == "second"


async def test_concurrent_unpinned_disjoint_edits_recompute_and_both_survive():
    async with _fresh_database() as (pool, vault_id):
        async with pool.acquire() as conn:
            vault = await conn.fetchval("SELECT name FROM vaults WHERE id = $1", vault_id)

        document_service = NativeDocumentService(pool=pool)
        created = await document_service.put(
            DocumentPutRequest(
                vault=vault,
                collection="notes",
                slug="edit-race",
                title="Edit race",
                content="alpha one\nbeta two",
            ),
            agent_id="creator",
        )
        prepared_count = 0
        both_prepared = asyncio.Event()

        async def hold_first_attempts(name: str) -> None:
            nonlocal prepared_count
            if name != "payload.after_prepare_before_tx":
                return
            prepared_count += 1
            if prepared_count == 2:
                both_prepared.set()
            await both_prepared.wait()

        native = NativeRevisionService(pool, failpoint=hold_first_attempts)

        async def injected_native() -> NativeRevisionService:
            return native

        document_service._native = injected_native  # type: ignore[method-assign]
        await asyncio.gather(
            document_service.edit(
                vault,
                created.path,
                "alpha",
                "ALPHA",
                agent_id="alpha-editor",
            ),
            document_service.edit(
                vault,
                created.path,
                "beta",
                "BETA",
                agent_id="beta-editor",
            ),
        )

        current = await document_service.get(vault, created.path)
        assert current.content == "ALPHA one\nBETA two"


@pytest.mark.parametrize("operation", ["update", "edit"])
async def test_unpinned_public_mutation_survives_more_than_four_successive_head_races(
    operation: str,
):
    async with _fresh_database() as (pool, vault_id):
        async with pool.acquire() as conn:
            vault = await conn.fetchval("SELECT name FROM vaults WHERE id = $1", vault_id)

        document_service = NativeDocumentService(pool=pool)
        created = await document_service.put(
            DocumentPutRequest(
                vault=vault,
                collection="notes",
                slug=f"many-races-{operation}",
                title="Many races",
                content="alpha body",
            ),
            agent_id="creator",
        )

        class SixRaceNativeService(NativeRevisionService):
            forced_races = 0

            async def replace_text(self, **kwargs):
                if kwargs["actor"] == "target" and self.forced_races < 6:
                    self.forced_races += 1
                    current = await self.get_current_reference(
                        namespace_id=kwargs["namespace_id"],
                        surface=kwargs["surface"],
                        reference=kwargs["path"],
                    )
                    await NativeRevisionService(pool).replace_text(
                        namespace_id=kwargs["namespace_id"],
                        surface=kwargs["surface"],
                        path=current.path,
                        payload=current.text,
                        actor=f"forced-racer-{self.forced_races}",
                        mutation_id=uuid.uuid4(),
                        expected_revision_id=current.revision_id,
                    )
                return await super().replace_text(**kwargs)

        native = SixRaceNativeService(pool)

        async def injected_native() -> NativeRevisionService:
            return native

        document_service._native = injected_native  # type: ignore[method-assign]
        if operation == "update":
            await document_service.update(
                vault,
                created.path,
                DocumentUpdateRequest(title="Eventually published"),
                agent_id="target",
            )
        else:
            await document_service.edit(
                vault,
                created.path,
                "alpha",
                "omega",
                agent_id="target",
            )

        assert native.forced_races == 6
        current = await document_service.get(vault, created.path)
        if operation == "update":
            assert current.title == "Eventually published"
            assert current.content == "alpha body"
        else:
            assert current.content == "omega body"


async def test_content_hash_only_update_retries_across_metadata_head_change():
    async with _fresh_database() as (pool, vault_id):
        async with pool.acquire() as conn:
            vault = await conn.fetchval("SELECT name FROM vaults WHERE id = $1", vault_id)

        target = NativeDocumentService(pool=pool)
        competitor = NativeDocumentService(pool=pool)
        created = await target.put(
            DocumentPutRequest(
                vault=vault,
                collection="notes",
                slug="hash-metadata-race",
                title="Original title",
                content="stable body",
            ),
            agent_id="creator",
        )
        original = await target.get(vault, created.path)

        class MetadataRaceNativeService(NativeRevisionService):
            raced = False

            async def replace_text(self, **kwargs):
                if kwargs["actor"] == "target" and not self.raced:
                    self.raced = True
                    await competitor.update(
                        vault,
                        created.path,
                        DocumentUpdateRequest(title="Concurrent metadata"),
                        agent_id="metadata-racer",
                    )
                return await super().replace_text(**kwargs)

        native = MetadataRaceNativeService(pool)

        async def injected_native() -> NativeRevisionService:
            return native

        target._native = injected_native  # type: ignore[method-assign]
        await target.update(
            vault,
            created.path,
            DocumentUpdateRequest(
                tags=["target"],
                expected_content_hash=original.content_hash,
            ),
            agent_id="target",
        )

        current = await target.get(vault, created.path)
        assert native.raced is True
        assert current.title == "Concurrent metadata"
        assert current.tags == ["target"]
        assert current.content == "stable body"


async def test_content_hash_only_update_conflicts_after_concurrent_body_change():
    async with _fresh_database() as (pool, vault_id):
        async with pool.acquire() as conn:
            vault = await conn.fetchval("SELECT name FROM vaults WHERE id = $1", vault_id)

        target = NativeDocumentService(pool=pool)
        competitor = NativeDocumentService(pool=pool)
        created = await target.put(
            DocumentPutRequest(
                vault=vault,
                collection="notes",
                slug="hash-body-race",
                title="Original title",
                content="original body",
            ),
            agent_id="creator",
        )
        original = await target.get(vault, created.path)

        class BodyRaceNativeService(NativeRevisionService):
            raced = False

            async def replace_text(self, **kwargs):
                if kwargs["actor"] == "target" and not self.raced:
                    self.raced = True
                    await competitor.update(
                        vault,
                        created.path,
                        DocumentUpdateRequest(content="changed body"),
                        agent_id="body-racer",
                    )
                return await super().replace_text(**kwargs)

        native = BodyRaceNativeService(pool)

        async def injected_native() -> NativeRevisionService:
            return native

        target._native = injected_native  # type: ignore[method-assign]
        with pytest.raises(ConflictError, match="content_hash moved"):
            await target.update(
                vault,
                created.path,
                DocumentUpdateRequest(
                    title="Must not publish",
                    expected_content_hash=original.content_hash,
                ),
                agent_id="target",
            )

        current = await target.get(vault, created.path)
        assert native.raced is True
        assert current.title == "Original title"
        assert current.content == "changed body"


async def test_expected_commit_update_remains_single_attempt_across_head_race():
    async with _fresh_database() as (pool, vault_id):
        async with pool.acquire() as conn:
            vault = await conn.fetchval("SELECT name FROM vaults WHERE id = $1", vault_id)

        target = NativeDocumentService(pool=pool)
        created = await target.put(
            DocumentPutRequest(
                vault=vault,
                collection="notes",
                slug="exact-head-race",
                title="Original title",
                content="stable body",
            ),
            agent_id="creator",
        )

        class ExactHeadRaceNativeService(NativeRevisionService):
            race_count = 0

            async def replace_text(self, **kwargs):
                if kwargs["actor"] == "target" and self.race_count == 0:
                    self.race_count += 1
                    current = await self.get_current_reference(
                        namespace_id=kwargs["namespace_id"],
                        surface=kwargs["surface"],
                        reference=kwargs["path"],
                    )
                    await NativeRevisionService(pool).replace_text(
                        namespace_id=kwargs["namespace_id"],
                        surface=kwargs["surface"],
                        path=current.path,
                        payload=current.text,
                        actor="exact-head-racer",
                        mutation_id=uuid.uuid4(),
                        expected_revision_id=current.revision_id,
                    )
                return await super().replace_text(**kwargs)

        native = ExactHeadRaceNativeService(pool)

        async def injected_native() -> NativeRevisionService:
            return native

        target._native = injected_native  # type: ignore[method-assign]
        with pytest.raises(ConflictError, match="Native Revision conflict"):
            await target.update(
                vault,
                created.path,
                DocumentUpdateRequest(
                    title="Must not publish",
                    expected_commit=created.commit_hash,
                ),
                agent_id="target",
            )

        assert native.race_count == 1
        current = await target.get(vault, created.path)
        assert current.title == "Original title"


async def test_public_move_retries_a_concurrent_rename_and_preserves_unspecified_slug():
    async with _fresh_database() as (pool, vault_id):
        async with pool.acquire() as conn:
            vault = await conn.fetchval("SELECT name FROM vaults WHERE id = $1", vault_id)

        target = NativeDocumentService(pool=pool)
        created = await target.put(
            DocumentPutRequest(
                vault=vault,
                collection="notes",
                slug="original-name",
                title="Move race",
                content="stable body",
            ),
            agent_id="creator",
        )

        class RenameRaceNativeService(NativeRevisionService):
            raced = False

            async def move_text(self, **kwargs):
                if kwargs["actor"] == "target" and not self.raced:
                    self.raced = True
                    await NativeRevisionService(pool).move_text(
                        namespace_id=kwargs["namespace_id"],
                        surface=kwargs["surface"],
                        path=kwargs["path"],
                        path_to="notes/concurrent-name.md",
                        actor="rename-racer",
                        mutation_id=uuid.uuid4(),
                        expected_revision_id=kwargs["expected_revision_id"],
                    )
                return await super().move_text(**kwargs)

        native = RenameRaceNativeService(pool)

        async def injected_native() -> NativeRevisionService:
            return native

        target._native = injected_native  # type: ignore[method-assign]
        moved = await target.move(
            vault,
            created.path,
            collection="archive",
            agent_id="target",
        )

        assert native.raced is True
        assert moved.path == "archive/concurrent-name.md"
        current = await target.get(vault, created.path)
        assert current.path == moved.path
        assert moved.current_commit == current.current_commit


async def test_public_move_retry_keeps_resource_identity_after_alias_shadowing_create():
    async with _fresh_database() as (pool, vault_id):
        async with pool.acquire() as conn:
            vault = await conn.fetchval("SELECT name FROM vaults WHERE id = $1", vault_id)

        target = NativeDocumentService(pool=pool)
        created = await target.put(
            DocumentPutRequest(
                vault=vault,
                collection="notes",
                slug="shadowed-name",
                title="Alias shadow race",
                content="original body",
            ),
            agent_id="creator",
        )
        _, original = await target._current(vault, created.path)

        class AliasShadowRaceNativeService(NativeRevisionService):
            raced = False
            replacement_resource_id: uuid.UUID | None = None

            async def move_text(self, **kwargs):
                if kwargs["actor"] == "target" and not self.raced:
                    self.raced = True
                    competitor = NativeRevisionService(pool)
                    await competitor.move_text(
                        namespace_id=kwargs["namespace_id"],
                        surface=kwargs["surface"],
                        path=kwargs["path"],
                        path_to="notes/concurrent-name.md",
                        actor="rename-racer",
                        mutation_id=uuid.uuid4(),
                        expected_revision_id=kwargs["expected_revision_id"],
                        expected_resource_id=kwargs["expected_resource_id"],
                    )
                    replacement = await competitor.create_text(
                        namespace_id=kwargs["namespace_id"],
                        surface=kwargs["surface"],
                        path=kwargs["path"],
                        payload=original.text.replace("original body", "replacement body"),
                        actor="alias-shadow-racer",
                        mutation_id=uuid.uuid4(),
                    )
                    self.replacement_resource_id = replacement.resource_id
                return await super().move_text(**kwargs)

        native = AliasShadowRaceNativeService(pool)

        async def injected_native() -> NativeRevisionService:
            return native

        target._native = injected_native  # type: ignore[method-assign]
        moved = await target.move(
            vault,
            created.path,
            collection="archive",
            agent_id="target",
        )

        assert native.raced is True
        assert moved.path == "archive/concurrent-name.md"
        original_after = await native.get_current_resource(
            namespace_id=vault_id,
            surface="document",
            resource_id=original.resource_id,
        )
        assert original_after.path == moved.path
        replacement = await native.get_current_reference(
            namespace_id=vault_id,
            surface="document",
            reference=created.path,
        )
        assert replacement.resource_id == native.replacement_resource_id
        assert replacement.resource_id != original.resource_id
        assert "replacement body" in replacement.text


async def test_collection_only_move_reallocates_after_destination_authority_race():
    async with _fresh_database() as (pool, vault_id):
        async with pool.acquire() as conn:
            vault = await conn.fetchval("SELECT name FROM vaults WHERE id = $1", vault_id)

        target = NativeDocumentService(pool=pool)
        created = await target.put(
            DocumentPutRequest(
                vault=vault,
                collection="notes",
                slug="allocation-race",
                title="Move allocation race",
                content="original body",
            ),
            agent_id="creator",
        )
        _, original = await target._current(vault, created.path)

        class DestinationRaceNativeService(NativeRevisionService):
            raced = False
            winner_resource_id: uuid.UUID | None = None

            async def move_text(self, **kwargs):
                if kwargs["actor"] == "target" and not self.raced:
                    self.raced = True
                    winner = await NativeRevisionService(pool).create_text(
                        namespace_id=kwargs["namespace_id"],
                        surface=kwargs["surface"],
                        path=kwargs["path_to"],
                        payload=original.text.replace("original body", "destination winner"),
                        actor="destination-racer",
                        mutation_id=uuid.uuid4(),
                    )
                    self.winner_resource_id = winner.resource_id
                return await super().move_text(**kwargs)

        native = DestinationRaceNativeService(pool)

        async def injected_native() -> NativeRevisionService:
            return native

        target._native = injected_native  # type: ignore[method-assign]
        moved = await target.move(
            vault,
            created.path,
            collection="archive",
            agent_id="target",
        )

        assert native.raced is True
        assert moved.path.startswith("archive/allocation-race-")
        assert moved.path.endswith(".md")
        assert (
            await native.get_current_resource(
                namespace_id=vault_id,
                surface="document",
                resource_id=original.resource_id,
            )
        ).path == moved.path
        destination_winner = await native.get_current_reference(
            namespace_id=vault_id,
            surface="document",
            reference="archive/allocation-race.md",
        )
        assert destination_winner.resource_id == native.winner_resource_id

        another = await target.put(
            DocumentPutRequest(
                vault=vault,
                collection="notes",
                slug="explicit-source",
                title="Explicit source",
                content="body",
            ),
            agent_id="creator",
        )
        with pytest.raises(ConflictError, match="already exists at path"):
            await target.move(
                vault,
                another.path,
                collection="archive",
                slug="allocation-race",
                agent_id="explicit-mover",
            )


async def test_collection_only_move_searches_beyond_all_uuid_path_candidates():
    async with _fresh_database() as (pool, vault_id):
        async with pool.acquire() as conn:
            vault = await conn.fetchval("SELECT name FROM vaults WHERE id = $1", vault_id)

        document_service = NativeDocumentService(pool=pool)
        created = await document_service.put(
            DocumentPutRequest(
                vault=vault,
                collection="notes",
                slug="exhausted-candidates",
                title="Exhausted candidates",
                content="original",
            ),
            agent_id="creator",
        )
        _, original = await document_service._current(vault, created.path)
        stem = "archive/exhausted-candidates"
        claimed_paths = [
            f"{stem}.md",
            *(f"{stem}-{original.resource_id.hex[:width]}.md" for width in (8, 12, 16, 32)),
            f"{stem}-{original.resource_id.hex}-2.md",
        ]
        native = NativeRevisionService(pool)
        for path in claimed_paths:
            await native.create_text(
                namespace_id=vault_id,
                surface="document",
                path=path,
                payload=f"claimed: {path}",
                actor="candidate-claimer",
                mutation_id=uuid.uuid4(),
            )

        expected = f"{stem}-{original.resource_id.hex}-3.md"
        assert (
            await document_service._resolve_native_free_path(
                vault_id,
                f"{stem}.md",
                original.resource_id,
            )
            == expected
        )
        moved = await asyncio.wait_for(
            document_service.move(
                vault,
                created.path,
                collection="archive",
                agent_id="mover",
            ),
            timeout=3,
        )

        assert moved.path == expected
        assert (
            await native.get_current_resource(
                namespace_id=vault_id,
                surface="document",
                resource_id=original.resource_id,
            )
        ).path == expected


async def test_public_move_response_hash_is_derived_from_the_committed_head():
    async with _fresh_database() as (pool, vault_id):
        async with pool.acquire() as conn:
            vault = await conn.fetchval("SELECT name FROM vaults WHERE id = $1", vault_id)

        target = NativeDocumentService(pool=pool)
        created = await target.put(
            DocumentPutRequest(
                vault=vault,
                collection="notes",
                slug="content-race",
                title="Content race",
                content="original body",
            ),
            agent_id="creator",
        )

        class ContentRaceNativeService(NativeRevisionService):
            raced = False

            async def move_text(self, **kwargs):
                if kwargs["actor"] == "target" and not self.raced:
                    self.raced = True
                    current = await self.get_current_reference(
                        namespace_id=kwargs["namespace_id"],
                        surface=kwargs["surface"],
                        reference=kwargs["path"],
                    )
                    await NativeRevisionService(pool).replace_text(
                        namespace_id=kwargs["namespace_id"],
                        surface=kwargs["surface"],
                        path=current.path,
                        payload=current.text.replace("original body", "concurrent body"),
                        actor="content-racer",
                        mutation_id=uuid.uuid4(),
                        expected_revision_id=current.revision_id,
                    )
                return await super().move_text(**kwargs)

        native = ContentRaceNativeService(pool)

        async def injected_native() -> NativeRevisionService:
            return native

        target._native = injected_native  # type: ignore[method-assign]
        moved = await target.move(
            vault,
            created.path,
            collection="archive",
            agent_id="target",
        )

        current = await target.get(vault, moved.path)
        assert current.content == "concurrent body"
        assert moved.content_hash == current.content_hash


async def test_explicit_slug_put_conflicts_when_base_is_claimed_after_precheck():
    async with _fresh_database() as (pool, vault_id):
        async with pool.acquire() as conn:
            vault = await conn.fetchval("SELECT name FROM vaults WHERE id = $1", vault_id)

        base_path = "notes/explicit-race.md"

        class ExplicitRaceDocumentService(NativeDocumentService):
            raced = False
            winner_resource_id: uuid.UUID | None = None

            async def _current_path_is_owned(self, checked_vault_id, path):
                owned = await super()._current_path_is_owned(checked_vault_id, path)
                if path == base_path and not self.raced:
                    self.raced = True
                    winner = await NativeRevisionService(pool).create_text(
                        namespace_id=checked_vault_id,
                        surface="document",
                        path=path,
                        payload="competitor",
                        actor="explicit-slug-racer",
                        mutation_id=uuid.uuid4(),
                    )
                    self.winner_resource_id = winner.resource_id
                return owned

        service = ExplicitRaceDocumentService(pool=pool)
        with pytest.raises(ConflictError, match=f"already exists at path: {base_path}"):
            await service.put(
                DocumentPutRequest(
                    vault=vault,
                    collection="notes",
                    slug="explicit-race",
                    title="Explicit race loser",
                    content="must not be suffixed",
                ),
                agent_id="loser",
            )

        assert service.raced is True
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT resource_id, current_path
                  FROM native_resources
                 WHERE namespace_id = $1
                   AND lifecycle = 'live'
                   AND current_path LIKE 'notes/explicit-race%'
                """,
                vault_id,
            )
        assert [(row["resource_id"], row["current_path"]) for row in rows] == [
            (service.winner_resource_id, base_path)
        ]


async def test_concurrent_title_derived_puts_allocate_distinct_paths():
    async with _fresh_database() as (pool, vault_id):
        async with pool.acquire() as conn:
            vault = await conn.fetchval("SELECT name FROM vaults WHERE id = $1", vault_id)

        both_resolved = asyncio.Event()
        resolved_count = 0

        class CoordinatedDocumentService(NativeDocumentService):
            async def _resolve_native_free_path(self, *args, **kwargs):
                nonlocal resolved_count
                path = await super()._resolve_native_free_path(*args, **kwargs)
                resolved_count += 1
                if resolved_count == 2:
                    both_resolved.set()
                await both_resolved.wait()
                return path

        service = CoordinatedDocumentService(pool=pool)
        request = DocumentPutRequest(
            vault=vault,
            collection="notes",
            title="Same implicit title",
            content="body",
        )
        first, second = await asyncio.gather(
            service.put(request, agent_id="first"),
            service.put(request, agent_id="second"),
        )

        assert first.path != second.path
        assert {first.path, second.path} & {"notes/same-implicit-title.md"}
        assert all(path.startswith("notes/same-implicit-title") for path in (first.path, second.path))

        with pytest.raises(ConflictError, match="already exists at path"):
            await NativeDocumentService(pool=pool).put(
                DocumentPutRequest(
                    vault=vault,
                    collection="notes",
                    slug="same-implicit-title",
                    title="Explicit slug remains exact",
                    content="must not suffix",
                ),
                agent_id="explicit",
            )


async def test_create_reuses_a_move_alias_and_atomically_retires_the_redirect():
    async with _fresh_database() as (pool, vault_id):
        service = NativeRevisionService(pool)
        created = await service.create_text(**_create_args(vault_id))
        moved = await service.move_text(
            namespace_id=vault_id,
            surface="document",
            path="notes/native.md",
            path_to="archive/native.md",
            actor="mover",
            mutation_id=uuid.uuid4(),
        )

        replacement_args = _create_args(vault_id)
        replacement_args["payload"] = "fresh lineage"
        replacement = await service.create_text(**replacement_args)

        assert replacement.resource_id != created.resource_id
        assert (
            await service.get_current_reference(
                namespace_id=vault_id,
                surface="document",
                reference="notes/native.md",
            )
        ).resource_id == replacement.resource_id
        assert (
            await service.get_current_reference(
                namespace_id=vault_id,
                surface="document",
                reference="archive/native.md",
            )
        ).resource_id == moved.resource_id
        async with pool.acquire() as conn:
            retired = await conn.fetchrow(
                """
                SELECT retired_revision_id, retired_at
                  FROM native_resource_path_aliases
                 WHERE namespace_id = $1 AND old_path = 'notes/native.md'
                """,
                vault_id,
            )
        assert retired["retired_revision_id"] == replacement.revision_id
        assert retired["retired_at"] is not None


async def test_alias_delete_competing_with_current_path_update_has_no_raw_pg_deadlock():
    async with _fresh_database() as (pool, vault_id):
        service = NativeRevisionService(pool)
        await service.create_text(**_create_args(vault_id))
        await service.move_text(
            namespace_id=vault_id,
            surface="document",
            path="notes/native.md",
            path_to="archive/native.md",
            actor="mover",
            mutation_id=uuid.uuid4(),
        )

        alias_has_resource = asyncio.Event()
        current_has_path = asyncio.Event()
        current_attempted_resource = asyncio.Event()

        class CoordinatedRepository(NativeRevisionRepository):
            async def lock_resource(self, conn, resource_id):
                task_name = asyncio.current_task().get_name()
                if task_name == "current-update":
                    current_attempted_resource.set()
                    return await super().lock_resource(conn, resource_id)
                resource = await super().lock_resource(conn, resource_id)
                if task_name == "alias-delete":
                    alias_has_resource.set()
                    path_waiter = asyncio.create_task(current_has_path.wait())
                    resource_waiter = asyncio.create_task(current_attempted_resource.wait())
                    _, pending = await asyncio.wait(
                        {path_waiter, resource_waiter},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for waiter in pending:
                        waiter.cancel()
                return resource

            async def lock_paths(self, conn, namespace_id, surface, *paths):
                if asyncio.current_task().get_name() == "current-update":
                    await alias_has_resource.wait()
                    await super().lock_paths(conn, namespace_id, surface, *paths)
                    current_has_path.set()
                    return
                await super().lock_paths(conn, namespace_id, surface, *paths)

        repository = CoordinatedRepository(pool)
        alias_delete = asyncio.create_task(
            NativeRevisionService(pool, repository=repository).delete_resource(
                namespace_id=vault_id,
                surface="document",
                path="notes/native.md",
                actor="deleter",
                mutation_id=uuid.uuid4(),
            ),
            name="alias-delete",
        )
        current_update = asyncio.create_task(
            NativeRevisionService(pool, repository=repository).replace_text(
                namespace_id=vault_id,
                surface="document",
                path="archive/native.md",
                payload="competing update",
                actor="updater",
                mutation_id=uuid.uuid4(),
            ),
            name="current-update",
        )
        outcomes = await asyncio.wait_for(
            asyncio.gather(alias_delete, current_update, return_exceptions=True),
            timeout=5,
        )

        assert not any(isinstance(item, asyncpg.DeadlockDetectedError) for item in outcomes)
        assert all(
            not isinstance(item, Exception) or isinstance(item, (ConflictError, NotFoundError)) for item in outcomes
        )


async def test_post_prepare_failpoint_is_distinct_and_precedes_authority_entry():
    async with _fresh_database() as (pool, vault_id):
        seen: list[str] = []

        async def stop_before_authority(name: str) -> None:
            seen.append(name)
            if name == "payload.after_prepare_before_tx":
                raise RuntimeError("stop before authority")

        with pytest.raises(RuntimeError, match="stop before authority"):
            await NativeRevisionService(pool, failpoint=stop_before_authority).create_text(**_create_args(vault_id))

        assert seen == [
            "payload.before_prepare",
            "payload.after_verified",
            "payload.after_prepare_before_tx",
        ]
        assert await _authority_counts(pool) == {
            "native_resources": 0,
            "native_revisions": 0,
            "native_payload_manifests": 0,
            "native_revision_activity": 0,
            "native_invalidation_intents": 0,
        }


@pytest.mark.parametrize(
    "boundary",
    [
        "authority.after_resource",
        "authority.after_manifest",
        "authority.after_revision",
        "authority.after_head",
        "authority.after_path",
        "authority.after_activity",
        "authority.after_invalidation",
        "authority.before_commit",
    ],
)
async def test_authority_failpoints_rollback_every_published_fact(boundary: str):
    async with _fresh_database() as (pool, vault_id):
        seen: list[str] = []

        async def failpoint(name: str) -> None:
            seen.append(name)
            if name == boundary:
                raise RuntimeError(f"injected:{name}")

        service = NativeRevisionService(pool, failpoint=failpoint)
        mutation_id = uuid.uuid4()
        with pytest.raises(RuntimeError, match=f"injected:{boundary}"):
            await service.create_text(**_create_args(vault_id, mutation_id=mutation_id))

        assert boundary in seen
        assert await _authority_counts(pool) == {
            "native_resources": 0,
            "native_revisions": 0,
            "native_payload_manifests": 0,
            "native_revision_activity": 0,
            "native_invalidation_intents": 0,
        }
        # Preparation is deliberately outside the authority transaction. The
        # unreferenced verified row is not a Revision and may be retried.
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM m1_reference_payloads") == 1


@pytest.mark.parametrize(
    "boundary",
    [
        "authority.after_path",
        "authority.after_alias",
        "authority.before_commit",
    ],
)
async def test_move_failpoints_rollback_head_path_alias_and_activity(boundary: str):
    async with _fresh_database() as (pool, vault_id):
        created = await NativeRevisionService(pool).create_text(**_create_args(vault_id))

        async def failpoint(name: str) -> None:
            if name == boundary:
                raise RuntimeError(f"injected:{name}")

        service = NativeRevisionService(pool, failpoint=failpoint)
        with pytest.raises(RuntimeError, match=f"injected:{boundary}"):
            await service.move_text(
                namespace_id=vault_id,
                surface="document",
                path="notes/native.md",
                path_to="archive/native.md",
                actor="mover",
                mutation_id=uuid.uuid4(),
                expected_revision_id=created.revision_id,
            )

        current = await NativeRevisionService(pool).get_current(
            namespace_id=vault_id,
            surface="document",
            path="notes/native.md",
        )
        assert current.revision_id == created.revision_id
        async with pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM native_resource_path_aliases") == 0
        assert await _authority_counts(pool) == {
            "native_resources": 1,
            "native_revisions": 1,
            "native_payload_manifests": 1,
            "native_revision_activity": 1,
            "native_invalidation_intents": 1,
        }


@pytest.mark.parametrize("boundary", ["authority.after_alias", "authority.before_commit"])
async def test_delete_failpoints_preserve_live_resource_and_alias(boundary: str):
    async with _fresh_database() as (pool, vault_id):
        service = NativeRevisionService(pool)
        created = await service.create_text(**_create_args(vault_id))
        moved = await service.move_text(
            namespace_id=vault_id,
            surface="document",
            path="notes/native.md",
            path_to="archive/native.md",
            actor="mover",
            mutation_id=uuid.uuid4(),
            expected_revision_id=created.revision_id,
        )

        async def failpoint(name: str) -> None:
            if name == boundary:
                raise RuntimeError(f"injected:{name}")

        with pytest.raises(RuntimeError, match=f"injected:{boundary}"):
            await NativeRevisionService(pool, failpoint=failpoint).delete_resource(
                namespace_id=vault_id,
                surface="document",
                path="notes/native.md",
                actor="deleter",
                mutation_id=uuid.uuid4(),
                expected_revision_id=moved.revision_id,
            )

        current = await service.get_current_reference(
            namespace_id=vault_id,
            surface="document",
            reference="notes/native.md",
        )
        assert current.revision_id == moved.revision_id
        async with pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    """
                SELECT count(*) FROM native_resource_path_aliases
                 WHERE retired_revision_id IS NULL
                """
                )
                == 1
            )


async def test_committed_response_lost_retry_returns_same_revision_once():
    async with _fresh_database() as (pool, vault_id):
        fired = False

        async def failpoint(name: str) -> None:
            nonlocal fired
            if name == "authority.after_commit_before_response" and not fired:
                fired = True
                raise RuntimeError("response lost")

        mutation_id = uuid.uuid4()
        service = NativeRevisionService(pool, failpoint=failpoint)
        with pytest.raises(RuntimeError, match="response lost"):
            await service.create_text(**_create_args(vault_id, mutation_id=mutation_id))

        retried = await service.create_text(**_create_args(vault_id, mutation_id=mutation_id))
        assert retried.idempotent_replay is True
        assert await _authority_counts(pool) == {
            "native_resources": 1,
            "native_revisions": 1,
            "native_payload_manifests": 1,
            "native_revision_activity": 1,
            "native_invalidation_intents": 1,
        }
        with pytest.raises(ConflictError, match="idempotency"):
            changed = _create_args(vault_id, mutation_id=mutation_id)
            changed["payload"] = "different retry"
            await service.create_text(**changed)


async def test_create_idempotency_fingerprint_includes_requested_resource_identity():
    async with _fresh_database() as (pool, vault_id):
        mutation_id = uuid.uuid4()
        first_resource_id = uuid.uuid4()
        second_resource_id = uuid.uuid4()
        service = NativeRevisionService(pool)
        args = _create_args(vault_id, mutation_id=mutation_id)
        args["resource_id"] = first_resource_id
        created = await service.create_text(**args)
        assert created.resource_id == first_resource_id

        changed = _create_args(vault_id, mutation_id=mutation_id)
        changed["resource_id"] = second_resource_id
        with pytest.raises(ConflictError, match="idempotency"):
            await service.create_text(**changed)


async def test_reference_payload_verifies_digest_size_and_utf8_before_authority_tx():
    async with _fresh_database() as (pool, vault_id):
        store = M1ReferencePayloadStore(pool)
        body = "한글 payload\n".encode()
        digest = hashlib.sha256(body).hexdigest()
        prepared = await store.prepare_text(
            namespace_id=vault_id,
            payload=body,
            expected_digest=digest,
            expected_size=len(body),
        )
        assert await store.open_verified(prepared.payload_id) == body

        with pytest.raises(ValidationError, match="UTF-8"):
            await store.prepare_text(namespace_id=vault_id, payload=b"\xff")
        with pytest.raises(ValidationError, match="NUL"):
            await store.prepare_text(namespace_id=vault_id, payload=b"a\x00b")
        with pytest.raises(ValidationError, match="digest"):
            await store.prepare_text(
                namespace_id=vault_id,
                payload=body,
                expected_digest="0" * 64,
            )
        with pytest.raises(ValidationError, match="size"):
            await store.prepare_text(namespace_id=vault_id, payload=body, expected_size=len(body) + 1)


async def test_schema_enforces_revision_identity_head_ownership_and_immutable_facts():
    async with _fresh_database() as (pool, vault_id):
        service = NativeRevisionService(pool)
        one = await service.create_text(**_create_args(vault_id))
        other_args = _create_args(vault_id)
        other_args["path"] = "notes/other.md"
        other = await service.create_text(**other_args)

        async with pool.acquire() as conn:
            definition = await conn.fetchval(
                """
                SELECT pg_get_constraintdef(oid)
                  FROM pg_constraint
                 WHERE conname = 'native_revisions_revision_id_shape'
                """
            )
            assert "{40}" in definition

            with pytest.raises(asyncpg.ForeignKeyViolationError):
                async with conn.transaction():
                    await conn.execute(
                        "UPDATE native_resources SET head_revision_id = $2 WHERE resource_id = $1",
                        one.resource_id,
                        other.revision_id,
                    )
            with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
                await conn.execute(
                    "UPDATE native_revisions SET message = 'mutated' WHERE revision_id = $1",
                    one.revision_id,
                )
            with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError):
                await conn.execute(
                    "DELETE FROM native_revision_activity WHERE revision_id = $1",
                    one.revision_id,
                )


async def test_revision_id_collision_is_retried_as_identity_not_path_conflict(monkeypatch):
    async with _fresh_database() as (pool, vault_id):
        service = NativeRevisionService(pool)
        first = await service.create_text(**_create_args(vault_id))
        allocated_after_retry = "f" * 40
        candidates = iter((first.revision_id, allocated_after_retry))
        monkeypatch.setattr(service, "_opaque_revision_id", lambda: next(candidates))

        second_args = _create_args(vault_id)
        second_args["path"] = "notes/collision-retried.md"
        second = await service.create_text(**second_args)
        assert second.revision_id == allocated_after_retry
        assert second.resource_id != first.resource_id
        assert (await _authority_counts(pool))["native_revisions"] == 2


async def test_vault_delete_cascades_the_experimental_native_subtree():
    async with _fresh_database() as (pool, vault_id):
        service = NativeRevisionService(pool)
        await service.create_text(**_create_args(vault_id))
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM vaults WHERE id = $1", vault_id)
            for table in (
                "native_resources",
                "native_revisions",
                "native_payload_manifests",
                "native_revision_activity",
                "native_invalidation_intents",
                "m1_reference_payloads",
            ):
                assert await conn.fetchval(f"SELECT count(*) FROM {table}") == 0


async def test_native_substrate_has_no_git_legacy_projection_or_write_lane_dependency():
    paths = (
        _BACKEND / "app" / "repositories" / "native_revision_repo.py",
        _BACKEND / "app" / "services" / "m1_reference_payload_store.py",
        _BACKEND / "app" / "services" / "native_revision_service.py",
    )
    source = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    for forbidden in ("GitService", "git_service", "current_commit", "write_lane", "documents"):
        assert forbidden not in source


async def test_full_native_document_lifecycle_preserves_compatibility_without_git_or_legacy_projection():
    async with _fresh_database() as (pool, vault_id):
        async with pool.acquire() as conn:
            vault = await conn.fetchval("SELECT name FROM vaults WHERE id = $1", vault_id)

        document_service = NativeDocumentService(pool=pool)
        backend = NativeRevisionBackend(pool=pool, document_service=document_service)

        created = await document_service.put(
            DocumentPutRequest(
                vault=vault,
                collection="notes",
                slug="native",
                title="Native note",
                content="Alpha body",
                type="note",
                status="draft",
                tags=["native"],
                summary="first revision",
            ),
            agent_id="m1-writer",
        )
        assert created.path == "notes/native.md"
        assert created.commit_hash == created.current_commit
        assert len(created.commit_hash) == 40
        assert created.action == "created"

        first = await document_service.get(vault, created.path)
        assert first.content == "Alpha body"
        assert first.title == "Native note"
        assert first.current_commit == created.commit_hash
        first_resource = (
            await NativeRevisionService(pool).get_current(
                namespace_id=vault_id,
                surface="document",
                path=created.path,
            )
        ).resource_id

        replaced = await document_service.update(
            vault,
            created.path,
            DocumentUpdateRequest(
                content="Beta body",
                title="Renamed note",
                status="active",
                message="replace native note",
                expected_commit=created.commit_hash,
            ),
            agent_id="m1-writer",
        )
        assert replaced.previous_commit == created.commit_hash
        assert replaced.commit_hash != created.commit_hash
        assert replaced.current_commit == replaced.commit_hash
        with pytest.raises(ConflictError, match="expected"):
            await document_service.update(
                vault,
                created.path,
                DocumentUpdateRequest(content="stale", expected_commit=created.commit_hash),
                agent_id="stale-writer",
            )

        moved = await document_service.move(
            vault,
            created.path,
            collection="archive",
            slug="renamed",
            message="move native note",
            agent_id="m1-writer",
        )
        assert moved.path == "archive/renamed.md"
        assert moved.previous_commit == replaced.commit_hash
        assert (await document_service.get(vault, created.path)).path == moved.path

        pinned = await document_service.get_at_commit(vault, created.path, created.commit_hash)
        assert pinned.path == moved.path
        assert pinned.title == "Renamed note"
        assert pinned.content == "Alpha body"
        assert pinned.current_commit == created.commit_hash
        assert pinned.metadata_is_current is True

        version = await backend.document_version(vault, created.path, created.commit_hash)
        assert version is not None
        version_doc, version_raw = version
        assert version_doc["path"] == moved.path
        assert "Alpha body" in version_raw

        history = await backend.document_history(vault, created.path, limit=20)
        assert history["uri"].endswith("/coll/archive/doc/renamed.md")
        assert [entry["hash"] for entry in history["history"]] == [
            moved.commit_hash,
            replaced.commit_hash,
            created.commit_hash,
        ]
        assert [entry["author"] for entry in history["history"]] == [
            "m1-writer",
            "m1-writer",
            "m1-writer",
        ]

        diff = await backend.document_diff(vault, created.path, replaced.commit_hash)
        assert diff is not None
        assert diff["commit"] == replaced.commit_hash
        assert diff["type"] == "modified"
        assert "-Alpha body" in diff["diff"]
        assert "+Beta body" in diff["diff"]

        activity = await backend.vault_activity(
            vault,
            max_count=20,
            since=None,
            path=None,
        )
        assert [entry["hash"] for entry in activity] == [
            moved.commit_hash,
            replaced.commit_hash,
            created.commit_hash,
        ]
        assert activity[0]["files"] == [{"path": moved.path, "change": "modified"}]
        recent = await backend.recent_changes("unused-user", vault=vault, limit=20)
        assert recent[0]["commit"] == moved.commit_hash
        assert recent[0]["title"] == "Renamed note"

        assert await document_service.delete(vault, created.path, agent_id="m1-writer") is True
        with pytest.raises(NotFoundError):
            await document_service.get(vault, created.path)
        with pytest.raises(NotFoundError):
            await document_service.get(vault, moved.path)

        recreated = await document_service.put(
            DocumentPutRequest(
                vault=vault,
                collection="archive",
                slug="renamed",
                title="Fresh note",
                content="Fresh lineage",
            ),
            agent_id="m1-writer",
        )
        fresh_snapshot = await NativeRevisionService(pool).get_current(
            namespace_id=vault_id,
            surface="document",
            path=recreated.path,
        )
        assert fresh_snapshot.resource_id != first_resource
        fresh_history = await backend.document_history(vault, recreated.path, limit=20)
        assert [entry["hash"] for entry in fresh_history["history"]] == [recreated.commit_hash]

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT resource_id, lifecycle, head_revision_id
                  FROM native_resources
                 WHERE namespace_id = $1 AND current_path = $2
                 ORDER BY created_at
                """,
                vault_id,
                recreated.path,
            )
            assert [row["lifecycle"] for row in rows] == ["deleted", "live"]
            assert (
                await conn.fetchval(
                    "SELECT action FROM native_revisions WHERE revision_id = $1",
                    rows[0]["head_revision_id"],
                )
                == "delete"
            )
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM documents WHERE vault_id = $1",
                    vault_id,
                )
                == 0
            )

        with pytest.raises(NativeRevisionUnsupportedSurfaceError):
            await document_service.browse(vault)
