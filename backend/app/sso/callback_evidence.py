"""Operation-bound evidence from the actual verified browser callback only."""

from datetime import timedelta
import base64
import hmac
import hashlib
import uuid
from urllib.parse import urlencode

from app.config import settings
from app.sso import identity_operations as ops
from app.sso.runtime_identity import source_tuple


def _cohort_user(row, user_id):
    matches = [item for item in ops._object(row["cohort"]) if item["existing_user_id"] == str(user_id)]
    if len(matches) != 1:
        raise ops._error("sso_operation_scope_forbidden", 403)
    return matches[0]


async def issue_challenge(operation_id, credential, user_id, request_id=None):
    pool = await ops.get_pool()
    async with pool.acquire() as conn, conn.transaction():
        row = await ops.authorize_read(conn, operation_id, credential)
        if "login-evidence" not in row["verbs"]:
            raise ops._error("sso_operation_scope_forbidden", 403)
        _cohort_user(row, user_id)
        # A challenge can be prepared in the old runtime but can only start a
        # browser ceremony after the exact installed target runtime is live.
        request_id = request_id or uuid.uuid4()
        challenge_id = uuid.uuid5(operation_id, str(request_id))
        raw = base64.urlsafe_b64encode(hmac.digest(credential.encode(),
            f"sso-login-challenge:{operation_id}:{request_id}:{user_id}".encode(), "sha256")).decode().rstrip("=")
        now = await conn.fetchval("SELECT clock_timestamp()")
        expires = min(row["expires_at"], now + timedelta(minutes=5))
        previous = await conn.fetchrow("SELECT * FROM sso_operation_login_challenges WHERE challenge_id=$1", challenge_id)
        if previous:
            if previous["operation_id"] != operation_id or previous["akb_user_id"] != user_id:
                raise ops._error("sso_login_challenge_request_conflict")
            expires = previous["expires_at"]
        await conn.execute("""INSERT INTO sso_operation_login_challenges
            (challenge_id,operation_id,akb_user_id,credential_hash,expires_at) VALUES($1,$2,$3,$4,$5) ON CONFLICT(challenge_id) DO NOTHING""",
            challenge_id, operation_id, user_id, hashlib.sha256(raw.encode()).hexdigest(), expires)
        return {"schema_version": 1, "operation_id": str(operation_id), "challenge_id": str(challenge_id),
                "akb_user_id": str(user_id), "expires_at": expires.isoformat(),
                "login_url": f"{settings.public_base_url.rstrip('/')}/api/v1/auth/sso/{row['provider_alias']}/login?{urlencode({'login_challenge': raw})}"}


async def lock_callback_authority(conn, challenge_id):
    initial = await conn.fetchrow("SELECT * FROM sso_operation_login_challenges WHERE challenge_id=$1", challenge_id)
    if initial is None:
        raise ops._error("sso_login_challenge_invalid", 401)
    row = await ops._lock_operation(conn, initial["operation_id"])
    challenge = await conn.fetchrow("SELECT * FROM sso_operation_login_challenges WHERE challenge_id=$1 FOR UPDATE", challenge_id)
    return row, challenge


async def browser_challenge(raw, alias):
    if not isinstance(raw, str) or not 32 <= len(raw) <= 128:
        raise ops._error("sso_login_challenge_invalid", 401)
    pool = await ops.get_pool()
    async with pool.acquire() as conn, conn.transaction():
        challenge = await conn.fetchrow("SELECT * FROM sso_operation_login_challenges WHERE credential_hash=$1",
                                       hashlib.sha256(raw.encode()).hexdigest())
        if challenge is None or challenge["consumed_at"] or challenge["expires_at"] <= await conn.fetchval("SELECT clock_timestamp()"):
            raise ops._error("sso_login_challenge_invalid", 401)
        row, challenge = await lock_callback_authority(conn, challenge["challenge_id"])
        if challenge["consumed_at"] or challenge["expires_at"] <= await conn.fetchval("SELECT clock_timestamp()"):
            raise ops._error("sso_login_challenge_invalid", 401)
        await _callback_context(conn, row, challenge["akb_user_id"], alias)
        return str(challenge["challenge_id"])


async def _callback_context(conn, row, user_id, alias):
    if row["retired_at"] is not None or row["expires_at"] <= await conn.fetchval("SELECT clock_timestamp()"):
        raise ops._error("sso_operation_retired", 403)
    await ops._live_actor(conn, row)
    target = await ops._bound_target(conn, row)
    await ops._read_boundary(conn, row)
    if source_tuple() != ops._object(row["target_tuple"]) or alias != row["provider_alias"]:
        raise ops._error("sso_login_challenge_runtime_conflict")
    item = _cohort_user(row, user_id)
    binding = await conn.fetchval("""SELECT TRUE FROM external_identities e JOIN users u ON u.id=e.user_id
        WHERE e.user_id=$1 AND e.issuer=$2 AND e.subject=$3 AND u.account_kind='human'
        AND u.account_status='active' FOR SHARE OF e,u""", user_id, target.issuer, item["broker_subject"])
    if binding is not True:
        raise ops._error("sso_login_challenge_binding_unavailable", 403)
    return target, item


