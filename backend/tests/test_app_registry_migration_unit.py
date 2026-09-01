"""Live-PostgreSQL contract for the app desired-state registry migration."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import pytest

pytestmark = pytest.mark.asyncio

_BACKEND = Path(__file__).resolve().parents[1]
_INIT_SQL = (_BACKEND / "app" / "db" / "init.sql").read_text()
_MIGRATION = _BACKEND / "app" / "db" / "migrations" / "047_app_registry.py"
_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:15432/akb",  # pragma: allowlist secret
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
    spec = importlib.util.spec_from_file_location("app_registry_migration", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@asynccontextmanager
async def _fresh_database(*, initialize: bool = True):
    if not await _can_connect():
        pytest.skip(f"Postgres not reachable at {_DSN}")
    admin = await asyncpg.connect(_DSN)
    name = f"akb_registry_{uuid.uuid4().hex[:12]}"
    await admin.execute(f'CREATE DATABASE "{name}"')
    dsn = _database_dsn(name)
    conn = await asyncpg.connect(dsn)
    try:
        if initialize:
            await conn.execute(_INIT_SQL)
        yield conn, dsn, name
    finally:
        await conn.close()
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


async def _apply(conn: asyncpg.Connection) -> None:
    await _load_migration().migrate(conn=conn)


async def _error_state(conn: asyncpg.Connection, sql: str, *args: object) -> str:
    with pytest.raises(asyncpg.PostgresError) as exc:
        await conn.execute(sql, *args)
    assert exc.value.sqlstate is not None
    return exc.value.sqlstate


async def _app(conn: asyncpg.Connection, key: str) -> uuid.UUID:
    return await conn.fetchval(
        "INSERT INTO app_definitions (app_key, display_name) VALUES ($1, $2) RETURNING id",
        key,
        f"App {key}",
    )


async def _release(
    conn: asyncpg.Connection,
    app_id: uuid.UUID,
    version: str,
) -> uuid.UUID:
    manifest = {
        "manifest_version": 2,
        "app_key": "registry-test",
        "source_revision": "a" * 40,
        "image_digest": "sha256:" + "b" * 64,
        "schema_version": 3,
        "schema": {"tables": [], "fingerprint": hashlib.sha256(b"[]").hexdigest()},
        "transition_plans": [{"source": "fresh", "steps": []}],
    }
    encoded = json.dumps(manifest, separators=(",", ":"))
    checksum = hashlib.sha256(encoded.encode()).hexdigest()
    return await conn.fetchval(
        "INSERT INTO app_releases "
        "(app_id, version, manifest, manifest_checksum) "
        "VALUES ($1, $2, $3::jsonb, $4) RETURNING id",
        app_id,
        version,
        encoded,
        checksum,
    )


async def _vault(conn: asyncpg.Connection, name: str) -> uuid.UUID:
    return await conn.fetchval(
        "INSERT INTO vaults (name, git_path) VALUES ($1, $2) RETURNING id",
        name,
        f"/tmp/{name}.git",
    )


async def _installation(
    conn: asyncpg.Connection,
    app_id: uuid.UUID,
    vault_id: uuid.UUID,
    release_id: uuid.UUID,
) -> uuid.UUID:
    return await conn.fetchval(
        "INSERT INTO vault_app_installations "
        "(app_id, vault_id, desired_release_id, current_release_id, lifecycle) "
        "VALUES ($1, $2, $3, $3, 'active') RETURNING id",
        app_id,
        vault_id,
        release_id,
    )


async def _resource(
    conn: asyncpg.Connection,
    installation_id: uuid.UUID,
    vault_id: uuid.UUID,
    kind: str,
    key: str,
) -> uuid.UUID:
    return await conn.fetchval(
        "INSERT INTO app_owned_resources "
        "(installation_id, vault_id, resource_kind, resource_key) "
        "VALUES ($1, $2, $3, $4) RETURNING id",
        installation_id,
        vault_id,
        kind,
        key,
    )


async def test_fresh_apply_second_apply_and_compatible_partial_object():
    async with _fresh_database() as (conn, _, _):
        await _apply(conn)
        app_id = await _app(conn, f"idempotent-{uuid.uuid4().hex}")
        before_objects = await conn.fetch(
            """
            SELECT relkind, count(*) AS count
              FROM pg_class
             WHERE relnamespace = 'public'::regnamespace
               AND (
                 relname LIKE 'app_%'
                 OR relname LIKE 'vault_app_%'
                 OR relname LIKE 'installation_%'
               )
             GROUP BY relkind
             ORDER BY relkind
            """
        )
        before_constraints = await conn.fetchval(
            """
            SELECT count(*)
              FROM pg_constraint
             WHERE conrelid IN (
                 'app_definitions'::regclass,
                 'app_releases'::regclass,
                 'vault_app_installations'::regclass,
                 'installation_grants'::regclass,
                 'app_owned_resources'::regclass
             )
            """
        )
        before_row = await conn.fetchrow("SELECT * FROM app_definitions WHERE id = $1", app_id)

        await _apply(conn)

        assert (
            await conn.fetch(
                """
            SELECT relkind, count(*) AS count
              FROM pg_class
             WHERE relnamespace = 'public'::regnamespace
               AND (
                 relname LIKE 'app_%'
                 OR relname LIKE 'vault_app_%'
                 OR relname LIKE 'installation_%'
               )
             GROUP BY relkind
             ORDER BY relkind
            """
            )
            == before_objects
        )
        assert (
            await conn.fetchval(
                """
                SELECT count(*)
                  FROM pg_constraint
                 WHERE conrelid IN (
                     'app_definitions'::regclass,
                     'app_releases'::regclass,
                     'vault_app_installations'::regclass,
                     'installation_grants'::regclass,
                     'app_owned_resources'::regclass
                 )
                """
            )
            == before_constraints
        )
        assert await conn.fetchrow("SELECT * FROM app_definitions WHERE id = $1", app_id) == before_row

    async with _fresh_database() as (conn, _, _):
        await conn.execute(
            """
            CREATE TABLE app_definitions (
                id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                app_key TEXT NOT NULL
            )
            """
        )
        preserved = await conn.fetchval(
            "INSERT INTO app_definitions (app_key) VALUES ($1) RETURNING id",
            f"partial-{uuid.uuid4().hex}",
        )

        await _apply(conn)

        row = await conn.fetchrow("SELECT app_key, metadata FROM app_definitions WHERE id = $1", preserved)
        assert row is not None
        assert row["app_key"].startswith("partial-")
        assert row["metadata"] == "{}"


async def test_app_release_ownership_duplicate_installation_and_lifecycle_coherence():
    async with _fresh_database() as (conn, _, _):
        await _apply(conn)
        app_a = await _app(conn, f"app-a-{uuid.uuid4().hex}")
        app_b = await _app(conn, f"app-b-{uuid.uuid4().hex}")
        a1 = await _release(conn, app_a, "1.0.0")
        a2 = await _release(conn, app_a, "2.0.0")
        b1 = await _release(conn, app_b, "1.0.0")
        vault_a = await _vault(conn, f"vault-a-{uuid.uuid4().hex[:10]}")
        vault_b = await _vault(conn, f"vault-b-{uuid.uuid4().hex[:10]}")

        assert (
            await _error_state(
                conn,
                "INSERT INTO vault_app_installations "
                "(app_id, vault_id, desired_release_id, current_release_id, lifecycle) "
                "VALUES ($1, $2, $3, NULL, 'active')",
                app_a,
                vault_a,
                a1,
            )
            == "23514"
        )
        installation_a = await _installation(conn, app_a, vault_a, a1)
        assert (
            await _error_state(
                conn,
                "INSERT INTO vault_app_installations "
                "(app_id, vault_id, desired_release_id, current_release_id, lifecycle) "
                "VALUES ($1, $2, $3, $3, 'active')",
                app_a,
                vault_a,
                a1,
            )
            == "23505"
        )

        assert (
            await _error_state(
                conn,
                "UPDATE vault_app_installations "
                "SET desired_release_id = $2, current_release_id = $2, lifecycle = 'active' "
                "WHERE id = $1",
                installation_a,
                b1,
            )
            == "23503"
        )
        assert (
            await _error_state(
                conn,
                "INSERT INTO vault_app_installations "
                "(app_id, vault_id, desired_release_id, current_release_id, lifecycle) "
                "VALUES ($1, $2, $3, $3, 'active')",
                app_b,
                vault_b,
                a1,
            )
            == "23503"
        )

        await conn.execute(
            "UPDATE vault_app_installations "
            "SET desired_release_id = $2, current_release_id = $3, lifecycle = 'upgrading' "
            "WHERE id = $1",
            installation_a,
            a2,
            a1,
        )
        assert (
            await _error_state(
                conn,
                "UPDATE vault_app_installations "
                "SET desired_release_id = current_release_id, lifecycle = 'upgrading' "
                "WHERE id = $1",
                installation_a,
            )
            == "23514"
        )
        await conn.execute(
            "UPDATE vault_app_installations "
            "SET desired_release_id = $2, current_release_id = $2, lifecycle = 'active' "
            "WHERE id = $1",
            installation_a,
            a2,
        )
        assert (
            await _error_state(
                conn,
                "UPDATE vault_app_installations SET lifecycle = 'uninstalled' WHERE id = $1",
                installation_a,
            )
            == "23514"
        )
        await conn.execute(
            "UPDATE vault_app_installations SET desired_release_id = NULL, lifecycle = 'uninstalled' WHERE id = $1",
            installation_a,
        )


async def test_release_rows_are_immutable_including_manifest_order_and_checksum():
    async with _fresh_database() as (conn, _, _):
        await _apply(conn)
        app_a = await _app(conn, f"release-a-{uuid.uuid4().hex}")
        app_b = await _app(conn, f"release-b-{uuid.uuid4().hex}")
        release_id = await _release(conn, app_a, "1.2.3")
        before = await conn.fetchrow(
            "SELECT app_id, version, manifest, manifest_checksum, registered_at FROM app_releases WHERE id = $1",
            release_id,
        )

        mutations = (
            ("UPDATE app_releases SET app_id = $2 WHERE id = $1", app_b),
            ("UPDATE app_releases SET version = '9.9.9' WHERE id = $1", None),
            (
                'UPDATE app_releases SET manifest = \'{"steps":[{"id":"changed"}]}\'::jsonb WHERE id = $1',
                None,
            ),
            (
                "UPDATE app_releases "
                'SET manifest = \'{"steps":[{"id":"second"},{"id":"first"}]}\'::jsonb '
                "WHERE id = $1",
                None,
            ),
            (
                "UPDATE app_releases SET manifest_checksum = repeat('0', 64) WHERE id = $1",
                None,
            ),
        )
        for sql, extra in mutations:
            args = (release_id, extra) if extra is not None else (release_id,)
            assert await _error_state(conn, sql, *args) == "55000"
        assert await _error_state(conn, "DELETE FROM app_releases WHERE id = $1", release_id) == "55000"
        assert (
            await conn.fetchrow(
                "SELECT app_id, version, manifest, manifest_checksum, registered_at FROM app_releases WHERE id = $1",
                release_id,
            )
            == before
        )


async def test_grant_generation_rejects_stale_skipped_duplicate_and_racing_writers():
    async with _fresh_database() as (conn, dsn, _):
        await _apply(conn)
        app_id = await _app(conn, f"generation-{uuid.uuid4().hex}")
        release_id = await _release(conn, app_id, "1.0.0")
        vault_id = await _vault(conn, f"generation-{uuid.uuid4().hex[:10]}")
        installation_id = await _installation(conn, app_id, vault_id, release_id)
        insert_sql = (
            "INSERT INTO installation_grants "
            "(installation_id, generation, capabilities, issuer, provenance) "
            "VALUES ($1, $2, $3::text[], $4, $5::jsonb)"
        )
        await conn.execute(
            insert_sql,
            installation_id,
            1,
            ["schema.read"],
            "operator",
            '{"source":"test"}',
        )
        assert (
            await _error_state(
                conn,
                insert_sql,
                installation_id,
                1,
                ["schema.read"],
                "operator",
                '{"source":"test"}',
            )
            == "23514"
        )
        assert (
            await _error_state(
                conn,
                insert_sql,
                installation_id,
                3,
                ["schema.read"],
                "operator",
                '{"source":"test"}',
            )
            == "23514"
        )

        async def race(generation: int) -> list[str]:
            async def attempt() -> str:
                contender = await asyncpg.connect(dsn)
                try:
                    await contender.execute(
                        insert_sql,
                        installation_id,
                        generation,
                        ["schema.read"],
                        "operator",
                        '{"source":"race"}',
                    )
                    return "ok"
                except asyncpg.PostgresError as exc:
                    return exc.sqlstate or "unknown"
                finally:
                    await contender.close()

            return await asyncio.gather(attempt(), attempt())

        for next_generation in (2, 3):
            await conn.execute(
                "UPDATE installation_grants "
                "SET status = 'revoked', revoked_at = NOW() "
                "WHERE installation_id = $1 AND status = 'active'",
                installation_id,
            )
            results = await race(next_generation)
            assert results.count("ok") == 1
            assert results.count("23514") == 1

        rows = await conn.fetch(
            "SELECT generation, status FROM installation_grants WHERE installation_id = $1 ORDER BY generation",
            installation_id,
        )
        assert [row["generation"] for row in rows] == [1, 2, 3]
        assert sum(row["status"] == "active" for row in rows) == 1


async def test_grant_capability_and_issuer_are_immutable_and_revoke_is_one_way():
    async with _fresh_database() as (conn, _, _):
        await _apply(conn)
        app_id = await _app(conn, f"grant-{uuid.uuid4().hex}")
        release_id = await _release(conn, app_id, "1.0.0")
        vault_id = await _vault(conn, f"grant-{uuid.uuid4().hex[:10]}")
        installation_id = await _installation(conn, app_id, vault_id, release_id)
        grant_id = await conn.fetchval(
            "INSERT INTO installation_grants "
            "(installation_id, generation, capabilities, issuer, provenance) "
            "VALUES ($1, 1, ARRAY['schema.read'], 'operator', "
            '\'{"source":"manual"}\'::jsonb) RETURNING id',
            installation_id,
        )
        before = await conn.fetchrow(
            "SELECT capabilities, issuer, provenance FROM installation_grants WHERE id = $1",
            grant_id,
        )
        assert (
            await _error_state(
                conn,
                "UPDATE installation_grants SET capabilities = ARRAY['schema.write'] WHERE id = $1",
                grant_id,
            )
            == "55000"
        )
        assert (
            await _error_state(
                conn,
                "UPDATE installation_grants SET issuer = 'platform' WHERE id = $1",
                grant_id,
            )
            == "55000"
        )
        assert (
            await _error_state(
                conn,
                'UPDATE installation_grants SET provenance = \'{"source":"other"}\'::jsonb WHERE id = $1',
                grant_id,
            )
            == "55000"
        )
        assert (
            await conn.fetchrow(
                "SELECT capabilities, issuer, provenance FROM installation_grants WHERE id = $1",
                grant_id,
            )
            == before
        )

        await conn.execute(
            "UPDATE installation_grants SET status = 'revoked', revoked_at = NOW() WHERE id = $1",
            grant_id,
        )
        assert (
            await _error_state(
                conn,
                "UPDATE installation_grants SET status = 'active', revoked_at = NULL WHERE id = $1",
                grant_id,
            )
            == "55000"
        )
        await conn.execute(
            "INSERT INTO installation_grants "
            "(installation_id, generation, capabilities, issuer, provenance) "
            "VALUES ($1, 2, ARRAY['schema.write'], 'platform', "
            '\'{"source":"restore"}\'::jsonb)',
            installation_id,
        )


async def test_uninstall_retains_owned_resources_and_registry_view_is_complete():
    async with _fresh_database() as (conn, _, _):
        await _apply(conn)
        app_id = await _app(conn, f"resources-{uuid.uuid4().hex}")
        release_id = await _release(conn, app_id, "1.0.0")
        vault_id = await _vault(conn, f"resources-{uuid.uuid4().hex[:10]}")
        other_vault_id = await _vault(conn, f"other-{uuid.uuid4().hex[:10]}")
        installation_id = await _installation(conn, app_id, vault_id, release_id)
        await _installation(conn, app_id, other_vault_id, release_id)
        await conn.execute(
            "INSERT INTO installation_grants "
            "(installation_id, generation, capabilities, issuer, provenance) "
            "VALUES ($1, 1, ARRAY['schema.read','schema.write'], 'operator', "
            '\'{"source":"install"}\'::jsonb)',
            installation_id,
        )
        await conn.executemany(
            "INSERT INTO app_owned_resources (installation_id, resource_kind, resource_key) VALUES ($1, $2, $3)",
            [
                (installation_id, "table", "orders"),
                (installation_id, "index", "orders_created_at_idx"),
            ],
        )
        await conn.execute(
            "UPDATE vault_app_installations SET desired_release_id = NULL, lifecycle = 'uninstalled' WHERE id = $1",
            installation_id,
        )
        await conn.execute(
            "UPDATE app_owned_resources SET status = 'retained' WHERE installation_id = $1",
            installation_id,
        )

        rows = await conn.fetch(
            "SELECT * FROM app_installation_registry WHERE installation_id = $1",
            installation_id,
        )
        assert len(rows) == 1
        row = rows[0]
        resources = json.loads(row["resources"])
        assert row["app_id"] == app_id
        assert row["vault_id"] == vault_id
        assert row["lifecycle"] == "uninstalled"
        assert row["desired_release_id"] is None
        assert row["current_release_id"] == release_id
        assert row["latest_grant_generation"] == 1
        assert row["latest_grant_status"] == "active"
        assert row["latest_grant_capabilities"] == ["schema.read", "schema.write"]
        assert {(r["kind"], r["key"], r["status"]) for r in resources} == {
            ("table", "orders", "retained"),
            ("index", "orders_created_at_idx", "retained"),
        }


async def test_resource_identity_is_unique_within_a_vault_but_reusable_across_vaults():
    async with _fresh_database() as (conn, _, _):
        await _apply(conn)
        app_a = await _app(conn, f"owner-a-{uuid.uuid4().hex}")
        app_b = await _app(conn, f"owner-b-{uuid.uuid4().hex}")
        release_a = await _release(conn, app_a, "1.0.0")
        release_b = await _release(conn, app_b, "1.0.0")
        vault_a = await _vault(conn, f"owner-a-{uuid.uuid4().hex[:10]}")
        vault_b = await _vault(conn, f"owner-b-{uuid.uuid4().hex[:10]}")
        installation_a = await _installation(conn, app_a, vault_a, release_a)
        installation_b = await _installation(conn, app_b, vault_a, release_b)
        installation_other_vault = await _installation(conn, app_b, vault_b, release_b)

        await _resource(conn, installation_a, vault_a, "table", "reef_issues")
        assert (
            await _error_state(
                conn,
                "INSERT INTO app_owned_resources "
                "(installation_id, vault_id, resource_kind, resource_key) "
                "VALUES ($1, $2, 'table', 'reef_issues')",
                installation_b,
                vault_a,
            )
            == "23505"
        )
        await _resource(
            conn,
            installation_other_vault,
            vault_b,
            "table",
            "reef_issues",
        )
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM app_owned_resources "
                "WHERE resource_kind = 'table' AND resource_key = 'reef_issues'"
            )
            == 2
        )


async def test_upgrade_from_previous_registry_shape_preserves_registry_rows():
    async with _fresh_database() as (conn, _, _):
        await conn.execute(
            """
            CREATE TABLE app_definitions (
                id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                app_key TEXT NOT NULL,
                display_name TEXT,
                description TEXT,
                metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE TABLE app_releases (
                id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                app_id UUID NOT NULL,
                version TEXT NOT NULL,
                manifest JSONB NOT NULL,
                manifest_checksum TEXT NOT NULL,
                registered_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE TABLE vault_app_installations (
                id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                app_id UUID NOT NULL,
                vault_id UUID NOT NULL,
                desired_release_id UUID,
                current_release_id UUID,
                lifecycle TEXT NOT NULL,
                blocked_reason TEXT,
                grant_generation BIGINT NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE TABLE installation_grants (
                id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                installation_id UUID NOT NULL,
                generation BIGINT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                capabilities TEXT[] NOT NULL,
                issuer TEXT NOT NULL,
                provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
                issued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                revoked_at TIMESTAMPTZ
            );
            CREATE TABLE app_owned_resources (
                id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                installation_id UUID NOT NULL,
                resource_kind TEXT NOT NULL,
                resource_key TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'owned',
                metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        app_id = await _app(conn, f"upgrade-{uuid.uuid4().hex}")
        release_id = await _release(conn, app_id, "1.0.0")
        vault_id = await _vault(conn, f"upgrade-{uuid.uuid4().hex[:10]}")
        installation_id = await conn.fetchval(
            "INSERT INTO vault_app_installations "
            "(app_id, vault_id, desired_release_id, current_release_id, lifecycle, grant_generation) "
            "VALUES ($1, $2, $3, $3, 'active', 1) RETURNING id",
            app_id,
            vault_id,
            release_id,
        )
        grant_id = await conn.fetchval(
            "INSERT INTO installation_grants "
            "(installation_id, generation, capabilities, issuer) "
            "VALUES ($1, 1, ARRAY['schema.read'], 'operator') RETURNING id",
            installation_id,
        )
        resource_id = await conn.fetchval(
            "INSERT INTO app_owned_resources "
            "(installation_id, resource_kind, resource_key, metadata) "
            "VALUES ($1, 'table', 'reef_issues', '{\"source\":\"previous\"}'::jsonb) "
            "RETURNING id",
            installation_id,
        )

        await _apply(conn)
        after_first = await conn.fetchrow(
            """
            SELECT
              (SELECT row_to_json(a) FROM app_definitions a WHERE id = $1) AS app,
              (SELECT row_to_json(r) FROM app_releases r WHERE id = $2) AS release,
              (SELECT row_to_json(i) FROM vault_app_installations i WHERE id = $3) AS installation,
              (SELECT row_to_json(g) FROM installation_grants g WHERE id = $4) AS grant,
              (SELECT row_to_json(o) FROM app_owned_resources o WHERE id = $5) AS resource
            """,
            app_id,
            release_id,
            installation_id,
            grant_id,
            resource_id,
        )
        assert json.loads(after_first["resource"])["vault_id"] == str(vault_id)

        await _apply(conn)
        after_second = await conn.fetchrow(
            """
            SELECT
              (SELECT row_to_json(a) FROM app_definitions a WHERE id = $1) AS app,
              (SELECT row_to_json(r) FROM app_releases r WHERE id = $2) AS release,
              (SELECT row_to_json(i) FROM vault_app_installations i WHERE id = $3) AS installation,
              (SELECT row_to_json(g) FROM installation_grants g WHERE id = $4) AS grant,
              (SELECT row_to_json(o) FROM app_owned_resources o WHERE id = $5) AS resource
            """,
            app_id,
            release_id,
            installation_id,
            grant_id,
            resource_id,
        )
        assert after_second == after_first


async def test_registry_identities_are_immutable_but_mutable_fields_still_update():
    async with _fresh_database() as (conn, _, _):
        await _apply(conn)
        app_a_key = f"identity-a-{uuid.uuid4().hex}"
        app_a = await _app(conn, app_a_key)
        app_b = await _app(conn, f"identity-b-{uuid.uuid4().hex}")
        release_a = await _release(conn, app_a, "1.0.0")
        vault_a = await _vault(conn, f"identity-a-{uuid.uuid4().hex[:10]}")
        vault_b = await _vault(conn, f"identity-b-{uuid.uuid4().hex[:10]}")
        installation_id = await _installation(conn, app_a, vault_a, release_a)
        resource_id = await _resource(
            conn,
            installation_id,
            vault_a,
            "table",
            "reef_issues",
        )

        assert (
            await _error_state(
                conn,
                "UPDATE app_definitions SET app_key = $2 WHERE id = $1",
                app_a,
                f"renamed-{uuid.uuid4().hex}",
            )
            == "55000"
        )
        await conn.execute(
            "UPDATE app_definitions "
            "SET display_name = 'Updated', metadata = '{\"reviewed\":true}'::jsonb "
            "WHERE id = $1",
            app_a,
        )

        for column, value in (("app_id", app_b), ("vault_id", vault_b)):
            assert (
                await _error_state(
                    conn,
                    f"UPDATE vault_app_installations SET {column} = $2 WHERE id = $1",
                    installation_id,
                    value,
                )
                == "55000"
            )
        await conn.execute(
            "UPDATE vault_app_installations SET desired_release_id = NULL, lifecycle = 'uninstalled' WHERE id = $1",
            installation_id,
        )

        identity_mutations = (
            ("installation_id", uuid.uuid4()),
            ("vault_id", vault_b),
            ("resource_kind", "document"),
            ("resource_key", "other"),
        )
        for column, value in identity_mutations:
            assert (
                await _error_state(
                    conn,
                    f"UPDATE app_owned_resources SET {column} = $2 WHERE id = $1",
                    resource_id,
                    value,
                )
                == "55000"
            )
        await conn.execute(
            "UPDATE app_owned_resources "
            "SET status = 'retained', metadata = '{\"reason\":\"uninstall\"}'::jsonb "
            "WHERE id = $1",
            resource_id,
        )

        app_row = await conn.fetchrow(
            "SELECT app_key, display_name, metadata FROM app_definitions WHERE id = $1",
            app_a,
        )
        assert app_row is not None
        assert app_row["app_key"] == app_a_key
        assert app_row["display_name"] == "Updated"
        assert json.loads(app_row["metadata"]) == {"reviewed": True}
        assert (
            await conn.fetchval(
                "SELECT lifecycle FROM vault_app_installations WHERE id = $1",
                installation_id,
            )
            == "uninstalled"
        )
        resource = await conn.fetchrow(
            "SELECT installation_id, vault_id, resource_kind, resource_key, status, metadata "
            "FROM app_owned_resources WHERE id = $1",
            resource_id,
        )
        assert resource is not None
        assert (
            resource["installation_id"],
            resource["vault_id"],
            resource["resource_kind"],
            resource["resource_key"],
        ) == (installation_id, vault_a, "table", "reef_issues")
        assert resource["status"] == "retained"
        assert json.loads(resource["metadata"]) == {"reason": "uninstall"}


async def test_vault_delete_cascades_only_its_registry_state_and_direct_deletes_fail():
    async with _fresh_database() as (conn, _, _):
        await _apply(conn)
        app_id = await _app(conn, f"delete-{uuid.uuid4().hex}")
        release_id = await _release(conn, app_id, "1.0.0")
        target_vault = await _vault(conn, f"delete-target-{uuid.uuid4().hex[:10]}")
        other_vault = await _vault(conn, f"delete-other-{uuid.uuid4().hex[:10]}")
        target_installation = await _installation(
            conn,
            app_id,
            target_vault,
            release_id,
        )
        other_installation = await _installation(
            conn,
            app_id,
            other_vault,
            release_id,
        )
        grant_id = await conn.fetchval(
            "INSERT INTO installation_grants "
            "(installation_id, generation, capabilities, issuer) "
            "VALUES ($1, 1, ARRAY['schema.write'], 'operator') RETURNING id",
            target_installation,
        )
        resource_id = await _resource(
            conn,
            target_installation,
            target_vault,
            "table",
            "reef_issues",
        )

        assert (
            await _error_state(
                conn,
                "DELETE FROM installation_grants WHERE id = $1",
                grant_id,
            )
            == "55000"
        )
        assert (
            await _error_state(
                conn,
                "DELETE FROM app_owned_resources WHERE id = $1",
                resource_id,
            )
            == "55000"
        )
        assert (
            await _error_state(
                conn,
                "DELETE FROM vault_app_installations WHERE id = $1",
                target_installation,
            )
            == "55000"
        )

        await conn.execute("DELETE FROM vaults WHERE id = $1", target_vault)

        assert (
            await conn.fetchval(
                "SELECT count(*) FROM vault_app_installations WHERE vault_id = $1",
                target_vault,
            )
            == 0
        )
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM installation_grants WHERE installation_id = $1",
                target_installation,
            )
            == 0
        )
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM app_owned_resources WHERE vault_id = $1",
                target_vault,
            )
            == 0
        )
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM vault_app_installations WHERE id = $1",
                other_installation,
            )
            == 1
        )
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM app_definitions WHERE id = $1",
                app_id,
            )
            == 1
        )
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM app_releases WHERE id = $1",
                release_id,
            )
            == 1
        )


