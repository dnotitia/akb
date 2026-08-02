#!/usr/bin/env python3
"""B-text receipt adapter for the selected PostgreSQL BodyStore candidate."""

from __future__ import annotations

import asyncio
import hashlib
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
from native_revision_m1_adapter import (  # noqa: E402
    AdapterError,
    receipt_provenance,
    receipt_safe_profile,
    required_environment,
    source_revision,
    validate_measurement_database,
)

PROTOCOL_VERSION = "akb-native-revision-m1-native-text/v1"


def public_body_fact(response: dict[str, Any]) -> dict[str, Any]:
    """Reduce a public GET response to receipt-safe byte facts."""
    body = str(response.get("content") or "").encode("utf-8")
    return {
        "revision": response["current_commit"],
        "sha256": hashlib.sha256(body).hexdigest(),
        "byte_size": len(body),
    }


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
            started = time.perf_counter()
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
                    "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                }
            )
            route = f"/api/v1/documents/{quote(vault_name, safe='')}/{quote(public_created['path'], safe='/')}"
            started = time.perf_counter()
            public_current = await client.get(route)
            public_current.raise_for_status()
            public_operations.append(
                {
                    "operation_id": "documentsGetDocument",
                    "revision": public_current.json()["current_commit"],
                    "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                }
            )
            started = time.perf_counter()
            public_pinned = await client.get(
                route, params={"version": public_created["current_commit"]}
            )
            public_pinned.raise_for_status()
            public_operations.append(
                {
                    "operation_id": "documentsGetDocument",
                    "selector": public_created["current_commit"],
                    "revision": public_pinned.json()["current_commit"],
                    "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                }
            )
            started = time.perf_counter()
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
                    "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                }
            )
            public_after = await client.get(route)
            public_after.raise_for_status()
            public_pinned_after = await client.get(
                route, params={"version": public_created["current_commit"]}
            )
            public_pinned_after.raise_for_status()
        revision = source_revision()
        runtime, environment = receipt_provenance(revision, database, namespace_id)
        safe_environment = {
            "tier": environment["tier"],
            "node_profile": receipt_safe_profile(environment["node_profile"]),
            "storage_profile": receipt_safe_profile(environment["storage_profile"]),
        }
        facts = {
            "create_current": public_body_fact(public_current.json()),
            "create_pinned": public_body_fact(public_pinned.json()),
            "replace_current": public_body_fact(public_after.json()),
            "create_pinned_after_replace": public_body_fact(public_pinned_after.json()),
        }
        assertions = {
            "public_create_current_matches_pinned": facts["create_current"] == facts["create_pinned"],
            "public_replace_changes_digest": facts["replace_current"]["sha256"] != facts["create_current"]["sha256"],
            "public_replace_changes_revision": facts["replace_current"]["revision"] != facts["create_current"]["revision"],
            "public_pinned_retained": facts["create_pinned_after_replace"] == facts["create_pinned"],
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
            "profile": safe_environment,
            "public_route_operations": public_operations,
            "public_body_facts": facts,
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
