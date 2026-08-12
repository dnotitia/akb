"""Crash-safe staged app rollout worker.

Claims are made with short transactions and row locks.  DDL/backfill work is
then performed for one target/step at a time under the installation advisory
lock; a persisted checkpoint makes a retry resume rather than restart.
"""

from __future__ import annotations

import logging
import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from app.config import settings
from app.db.postgres import get_pool
from app.exceptions import ConflictError, ValidationError
from app.repositories import table_data_repo, table_registry_repo
from app.services import app_rollout_service
from app.services._backfill import BackfillRunner
from app.services.app_inventory_service import expected_schema_fingerprint
from app.services.table_service import alter_table
from app.services.role_sync import get_role_sync

logger = logging.getLogger("akb.app_rollout_worker")
LEASE_SECONDS = 120


def _safe_identifier(value: str) -> str:
    if not isinstance(value, str) or not value or not value.replace("_", "").isalnum() or not value[0].isalpha():
        raise ValidationError("Rollout identifier is invalid")
    return table_data_repo.safe_ident(value)


async def _claim_target(conn: Any) -> dict[str, Any] | None:
    # A later batch is eligible only after all targets in the preceding batch
    # are applied/replayed.  This keeps the canary and 10-wide order durable.
    row = await conn.fetchrow(
        """
        WITH eligible AS (
            SELECT t.id
              FROM app_rollout_targets t
             WHERE t.state='pending'
               AND NOT EXISTS (
                   SELECT 1 FROM app_rollout_targets prior
                    WHERE prior.job_id=t.job_id
                      AND prior.batch_no < t.batch_no
                      AND prior.state NOT IN ('applied','replayed')
               )
               AND NOT EXISTS (
                   SELECT 1 FROM app_rollout_targets same_batch
                    WHERE same_batch.job_id=t.job_id
                      AND same_batch.batch_no=t.batch_no
                      AND same_batch.state='running'
               )
             ORDER BY t.job_id, t.batch_no, t.ordinal
             FOR UPDATE SKIP LOCKED
             LIMIT 1
        )
        UPDATE app_rollout_targets t
           SET state='running', attempts=t.attempts+1,
               lease_owner=$1, lease_expires_at=NOW()+($2::text || ' seconds')::interval,
               started_at=COALESCE(t.started_at,NOW())
          FROM eligible e
         WHERE t.id=e.id
        RETURNING t.id, t.job_id, t.app_id, t.installation_id, t.vault_id,
                  t.release_id, t.batch_no, t.ordinal
        """,
        _worker_id,
        str(LEASE_SECONDS),
    )
    return dict(row) if row else None


async def _claim_or_reclaim(conn: Any) -> dict[str, Any] | None:
    # Reclaim only the same checksum/release job.  Expired claims from a
    # previous worker are safe to retry; a different release never reuses them.
    await conn.execute(
        """
        UPDATE app_rollout_targets
           SET state='pending', lease_owner=NULL, lease_expires_at=NULL
         WHERE state='running' AND lease_expires_at < NOW()
           AND job_id IN (SELECT id FROM app_rollout_jobs WHERE status IN ('pending','running'))
        """
    )
    return await _claim_target(conn)