async def test_existing_data_is_unchanged_and_ordinary_role_gets_no_registry_access():
    async with _fresh_database() as (conn, _, name):
        user_id = await conn.fetchval(
            "INSERT INTO users (username, email, password_hash) VALUES ($1, $2, 'sentinel-hash') RETURNING id",
            f"user-{uuid.uuid4().hex}",
            f"{uuid.uuid4().hex}@example.invalid",
        )
        vault_id = await _vault(conn, f"sentinel-{uuid.uuid4().hex[:10]}")
        token_id = await conn.fetchval(
            "INSERT INTO tokens "
            "(user_id, name, token_hash, token_prefix) "
            "VALUES ($1, 'sentinel', $2, 'akb_test') RETURNING id",
            user_id,
            uuid.uuid4().hex,
        )
        table_id = await conn.fetchval(
            "INSERT INTO vault_tables (vault_id, name, columns) VALUES ($1, 'sentinel', '[]'::jsonb) RETURNING id",
            vault_id,
        )
        row_id = await conn.fetchval(
            "INSERT INTO vault_table_rows (table_id, data) VALUES ($1, '{\"preserved\":true}'::jsonb) RETURNING id",
            table_id,
        )
        before = await conn.fetchrow(
            """
            SELECT
              (SELECT row_to_json(u) FROM users u WHERE id = $1) AS user_row,
              (SELECT row_to_json(v) FROM vaults v WHERE id = $2) AS vault_row,
              (SELECT row_to_json(t) FROM tokens t WHERE id = $3) AS token_row,
              (SELECT row_to_json(r) FROM vault_table_rows r WHERE id = $4) AS data_row
            """,
            user_id,
            vault_id,
            token_id,
            row_id,
        )

        await _apply(conn)

        after = await conn.fetchrow(
            """
            SELECT
              (SELECT row_to_json(u) FROM users u WHERE id = $1) AS user_row,
              (SELECT row_to_json(v) FROM vaults v WHERE id = $2) AS vault_row,
              (SELECT row_to_json(t) FROM tokens t WHERE id = $3) AS token_row,
              (SELECT row_to_json(r) FROM vault_table_rows r WHERE id = $4) AS data_row
            """,
            user_id,
            vault_id,
            token_id,
            row_id,
        )
        assert after == before

        role = f"akb_registry_probe_{uuid.uuid4().hex[:12]}"
        await conn.execute(f'CREATE ROLE "{role}" NOLOGIN')
        await conn.execute(f'GRANT CONNECT ON DATABASE "{name}" TO "{role}"')
        await conn.execute(f'GRANT USAGE ON SCHEMA public TO "{role}"')
        await conn.execute(f'SET ROLE "{role}"')
        try:
            assert await _error_state(conn, "SELECT count(*) FROM app_definitions") == "42501"
            assert await _error_state(conn, "SELECT count(*) FROM app_installation_registry") == "42501"
        finally:
            await conn.execute("RESET ROLE")
