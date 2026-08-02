#!/usr/bin/env python3
"""B-text receipt adapter for the selected PostgreSQL BodyStore candidate."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import asyncpg
import httpx

BACKEND = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
for entry in (BACKEND, SCRIPT_DIR):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from app.services.m1_pg_body_store import M1PgBodyStore  # noqa: E402
from app.services.native_revision_service import NativeRevisionService  # noqa: E402
from native_revision_m1_adapter import (  # noqa: E402
    AdapterError,
    receipt_provenance,
    required_environment,
    source_revision,
    validate_measurement_database,
)

PROTOCOL_VERSION = "akb-native-revision-m1-native-text/v1"


async def _migration(conn, filename: str) -> None:
    path = BACKEND / "app" / "db" / "migrations" / filename
    spec = importlib.util.spec_from_file_location(f"native_text_{filename}", path)
    if spec is None or spec.loader is None:
        raise AdapterError(f"cannot load migration {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    await module.migrate(conn=conn)


async def run() -> dict[str, Any]:
    dsn = required_environment("AKB_NATIVE_REVISION_MEASUREMENT_DSN")
    conn = await asyncpg.connect(dsn, timeout=10)
    pool = None
    namespace_id = None
    try:
        database = str(await conn.fetchval("SELECT current_database()"))
        validate_measurement_database(database)
        await conn.execute((BACKEND / "app" / "db" / "init.sql").read_text())
        for filename in ("048_native_revision_core.py", "049_native_revision_m1_pg_body.py"):
            await _migration(conn, filename)
        namespace_id = await conn.fetchval(
            """
            INSERT INTO vaults (name, git_path, public_access)
            VALUES ($1, '/tmp/native-text-unused.git', 'writer') RETURNING id
            """,
            f"m1-native-text-{uuid.uuid4().hex}",
        )
        await conn.close()
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=6)
        store = M1PgBodyStore(pool)
        service = NativeRevisionService(pool, payload_store=store)
        operation_ids: list[str] = []
        samples: list[dict[str, Any]] = []

        async def observed(operation_id: str, call):
            started = time.perf_counter()
            value = await call
            operation_ids.append(operation_id)
            samples.append({"operation_id": operation_id, "latency_ms": round((time.perf_counter() - started) * 1000, 3)})
            return value

        first = "---\ntitle: Text receipt\n---\nfirst exact bytes\n"
        second = "---\ntitle: Text receipt\n---\nsecond exact bytes 길이\n"
        created = await observed(
            "nativeTextCreate",
            service.create_text(
                namespace_id=namespace_id,
                surface="document",
                path="receipt.md",
                payload=first,
                actor="native-text-adapter",
                mutation_id=uuid.uuid4(),
            ),
        )
        current_first = await observed(
            "nativeTextCurrentGet",
            service.get_current_resource(
                namespace_id=namespace_id,
                surface="document",
                resource_id=created.resource_id,
            ),
        )
        pinned_first = await observed(
            "nativeTextPinnedGet",
            service.get_resource_revision(
                namespace_id=namespace_id,
                surface="document",
                resource_id=created.resource_id,
                revision_id=created.revision_id,
            ),
        )
        base_url = required_environment("AKB_NATIVE_REVISION_PUBLIC_BASE_URL").rstrip("/")
        token = required_environment("AKB_NATIVE_REVISION_PUBLIC_TOKEN")
        public_operations: list[dict[str, Any]] = []
        async with httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        ) as client:
            # Build the route separately to keep the stored path opaque to the
            # adapter's evidence; URL quoting is the public client behavior.
            from urllib.parse import quote

            async with pool.acquire() as lookup:
                vault_name = await lookup.fetchval("SELECT name FROM vaults WHERE id = $1", namespace_id)
            public_create = await client.post(
                "/api/v1/documents",
                json={
                    "vault": vault_name,
                    "title": "Public text receipt",
                    "slug": "public-receipt",
                    "content": "public first exact bytes",
                    "type": "note",
                    "status": "draft",
                },
            )
            public_create.raise_for_status()
            public_created = public_create.json()
            public_operations.append(
                {
                    "operation_id": "documentsPutDocument",
                    "revision": public_created["current_commit"],
                }
            )
            route = f"/api/v1/documents/{quote(vault_name, safe='')}/{quote(public_created['path'], safe='/')}"
            public_current = await client.get(route)
            public_current.raise_for_status()
            public_operations.append(
                {
                    "operation_id": "documentsGetDocument",
                    "revision": public_current.json()["current_commit"],
                }
            )
            public_pinned = await client.get(
                route, params={"version": public_created["current_commit"]}
            )
            public_pinned.raise_for_status()
            public_operations.append(
                {
                    "operation_id": "documentsGetDocument",
                    "selector": public_created["current_commit"],
                    "revision": public_pinned.json()["current_commit"],
                }
            )
            public_replace = await client.patch(
                route,
                json={
                    "content": "public second exact bytes",
                    "expected_commit": public_created["current_commit"],
                    "message": "M1 native-text public replace",
                },
            )
            public_replace.raise_for_status()
            public_operations.append(
                {
                    "operation_id": "documentsUpdateDocument",
                    "revision": public_replace.json()["current_commit"],
                }
            )
            public_after = await client.get(route)
            public_after.raise_for_status()
            public_pinned_after = await client.get(
                route, params={"version": public_created["current_commit"]}
            )
            public_pinned_after.raise_for_status()
        replaced = await observed(
            "nativeTextReplace",
            service.replace_text(
                namespace_id=namespace_id,
                surface="document",
                path="receipt.md",
                payload=second,
                actor="native-text-adapter",
                mutation_id=uuid.uuid4(),
                expected_revision_id=created.revision_id,
                expected_resource_id=created.resource_id,
            ),
        )
        current_second = await observed(
            "nativeTextCurrentGetAfterReplace",
            service.get_current_resource(
                namespace_id=namespace_id,
                surface="document",
                resource_id=created.resource_id,
            ),
        )
        pinned_first_after = await observed(
            "nativeTextPinnedGetAfterReplace",
            service.get_resource_revision(
                namespace_id=namespace_id,
                surface="document",
                resource_id=created.resource_id,
                revision_id=created.revision_id,
            ),
        )
        revision = source_revision()
        runtime, environment = receipt_provenance(revision, database, namespace_id)
        facts = {
            "create_revision": created.revision_id,
            "replace_revision": replaced.revision_id,
            "current_head": current_second.revision_id,
            "create": {
                "bytes_hex": current_first.payload_bytes.hex(),
                "digest": current_first.digest,
                "size": current_first.byte_size,
            },
            "pinned_create": {
                "bytes_hex": pinned_first.payload_bytes.hex(),
                "digest": pinned_first.digest,
                "size": pinned_first.byte_size,
            },
            "replace": {
                "bytes_hex": current_second.payload_bytes.hex(),
                "digest": current_second.digest,
                "size": current_second.byte_size,
            },
            "pinned_create_after_replace": {
                "bytes_hex": pinned_first_after.payload_bytes.hex(),
                "digest": pinned_first_after.digest,
                "size": pinned_first_after.byte_size,
            },
        }
        assertions = {
            "create_exact": current_first.payload_bytes == first.encode(),
            "pinned_exact": pinned_first.payload_bytes == first.encode(),
            "replace_exact": current_second.payload_bytes == second.encode(),
            "pinned_retained": pinned_first_after.payload_bytes == first.encode(),
            "head_replaced": current_second.revision_id == replaced.revision_id,
            "placement_selected": current_second.selected_placement == "pg-bodystore-v1",
            "public_create_current_exact": public_current.json()["content"] == "public first exact bytes",
            "public_pinned_exact": public_pinned.json()["content"] == "public first exact bytes",
            "public_replace_exact": public_after.json()["content"] == "public second exact bytes",
            "public_pinned_retained": public_pinned_after.json()["content"] == "public first exact bytes",
        }
        async with pool.acquire() as cleanup:
            await cleanup.execute("DELETE FROM vaults WHERE id = $1", namespace_id)
        residue = await store.namespace_residue(namespace_id)
        assertions["residue_closed"] = residue == {"bodies": 0, "body_bytes": 0, "distinct_digests": 0}
        if not all(assertions.values()):
            raise AdapterError(f"native text assertion failed: {assertions}")
        return {
            "protocol_version": PROTOCOL_VERSION,
            "source": runtime,
            "profile": environment,
            "internal_measurement_operations": operation_ids,
            "public_route_operations": public_operations,
            "body_facts": facts,
            "latency_samples": samples,
            "cleanup_residue": residue,
            "assertions": assertions,
        }
    finally:
        if pool is not None:
            await pool.close()
        elif not conn.is_closed():
            await conn.close()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run()), ensure_ascii=False, sort_keys=True))
