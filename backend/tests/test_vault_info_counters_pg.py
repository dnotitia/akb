"""Vault info document counters follow the active authority (akb#525).

On `postgres_native` the native document path never writes the legacy
`documents` catalog, so the legacy `COUNT(*) FROM documents` /
latest-`updated_at` queries under-report from the cutover onward. These
tests pin the backend-switched counter queries against a real Postgres
(init.sql + 048 native core), the same convention as
test_native_file_projection_pg.py (fresh database per run, self-skip if
unreachable).
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import uuid
from pathlib import Path

import asyncpg
import pytest

from app.services import access_service


pytestmark = pytest.mark.asyncio

_BACKEND = Path(__file__).resolve().parents[1]
_INIT_SQL = (_BACKEND / "app" / "db" / "init.sql").read_text(encoding="utf-8")
_MIGRATIONS = _BACKEND / "app" / "db" / "migrations"
_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:5433/akb",  # pragma: allowlist secret
)


async def _reachable() -> bool:
    try:
        conn = await asyncpg.connect(_DSN, timeout=2)
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


def _load(filename: str):
    path = _MIGRATIONS / filename
    spec = importlib.util.spec_from_file_location(f"migration_vault_info_{filename}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def pool(monkeypatch):
    if not await _reachable():
        pytest.skip(f"Postgres not reachable at {_DSN}")
    name = f"akb_vault_info_counters_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(_DSN)
    conn = None
    pool = None
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        dsn = f"{_DSN.rsplit('/', 1)[0]}/{name}"
        conn = await asyncpg.connect(dsn)
        await conn.execute(_INIT_SQL)
        for filename in (
            "010_external_git_mirror.py",
            "015_events_outbox.py",
            "044_vault_write_policy.py",
            "045_vault_write_grant_actions.py",
            "048_native_revision_core.py",
        ):
            await _load(filename).migrate(conn=conn)
        await conn.close()
        conn = None
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)

        from app.repositories import vault_write_policy_repo
        from app.services import auth_service, table_service

        async def _get_pool():
            return pool

        monkeypatch.setattr(auth_service, "get_pool", _get_pool)
        monkeypatch.setattr(vault_write_policy_repo, "get_pool", _get_pool)
        monkeypatch.setattr(access_service, "get_pool", _get_pool)
        monkeypatch.setattr(table_service, "get_pool", _get_pool)
        yield pool
    finally:
        if pool is not None:
            await pool.close()
        if conn is not None and not conn.is_closed():
            await conn.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


async def _create_user(pool) -> uuid.UUID:
    user_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:12]
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, username, email, password_hash) VALUES ($1, $2, $3, 'x')",
            user_id,
            f"vic-{suffix}",
            f"vic-{suffix}@example.com",
        )
    return user_id


async def _create_vault(pool, owner_id: uuid.UUID) -> tuple[uuid.UUID, str]:
    vault_id = uuid.uuid4()
    name = f"vic-vault-{uuid.uuid4().hex[:10]}"
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO vaults (id, name, git_path, owner_id) VALUES ($1, $2, $3, $4)",
            vault_id,
            name,
            f"/tmp/vic-{vault_id}.git",
            owner_id,
        )
    return vault_id, name


async def _insert_native_doc(pool, vault_id: uuid.UUID, path: str, actor: str = "fixture") -> dict:
    """Minimal live native document: resource + head revision + manifest.

    Bypasses the service layer on purpose — the counters must read whatever
    the native path durably wrote, regardless of which service wrote it.
    Companion rows (activity event, invalidation intent) satisfy the
    DEFERRABLE revision FKs. One explicit transaction for resource + revision
    + head-set: the head-validation CONSTRAINT TRIGGER is DEFERRABLE, and
    asyncpg otherwise autocommits each statement (verified by probe), which
    would fire the trigger before the head exists. Same single-transaction
    shape as the service layer's publish.
    Returns the row ids so callers can build follow-up revisions on top.
    """
    resource_id = uuid.uuid4()
    revision_id = uuid.uuid4().hex + uuid.uuid4().hex[:8]  # 40-hex shape
    manifest_id = uuid.uuid4()
    payload_id = uuid.uuid4()
    activity_id = uuid.uuid4()
    intent_id = uuid.uuid4()
    mutation_id = uuid.uuid4()
    # Distinct body per document: payloads dedupe on (namespace, digest, size).
    body = f"hello {resource_id.hex[:8]}\n".encode()
    digest = hashlib.sha256(body).hexdigest()
    size = len(body)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO native_resources (resource_id, namespace_id, surface,
                content_profile, current_path, lifecycle)
                VALUES ($1, $2, 'document', 'text', $3, 'live')
                """,
                resource_id,
                vault_id,
                path,
            )
            await conn.execute(
                """
                INSERT INTO m1_reference_payloads (
                    payload_id, namespace_id, content_profile, digest, byte_size,
                    encoding, selected_placement, verification_profile, canonical_bytes
                ) VALUES ($1, $2, 'text', $3, $4, 'utf-8',
                          'm1-reference-payload-v1', 'sha256-size-utf8-v1', $5)
                """,
                payload_id,
                vault_id,
                digest,
                size,
                body,
            )
            await conn.execute(
                """
                INSERT INTO native_payload_manifests (
                    payload_manifest_id, namespace_id, resource_id, content_profile,
                    digest, byte_size, encoding, selected_placement,
                    private_locator, verification_profile
                ) VALUES ($1, $2, $3, 'text', $4, $6, 'utf-8',
                          'm1-reference-payload-v1', $5, 'sha256-size-utf8-v1')
                """,
                manifest_id,
                vault_id,
                resource_id,
                digest,
                payload_id,
                size,
            )
            await conn.execute(
                """
                INSERT INTO native_revision_activity (
                    activity_event_id, namespace_id, resource_id, revision_id,
                    action, actor, occurred_at
                ) VALUES ($1, $2, $3, $4, 'create', $5, NOW())
                """,
                activity_id,
                vault_id,
                resource_id,
                revision_id,
                actor,
            )
            await conn.execute(
                """
                INSERT INTO native_invalidation_intents (
                    intent_id, namespace_id, resource_id, revision_id,
                    reason, occurred_at
                ) VALUES ($1, $2, $3, $4, 'create', NOW())
                """,
                intent_id,
                vault_id,
                resource_id,
                revision_id,
            )
            await conn.execute(
                """
                INSERT INTO native_revisions (
                    revision_id, namespace_id, resource_id, action,
                    path_at_revision, path_from, path_to, payload_manifest_id,
                    mutation_id, request_fingerprint,
                    actor, activity_event_id, invalidation_intent_id
                ) VALUES ($1, $2, $3, 'create', $4, NULL, $4, $5, $6, $7, $8, $9, $10)
                """,
                revision_id,
                vault_id,
                resource_id,
                path,
                manifest_id,
                mutation_id,
                "c" * 64,
                actor,
                activity_id,
                intent_id,
            )
            await conn.execute(
                "UPDATE native_resources SET head_revision_id = $2 WHERE resource_id = $1",
                resource_id,
                revision_id,
            )
    return {
        "resource_id": resource_id,
        "revision_id": revision_id,
        "manifest_id": manifest_id,
        "payload_id": payload_id,
        "activity_id": activity_id,
        "intent_id": intent_id,
        "mutation_id": mutation_id,
        "digest": digest,
        "size": size,
        "body": body,
    }


