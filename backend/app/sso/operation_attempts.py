"""Explicit human reauthorization of an immutable operation intent."""

from datetime import timedelta
import hashlib
import secrets

from app.sso import identity_operations as ops
from app.sso.runtime_identity import source_tuple, lock_source


INTENT_FIELDS = (
    "operation_id", "installation_id", "actor_id", "actor_token_id", "provider_alias", "owner_id",
    "provider_revision", "provider_fingerprint", "provider_payload_sha256", "target_id", "target_fingerprint",
    "target_realm_id", "source_tuple", "target_tuple", "cohort", "cohort_sha256", "verbs", "provider_secret_revision",
)
JSON_FIELDS = {"provider_payload_sha256", "source_tuple", "target_tuple", "cohort"}


def intent_hash(row):
    values = {key: ops._object(row[key]) if key in JSON_FIELDS else str(row[key])
              if key in {"operation_id", "installation_id", "actor_id", "actor_token_id", "target_id"} else row[key]
              for key in INTENT_FIELDS}
    return ops._digest(values)


async def reauthorize(parent_id, *, operation_id, expected_intent_sha256, ttl_seconds, granted_by):
    if not 1 <= ttl_seconds <= 600 or operation_id == parent_id:
        raise ops._error("sso_operation_scope_invalid", 422)
    pool = await ops.get_pool()
    async with pool.acquire() as conn, conn.transaction():
        parent = await conn.fetchrow("SELECT * FROM sso_identity_operations WHERE operation_id=$1 FOR UPDATE", parent_id)
        if parent is None or parent["parent_operation_id"] is not None:
            raise ops._error("sso_operation_not_found", 404)
        if parent["lineage_closed_at"] is not None or parent["source_tuple"] is None or parent["target_tuple"] is None:
            raise ops._error("sso_operation_lineage_closed", 403)
        intent = intent_hash(parent)
        if not secrets.compare_digest(intent, expected_intent_sha256):
            raise ops._error("sso_operation_intent_conflict")
        target = await ops._bound_target(conn, parent)
        source, future = ops._object(parent["source_tuple"]), ops._object(parent["target_tuple"])
        current = source_tuple()
        if current == source:
            verbs, scope = parent["verbs"], "source-recovery"
        elif current == future and future["runtime_generation"] > source["runtime_generation"]:
            verbs, scope = ["login-evidence"], "target-evidence"
        else:
            raise ops._error("sso_operation_runtime_conflict")
        await lock_source(conn, current)
        await ops._alias_lock(conn, parent["provider_alias"], target)
        owner = await conn.fetchrow("SELECT * FROM sso_provider_ownership WHERE target_id=$1 AND alias=$2 FOR SHARE",
                                    target.target_id, parent["provider_alias"])
        if (owner is None or owner["owner_id"] != parent["owner_id"] or owner["revision"] != parent["current_provider_revision"]
            or owner["fingerprint"] != parent["current_provider_fingerprint"]):
            raise ops._error("sso_provider_revision_conflict")
        if owner["emergency_disabled"]:
            raise ops._error("sso_provider_emergency_disabled")
        provider = await ops._provider(parent["provider_alias"], target=target, allow_missing=not bool(ops._object(parent["cohort"])))
        if ops.provider_fingerprint(provider, alias=parent["provider_alias"]) != owner["fingerprint"]:
            raise ops._error("sso_provider_fingerprint_conflict")
        await ops._live_actor(conn, {**dict(parent), "granted_by": granted_by})
        if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM sso_identity_operations WHERE operation_id=$1)", operation_id):
            raise ops._error("sso_operation_id_conflict")
        attempt = await conn.fetchval("SELECT COALESCE(MAX(authority_attempt),1)+1 FROM sso_identity_operations WHERE operation_id=$1 OR parent_operation_id=$1", parent_id)
        raw = ops.PREFIX + secrets.token_urlsafe(32)
        now = await conn.fetchval("SELECT clock_timestamp()")
        expires = now + timedelta(seconds=ttl_seconds)
        # Locking the root before every attempt effect serializes supersession
        # with writes. Old credentials are retired in the same commit.
        await conn.execute("""UPDATE sso_identity_operations SET retired_at=COALESCE(retired_at,$2),phase='retired'
            WHERE operation_id=$1 OR parent_operation_id=$1""", parent_id, now)
        await conn.execute("UPDATE sso_identity_operations SET intent_sha256=$2 WHERE operation_id=$1", parent_id, intent)
        await conn.execute("""INSERT INTO sso_identity_operations
            (operation_id,installation_id,credential_hash,granted_by,actor_id,actor_token_id,provider_alias,owner_id,
             provider_revision,provider_fingerprint,current_provider_revision,current_provider_fingerprint,
             provider_payload_sha256,runtime_generation,session_epoch,cohort,cohort_sha256,verbs,expires_at,created_at,
             target_id,target_fingerprint,target_realm_id,source_tuple,target_tuple,provider_secret_revision,
             parent_operation_id,intent_sha256,authority_attempt,phase,existing_identity_ids_hash,prepared_identity_ids_hash)
            SELECT $2,installation_id,$3,$4,actor_id,actor_token_id,provider_alias,owner_id,
             provider_revision,provider_fingerprint,current_provider_revision,current_provider_fingerprint,
             provider_payload_sha256,runtime_generation,session_epoch,cohort,cohort_sha256,$5, $6,$7,
             target_id,target_fingerprint,target_realm_id,source_tuple,target_tuple,provider_secret_revision,
             $1,$8,$9,CASE WHEN prepared_identity_ids_hash IS NULL THEN 'granted' ELSE 'prepared' END,
             existing_identity_ids_hash,prepared_identity_ids_hash
             FROM sso_identity_operations WHERE operation_id=$1""",
            parent_id, operation_id, hashlib.sha256(raw.encode()).hexdigest(), granted_by, verbs, expires, now, intent, attempt)
        from app.repositories.events_repo import emit_event
        await emit_event(conn, "auth.sso_operation_reauthorized", actor_id=str(granted_by),
                         payload={"operation_id": str(operation_id), "parent_operation_id": str(parent_id),
                                  "intent_sha256": intent, "authority_attempt": attempt, "authority_scope": scope})
        return {"schema_version": 1, "operation_id": str(operation_id), "parent_operation_id": str(parent_id),
                "credential": raw, "expires_at": expires.isoformat(), "cohort_sha256": parent["cohort_sha256"],
                "provider_revision": parent["current_provider_revision"], "intent_sha256": intent,
                "authority_attempt": attempt, "authority_scope": scope}