async def _mark_job_blocked(conn: Any, target: dict[str, Any], reason: str) -> None:
    safe_reason = reason if app_rollout_service._REASON.fullmatch(reason) else "execution_failed"
    await conn.execute(
        "UPDATE app_rollout_steps SET state='failed', reason_code=$2, lease_owner=NULL, lease_expires_at=NULL, completed_at=NOW() WHERE target_id=$1 AND state='running'",
        target["id"],
        safe_reason,
    )
    await conn.execute(
        "UPDATE app_rollout_targets SET state='failed', reason_code=$2, lease_owner=NULL, lease_expires_at=NULL, completed_at=NOW() WHERE id=$1",
        target["id"], safe_reason,
    )
    await conn.execute(
        "UPDATE app_rollout_snapshot_targets SET state='denied', reason_code=$2 WHERE id=(SELECT snapshot_target_id FROM app_rollout_targets WHERE id=$1)",
        target["id"], safe_reason,
    )
    await conn.execute(
        "UPDATE vault_app_installations SET lifecycle='blocked', blocked_reason=$2 WHERE id=$1 AND app_id=$3",
        target["installation_id"], safe_reason, target["app_id"],
    )
    await conn.execute(
        "UPDATE app_rollout_jobs SET status='blocked', blocked_reason=$2, completed_at=COALESCE(completed_at,NOW()) WHERE id=$1",
        target["job_id"], safe_reason,
    )
    await conn.execute(
        "INSERT INTO app_rollout_audit(job_id,app_id,installation_id,action,outcome,reason_code) VALUES($1,$2,$3,'execute','blocked',$4)",
        target["job_id"],
        target["app_id"],
        target["installation_id"],
        safe_reason,
    )
    await conn.execute(
        "UPDATE app_rollout_targets SET state='blocked', reason_code=$2 WHERE job_id=$1 AND state='pending'",
        target["job_id"], "rollout_blocked",
    )
    await conn.execute(
        "UPDATE app_rollout_steps SET state='blocked', reason_code='rollout_blocked', lease_owner=NULL, lease_expires_at=NULL WHERE job_id=$1 AND state='pending'",
        target["job_id"],
    )
    await conn.execute(
        "UPDATE app_rollout_snapshot_targets SET state='skipped', reason_code='rollout_blocked' WHERE id IN (SELECT snapshot_target_id FROM app_rollout_targets WHERE job_id=$1 AND state='blocked')",
        target["job_id"],
    )


async def _preflight_target(conn: Any, target: dict[str, Any]) -> tuple[bool, str | None]:
    row = await conn.fetchrow(
        """
        SELECT i.lifecycle, i.current_release_id, i.desired_release_id, i.grant_generation,
               g.generation AS active_generation, g.status AS grant_status,
               o.observed_release_id, o.observed_grant_generation, o.observed_generation
          FROM vault_app_installations i
          LEFT JOIN LATERAL (SELECT generation,status FROM installation_grants WHERE installation_id=i.id AND status='active' ORDER BY generation DESC LIMIT 1) g ON TRUE
          LEFT JOIN app_installation_observed_states o ON o.installation_id=i.id
         WHERE i.id=$1 AND i.app_id=$2
         FOR UPDATE OF i
        """,
        target["installation_id"], target["app_id"],
    )
    if row is None or row["lifecycle"] != "upgrading" or row["desired_release_id"] != target["release_id"]:
        return False, "installation_stale"
    if row["grant_status"] != "active" or row["active_generation"] != row["grant_generation"]:
        return False, "grant_stale"
    if row["observed_generation"] is None or row["observed_release_id"] != row["current_release_id"] or row["observed_grant_generation"] != row["grant_generation"]:
        return False, "observed_stale"
    return True, None


async def _owned(conn: Any, installation_id: uuid.UUID, vault_id: uuid.UUID, table: str) -> bool:
    return bool(
        await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM app_owned_resources WHERE installation_id=$1 AND vault_id=$2 AND resource_kind='table' AND resource_key=$3 AND status='owned')",
            installation_id,
            vault_id,
            table,
        )
    )