async def record_callback(conn, challenge_id, *, user_id, issuer, subject, provider_alias):
    """Internal session-transaction hook; never mounted as an HTTP writer."""
    row, challenge = await lock_callback_authority(conn, challenge_id)
    if (challenge is None or challenge["akb_user_id"] != user_id or challenge["consumed_at"] is not None
        or challenge["expires_at"] <= await conn.fetchval("SELECT clock_timestamp()")):
        raise ops._error("sso_login_challenge_invalid", 401)
    target, item = await _callback_context(conn, row, user_id, provider_alias)
    if (issuer, subject) != (target.issuer, item["broker_subject"]):
        raise ops._error("sso_login_challenge_binding_unavailable", 403)
    provider = await ops._provider(provider_alias, target=target)
    owner = await conn.fetchrow("SELECT * FROM sso_provider_ownership WHERE target_id=$1 AND alias=$2 FOR SHARE", target.target_id, provider_alias)
    revision, secret_revision = None, None
    if (owner is not None and not owner["emergency_disabled"] and owner["owner_id"] == row["owner_id"]
        and owner["revision"] == row["current_provider_revision"]
        and owner["fingerprint"] == row["current_provider_fingerprint"] == ops.provider_fingerprint(provider)):
        revision, secret_revision = owner["revision"], owner["secret_revision"]
    if provider.state != "enabled":
        raise ops._error("sso_login_challenge_provider_unavailable")
    runtime = source_tuple()
    await ops._live_actor(conn, row)
    now = await conn.fetchval("SELECT clock_timestamp()")
    if challenge["expires_at"] <= now or row["expires_at"] <= now:
        raise ops._error("sso_login_challenge_invalid", 401)
    await conn.execute("""INSERT INTO sso_callback_login_events(event_id,operation_id,challenge_id,installation_id,target_id,
        akb_user_id,issuer,provider_alias,subject,runtime_generation,sso_session_epoch,source_revision,cohort_sha256,
        provider_revision,provider_secret_revision) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)""",
        uuid.uuid4(), row["operation_id"], challenge_id, row["installation_id"], target.target_id, user_id,
        issuer, provider_alias, subject, runtime["runtime_generation"], uuid.UUID(runtime["sso_session_epoch"]),
        runtime["source_revision"], row["cohort_sha256"], revision, secret_revision)
    await conn.execute("UPDATE sso_operation_login_challenges SET consumed_at=clock_timestamp() WHERE challenge_id=$1", challenge_id)


async def events(operation_id, challenge_id, user_id, *, credential=None, actor=None):
    pool = await ops.get_pool()
    async with pool.acquire() as conn, conn.transaction():
        if credential is not None:
            row = await ops.authorize_read(conn, operation_id, credential)
        else:
            row = await conn.fetchrow("SELECT * FROM sso_identity_operations WHERE operation_id=$1 FOR SHARE", operation_id)
            if (row is None or actor is None or str(actor.user_id) != str(row["actor_id"])
                or str(actor.token_id) != str(row["actor_token_id"]) or actor.account_kind != "service"):
                raise ops._error("sso_operation_scope_forbidden", 403)
            await ops._live_actor(conn, row)
            await ops._bound_target(conn, row)
            await ops._read_boundary(conn, row)
        _cohort_user(row, user_id)
        rows = await conn.fetch("""SELECT * FROM sso_callback_login_events WHERE operation_id=$1 AND challenge_id=$2
            AND akb_user_id=$3 ORDER BY occurred_at,event_id""", operation_id, challenge_id, user_id)
        return {"schema_version": 1, "operation_id": str(operation_id), "challenge_id": str(challenge_id),
                "events": [dict(item) for item in rows]}


async def verify_callback(challenge_id, *, issuer, subject, provider_alias):
    pool = await ops.get_pool()
    async with pool.acquire() as conn, conn.transaction():
        row, challenge = await lock_callback_authority(conn, challenge_id)
        if challenge is None or challenge["consumed_at"] or challenge["expires_at"] <= await conn.fetchval("SELECT clock_timestamp()"):
            raise ops._error("sso_login_challenge_invalid", 401)
        target, item = await _callback_context(conn, row, challenge["akb_user_id"], provider_alias)
        if (issuer, subject) != (target.issuer, item["broker_subject"]):
            raise ops._error("sso_login_challenge_binding_unavailable", 403)
