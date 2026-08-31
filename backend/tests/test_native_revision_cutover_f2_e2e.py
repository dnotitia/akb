"""F2: seed a real Legacy AKB, stop it, and cut over the same DB/Git."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import sys
import uuid
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
import yaml


_BACKEND = Path(__file__).resolve().parents[1]
_CI = _BACKEND / "scripts" / "ci"
sys.path.insert(0, str(_CI))

from e2e_runtime import CredentialNames, E2ERuntime, RuntimeConfig  # noqa: E402


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AKB_RUN_NATIVE_CUTOVER_F2") != "1",
        reason="set AKB_RUN_NATIVE_CUTOVER_F2=1 for the disposable live fixture",
    ),
]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _rpc_payload(response: httpx.Response) -> dict[str, Any]:
    response.raise_for_status()
    if "text/event-stream" not in response.headers.get("content-type", ""):
        value = response.json()
        assert isinstance(value, dict)
        return value
    for line in response.text.splitlines():
        if line.startswith("data:"):
            value = json.loads(line.removeprefix("data:").strip())
            assert isinstance(value, dict)
            return value
    raise AssertionError("MCP response carried no JSON-RPC data")


async def _register_and_pat(
    client: httpx.AsyncClient,
    *,
    username: str,
    password: str,
) -> str:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.invalid",
            "password": password,
        },
    )
    assert response.status_code in {200, 201}, response.text
    response = await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    response.raise_for_status()
    session = response.json()["token"]
    response = await client.post(
        "/api/v1/auth/tokens",
        headers={"Authorization": f"Bearer {session}"},
        json={"name": "native-cutover-f2"},
    )
    response.raise_for_status()
    return str(response.json()["token"])


async def _mcp_session(client: httpx.AsyncClient, token: str) -> str:
    response = await client.post(
        "/mcp/",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "native-cutover-f2", "version": "1"},
            },
        },
    )
    _rpc_payload(response)
    session_id = response.headers.get("mcp-session-id")
    assert session_id
    response = await client.post(
        "/mcp/",
        headers={
            "Authorization": f"Bearer {token}",
            "mcp-session-id": session_id,
            "Accept": "application/json, text/event-stream",
        },
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert response.status_code in {200, 202}
    return session_id


async def _mcp_call(
    client: httpx.AsyncClient,
    *,
    token: str,
    session_id: str,
    request_id: int,
    tool: str,
    arguments: dict[str, Any],
) -> tuple[dict[str, Any], Any]:
    response = await client.post(
        "/mcp/",
        headers={
            "Authorization": f"Bearer {token}",
            "mcp-session-id": session_id,
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
    )
    payload = _rpc_payload(response)
    result = payload["result"]
    assert isinstance(result, dict)
    content = result.get("content")
    assert isinstance(content, list) and content
    text = content[0].get("text")
    try:
        decoded = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        decoded = text
    return result, decoded


async def _legacy_snapshot(
    conn: asyncpg.Connection,
    vault_names: list[str],
) -> dict[str, list[tuple[Any, ...]]]:
    return {
        "vaults": [
            tuple(row)
            for row in await conn.fetch(
                """
                SELECT id, name, description, owner_id, public_access, status, git_path
                  FROM vaults
                 WHERE name = ANY($1::text[])
                 ORDER BY name
                """,
                vault_names,
            )
        ],
        "access": [
            tuple(row)
            for row in await conn.fetch(
                """
                SELECT v.name, u.username, a.role, a.granted_by
                  FROM vault_access a
                  JOIN vaults v ON v.id = a.vault_id
                  JOIN users u ON u.id = a.user_id
                 WHERE v.name = ANY($1::text[])
                 ORDER BY v.name, u.username
                """,
                vault_names,
            )
        ],
        "documents": [
            tuple(row)
            for row in await conn.fetch(
                """
                SELECT d.id, v.name, d.path, d.title, d.current_commit,
                       d.content_hash, d.source, d.created_at, d.updated_at
                  FROM documents d
                  JOIN vaults v ON v.id = d.vault_id
                 WHERE v.name = ANY($1::text[])
                 ORDER BY v.name, d.path
                """,
                vault_names,
            )
        ],
        "aliases": [
            tuple(row)
            for row in await conn.fetch(
                """
                SELECT v.name, a.resource_type, a.old_ref, a.resource_id
                  FROM resource_aliases a
                  JOIN vaults v ON v.id = a.vault_id
                 WHERE v.name = ANY($1::text[])
                 ORDER BY v.name, a.resource_type, a.old_ref
                """,
                vault_names,
            )
        ],
        "files": [
            tuple(row)
            for row in await conn.fetch(
                """
                SELECT f.id, v.name, f.kind, f.upload_state, f.name, f.s3_key,
                       f.mime_type, f.size_bytes, f.content_hash, f.storage_version
                  FROM vault_files f
                  JOIN vaults v ON v.id = f.vault_id
                 WHERE v.name = ANY($1::text[])
                 ORDER BY v.name, f.name
                """,
                vault_names,
            )
        ],
    }


async def _pending_counts(conn: asyncpg.Connection) -> dict[str, int]:
    row = await conn.fetchrow(
        """
        SELECT
            (SELECT count(*) FROM chunks
              WHERE vector_indexed_at IS NULL
                AND vector_abandoned_at IS NULL) AS chunks,
            (SELECT count(*) FROM vector_delete_outbox
              WHERE processed_at IS NULL) AS vector_delete,
            (SELECT count(*) FROM s3_delete_outbox
              WHERE processed_at IS NULL) AS s3_delete,
            (SELECT count(*) FROM native_invalidation_intents
              WHERE completed_at IS NULL) AS native_invalidation,
            (SELECT count(*) FROM native_file_projection_outbox
              WHERE completed_at IS NULL) AS native_file_projection
        """
    )
    assert row is not None
    return {key: int(value) for key, value in dict(row).items()}


async def test_real_legacy_seed_stops_then_backfills_same_database_and_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = _BACKEND.parent
    runtime_root = tmp_path / "native-cutover-f2-runtime"
    suffix = uuid.uuid4().hex[:10]
    username_env = f"AKB_F2_USER_{suffix.upper()}"
    password_env = f"AKB_F2_PASSWORD_{suffix.upper()}"
    monkeypatch.setenv(username_env, f"f2-bootstrap-{suffix}")
    monkeypatch.setenv(password_env, f"F2-bootstrap-{suffix}-pass")
    runtime = E2ERuntime(
        RuntimeConfig(
            checkout=checkout,
            runtime_root=runtime_root,
            mode="serve",
            compose_file=_CI / "dependency-compose.yaml",
            compose_project=f"akb-native-cutover-f2-{suffix}",
            app_port=_free_port(),
            embed_port=_free_port(),
            fixture_port=_free_port(),
            credentials=CredentialNames(
                username_env=username_env,
                password_env=password_env,
            ),
        )
    )
    original_cwd = Path.cwd()
    pool: asyncpg.Pool | None = None
    try:
        await runtime.prepare()
        owner = f"f2-owner-{suffix}"
        reader = f"f2-reader-{suffix}"
        password = f"F2-user-{suffix}-pass"
        vault_one = f"f2-one-{suffix}"
        vault_two = f"f2-two-{suffix}"
        external_name = f"f2-external-{suffix}"
        vault_names = [vault_one, vault_two]

        async with httpx.AsyncClient(
            base_url=runtime.config.app_origin,
            timeout=30,
            follow_redirects=True,
        ) as client:
            owner_pat = await _register_and_pat(
                client,
                username=owner,
                password=password,
            )
            reader_pat = await _register_and_pat(
                client,
                username=reader,
                password=password,
            )
            owner_headers = {"Authorization": f"Bearer {owner_pat}"}
            reader_headers = {"Authorization": f"Bearer {reader_pat}"}
            session_id = await _mcp_session(client, owner_pat)

            response = await client.post(
                "/api/v1/vaults",
                headers=owner_headers,
                params={"name": vault_one, "description": "F2 REST vault"},
            )
            response.raise_for_status()
            result, created_vault = await _mcp_call(
                client,
                token=owner_pat,
                session_id=session_id,
                request_id=2,
                tool="akb_create_vault",
                arguments={"name": vault_two, "description": "F2 MCP vault"},
            )
            assert result.get("isError") is not True
            assert created_vault["name"] == vault_two

            response = await client.post(
                f"/api/v1/vaults/{vault_one}/grant",
                headers=owner_headers,
                json={"user": reader, "role": "reader"},
            )
            response.raise_for_status()

            response = await client.post(
                "/api/v1/documents",
                headers=owner_headers,
                json={
                    "vault": vault_one,
                    "collection": "guides",
                    "slug": "moving",
                    "title": "Moving document",
                    "content": "# Moving\n\nversion one\n",
                    "status": "active",
                },
            )
            response.raise_for_status()
            moving_initial = response.json()
            response = await client.patch(
                f"/api/v1/documents/{vault_one}/guides/moving.md",
                headers=owner_headers,
                json={"content": "# Moving\n\nversion two\n", "message": "F2 update"},
            )
            response.raise_for_status()
            response = await client.post(
                f"/api/v1/documents/{vault_one}/guides/moving.md/move",
                headers=owner_headers,
                json={"collection": "archive", "slug": "moved", "message": "F2 move"},
            )
            response.raise_for_status()
            moved = response.json()
            assert moved["path"] == "archive/moved.md"

            lifecycle_path = "lifecycle/recreated.md"
            response = await client.post(
                "/api/v1/documents",
                headers=owner_headers,
                json={
                    "vault": vault_one,
                    "collection": "lifecycle",
                    "slug": "recreated",
                    "title": "Lifecycle old",
                    "content": "old lifecycle body\n",
                },
            )
            response.raise_for_status()
            response = await client.delete(
                f"/api/v1/documents/{vault_one}/{lifecycle_path}",
                headers=owner_headers,
            )
            response.raise_for_status()
            response = await client.post(
                "/api/v1/documents",
                headers=owner_headers,
                json={
                    "vault": vault_one,
                    "collection": "lifecycle",
                    "slug": "recreated",
                    "title": "Lifecycle new",
                    "content": "new lifecycle body\n",
                },
            )
            response.raise_for_status()

            result, mcp_document = await _mcp_call(
                client,
                token=owner_pat,
                session_id=session_id,
                request_id=3,
                tool="akb_put",
                arguments={
                    "vault": vault_two,
                    "collection": "notes",
                    "slug": "mcp-document",
                    "title": "MCP document",
                    "content": "# MCP\n\nversion one\n",
                    "status": "active",
                },
            )
            assert result.get("isError") is not True
            assert mcp_document["path"] == "notes/mcp-document.md"
            response = await client.patch(
                f"/api/v1/documents/{vault_two}/notes/mcp-document.md",
                headers=owner_headers,
                json={"content": "# MCP\n\nversion two\n", "message": "F2 MCP update"},
            )
            response.raise_for_status()

            current = await client.get(
                f"/api/v1/documents/{vault_one}/archive/moved.md",
                headers=owner_headers,
            )
            current.raise_for_status()
            pinned = await client.get(
                f"/api/v1/documents/{vault_one}/guides/moving.md",
                headers=owner_headers,
                params={"version": moving_initial["commit_hash"]},
            )
            pinned.raise_for_status()
            history = await client.get(
                f"/api/v1/history/{vault_one}/archive/moved.md",
                headers=owner_headers,
            )
            history.raise_for_status()
            diff = await client.get(
                f"/api/v1/diff/{vault_one}/archive/moved.md",
                headers=owner_headers,
                params={"commit": moved["commit_hash"]},
            )
            diff.raise_for_status()
            activity = await client.get(
                f"/api/v1/activity/{vault_one}",
                headers=owner_headers,
            )
            activity.raise_for_status()
            reader_current = await client.get(
                f"/api/v1/documents/{vault_one}/archive/moved.md",
                headers=reader_headers,
            )
            reader_current.raise_for_status()
            assert reader_current.json()["content"] == current.json()["content"]
            assert len(history.json()["history"]) >= 3
            assert diff.json()["kind"] == "document_diff"
            assert activity.json()["activity"]

            file_bytes = b"F2 searchable text file\nlegacy to native\n"
            file_digest = hashlib.sha256(file_bytes).hexdigest()
            response = await client.post(
                f"/api/v1/files/{vault_one}/upload",
                headers=owner_headers,
                params={
                    "filename": "cutover.txt",
                    "collection": "files",
                    "mime_type": "text/plain",
                    "content_hash": file_digest,
                },
            )
            response.raise_for_status()
            upload = response.json()
            response = await client.put(
                upload["upload_url"],
                content=file_bytes,
                headers={"Content-Type": "text/plain"},
            )
            response.raise_for_status()
            file_id = upload["uri"].rsplit("/", 1)[-1]
            response = await client.post(
                f"/api/v1/files/{vault_one}/{file_id}/confirm",
                headers=owner_headers,
                params={"content_hash": file_digest, "hash_algorithm": "sha256"},
            )
            response.raise_for_status()
            files = await client.get(
                f"/api/v1/files/{vault_one}",
                headers=reader_headers,
            )
            files.raise_for_status()
            assert any(item["uri"] == upload["uri"] for item in files.json()["items"])

            binary_bytes = b"\x89PNG\r\n\x1a\nF2 binary fixture\x00\x01"
            binary_digest = hashlib.sha256(binary_bytes).hexdigest()
            response = await client.post(
                f"/api/v1/files/{vault_one}/upload",
                headers=owner_headers,
                params={
                    "filename": "cutover.bin",
                    "collection": "files",
                    "mime_type": "application/octet-stream",
                    "content_hash": binary_digest,
                },
            )
            response.raise_for_status()
            binary_upload = response.json()
            response = await client.put(
                binary_upload["upload_url"],
                content=binary_bytes,
                headers={"Content-Type": "application/octet-stream"},
            )
            response.raise_for_status()
            binary_file_id = binary_upload["uri"].rsplit("/", 1)[-1]
            response = await client.post(
                f"/api/v1/files/{vault_one}/{binary_file_id}/confirm",
                headers=owner_headers,
                params={"content_hash": binary_digest, "hash_algorithm": "sha256"},
            )
            response.raise_for_status()

            external_result, external_payload = await _mcp_call(
                client,
                token=owner_pat,
                session_id=session_id,
                request_id=4,
                tool="akb_create_vault",
                arguments={
                    "name": external_name,
                    "external_git": {"url": "https://example.invalid/repository.git"},
                },
            )
            external_text = json.dumps(
                {"result": external_result, "payload": external_payload},
                sort_keys=True,
            ).lower()
            assert "error" in external_text

        conn = await asyncpg.connect(
            host="127.0.0.1",
            port=15432,
            user="akb",
            password="akb",  # pragma: allowlist secret
            database="akb",
        )
        try:
            for _ in range(40):
                    if (await _pending_counts(conn))["chunks"] == 0:
                        break
                    await asyncio.sleep(0.25)
            pending_before = await _pending_counts(conn)
            legacy_before = await _legacy_snapshot(conn, vault_names)
            fixture_document_count = sum(
                row[2] != "overview/vault-skill.md"
                for row in legacy_before["documents"]
            )
            seed_document_count = sum(
                row[2] == "overview/vault-skill.md"
                for row in legacy_before["documents"]
            )
            assert fixture_document_count == 3
            assert seed_document_count == 2
            assert await conn.fetchval(
                "SELECT count(*) FROM vaults WHERE name = $1",
                external_name,
            ) == 0
        finally:
            await conn.close()

        await runtime._stop_named_process("backend")
        async with httpx.AsyncClient(timeout=2) as stopped_client:
            with pytest.raises(httpx.TransportError):
                await stopped_client.get(f"{runtime.config.app_origin}/readyz")

        assert "app.config" not in sys.modules
        os.chdir(runtime_root)
        from app.services.git_service import GitService
        from app.services.native_revision_authority import NativeAuthorityIdentity
        from app.services.native_revision_backfill import NativeRevisionBackfill
        from app.services.native_revision_cutover import (
            CutoverVaultInput,
            NativeRevisionCutover,
            NativeRevisionCutoverVerifier,
        )

        pool = await asyncpg.create_pool(
            host="127.0.0.1",
            port=15432,
            user="akb",
            password="akb",  # pragma: allowlist secret
            database="akb",
            min_size=1,
            max_size=4,
        )
        git = GitService(storage_path=str(runtime.config.vault_dir))
        async with pool.acquire() as conn:
            vault_rows = await conn.fetch(
                "SELECT id, name FROM vaults WHERE name = ANY($1::text[]) ORDER BY name",
                vault_names,
            )
        assert [row["name"] for row in vault_rows] == sorted(vault_names)
        fixed = {
            row["name"]: git.current_commit(row["name"])
            for row in vault_rows
        }
        assert all(value is not None for value in fixed.values())

        product_verifier = NativeRevisionCutoverVerifier(pool, git=git)

        class RecordingVerifier:
            def __init__(self) -> None:
                self.receipts: dict[uuid.UUID, dict[str, Any]] = {}

            async def compare_run(self, run_id: uuid.UUID) -> dict[str, Any]:
                receipt = dict(await product_verifier.compare_run(run_id))
                self.receipts[run_id] = receipt
                return receipt

        recording = RecordingVerifier()
        cutover = NativeRevisionCutover(
            pool,
            backfill=NativeRevisionBackfill(pool, git=git),
            verifier=recording,
        )

        # G3 classification boundary: represent one already-persisted mirror
        # exactly as the product DB does. It must be reported without creating
        # a mirror migration run or preventing the manual vaults from being
        # independently planned. The mirror is then explicitly retired from
        # this disposable fixture before the database-wide authority flip.
        mirror_name = f"f2-persisted-mirror-{suffix}"
        git.init_vault(mirror_name)
        mirror_oid = git.commit_file(
            mirror_name,
            "mirror.md",
            "persisted mirror fixture\n",
            "[create] mirror.md\n\nagent: fixture\naction: create\nsummary: mirror",
        )
        async with pool.acquire() as conn:
            mirror_id = await conn.fetchval(
                """
                INSERT INTO vaults (name, git_path, status)
                VALUES ($1, $2, 'active') RETURNING id
                """,
                mirror_name,
                str(git._bare_path(mirror_name)),
            )
            await conn.execute(
                """
                INSERT INTO vault_external_git (vault_id, remote_url, remote_branch)
                VALUES ($1, 'https://git.example.invalid/fixture.git', 'main')
                """,
                mirror_id,
            )
        classification = await cutover.plan(
            vaults=[
                *[
                    CutoverVaultInput(
                        namespace_id=row["id"],
                        fixed_ref=str(fixed[row["name"]]),
                    )
                    for row in vault_rows
                ],
                CutoverVaultInput(namespace_id=mirror_id, fixed_ref=mirror_oid),
            ],
            coverage_version=f"native-cutover-g3-classification-{suffix}",
        )
        assert {item.namespace_id for item in classification.vaults} == {
            row["id"] for row in vault_rows
        }
        assert [
            (item.namespace_id, item.reason)
            for item in classification.exclusions
        ] == [(mirror_id, "external_git_requires_collector")]
        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT count(*) FROM native_revision_migration_runs WHERE namespace_id = $1",
                mirror_id,
            ) == 0
            await conn.execute("DELETE FROM vaults WHERE id = $1", mirror_id)
            assert await conn.fetchval(
                "SELECT count(*) FROM vault_external_git WHERE vault_id = $1",
                mirror_id,
            ) == 0
            assert await conn.fetchval(
                """
                SELECT count(*) FROM native_revision_cutover_exclusions
                 WHERE cutover_id = $1 AND namespace_id = $2
                """,
                classification.cutover_id,
                mirror_id,
            ) == 1

        # Membership and classifications are bound facts, so retiring the
        # excluded mirror requires a fresh complete database plan. Reusing the
        # classification receipt would be a reclassified-state authority bug.
        planned = await cutover.plan(
            vaults=[
                CutoverVaultInput(
                    namespace_id=row["id"],
                    fixed_ref=str(fixed[row["name"]]),
                )
                for row in vault_rows
            ],
            coverage_version=f"native-cutover-f3-authority-{suffix}",
        )
        assert planned.exclusions == ()
        assert len(planned.files) == 2
        assert {item.disposition for item in planned.files} == {
            "native_text",
            "preserved_binary",
        }
        applied = await cutover.apply(planned.cutover_id)
        assert applied.status == "applied"
        assert {item.status for item in applied.files} == {"applied"}
        async with pool.acquire() as conn:
            native_counts_before_replay = tuple(
                await conn.fetchrow(
                    """
                    SELECT (SELECT count(*) FROM native_resources),
                           (SELECT count(*) FROM native_revisions),
                           (SELECT count(*) FROM native_revision_migration_items)
                    """
                )
            )
        replay = await cutover.apply(planned.cutover_id)
        assert replay == applied
        async with pool.acquire() as conn:
            native_counts_after_replay = tuple(
                await conn.fetchrow(
                    """
                    SELECT (SELECT count(*) FROM native_resources),
                           (SELECT count(*) FROM native_revisions),
                           (SELECT count(*) FROM native_revision_migration_items)
                    """
                )
            )
        assert native_counts_after_replay == native_counts_before_replay

        verified = await cutover.verify(planned.cutover_id)
        assert verified.status == "verified"
        summaries = [receipt["summary"] for receipt in recording.receipts.values()]
        compared_resource_count = sum(item["resource_count"] for item in summaries)
        semantic_operation_count = sum(item["operation_count"] for item in summaries)
        assert compared_resource_count == len(legacy_before["documents"])
        assert semantic_operation_count == compared_resource_count * 4
        assert all(item["unexplained_mismatch_count"] == 0 for item in summaries)

        async with pool.acquire() as conn:
            legacy_after = await _legacy_snapshot(conn, vault_names)
            pending_after = await _pending_counts(conn)
            text_file_id = uuid.UUID(file_id)
            binary_native_file_id = uuid.UUID(binary_file_id)
            text_file_native_rows = await conn.fetchval(
                "SELECT count(*) FROM native_resources WHERE resource_id = $1",
                text_file_id,
            )
            binary_file_native_rows = await conn.fetchval(
                "SELECT count(*) FROM native_resources WHERE resource_id = $1",
                binary_native_file_id,
            )
            native_documents = await conn.fetchval(
                """
                SELECT count(*)
                  FROM native_resources r
                  JOIN vaults v ON v.id = r.namespace_id
                 WHERE v.name = ANY($1::text[]) AND r.surface = 'document'
                """,
                vault_names,
            )
        assert legacy_after == legacy_before
        assert text_file_native_rows == 1
        assert binary_file_native_rows == 0
        assert native_documents == compared_resource_count
        assert {item.status for item in verified.files} == {"verified"}

        native_identity = NativeAuthorityIdentity(
            tenant_id=f"f2-tenant-{suffix}",
            namespace=f"f2-namespace-{suffix}",
            database_id=uuid.uuid4(),
            current_database="akb",
            runtime_image_digest="sha256:" + hashlib.sha256(suffix.encode()).hexdigest(),
        )
        authority = await cutover.commit(
            planned.cutover_id,
            identity=native_identity,
        )
        assert authority.status == "committed"
        assert await cutover.commit(
            planned.cutover_id,
            identity=native_identity,
        ) == authority

        config_path = runtime.config.config_dir / "app.yaml"
        app_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        app_config.update(
            {
                "document_revision_backend": "postgres_native",
                "document_revision_tenant_id": native_identity.tenant_id,
                "document_revision_namespace": native_identity.namespace,
                "document_revision_database_id": str(native_identity.database_id),
                "document_revision_runtime_image_digest": (
                    native_identity.runtime_image_digest
                ),
            }
        )
        config_path.write_text(
            yaml.safe_dump(app_config, sort_keys=False),
            encoding="utf-8",
        )
        os.chmod(config_path, 0o600)
        await runtime._start_backend()

        async with httpx.AsyncClient(
            base_url=runtime.config.app_origin,
            timeout=30,
            follow_redirects=True,
        ) as native_client:
            current_native = await native_client.get(
                f"/api/v1/documents/{vault_one}/archive/moved.md",
                headers=owner_headers,
            )
            current_native.raise_for_status()
            assert current_native.json()["content"] == current.json()["content"]
            native_head_before_write = str(current_native.json()["current_commit"])
            alias_current_native = await native_client.get(
                f"/api/v1/documents/{vault_one}/guides/moving.md",
                headers=owner_headers,
            )
            assert alias_current_native.status_code == 200, alias_current_native.text
            pinned_native = await native_client.get(
                f"/api/v1/documents/{vault_one}/guides/moving.md",
                headers=owner_headers,
                params={"version": moving_initial["commit_hash"]},
            )
            assert pinned_native.status_code == 200, pinned_native.text
            assert pinned_native.json()["content"] == pinned.json()["content"]
            pinned_native_prefix = await native_client.get(
                f"/api/v1/documents/{vault_one}/guides/moving.md",
                headers=owner_headers,
                params={"version": moving_initial["commit_hash"][:8]},
            )
            pinned_native_prefix.raise_for_status()
            assert pinned_native_prefix.json()["content"] == pinned.json()["content"]
            history_native = await native_client.get(
                f"/api/v1/history/{vault_one}/archive/moved.md",
                headers=owner_headers,
            )
            history_native.raise_for_status()
            assert len(history_native.json()["history"]) >= 3
            diff_native = await native_client.get(
                f"/api/v1/diff/{vault_one}/archive/moved.md",
                headers=owner_headers,
                params={"commit": moved["commit_hash"]},
            )
            diff_native.raise_for_status()
            activity_native = await native_client.get(
                f"/api/v1/activity/{vault_one}",
                headers=owner_headers,
            )
            activity_native.raise_for_status()
            assert activity_native.json()["activity"]
            reader_native = await native_client.get(
                f"/api/v1/documents/{vault_one}/archive/moved.md",
                headers=reader_headers,
            )
            reader_native.raise_for_status()
            assert reader_native.json()["content"] == current_native.json()["content"]
            files_native = await native_client.get(
                f"/api/v1/files/{vault_one}",
                headers=reader_headers,
            )
            files_native.raise_for_status()
            assert any(
                item["uri"] == upload["uri"]
                for item in files_native.json()["items"]
            )
            assert any(
                item["uri"] == binary_upload["uri"]
                for item in files_native.json()["items"]
            )
            native_session = await _mcp_session(native_client, owner_pat)
            mcp_result, mcp_read = await _mcp_call(
                native_client,
                token=owner_pat,
                session_id=native_session,
                request_id=5,
                tool="akb_get",
                arguments={"uri": mcp_document["uri"]},
            )
            assert mcp_result.get("isError") is not True
            assert "version two" in json.dumps(mcp_read)

        async with pool.acquire() as conn:
            for _ in range(120):
                pending_after_restart = await _pending_counts(conn)
                if (
                    pending_after_restart["native_invalidation"] == 0
                    and pending_after_restart["chunks"] == 0
                ):
                    break
                await asyncio.sleep(0.25)
            authority_status = await conn.fetchval(
                "SELECT status FROM native_revision_existing_authority"
            )
            text_file_projection = await conn.fetchrow(
                """
                SELECT
                    (SELECT count(*) FROM native_derived_heads
                      WHERE resource_id = $1) AS heads,
                    (SELECT count(*) FROM chunks
                      WHERE source_type = 'native_file'
                        AND source_id = $1) AS chunks,
                    (SELECT count(*) FROM chunks
                      WHERE source_type = 'native_file'
                        AND source_id = $1
                        AND vector_indexed_at IS NOT NULL) AS indexed_chunks,
                    (SELECT count(*) FROM native_resources
                      WHERE resource_id = $2) AS binary_resources,
                    (SELECT count(*) FROM chunks
                      WHERE source_type = 'native_file'
                        AND source_id = $2) AS binary_chunks
                """,
                text_file_id,
                binary_native_file_id,
            )
        assert authority_status == "committed"
        assert pending_after_restart["native_invalidation"] == 0
        assert pending_after_restart["chunks"] == 0
        assert text_file_projection is not None
        assert text_file_projection["heads"] == 1
        assert text_file_projection["chunks"] >= 1
        assert (
            text_file_projection["indexed_chunks"]
            == text_file_projection["chunks"]
        )
        assert text_file_projection["binary_resources"] == 0
        assert text_file_projection["binary_chunks"] == 0

        async with httpx.AsyncClient(
            base_url=runtime.config.app_origin,
            timeout=30,
            follow_redirects=True,
        ) as search_client:
            grep_native_file = await search_client.get(
                "/api/v1/grep",
                headers=owner_headers,
                params={
                    "q": "legacy to native",
                    "vault": vault_one,
                    "measurement_include_text_files": "true",
                },
            )
            grep_native_file.raise_for_status()
            grep_uris = {
                item["uri"] for item in grep_native_file.json()["results"]
            }
            assert upload["uri"] in grep_uris
            assert binary_upload["uri"] not in grep_uris

            semantic_native_file = await search_client.get(
                "/api/v1/search",
                headers=owner_headers,
                params=[
                    ("q", "legacy to native"),
                    ("vault", vault_one),
                    ("source_uris", upload["uri"]),
                    ("limit", "10"),
                ],
            )
            semantic_native_file.raise_for_status()
            semantic_results = semantic_native_file.json()["results"]
            assert any(
                item["uri"] == upload["uri"]
                and item["source_type"] == "file"
                for item in semantic_results
            )

            # G2b: after authority handoff, ordinary public File mutations keep
            # S3/catalogue authority and converge the additive Native text
            # projection through the existing worker loop.
            post_text_v1 = b"post cutover file projection version alpha\n"
            post_digest_v1 = hashlib.sha256(post_text_v1).hexdigest()
            response = await search_client.post(
                f"/api/v1/files/{vault_one}/upload",
                headers=owner_headers,
                params={
                    "filename": "post-cutover.txt",
                    "collection": "files",
                    "mime_type": "text/plain",
                    "content_hash": post_digest_v1,
                },
            )
            response.raise_for_status()
            post_upload = response.json()
            response = await search_client.put(
                post_upload["upload_url"],
                content=post_text_v1,
                headers={"Content-Type": "text/plain"},
            )
            response.raise_for_status()
            post_file_id = post_upload["uri"].rsplit("/", 1)[-1]
            response = await search_client.post(
                f"/api/v1/files/{vault_one}/{post_file_id}/confirm",
                headers=owner_headers,
                params={"content_hash": post_digest_v1, "hash_algorithm": "sha256"},
            )
            response.raise_for_status()

            async def wait_projection(outcome: str, lifecycle: str) -> dict[str, Any]:
                async with pool.acquire() as projection_conn:
                    for _ in range(160):
                        row = await projection_conn.fetchrow(
                            """
                            SELECT o.outcome, o.completed_at, r.lifecycle,
                                   r.head_revision_id,
                                   (SELECT count(*) FROM native_invalidation_intents
                                     WHERE completed_at IS NULL) AS invalidations,
                                   (SELECT count(*) FROM chunks
                                     WHERE vector_indexed_at IS NULL
                                       AND vector_abandoned_at IS NULL) AS chunks
                              FROM native_file_projection_outbox o
                         LEFT JOIN native_resources r ON r.resource_id = o.file_id
                             WHERE o.file_id = $1
                            """,
                            uuid.UUID(post_file_id),
                        )
                        if (
                            row is not None
                            and row["completed_at"] is not None
                            and row["outcome"] == outcome
                            and row["lifecycle"] == lifecycle
                            and row["invalidations"] == 0
                            and row["chunks"] == 0
                        ):
                            return dict(row)
                        await asyncio.sleep(0.25)
                raise AssertionError(
                    f"Native File projection did not reach {outcome}/{lifecycle}: {row}"
                )

            created_projection = await wait_projection("created", "live")
            response = await search_client.get(
                "/api/v1/grep",
                headers=owner_headers,
                params={
                    "q": "projection version alpha",
                    "vault": vault_one,
                    "measurement_include_text_files": "true",
                },
            )
            response.raise_for_status()
            assert post_upload["uri"] in {
                item["uri"] for item in response.json()["results"]
            }
            response = await search_client.get(
                "/api/v1/search",
                headers=owner_headers,
                params=[
                    ("q", "projection version alpha"),
                    ("vault", vault_one),
                    ("source_uris", post_upload["uri"]),
                    ("limit", "10"),
                ],
            )
            response.raise_for_status()
            assert any(
                item["uri"] == post_upload["uri"]
                for item in response.json()["results"]
            )

            post_text_v2 = b"post cutover file projection version beta\n"
            post_digest_v2 = hashlib.sha256(post_text_v2).hexdigest()
            response = await search_client.post(
                f"/api/v1/files/{vault_one}/{post_file_id}/replace",
                headers=owner_headers,
                params={
                    "content_hash": post_digest_v2,
                    "mime_type": "text/plain",
                    "expected_content_hash": post_digest_v1,
                },
            )
            response.raise_for_status()
            replacement = response.json()
            response = await search_client.put(
                replacement["upload_url"],
                content=post_text_v2,
                headers={"Content-Type": "text/plain"},
            )
            response.raise_for_status()
            response = await search_client.post(
                f"/api/v1/files/{vault_one}/{post_file_id}/replace/"
                f"{replacement['replacement_id']}/confirm",
                headers=owner_headers,
                params={
                    "content_hash": post_digest_v2,
                    "expected_content_hash": post_digest_v1,
                },
            )
            response.raise_for_status()
            replaced_projection = await wait_projection("replaced", "live")
            assert replaced_projection["head_revision_id"] != created_projection["head_revision_id"]
            response = await search_client.get(
                "/api/v1/grep",
                headers=owner_headers,
                params={
                    "q": "projection version beta",
                    "vault": vault_one,
                    "measurement_include_text_files": "true",
                },
            )
            response.raise_for_status()
            assert post_upload["uri"] in {
                item["uri"] for item in response.json()["results"]
            }

            post_binary = b"\x00post-cutover-binary\x01"
            post_binary_digest = hashlib.sha256(post_binary).hexdigest()
            response = await search_client.post(
                f"/api/v1/files/{vault_one}/{post_file_id}/replace",
                headers=owner_headers,
                params={
                    "content_hash": post_binary_digest,
                    "mime_type": "application/octet-stream",
                    "expected_content_hash": post_digest_v2,
                },
            )
            response.raise_for_status()
            replacement = response.json()
            response = await search_client.put(
                replacement["upload_url"],
                content=post_binary,
                headers={"Content-Type": "application/octet-stream"},
            )
            response.raise_for_status()
            response = await search_client.post(
                f"/api/v1/files/{vault_one}/{post_file_id}/replace/"
                f"{replacement['replacement_id']}/confirm",
                headers=owner_headers,
                params={
                    "content_hash": post_binary_digest,
                    "expected_content_hash": post_digest_v2,
                },
            )
            response.raise_for_status()
            await wait_projection("deleted", "deleted")
            response = await search_client.get(
                "/api/v1/grep",
                headers=owner_headers,
                params={
                    "q": "projection version beta",
                    "vault": vault_one,
                    "measurement_include_text_files": "true",
                },
            )
            response.raise_for_status()
            assert post_upload["uri"] not in {
                item["uri"] for item in response.json()["results"]
            }

            post_text_v3 = b"post cutover file projection version gamma\n"
            post_digest_v3 = hashlib.sha256(post_text_v3).hexdigest()
            response = await search_client.post(
                f"/api/v1/files/{vault_one}/{post_file_id}/replace",
                headers=owner_headers,
                params={
                    "content_hash": post_digest_v3,
                    "mime_type": "text/plain",
                    "expected_content_hash": post_binary_digest,
                },
            )
            response.raise_for_status()
            replacement = response.json()
            response = await search_client.put(
                replacement["upload_url"],
                content=post_text_v3,
                headers={"Content-Type": "text/plain"},
            )
            response.raise_for_status()
            response = await search_client.post(
                f"/api/v1/files/{vault_one}/{post_file_id}/replace/"
                f"{replacement['replacement_id']}/confirm",
                headers=owner_headers,
                params={
                    "content_hash": post_digest_v3,
                    "expected_content_hash": post_binary_digest,
                },
            )
            response.raise_for_status()
            restored_projection = await wait_projection("restored", "live")
            assert restored_projection["head_revision_id"] != replaced_projection["head_revision_id"]
            response = await search_client.get(
                "/api/v1/search",
                headers=owner_headers,
                params=[
                    ("q", "projection version gamma"),
                    ("vault", vault_one),
                    ("source_uris", post_upload["uri"]),
                    ("limit", "10"),
                ],
            )
            response.raise_for_status()
            assert any(
                item["uri"] == post_upload["uri"]
                for item in response.json()["results"]
            )

            response = await search_client.delete(
                f"/api/v1/files/{vault_one}/{post_file_id}",
                headers=owner_headers,
            )
            response.raise_for_status()
            await wait_projection("deleted", "deleted")
            response = await search_client.get(
                "/api/v1/grep",
                headers=owner_headers,
                params={
                    "q": "projection version gamma",
                    "vault": vault_one,
                    "measurement_include_text_files": "true",
                },
            )
            response.raise_for_status()
            assert post_upload["uri"] not in {
                item["uri"] for item in response.json()["results"]
            }

            # F4: authority minting above already crossed the forward-only
            # boundary. This later write proves post-boundary Native mutation;
            # the fixed Legacy Git refs remain a retained-history bridge.
            native_write_body = "# Moving\n\nversion three from Native"
            response = await search_client.patch(
                f"/api/v1/documents/{vault_one}/archive/moved.md",
                headers=owner_headers,
                json={"content": native_write_body, "message": "F4 Native update"},
            )
            response.raise_for_status()
            native_write = response.json()
            native_head_after_write = str(native_write["commit_hash"])
            assert native_head_after_write != native_head_before_write
            response = await search_client.get(
                f"/api/v1/documents/{vault_one}/archive/moved.md",
                headers=owner_headers,
            )
            response.raise_for_status()
            assert response.json()["content"] == native_write_body

        async with pool.acquire() as conn:
            post_cutover_projection = await conn.fetchrow(
                """
                SELECT r.resource_id, r.lifecycle,
                       count(nr.revision_id) AS revisions,
                       o.outcome, o.completed_at IS NOT NULL AS completed,
                       (SELECT count(*) FROM chunks
                         WHERE source_type = 'native_file'
                           AND source_id = r.resource_id) AS chunks
                  FROM native_resources r
                  JOIN native_revisions nr ON nr.resource_id = r.resource_id
                  JOIN native_file_projection_outbox o ON o.file_id = r.resource_id
                 WHERE r.resource_id = $1
                 GROUP BY r.resource_id, r.lifecycle, o.outcome, o.completed_at
                """,
                uuid.UUID(post_file_id),
            )
            persisted_native_head = await conn.fetchval(
                """
                SELECT r.head_revision_id
                  FROM native_resources r
                  JOIN vaults v ON v.id = r.namespace_id
                 WHERE v.name = $1 AND r.surface = 'document'
                   AND r.current_path = 'archive/moved.md'
                """,
                vault_one,
            )
        assert post_cutover_projection is not None
        assert post_cutover_projection["resource_id"] == uuid.UUID(post_file_id)
        assert post_cutover_projection["lifecycle"] == "deleted"
        assert post_cutover_projection["revisions"] == 5
        assert post_cutover_projection["outcome"] == "deleted"
        assert post_cutover_projection["completed"] is True
        assert post_cutover_projection["chunks"] == 0
        assert persisted_native_head == native_head_after_write
        assert {
            name: git.current_commit(name)
            for name in vault_names
        } == fixed

        # Restart with one durable projection intent deliberately left pending.
        # This models a crash after catalogue commit but before worker
        # acknowledgement; it changes no public File or Native payload state.
        await runtime._stop_named_process("backend")
        restart_intent_id = uuid.uuid4()
        async with pool.acquire() as conn:
            updated = await conn.execute(
                """
                UPDATE native_file_projection_outbox
                   SET intent_id = $2, generation = generation + 1,
                       claimed_at = NULL, retry_count = 0, next_attempt_at = NULL,
                       completed_at = NULL, outcome = NULL, last_error = NULL,
                       created_at = NOW()
                 WHERE file_id = $1 AND source_present = FALSE
                """,
                uuid.UUID(post_file_id),
                restart_intent_id,
            )
            assert updated == "UPDATE 1"
            assert await conn.fetchval(
                """
                SELECT count(*) FROM native_file_projection_outbox
                 WHERE file_id = $1 AND completed_at IS NULL
                """,
                uuid.UUID(post_file_id),
            ) == 1
        await runtime._start_backend()

        async with pool.acquire() as conn:
            for _ in range(160):
                resumed_projection = await conn.fetchrow(
                    """
                    SELECT intent_id, outcome, completed_at
                      FROM native_file_projection_outbox
                     WHERE file_id = $1
                    """,
                    uuid.UUID(post_file_id),
                )
                if (
                    resumed_projection is not None
                    and resumed_projection["intent_id"] == restart_intent_id
                    and resumed_projection["completed_at"] is not None
                ):
                    break
                await asyncio.sleep(0.25)
            authority_after_write_restart = await conn.fetchval(
                "SELECT status FROM native_revision_existing_authority"
            )
            persisted_head_after_restart = await conn.fetchval(
                """
                SELECT r.head_revision_id
                  FROM native_resources r
                  JOIN vaults v ON v.id = r.namespace_id
                 WHERE v.name = $1 AND r.surface = 'document'
                   AND r.current_path = 'archive/moved.md'
                """,
                vault_one,
            )
        assert resumed_projection is not None
        assert resumed_projection["outcome"] == "already_absent"
        assert resumed_projection["completed_at"] is not None
        assert authority_after_write_restart == "committed"
        assert persisted_head_after_restart == native_head_after_write

        async with httpx.AsyncClient(
            base_url=runtime.config.app_origin,
            timeout=30,
            follow_redirects=True,
        ) as restarted_client:
            response = await restarted_client.get(
                f"/api/v1/documents/{vault_one}/archive/moved.md",
                headers=owner_headers,
            )
            response.raise_for_status()
            assert response.json()["content"] == native_write_body
            assert response.json()["current_commit"] == native_head_after_write
        assert {
            name: git.current_commit(name)
            for name in vault_names
        } == fixed
        evidence = {
            "fixture": "F2-F3-real-legacy-same-db-git-native-restart",
            "vault_count": 2,
            "fixture_document_count": fixture_document_count,
            "seed_document_count": seed_document_count,
            "compared_resource_count": compared_resource_count,
            "semantic_operation_count": semantic_operation_count,
            "unexplained_mismatch_count": 0,
            "apply_replay_changed_counts": False,
            "legacy_rows_changed": False,
            "reader_acl_preserved": True,
            "external_git": "publicly-rejected-before-persistence",
            "persisted_external_mirror": (
                "classified-ineligible-without-migration-run"
            ),
            "text_file": "s3-preserved-native-searchable-projection-verified",
            "binary_file": "s3-preserved-without-fake-text-projection",
            "native_text_file_chunks": text_file_projection["chunks"],
            "native_file_public_grep": "passed",
            "native_file_semantic_search": "passed",
            "post_cutover_file_mutations": (
                "create-replace-text-binary-text-delete-passed"
            ),
            "post_cutover_file_revision_count": post_cutover_projection["revisions"],
            "post_cutover_file_final_chunks": post_cutover_projection["chunks"],
            "pending_before_stop": pending_before,
            "pending_after_backfill": pending_after,
            "pending_after_native_restart": pending_after_restart,
            "fixed_ref_count": len(fixed),
            "authority_status": authority_status,
            "native_public_read_smoke": "passed",
            "authority_mint_was_forward_boundary": True,
            "post_authority_native_write": True,
            "native_document_head_advanced": True,
            "native_write_survived_restart": True,
            "pending_file_projection_resumed_after_restart": True,
            "frozen_git_refs_changed": False,
        }
        print("AKB_NATIVE_CUTOVER_F2_F3 " + json.dumps(evidence, sort_keys=True))
    finally:
        if pool is not None:
            await pool.close()
        os.chdir(original_cwd)
        await runtime.cleanup()
