#!/usr/bin/env python3
"""First-party B-text/B-grep measurement adapter for a real PostgreSQL DB."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import asyncpg


BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.exceptions import ForbiddenError  # noqa: E402
from app.services.m1_native_grep_service import M1NativeGrepService  # noqa: E402
from app.services.m1_pg_body_store import M1PgBodyStore  # noqa: E402
from app.services.native_revision_service import NativeRevisionService  # noqa: E402
from native_revision_m1_adapter import (  # noqa: E402
    AdapterError,
    receipt_provenance,
    receipt_safe_profile,
    required_environment,
    run_artifact_path,
    source_revision,
    validate_measurement_database,
    write_bound_json,
)


PROTOCOL_VERSION = "akb-native-revision-m1-text-grep/v1"
WORKLOADS = {"W3-document-grep", "W3-text-file-grep"}


def _safe_request_outcome(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        key: raw[key]
        for key in ("surface", "revision_id", "byte_size", "latency_ms")
        if key in raw
    }


def _safe_grep_observation(raw: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {
        key: raw[key]
        for key in (
            "searched_resources",
            "searched_bytes",
            "total_resources",
            "total_matches",
            "returned_resources",
            "returned_matches",
            "n_resources",
            "truncated",
        )
        if key in raw
    }
    safe["resources"] = [
        {
            key: row[key]
            for key in ("resource_type", "revision", "content_hash")
            if key in row
        }
        for row in (raw.get("results") or raw.get("resources") or [])
    ]
    return safe


def _safe_cases(cases: dict[str, Any]) -> dict[str, Any]:
    return {
        "assertions": cases["assertions"],
        "observations": {
            name: _safe_grep_observation(value)
            for name, value in cases.get("observations", {}).items()
        },
    }


async def _load_migration(conn: asyncpg.Connection, filename: str) -> None:
    path = BACKEND / "app" / "db" / "migrations" / filename
    spec = importlib.util.spec_from_file_location(f"akb_{filename.replace('.', '_')}", path)
    if spec is None or spec.loader is None:
        raise AdapterError(f"cannot load migration {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    await module.migrate(conn=conn)


async def initialise(
    dsn: str,
) -> tuple[asyncpg.Pool, str, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    conn = await asyncpg.connect(dsn, timeout=10)
    try:
        database = str(await conn.fetchval("SELECT current_database()"))
        validate_measurement_database(database)
        await conn.execute((BACKEND / "app" / "db" / "init.sql").read_text(encoding="utf-8"))
        await _load_migration(conn, "048_native_revision_core.py")
        await _load_migration(conn, "049_native_revision_m1_pg_body.py")
        await _load_migration(conn, "053_native_revision_m1_payload_placement.py")
        suffix = uuid.uuid4().hex
        owner_id = await conn.fetchval(
            """
            INSERT INTO users (username, email, password_hash)
            VALUES ($1, $2, 'm1-measurement-disabled') RETURNING id
            """,
            f"m1-text-owner-{suffix}",
            f"m1-text-owner-{suffix}@invalid.example",
        )
        denied_id = await conn.fetchval(
            """
            INSERT INTO users (username, email, password_hash)
            VALUES ($1, $2, 'm1-measurement-disabled') RETURNING id
            """,
            f"m1-text-denied-{suffix}",
            f"m1-text-denied-{suffix}@invalid.example",
        )
        reader_id = await conn.fetchval(
            """
            INSERT INTO users (username, email, password_hash)
            VALUES ($1, $2, 'm1-measurement-disabled') RETURNING id
            """,
            f"m1-text-reader-{suffix}",
            f"m1-text-reader-{suffix}@invalid.example",
        )
        allowed_vault = await conn.fetchval(
            """
            INSERT INTO vaults (name, git_path, owner_id)
            VALUES ($1, '/tmp/m1-native-measurement-unused.git', $2) RETURNING id
            """,
            f"m1-text-allowed-{suffix}",
            owner_id,
        )
        denied_vault = await conn.fetchval(
            """
            INSERT INTO vaults (name, git_path, owner_id)
            VALUES ($1, '/tmp/m1-native-measurement-unused.git', $2) RETURNING id
            """,
            f"m1-text-denied-{suffix}",
            denied_id,
        )
        await conn.execute(
            "INSERT INTO vault_access (vault_id, user_id, role, granted_by) VALUES ($1, $2, 'reader', $3)",
            allowed_vault,
            reader_id,
            owner_id,
        )
        return (
            await asyncpg.create_pool(dsn, min_size=1, max_size=12),
            database,
            owner_id,
            denied_id,
            reader_id,
            allowed_vault,
            denied_vault,
        )
    finally:
        await conn.close()


async def _create(
    service: NativeRevisionService,
    *,
    namespace_id: uuid.UUID,
    surface: str,
    path: str,
    body: str,
) -> dict[str, Any]:
    started = time.perf_counter()
    result = await service.create_text(
        namespace_id=namespace_id,
        surface=surface,
        path=path,
        payload=body,
        actor="m1-text-grep-adapter",
        mutation_id=uuid.uuid4(),
        expected_size=len(body.encode("utf-8")),
    )
    return {
        "resource_id": str(result.resource_id),
        "revision_id": result.revision_id,
        "surface": surface,
        "path": path,
        "byte_size": len(body.encode("utf-8")),
        "latency_ms": round((time.perf_counter() - started) * 1000, 3),
    }


async def _timed_grep(grep: M1NativeGrepService, pattern: str, **kwargs) -> tuple[dict[str, Any], float]:
    started = time.perf_counter()
    result = await grep.grep(pattern, **kwargs)
    return result, round((time.perf_counter() - started) * 1000, 3)


async def _document_workload(
    service: NativeRevisionService,
    grep: M1NativeGrepService,
    owner_id: uuid.UUID,
    reader_id: uuid.UUID,
    allowed_vault: uuid.UUID,
    denied_vault: uuid.UUID,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[float]]:
    requests = [
        await _create(
            service,
            namespace_id=allowed_vault,
            surface="document",
            path="guides/alpha.md",
            body="Title\nNeedle Alpha\ncommon token\n",
        ),
        await _create(
            service,
            namespace_id=allowed_vault,
            surface="document",
            path="guides/beta.md",
            body="Title\nneedle beta\ncommon token\n",
        ),
        await _create(
            service,
            namespace_id=denied_vault,
            surface="document",
            path="secret.md",
            body="Needle forbidden\n",
        ),
        await _create(
            service,
            namespace_id=allowed_vault,
            surface="document",
            path="team_ax/escape.md",
            body="wildcard-scope-token\n",
        ),
    ]
    latencies: list[float] = []
    literal, elapsed = await _timed_grep(grep, "needle", user_id=owner_id)
    latencies.append(elapsed)
    regex, elapsed = await _timed_grep(grep, r"Needle\s+Alpha", user_id=owner_id, regex=True)
    latencies.append(elapsed)
    case, elapsed = await _timed_grep(grep, "Needle", user_id=owner_id, case_sensitive=True)
    latencies.append(elapsed)
    no_match, elapsed = await _timed_grep(grep, "absent-token", user_id=owner_id)
    latencies.append(elapsed)
    count, elapsed = await _timed_grep(grep, "common", user_id=owner_id, count_only=True)
    latencies.append(elapsed)
    files, elapsed = await _timed_grep(grep, "common", user_id=owner_id, files_with_matches=True)
    latencies.append(elapsed)
    scoped, elapsed = await _timed_grep(grep, "needle", user_id=owner_id, collection="guides/alpha.md")
    latencies.append(elapsed)
    wildcard_scope, elapsed = await _timed_grep(
        grep,
        "wildcard-scope-token",
        user_id=owner_id,
        collection="team_%",
    )
    latencies.append(elapsed)
    reader_replace_denied = False
    try:
        await grep.grep(
            "common",
            user_id=reader_id,
            replace="forbidden-reader-write",
            actor="m1-reader",
        )
    except ForbiddenError:
        reader_replace_denied = True
    replaced, elapsed = await _timed_grep(
        grep,
        "common",
        user_id=owner_id,
        replace="shared",
        actor="m1-text-grep-adapter",
    )
    latencies.append(elapsed)
    after_replace, elapsed = await _timed_grep(grep, "shared", user_id=owner_id)
    latencies.append(elapsed)
    assertions = {
        "literal_two_documents": literal["total_resources"] == 2 and literal["total_matches"] == 2,
        "regex_one_document": regex["total_resources"] == 1 and regex["total_matches"] == 1,
        "case_sensitive_one": case["total_resources"] == 1,
        "no_match": no_match["total_resources"] == 0,
        "count_shape": count["total_resources"] == 2 and count["total_matches"] == 2,
        "resource_list_shape": files["n_resources"] == 2,
        "scope_one": scoped["total_resources"] == 1,
        "wildcard_scope_literal": wildcard_scope["total_resources"] == 0,
        "acl_excluded": all("forbidden" not in match["text"] for row in literal["results"] for match in row["matches"]),
        "reader_replace_denied": reader_replace_denied,
        "replace_revisioned": replaced["replaced_resources"] == 2 and after_replace["total_resources"] == 2,
        "document_only": all(row["resource_type"] == "document" for row in literal["results"]),
    }
    return {"assertions": assertions, "observations": {"literal": literal, "count": count, "files": files}}, requests, latencies


async def _text_file_workload(
    service: NativeRevisionService,
    grep: M1NativeGrepService,
    owner_id: uuid.UUID,
    allowed_vault: uuid.UUID,
    denied_vault: uuid.UUID,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[float]]:
    requests = [
        await _create(
            service,
            namespace_id=allowed_vault,
            surface="document",
            path="src/readme.md",
            body="shared-symbol from document\n",
        ),
        await _create(
            service,
            namespace_id=allowed_vault,
            surface="file",
            path="src/module.py",
            body="def shared_symbol():\n    return 'needle-file'\n",
        ),
        await _create(
            service,
            namespace_id=denied_vault,
            surface="file",
            path="src/secret.py",
            body="needle-file forbidden\n",
        ),
    ]
    latencies: list[float] = []
    additive, elapsed = await _timed_grep(
        grep, "shared", user_id=owner_id, include_text_files=True
    )
    latencies.append(elapsed)
    file_only, elapsed = await _timed_grep(
        grep, "needle-file", user_id=owner_id, include_text_files=True
    )
    latencies.append(elapsed)
    case, elapsed = await _timed_grep(
        grep,
        "NEEDLE-FILE",
        user_id=owner_id,
        case_sensitive=True,
        include_text_files=True,
    )
    latencies.append(elapsed)
    no_match, elapsed = await _timed_grep(
        grep, "missing-file-token", user_id=owner_id, include_text_files=True
    )
    latencies.append(elapsed)
    file_resource_id = uuid.UUID(requests[1]["resource_id"])
    pinned_scope, elapsed = await _timed_grep(
        grep,
        "needle-file",
        user_id=owner_id,
        resource_id=file_resource_id,
        include_text_files=True,
    )
    latencies.append(elapsed)
    snapshot = await service.get_current(
        namespace_id=allowed_vault,
        surface="file",
        path="src/module.py",
    )
    assertions = {
        "resource_neutral_document_and_file": {row["resource_type"] for row in additive["results"]} == {"document", "file"},
        "file_match": file_only["total_resources"] == 1 and file_only["results"][0]["resource_type"] == "file",
        "case_sensitive_no_match": case["total_resources"] == 0,
        "no_match": no_match["total_resources"] == 0,
        "acl_excluded": file_only["total_matches"] == 1,
        "single_resource_scope": pinned_scope["total_resources"] == 1,
        "head_revision_reported": pinned_scope["results"][0]["revision"] == snapshot.revision_id,
        "exact_bytes": snapshot.payload_bytes == b"def shared_symbol():\n    return 'needle-file'\n",
        "pg_body_profile": snapshot.selected_placement == "pg-bodystore-v1",
    }
    return {"assertions": assertions, "observations": {"additive": additive, "file_only": file_only}}, requests, latencies


async def run() -> dict[str, Any]:
    workload = required_environment("AKB_NATIVE_REVISION_WORKLOAD")
    if workload not in WORKLOADS:
        raise AdapterError(f"unsupported native text/grep workload: {workload}")
    dsn = required_environment("AKB_NATIVE_REVISION_MEASUREMENT_DSN")
    pool, database, owner_id, denied_id, reader_id, allowed_vault, denied_vault = await initialise(dsn)
    requests: list[dict[str, Any]] = []
    try:
        store = M1PgBodyStore(pool)
        service = NativeRevisionService(pool, payload_store=store)
        grep = M1NativeGrepService(pool, body_store=store)
        if workload == "W3-document-grep":
            cases, requests, grep_latencies = await _document_workload(
                service, grep, owner_id, reader_id, allowed_vault, denied_vault
            )
        else:
            cases, requests, grep_latencies = await _text_file_workload(
                service, grep, owner_id, allowed_vault, denied_vault
            )
        if not all(cases["assertions"].values()):
            failed = [name for name, passed in cases["assertions"].items() if not passed]
            raise AdapterError(f"native text/grep semantic assertions failed: {failed}")
        async with pool.acquire() as conn:
            profile = dict(
                await conn.fetchrow(
                    """
                    SELECT count(*)::int AS bodies,
                           coalesce(sum(byte_size), 0)::bigint AS body_bytes,
                           count(DISTINCT digest)::int AS distinct_digests
                      FROM m1_reference_payloads
                     WHERE namespace_id = $1 AND selected_placement = 'pg-bodystore-v1'
                    """,
                    allowed_vault,
                )
            )
        revision = source_revision()
        runtime, environment = receipt_provenance(revision, database, allowed_vault)
        safe_cases = _safe_cases(cases)
        safe_requests = [_safe_request_outcome(request) for request in requests]
        safe_environment = {
            "tier": environment["tier"],
            "node_profile": receipt_safe_profile(environment["node_profile"]),
            "storage_profile": receipt_safe_profile(environment["storage_profile"]),
        }
        request_artifact = write_bound_json(
            run_artifact_path("native-text-grep-requests"),
            {"workload": workload, "requests": safe_requests, "grep_latency_ms": grep_latencies},
        )
        authority_artifact = write_bound_json(
            run_artifact_path("native-text-grep-authority"),
            {"workload": workload, "assertions": cases["assertions"], "body_profile": profile},
        )
        return {
            "protocol_version": PROTOCOL_VERSION,
            "workload": workload,
            "cases": safe_cases,
            "receipt": {
                "inputs": receipt_safe_profile(
                    {
                        "run_id": required_environment("AKB_NATIVE_REVISION_RUN_ID"),
                        "corpus_id": str(allowed_vault),
                    }
                ),
                "runtime": runtime,
                "environment": safe_environment,
                "latency": {"samples_or_artifact": grep_latencies, "unit": "ms"},
                "resources": {
                    "snapshot": {
                        "database_binding": receipt_safe_profile({"database": database})["binding"],
                        "body_profile": profile,
                    }
                },
                "requests": {
                    "outcomes": safe_requests,
                    "artifact_digest": request_artifact["sha256"],
                },
            },
            "provenance": {
                "adapter": {
                    "identity": "akb.backend.scripts.native_revision_m1_text_grep_adapter",
                    "source_revision": revision,
                },
                "request_artifact": {"sha256": request_artifact["sha256"]},
                "authority_artifact": {"sha256": authority_artifact["sha256"]},
            },
        }
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM vaults WHERE id = ANY($1::uuid[])", [allowed_vault, denied_vault])
            await conn.execute(
                "DELETE FROM users WHERE id = ANY($1::uuid[])",
                [owner_id, denied_id, reader_id],
            )
        await pool.close()


def main() -> int:
    observation_path = Path(required_environment("AKB_NATIVE_REVISION_NATIVE_OBSERVATION_PATH"))
    write_bound_json(observation_path, asyncio.run(run()))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AdapterError as exc:
        print(f"native revision M1 text/grep adapter: {exc}", file=sys.stderr)
        raise SystemExit(2)
