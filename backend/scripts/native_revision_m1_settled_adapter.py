#!/usr/bin/env python3
"""Public update/search settled-state adapter for the real native pipeline."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

import asyncpg
import httpx

BACKEND = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
for entry in (BACKEND, SCRIPT_DIR):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from native_revision_m1_adapter import (  # noqa: E402
    AdapterError,
    bounded_json_object,
    required_environment,
    source_revision,
    validate_measurement_database,
)

PROTOCOL_VERSION = "akb-native-revision-m1-settled-search/v1"


def _schema() -> str:
    value = os.environ.get("AKB_NATIVE_REVISION_VECTOR_SCHEMA", "vector_index")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) is None:
        raise AdapterError("AKB_NATIVE_REVISION_VECTOR_SCHEMA must be a plain SQL identifier")
    return value


async def pipeline_snapshot(conn, resource_id: uuid.UUID) -> dict[str, int | str | None]:
    schema = _schema()
    vector_relation = await conn.fetchval("SELECT to_regclass($1)", f"{schema}.chunks")
    row = await conn.fetchrow(
        f"""
        SELECT
          (SELECT head_revision_id FROM native_resources WHERE resource_id = $1) AS current_head,
          (SELECT COUNT(*) FROM native_invalidation_intents
            WHERE resource_id = $1 AND completed_at IS NULL)::int AS pending_intents,
          (SELECT COUNT(*) FROM chunks
            WHERE source_type = 'native_document' AND source_id = $1)::int AS chunks,
          (SELECT COUNT(*) FROM chunks
            WHERE source_type = 'native_document' AND source_id = $1
              AND vector_indexed_at IS NULL)::int AS pending_chunks,
          (SELECT COUNT(*) FROM native_derived_chunks WHERE resource_id = $1)::int AS mappings,
          (SELECT COUNT(*) FROM vector_delete_outbox
            WHERE source_type = 'native_document' AND source_id = $1
              AND processed_at IS NULL)::int AS pending_deletes,
          (SELECT revision_id FROM native_derived_heads WHERE resource_id = $1) AS mapped_revision,
          CASE WHEN $2::text IS NULL THEN 0 ELSE
            (SELECT COUNT(*) FROM {schema}.chunks
              WHERE source_type = 'native_document' AND source_id = $1)::int
          END AS vector_points
        """,
        resource_id,
        str(vector_relation) if vector_relation is not None else None,
    )
    return dict(row)


async def chunk_ids(conn, resource_id: uuid.UUID) -> list[uuid.UUID]:
    rows = await conn.fetch(
        "SELECT id FROM chunks WHERE source_type = 'native_document' AND source_id = $1 ORDER BY id",
        resource_id,
    )
    return [row["id"] for row in rows]


async def stale_chunk_residue(conn, ids: list[uuid.UUID]) -> dict[str, int]:
    if not ids:
        return {"chunks": 0, "mappings": 0, "vector_points": 0, "pending_deletes": 0}
    schema = _schema()
    row = await conn.fetchrow(
        f"""
        SELECT
          (SELECT COUNT(*) FROM chunks WHERE id = ANY($1::uuid[]))::int AS chunks,
          (SELECT COUNT(*) FROM native_derived_chunks WHERE chunk_id = ANY($1::uuid[]))::int AS mappings,
          (SELECT COUNT(*) FROM {schema}.chunks WHERE chunk_id = ANY($1::uuid[]))::int AS vector_points,
          (SELECT COUNT(*) FROM vector_delete_outbox
            WHERE chunk_id = ANY($1::uuid[]) AND processed_at IS NULL)::int AS pending_deletes
        """,
        ids,
    )
    return dict(row)


async def settle(
    conn,
    resource_id: uuid.UUID,
    *,
    timeout_seconds: float,
    poll_seconds: float = 0.05,
) -> dict[str, Any]:
    started = time.perf_counter()
    polls = 0
    while True:
        polls += 1
        snapshot = await pipeline_snapshot(conn, resource_id)
        if (
            snapshot["pending_intents"] == 0
            and snapshot["pending_chunks"] == 0
            and snapshot["pending_deletes"] == 0
            and snapshot["chunks"] == snapshot["mappings"] == snapshot["vector_points"]
            and snapshot["mapped_revision"] == snapshot["current_head"]
        ):
            return {"polls": polls, "elapsed_seconds": time.perf_counter() - started, "state": snapshot}
        if time.perf_counter() - started >= timeout_seconds:
            raise AdapterError(f"native pipeline did not settle: {snapshot}")
        await asyncio.sleep(poll_seconds)


async def settle_deleted(
    conn,
    resource_id: uuid.UUID,
    *,
    timeout_seconds: float,
    poll_seconds: float = 0.05,
) -> dict[str, Any]:
    started = time.perf_counter()
    polls = 0
    while True:
        polls += 1
        snapshot = await pipeline_snapshot(conn, resource_id)
        if (
            snapshot["pending_intents"] == 0
            and snapshot["pending_deletes"] == 0
            and snapshot["chunks"] == snapshot["mappings"] == snapshot["vector_points"] == 0
        ):
            return {"polls": polls, "elapsed_seconds": time.perf_counter() - started, "state": snapshot}
        if time.perf_counter() - started >= timeout_seconds:
            raise AdapterError(f"native delete did not settle: {snapshot}")
        await asyncio.sleep(poll_seconds)


async def run() -> dict[str, Any]:
    dsn = required_environment("AKB_NATIVE_REVISION_MEASUREMENT_DSN")
    base_url = required_environment("AKB_NATIVE_REVISION_PUBLIC_BASE_URL").rstrip("/")
    token = required_environment("AKB_NATIVE_REVISION_PUBLIC_TOKEN")
    vault = required_environment("AKB_NATIVE_REVISION_PUBLIC_VAULT")
    path = required_environment("AKB_NATIVE_REVISION_PUBLIC_DOCUMENT")
    expected_token = required_environment("AKB_NATIVE_REVISION_EXPECTED_SEARCH_TOKEN")
    prior_token = required_environment("AKB_NATIVE_REVISION_PRIOR_SEARCH_TOKEN")
    timeout_seconds = float(os.environ.get("AKB_NATIVE_REVISION_SETTLEMENT_TIMEOUT_SECONDS", "60"))
    conn = await asyncpg.connect(dsn, timeout=10)
    try:
        database = str(await conn.fetchval("SELECT current_database()"))
        validate_measurement_database(database)
        resource_id = await conn.fetchval(
            """
            SELECT r.resource_id
              FROM native_resources r JOIN vaults v ON v.id = r.namespace_id
             WHERE v.name = $1 AND r.surface = 'document'
               AND r.current_path = $2 AND r.lifecycle = 'live'
            """,
            vault,
            path,
        )
        if resource_id is None:
            raise AdapterError("public settled fixture is not a live native Document")
        before = await pipeline_snapshot(conn, resource_id)
        prior_chunk_ids = await chunk_ids(conn, resource_id)
        headers = {"Authorization": f"Bearer {token}"}
        encoded_path = quote(path, safe="/")
        operations: list[dict[str, Any]] = []
        async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=15) as client:
            started = time.perf_counter()
            update = await client.patch(
                f"/api/v1/documents/{quote(vault, safe='')}/{encoded_path}",
                json={"content": f"# Settled\n{expected_token}\n", "message": "M1 r5 settled update"},
            )
            update.raise_for_status()
            updated = update.json()
            operations.append({"operation_id": "documentsUpdateDocument", "latency_ms": (time.perf_counter() - started) * 1000})
            current = await client.get(f"/api/v1/documents/{quote(vault, safe='')}/{encoded_path}")
            current.raise_for_status()
            operations.append({"operation_id": "documentsGetDocument", "current_head": current.json()["current_commit"]})
            settled = await settle(conn, resource_id, timeout_seconds=timeout_seconds)
            found = await client.get("/api/v1/search", params={"q": expected_token, "vault": vault, "limit": 20})
            found.raise_for_status()
            operations.append({"operation_id": "searchSearchDocuments", "query": "current", "returned": found.json()["returned"]})
            stale = await client.get("/api/v1/search", params={"q": prior_token, "vault": vault, "limit": 20})
            stale.raise_for_status()
            operations.append({"operation_id": "searchSearchDocuments", "query": "prior", "returned": stale.json()["returned"]})
        after = await pipeline_snapshot(conn, resource_id)
        stale_after_replace = await stale_chunk_residue(conn, prior_chunk_ids)
        current_chunk_ids = await chunk_ids(conn, resource_id)
        current_head = updated["current_commit"]
        expected_uri = f"akb://{vault}/doc/{path}"
        current_results = found.json()["results"]
        assertions = {
            "public_current_head": current.json()["current_commit"] == current_head,
            "mapping_binds_current_head": after["mapped_revision"] == current_head,
            "actual_chunks": int(after["chunks"] or 0) > 0 and after["chunks"] == after["mappings"],
            "sparse_pgvector_points": after["vector_points"] == after["chunks"],
            "current_query_hit": any(row["uri"] == expected_uri for row in current_results),
            "prior_query_absent": all(row["uri"] != expected_uri for row in stale.json()["results"]),
            "queues_settled": after["pending_intents"] == after["pending_chunks"] == after["pending_deletes"] == 0,
            "prior_chunk_ids_absent": all(value == 0 for value in stale_after_replace.values()),
        }
        async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=15) as client:
            deleted = await client.delete(
                f"/api/v1/documents/{quote(vault, safe='')}/{encoded_path}"
            )
            deleted.raise_for_status()
            operations.append({"operation_id": "documentsDeleteDocument"})
        delete_settlement = await settle_deleted(
            conn, resource_id, timeout_seconds=timeout_seconds
        )
        post_delete = await pipeline_snapshot(conn, resource_id)
        stale_after_delete = await stale_chunk_residue(conn, current_chunk_ids)
        assertions["delete_residue_closed"] = (
            post_delete["chunks"]
            == post_delete["mappings"]
            == post_delete["vector_points"]
            == post_delete["pending_deletes"]
            == 0
        ) and all(value == 0 for value in stale_after_delete.values())
        if not all(assertions.values()):
            raise AdapterError(f"settled pipeline assertion failed: {assertions}")
        return {
            "protocol_version": PROTOCOL_VERSION,
            "source": {
                "source_revision": source_revision(),
                "image_digest": required_environment("AKB_NATIVE_REVISION_RUNTIME_IMAGE_DIGEST"),
                "config_digest": required_environment("AKB_NATIVE_REVISION_RUNTIME_CONFIG_DIGEST"),
            },
            "profile": {
                "database": database,
                "vector_driver": "pgvector",
                "dense": "disabled-by-deployment",
                "runtime": bounded_json_object("AKB_NATIVE_REVISION_RUNTIME_PROFILE_JSON", required=True),
            },
            "resource_id": str(resource_id),
            "current_head": current_head,
            "pre_queue": before,
            "settlement_polling": settled,
            "post_queue": after,
            "prior_chunk_ids": [str(value) for value in prior_chunk_ids],
            "stale_after_replace": stale_after_replace,
            "deleted_chunk_ids": [str(value) for value in current_chunk_ids],
            "stale_after_delete": stale_after_delete,
            "delete_settlement_polling": delete_settlement,
            "cleanup_residue": post_delete,
            "public_operations": operations,
            "search_hit": {"uri": expected_uri, "resource_id": str(resource_id), "revision": current_head},
            "assertions": assertions,
        }
    finally:
        await conn.close()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run()), ensure_ascii=False, sort_keys=True))