async def _create_table_owned(conn: Any, target: dict[str, Any], payload: dict[str, Any]) -> None:
    table_name = _safe_identifier(payload["table"])
    existing = await table_registry_repo.find_by_name(conn, target["vault_id"], table_name)
    if existing:
        if not await _owned(conn, target["installation_id"], target["vault_id"], table_name):
            raise ConflictError("Rollout table is owned by another installation")
        return
    vault = await conn.fetchrow("SELECT name FROM vaults WHERE id=$1", target["vault_id"])
    if vault is None:
        raise ConflictError("Rollout vault is unavailable")
    columns = list(payload["columns"])
    pg_name = table_data_repo.pg_table_name(vault["name"], table_name)
    table_id = uuid.uuid4()
    await table_data_repo.create_dynamic_table(conn, pg_name, columns, vault_name=vault["name"])
    await table_registry_repo.insert(
        conn,
        table_id=table_id,
        vault_id=target["vault_id"],
        name=table_name,
        description="",
        columns=columns,
        created_by="app-rollout-worker",
        now=datetime.now(timezone.utc),
    )
    await get_role_sync().grant_table_in_conn(conn, target["vault_id"], pg_name)
    await conn.execute(
        """INSERT INTO app_owned_resources(installation_id,vault_id,resource_kind,resource_key,status,metadata) VALUES($1,$2,'table',$3,'owned','{}'::jsonb) ON CONFLICT (installation_id,resource_kind,resource_key) DO NOTHING""",
        target["installation_id"], target["vault_id"], table_name,
    )