def _native_backend(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "document_revision_backend", "postgres_native", raising=False)
    monkeypatch.setattr(settings, "native_revision_m1_measurement_only", False, raising=False)
    monkeypatch.setattr(settings, "db_name", "akb", raising=False)


def _legacy_backend(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "document_revision_backend", "bare_git", raising=False)


async def test_native_counters_read_native_tables(pool, monkeypatch):
    """akb#525: on postgres_native, document_count/last_activity come from
    native_resources/native_revisions -- the legacy `documents` table is
    untouched and must not be read."""
    _native_backend(monkeypatch)
    owner_id = await _create_user(pool)
    vault_id, vault_name = await _create_vault(pool, owner_id)
    await _insert_native_doc(pool, vault_id, "notes/a.md", actor="fixture-owner")
    await _insert_native_doc(pool, vault_id, "notes/b.md", actor="fixture-owner")

    info = await access_service.get_vault_info(str(owner_id), vault_name)

    assert info["document_count"] == 2
    assert info["last_activity"] is not None
    assert info["last_active_user"] == "fixture-owner"


async def test_native_counters_ignore_deleted_and_files(pool, monkeypatch):
    """Only live document surfaces count: deleted resources and native text
    files are invisible to document_count."""
    _native_backend(monkeypatch)
    owner_id = await _create_user(pool)
    vault_id, vault_name = await _create_vault(pool, owner_id)
    await _insert_native_doc(pool, vault_id, "notes/a.md")
    # A document resource whose head is a delete revision: present in the
    # table but lifecycle='deleted', so counters must skip it. The delete
    # revision is a child of the live create head, so it shares the
    # transaction discipline the probe proved: activity + intent + revision
    # while the resource still carries its live create head, then the flip
    # — all in ONE transaction (the failed two-transaction and autocommit
    # variants break either the DEFERRABLE activity FK or the deferred
    # head-validation trigger).
    gone = await _insert_native_doc(pool, vault_id, "notes/gone.md")
    gone_rev = uuid.uuid4().hex + uuid.uuid4().hex[:8]
    gone_activity = uuid.uuid4()
    gone_intent = uuid.uuid4()
    gone_mutation = uuid.uuid4()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO native_revision_activity (
                    activity_event_id, namespace_id, resource_id, revision_id,
                    action, actor, occurred_at
                ) VALUES ($1, $2, $3, $4, 'delete', 'fixture', NOW())
                """,
                gone_activity,
                vault_id,
                gone["resource_id"],
                gone_rev,
            )
            await conn.execute(
                """
                INSERT INTO native_invalidation_intents (
                    intent_id, namespace_id, resource_id, revision_id,
                    reason, occurred_at
                ) VALUES ($1, $2, $3, $4, 'delete', NOW())
                """,
                gone_intent,
                vault_id,
                gone["resource_id"],
                gone_rev,
            )
            await conn.execute(
                """
                INSERT INTO native_revisions (
                    revision_id, namespace_id, resource_id, parent_revision_id,
                    action, path_at_revision, path_from, path_to,
                    mutation_id, request_fingerprint,
                    actor, activity_event_id, invalidation_intent_id
                ) VALUES ($1, $2, $3, $4, 'delete', 'notes/gone.md', NULL, NULL, $5, $6, 'fixture', $7, $8)
                """,
                gone_rev,
                vault_id,
                gone["resource_id"],
                gone["revision_id"],
                gone_mutation,
                "d" * 64,
                gone_activity,
                gone_intent,
            )
            await conn.execute(
                "UPDATE native_resources SET lifecycle = 'deleted', head_revision_id = $2 WHERE resource_id = $1",
                gone["resource_id"],
                gone_rev,
            )
        # A live native text file (same authority namespace, other surface).
        # Files publish heads exactly like documents (the head-validation
        # trigger requires every resource row to carry one), so mint a full
        # create chain and flip only the surface. A distinct body keeps it
        # from deduping against the documents above.
        file_doc = await _insert_native_doc(pool, vault_id, "files/a.txt")
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE native_resources SET surface = 'file' WHERE resource_id = $1",
                file_doc["resource_id"],
            )

    info = await access_service.get_vault_info(str(owner_id), vault_name)

    assert info["document_count"] == 1


async def test_native_counters_empty_vault_reads_zero_not_stale(pool, monkeypatch):
    """A vault with no native documents reads 0/None -- never a frozen
    pre-cutover number (there is no legacy row to freeze here by design)."""
    _native_backend(monkeypatch)
    owner_id = await _create_user(pool)
    _vault_id, vault_name = await _create_vault(pool, owner_id)

    info = await access_service.get_vault_info(str(owner_id), vault_name)

    assert info["document_count"] == 0
    assert info["last_activity"] is None
    assert info["last_active_user"] is None


async def test_legacy_counters_unchanged_on_bare_git(pool, monkeypatch):
    """On bare_git the legacy catalog queries run exactly as before -- a
    native resource must NOT leak into the legacy count."""
    _legacy_backend(monkeypatch)
    owner_id = await _create_user(pool)
    vault_id, vault_name = await _create_vault(pool, owner_id)
    await _insert_native_doc(pool, vault_id, "notes/a.md")

    info = await access_service.get_vault_info(str(owner_id), vault_name)

    assert info["document_count"] == 0
    assert info["last_activity"] is None
