"""Opt-in Docker regression for reset isolation, run by the E2E workflow."""

from __future__ import annotations

import asyncio
import os
import secrets
import socket
from contextlib import ExitStack, suppress

import asyncpg
import boto3
import httpx
import pytest

from tests.test_e2e_runtime_unit import COMPOSE_FILE, REPO_ROOT, E2ERuntime, RuntimeConfig


@pytest.mark.needs_resource("an isolated Docker runtime; the e2e workflow runs it with AKB_TEST_RESET_ISOLATION=1")
@pytest.mark.skipif(os.environ.get("AKB_TEST_RESET_ISOLATION") != "1", reason="requires isolated Docker runtime")
async def test_reset_waits_for_writers_and_reuses_dependencies_with_isolated_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("AKB_E2E_USERNAME", "reset-test-" + secrets.token_hex(8))
    monkeypatch.setenv("AKB_E2E_PASSWORD", secrets.token_urlsafe(32))
    with ExitStack() as stack:
        sockets = [stack.enter_context(socket.socket()) for _ in range(5)]
        for listener in sockets:
            listener.bind(("127.0.0.1", 0))
        app, embed, fixture, postgres, minio = [listener.getsockname()[1] for listener in sockets]
    runtime = E2ERuntime(RuntimeConfig(
        checkout=REPO_ROOT, runtime_root=tmp_path / "runtime", mode="serve",
        compose_file=COMPOSE_FILE, compose_project="akb-reset-test-" + secrets.token_hex(6),
        app_port=app, embed_port=embed, fixture_port=fixture,
        postgres_port=postgres, minio_port=minio, profile="transport-proxy",
    ))
    old_db = None
    reset_request = None
    try:
        await runtime.prepare()
        old_db = await asyncpg.connect(
            host="127.0.0.1", port=postgres, user="akb", password="akb", database="akb",  # pragma: allowlist secret
        )
        username, _password = runtime.config.credentials.values()
        baseline_user = await old_db.fetchrow(
            "SELECT id, password_hash FROM users WHERE username=$1", username,
        )
        await old_db.execute("CREATE TABLE reset_sentinel(value text)")
        await old_db.execute("BEGIN")
        await old_db.execute("INSERT INTO reset_sentinel VALUES ('previous-generation')")
        await old_db.execute("LOCK TABLE chunks IN ROW EXCLUSIVE MODE")
        # Reusing a computed hash must still repair mutable account state.
        await old_db.execute("""
            UPDATE users SET password_hash=$2, is_admin=FALSE,
                             credential_change_required=TRUE
             WHERE username=$1
        """, username, "test-modified-hash")
        old_git = runtime.config.vault_dir
        (old_git / "reset-sentinel").write_text("previous-generation")
        old_pat = runtime._pat_value
        old_stdio = runtime._children["stdio"].process.pid
        old_backend = runtime._children["backend"].process
        old_embed = runtime._children["embed"].process
        old_identity = runtime._dependency_identity
        old_bucket = runtime.config.s3_bucket
        s3 = boto3.client(
            "s3", endpoint_url=runtime.config.minio_origin,
            aws_access_key_id="akb-ci", aws_secret_access_key="akb-ci-secret",  # pragma: allowlist secret
        )
        await asyncio.to_thread(s3.put_object, Bucket=old_bucket, Key="reset-sentinel", Body=b"old")

        # A transaction from the prior scenario remains active after the backend
        # drains. Reset must wait for it rather than truncate concurrently or
        # destroy the container to interrupt the writer.
        async with httpx.AsyncClient(timeout=210) as client:
            reset_request = asyncio.create_task(client.post(
                runtime.config.fixture_origin + "/reset", json={"scenario": "empty"},
            ))
            await asyncio.wait_for(old_backend.wait(), timeout=30)
            await asyncio.sleep(0.2)
            assert not reset_request.done()
            assert not old_db.is_closed()
            assert runtime.fixture_health()["status"] != "ready"
            # SQL admission is fenced while reset waits for existing clients.
            # This also closes the race between checking for writers and cleanup.
            async with asyncio.timeout(5):
                while True:
                    try:
                        admitted = await asyncpg.connect(
                            host="127.0.0.1", port=postgres, user="akb", password="akb", database="akb",  # pragma: allowlist secret
                        )
                    except asyncpg.PostgresError as exc:
                        assert exc.sqlstate == "55000"
                        break
                    else:
                        await admitted.close()
                        await asyncio.sleep(0.05)
            assert await old_db.fetchval("SELECT count(*) FROM reset_sentinel") == 1
            await old_db.execute("COMMIT")
            await old_db.close()
            response = await reset_request
            assert response.status_code == 200
            assert response.json()["status"] == "ready"

        assert old_db.is_closed()

        new_db = await asyncpg.connect(
            host="127.0.0.1", port=postgres, user="akb", password="akb", database="akb",  # pragma: allowlist secret
        )
        try:
            assert await new_db.fetchval("SELECT to_regclass('public.reset_sentinel') IS NULL")
            assert await new_db.fetchval("SELECT count(*) FROM bm25_vocab_epoch") == 1
            assert await new_db.fetchval("SELECT epoch FROM bm25_vocab_epoch") == 0
            restored = await new_db.fetchrow("""
                SELECT id, password_hash, is_admin, account_status, credential_change_required
                  FROM users WHERE username=$1
            """, username)
            assert restored["id"] == baseline_user["id"]
            hash_reused = restored["password_hash"] == baseline_user["password_hash"]
            assert hash_reused
            assert restored["is_admin"] is True
            assert restored["account_status"] == "active"
            assert restored["credential_change_required"] is False
        finally:
            await new_db.close()
        assert runtime._dependency_identity == old_identity
        assert await asyncio.to_thread(runtime._dependency_identity_snapshot) == old_identity
        assert runtime._children["embed"].process is old_embed
        assert old_embed.returncode is None
        assert runtime._children["backend"].process.pid != old_backend.pid
        assert runtime.config.vault_dir != old_git
        # A detached old Git operation can only write its retired path.
        (old_git / "late-write").write_text("completed after reset")
        assert not (runtime.config.vault_dir / "late-write").exists()
        assert not (runtime.config.vault_dir / "reset-sentinel").exists()
        assert runtime.config.s3_bucket != old_bucket
        # MinIO is retained; any delayed old PUT is contained in the old bucket.
        await asyncio.to_thread(s3.put_object, Bucket=old_bucket, Key="late-write", Body=b"old")
        old_objects = await asyncio.to_thread(s3.list_objects_v2, Bucket=old_bucket)
        assert {item["Key"] for item in old_objects.get("Contents", [])} >= {"reset-sentinel", "late-write"}
        objects = await asyncio.to_thread(s3.list_objects_v2, Bucket=runtime.config.s3_bucket)
        assert not objects.get("Contents")
        assert runtime._pat_value != old_pat
        assert runtime._children["stdio"].process.pid != old_stdio
        assert runtime._stdio_initialize_observed
        assert runtime.fixture_health()["status"] == "ready"

        # Verify the backend actually loaded the new generation's config, not
        # just that the supervisor computed a different directory name.
        async with httpx.AsyncClient(
            base_url=runtime.config.app_origin,
            headers={"Authorization": "Bearer " + runtime._pat_value}, timeout=30,
        ) as client:
            stale = await client.get("/api/v1/vaults", headers={"Authorization": "Bearer " + old_pat})
            assert stale.status_code == 401
            response = await client.post("/api/v1/vaults", params={"name": "reset-generation-check"})
            response.raise_for_status()
            response = await client.post("/api/v1/documents", json={
                "vault": "reset-generation-check", "collection": "checks", "title": "generation",
                "content": "New generation document.", "type": "note", "tags": [],
            })
            response.raise_for_status()
        assert (runtime.config.vault_dir / "reset-generation-check.git" / "HEAD").is_file()
        assert not (old_git / "reset-generation-check.git").exists()

        # A missing canonical table is schema damage, not user-created data to
        # discard. Reset must fail before cleanup and retain SQL admission fence.
        assert ("public", "oidc_transients") in runtime._schema_tables
        damaged_db = await asyncpg.connect(
            host="127.0.0.1", port=postgres, user="akb", password="akb", database="akb",  # pragma: allowlist secret
        )
        try:
            assert await damaged_db.fetchval("SELECT to_regclass('public.oidc_transients') IS NOT NULL")
            await damaged_db.execute("DROP TABLE public.oidc_transients")
        finally:
            await damaged_db.close()
        async with httpx.AsyncClient(timeout=210) as client:
            failed = await client.post(runtime.config.fixture_origin + "/reset", json={"scenario": "empty"})
            assert failed.status_code == 500
            health = await client.get(runtime.config.fixture_origin + "/health")
            discovery = await client.get(runtime.config.fixture_origin + "/discover")
        assert health.json()["status"] == "failed"
        assert discovery.json()["status"] == "failed"
        assert runtime._dependency_reset_evidence["failed_phase"] == "reset_postgres"
        maintenance = await asyncpg.connect(
            host="127.0.0.1", port=postgres, user="akb", password="akb", database="postgres",  # pragma: allowlist secret
        )
        try:
            assert await maintenance.fetchval("SELECT datallowconn FROM pg_database WHERE datname='akb'") is False
        finally:
            await maintenance.close()
        with pytest.raises(asyncpg.PostgresError) as denied:
            await asyncpg.connect(
                host="127.0.0.1", port=postgres, user="akb", password="akb", database="akb",  # pragma: allowlist secret
            )
        assert denied.value.sqlstate == "55000"
    finally:
        if reset_request is not None and not reset_request.done():
            reset_request.cancel()
            with suppress(asyncio.CancelledError):
                await reset_request
        if old_db is not None and not old_db.is_closed():
            await old_db.close(timeout=2)
        await runtime.cleanup()