async def _run_backfill(conn: Any, target: dict[str, Any], step: dict[str, Any], payload: dict[str, Any]) -> bool:
    table_name = _safe_identifier(payload["table"])
    column = _safe_identifier(payload["column"])
    primary_key = _safe_identifier(payload["primary_key"])
    if primary_key != "id":
        raise ValidationError("v1 backfill cursor must use the stable id primary key")
    vault_name = await conn.fetchval("SELECT name FROM vaults WHERE id=$1", target["vault_id"])
    if not vault_name:
        raise ConflictError("Rollout vault is unavailable")
    pg_name = table_data_repo.pg_table_name(vault_name, table_name)
    checkpoint = step["checkpoint"] or {}
    if isinstance(checkpoint, str):
        try:
            checkpoint = json.loads(checkpoint)
        except (TypeError, ValueError, json.JSONDecodeError):
            checkpoint = {}
    if not isinstance(checkpoint, dict):
        checkpoint = {}
    cursor = checkpoint.get("cursor")
    batch_size = int(payload["batch_size"])
    if cursor:
        rows = await conn.fetch(
            f"SELECT id FROM {pg_name} WHERE {column} IS NULL AND id > $1::uuid ORDER BY id LIMIT $2",
            uuid.UUID(str(cursor)), batch_size,
        )
    else:
        rows = await conn.fetch(
            f"SELECT id FROM {pg_name} WHERE {column} IS NULL ORDER BY id LIMIT $1",
            batch_size,
        )
    if not rows:
        await conn.execute("UPDATE app_rollout_steps SET state='applied', completed_at=NOW(), lease_owner=NULL, lease_expires_at=NULL WHERE id=$1", step["id"])
        return True
    ids = [row["id"] for row in rows]
    if cursor:
        previous_cursor = uuid.UUID(str(cursor))
        if any(row["id"] <= previous_cursor for row in rows):
            raise ConflictError("Rollout backfill cursor did not advance")
    await conn.execute(
        f"UPDATE {pg_name} SET {column}=$1 WHERE id=ANY($2::uuid[]) AND {column} IS NULL",
        payload.get("value"), ids,
    )
    last = str(ids[-1])
    previous_completed = checkpoint.get("completed", 0)
    previous_total = checkpoint.get("total", 0)
    if not isinstance(previous_completed, int) or isinstance(previous_completed, bool):
        previous_completed = 0
    if not isinstance(previous_total, int) or isinstance(previous_total, bool):
        previous_total = 0
    completed = previous_completed + len(ids)
    checkpoint = {
        "cursor": last,
        "phase": "backfill",
        "step": step["step_id"],
        "completed": completed,
        "total": max(previous_total, completed),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    await conn.execute("UPDATE app_rollout_steps SET checkpoint=$2::jsonb, state='pending', lease_owner=NULL, lease_expires_at=NULL WHERE id=$1", step["id"], json.dumps(checkpoint, ensure_ascii=False, separators=(",", ":")))
    return False


async def _execute_step(conn: Any, target: dict[str, Any], step: dict[str, Any], manifest_step: dict[str, Any]) -> bool:
    payload = manifest_step["payload"]
    table_name = _safe_identifier(payload["table"])
    operation = manifest_step["operation"]
    if operation != "create_table" and not await _owned(conn, target["installation_id"], target["vault_id"], table_name):
        raise ConflictError("Rollout resource ownership preflight failed")
    if operation == "create_table":
        await _create_table_owned(conn, target, payload)
        await conn.execute("UPDATE app_rollout_steps SET state='applied', completed_at=NOW(), lease_owner=NULL, lease_expires_at=NULL WHERE id=$1", step["id"])
        return True
    if operation == "backfill_column":
        return await _run_backfill(conn, target, step, payload)
    if operation == "add_column":
        await alter_table(target["vault_id"], table_name, actor_id="app-rollout-worker", add_columns=[payload["column"]], _conn=conn, _defer_index=True)
    elif operation == "add_index":
        await alter_table(target["vault_id"], table_name, actor_id="app-rollout-worker", add_indexes=[{"name": payload["name"], "columns": [{"name": c, "order": "asc"} for c in payload["columns"]]}], _conn=conn, _defer_index=True)
    elif operation == "set_not_null":
        column = _safe_identifier(payload["column"])
        vault_name = await conn.fetchval("SELECT name FROM vaults WHERE id=$1", target["vault_id"])
        if not vault_name:
            raise ConflictError("Rollout vault is unavailable")
        pg_name = table_data_repo.pg_table_name(vault_name, table_name)
        remaining = await conn.fetchval(f"SELECT COUNT(*) FROM {pg_name} WHERE {column} IS NULL")
        if remaining:
            raise ConflictError("Rollout not-null precondition failed")
        await alter_table(target["vault_id"], table_name, actor_id="app-rollout-worker", alter_columns=[{"name": payload["column"], "set_not_null": True}], _conn=conn, _defer_index=True)
    await conn.execute("UPDATE app_rollout_steps SET state='applied', completed_at=NOW(), lease_owner=NULL, lease_expires_at=NULL WHERE id=$1", step["id"])
    return True


async def _schema_fingerprint(conn: Any, vault_id: uuid.UUID, installation_id: uuid.UUID) -> str:
    rows = await conn.fetch(
        """SELECT resource.resource_key, table_row.columns, table_row.unique_keys, table_row.indexes
             FROM app_owned_resources resource
             JOIN vault_tables table_row
               ON table_row.vault_id=resource.vault_id AND table_row.name=resource.resource_key
            WHERE resource.installation_id=$1 AND resource.vault_id=$2
              AND resource.resource_kind='table' AND resource.status='owned'
            ORDER BY resource.resource_key""",
        installation_id,
        vault_id,
    )
    payload = [
        {
            "name": row["resource_key"],
            "columns": row["columns"],
            "unique_keys": row["unique_keys"],
            "indexes": row["indexes"],
        }
        for row in rows
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


async def _process_target(target: dict[str, Any]) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.fetchval("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"app-installation-rollout:{target['installation_id']}")
            ok, reason = await _preflight_target(conn, target)
            if not ok:
                await _mark_job_blocked(conn, target, reason or "preflight_failed")
                return
            job = await conn.fetchrow("SELECT release_id, manifest_checksum FROM app_rollout_jobs WHERE id=$1", target["job_id"])
            release = await conn.fetchrow("SELECT manifest FROM app_releases WHERE app_id=$1 AND id=$2", target["app_id"], target["release_id"])
            if job is None or release is None:
                await _mark_job_blocked(conn, target, "release_missing")
                return
            try:
                manifest = app_rollout_service.validate_manifest(release["manifest"], job["manifest_checksum"])
            except Exception:
                await _mark_job_blocked(conn, target, "manifest_invalid")
                return
            steps = await conn.fetch("SELECT id, step_id, step_order, operation, state, checkpoint, reason_code FROM app_rollout_steps WHERE target_id=$1 ORDER BY step_order", target["id"])
            for step in steps:
                if step["state"] in {"applied", "replayed"}:
                    continue
                manifest_step = next((item for item in manifest["steps"] if item["id"] == step["step_id"]), None)
                if manifest_step is None:
                    await _mark_job_blocked(conn, target, "step_missing")
                    return
                await conn.execute("UPDATE app_rollout_steps SET state='running', lease_owner=$2, lease_expires_at=NOW()+INTERVAL '120 seconds' WHERE id=$1", step["id"], _worker_id)
                try:
                    # Keep a failed DDL/data statement inside a savepoint so
                    # the enclosing target transaction remains usable for the
                    # bounded failure ledger update below.
                    async with conn.transaction():
                        done = await _execute_step(conn, target, dict(step), manifest_step)
                except Exception:
                    logger.exception("rollout step failed")
                    await _mark_job_blocked(conn, target, "step_failed")
                    return
                if not done:
                    # A bounded backfill commits its cursor and yields the
                    # target so the next claim resumes from that cursor.
                    await conn.execute(
                        "UPDATE app_rollout_targets SET state='pending', lease_owner=NULL, lease_expires_at=NULL WHERE id=$1",
                        target["id"],
                    )
                    return
            expected = expected_schema_fingerprint(release["manifest"])
            try:
                async with conn.transaction():
                    actual = await _schema_fingerprint(conn, target["vault_id"], target["installation_id"])
            except Exception:
                await _mark_job_blocked(conn, target, "schema_unavailable")
                return
            if expected is not None and actual.lower() != expected.lower():
                await _mark_job_blocked(conn, target, "schema_mismatch")
                return
            await conn.execute("UPDATE app_rollout_targets SET state='applied', lease_owner=NULL, lease_expires_at=NULL, completed_at=NOW() WHERE id=$1", target["id"])
            await conn.execute("UPDATE vault_app_installations SET lifecycle='active', current_release_id=desired_release_id, blocked_reason=NULL WHERE id=$1 AND app_id=$2", target["installation_id"], target["app_id"])
            observed = await conn.fetchrow("SELECT observed_generation, observed_at, observed_grant_generation FROM app_installation_observed_states WHERE installation_id=$1", target["installation_id"])
            if observed:
                await conn.execute("UPDATE app_installation_observed_states SET observed_generation=GREATEST(observed_generation,$2), observed_at=GREATEST(observed_at,NOW()), observed_release_id=$3, observed_release_version=(SELECT version FROM app_releases WHERE id=$3), schema_fingerprint=$5, observed_grant_generation=$4, checkpoint='{}'::jsonb, recent_error=NULL WHERE installation_id=$1", target["installation_id"], observed["observed_generation"] + 1, target["release_id"], observed["observed_grant_generation"], actual if expected is not None else None)
            remaining = await conn.fetchval("SELECT COUNT(*) FROM app_rollout_targets WHERE job_id=$1 AND state NOT IN ('applied','replayed')", target["job_id"])
            await conn.execute("UPDATE app_rollout_jobs SET status=$2, completed_at=CASE WHEN $2='applied' THEN NOW() ELSE completed_at END WHERE id=$1", target["job_id"], "applied" if not remaining else "running")
            await conn.execute(
                "INSERT INTO app_rollout_audit(job_id,app_id,installation_id,action,outcome,reason_code) VALUES($1,$2,$3,'execute','applied','converged')",
                target["job_id"], target["app_id"], target["installation_id"],
            )


async def _process_once() -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            target = await _claim_or_reclaim(conn)
            if target:
                await conn.execute("UPDATE app_rollout_jobs SET status='running' WHERE id=$1 AND status='pending'", target["job_id"])
    if not target:
        return 0
    await _process_target(target)
    return 1


_worker_id = f"rollout-{uuid.uuid4()}"
_runner = BackfillRunner(
    "app_rollout_worker",
    _process_once,
    concurrency=max(1, int(getattr(settings, "app_rollout_worker_concurrency", 1))),
)
start = _runner.start
stop = _runner.stop
run_once = _process_once
