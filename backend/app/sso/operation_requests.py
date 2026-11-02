"""Operation requests: a service key proposes, a human approves the exact digest.

An administrator service key proposes an ownership claim or a provider grant.
A human administrator approves or denies the exact request, identified by the
SHA-256 of its canonical document, so a stale screen cannot approve anything it
did not show. Approving a claim runs the existing ownership compare-and-set
with the human as the actor. Approving a grant records the approval only: the
requesting token later collects the operation credential once, sealed to the
X25519 key it generated for this request, and at that moment every existing
grant check runs again against the current state, including that the approving
human is still an active administrator. No credential reaches a human, a
response to a human, the request row, an event or a log.

Requests expire on their own. There is no background worker: every entry point
first closes, in its own transaction, the requests whose window has passed and
records one ``expired`` event for each.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import secrets
from typing import Any
import uuid

from app.db.postgres import get_pool
from app.repositories.events_repo import emit_event
from app.sso import identity_operations as ops, management_targets, sealing
from app.sso.runtime_identity import source_tuple

PENDING_SECONDS = 3600
LIST_LIMIT = 100
PROVIDER_VERBS = ("configure", "enable", "disable")
_SELECT = """SELECT r.*, requester.username AS requester_username, decider.username AS decider_username
    FROM sso_operation_requests r
    LEFT JOIN users requester ON requester.id = r.requested_by
    LEFT JOIN users decider ON decider.id = r.decided_by"""


def _error(code: str, status: int = 409):
    return ops._error(code, status)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def view(row) -> dict[str, Any]:
    """The secret-free representation shared by humans and the requester."""
    return {"schema_version": 1, "request_id": str(row["request_id"]), "request_sha256": row["request_sha256"],
            "request": ops._object(row["request"]), "status": row["status"],
            "created_at": _iso(row["created_at"]), "expires_at": _iso(row["expires_at"]),
            "requester_username": row["requester_username"],
            "decided_by": str(row["decided_by"]) if row["decided_by"] else None,
            "decider_username": row["decider_username"], "decided_at": _iso(row["decided_at"]),
            "operation_expires_at": _iso(row["operation_expires_at"]), "closed_at": _iso(row["closed_at"])}


async def _expire_due(pool) -> None:
    async with pool.acquire() as conn, conn.transaction():
        expired = await conn.fetch("""UPDATE sso_operation_requests SET status='expired', closed_at=clock_timestamp()
            WHERE (status='pending' AND expires_at<=clock_timestamp())
               OR (status='approved' AND kind='provider_grant' AND operation_expires_at<=clock_timestamp())
            RETURNING request_id, kind, provider_alias, request_sha256, decided_by""")
        for row in expired:
            await emit_event(conn, "auth.sso_operation_request_expired", actor_id=None,
                             payload={"request_id": str(row["request_id"]), "kind": row["kind"],
                                      "provider_alias": row["provider_alias"], "request_sha256": row["request_sha256"],
                                      "approved": row["decided_by"] is not None})


async def _live_requester(conn, user_id, token_id) -> None:
    # The same conjunction the issued operation's actor must satisfy.
    live = await conn.fetchval("""SELECT TRUE FROM tokens t JOIN users u ON u.id=t.user_id
        WHERE t.id=$1 AND t.user_id=$2 AND t.key_class='service'
        AND t.scopes @> ARRAY['read','write','admin']::text[]
        AND (t.expires_at IS NULL OR t.expires_at>clock_timestamp())
        AND u.account_status='active' AND u.account_kind='service' AND u.is_admin
        FOR SHARE OF t,u""", token_id, user_id)
    if live is not True:
        raise _error("sso_operation_actor_retired", 403)


def _service_identity(actor) -> tuple[uuid.UUID, uuid.UUID]:
    if getattr(actor, "account_kind", "human") != "service" or getattr(actor, "token_id", None) is None:
        raise _error("sso_operation_request_service_key_required", 403)
    return uuid.UUID(str(actor.user_id)), uuid.UUID(str(actor.token_id))


def _is_requester(row, actor) -> bool:
    if getattr(actor, "account_kind", "human") != "service":
        return False
    return str(actor.user_id) == str(row["requested_by"]) and str(actor.token_id) == str(row["requested_token_id"])


def _text(value: str, maximum: int) -> str:
    if (not isinstance(value, str) or not value or len(value) > maximum
            or any(ord(character) < 0x20 or ord(character) == 0x7f for character in value)):
        raise _error("sso_operation_request_invalid", 422)
    return value


def _grant_terms(spec: dict[str, Any]) -> dict[str, Any]:
    verbs = list(spec["verbs"])
    hashes = dict(spec["provider_payload_sha256"])
    if (not verbs or len(set(verbs)) != len(verbs) or not set(verbs) <= set(PROVIDER_VERBS)
            or set(hashes) != set(verbs)
            or any(not isinstance(value, str) or len(value) != 64
                   or any(character not in "0123456789abcdef" for character in value) for value in hashes.values())
            or not isinstance(spec["ttl_seconds"], int) or not 1 <= spec["ttl_seconds"] <= 600):
        raise _error("sso_operation_scope_invalid", 422)
    summary = spec.get("configure_summary")
    # A configure is approved from its secret-free summary; no summary, no configure.
    if ("configure" in verbs) != (summary is not None):
        raise _error("sso_operation_scope_invalid", 422)
    if summary is not None:
        limits = {"provider_type": 80, "display_name": 80, "issuer": 2048, "discovery_url": 2048, "client_id": 255}
        summary = {**{field: _text(summary[field], limit) for field, limit in limits.items()},
                   "client_secret_supplied": summary["client_secret_supplied"]}
        if not isinstance(summary["client_secret_supplied"], bool):
            raise _error("sso_operation_request_invalid", 422)
    label = ops._secret_revision_label(spec.get("provider_secret_revision"))
    try:
        sealing.public_key(spec["recipient_public_key"])
    except sealing.SealingKeyError:
        raise _error("sso_operation_recipient_key_invalid", 422) from None
    return {"operation_id": str(spec["operation_id"]), "source": source_tuple(),
            "expected_provider_revision": spec["expected_provider_revision"], "verbs": verbs,
            "provider_payload_sha256": hashes, "configure_summary": summary,
            "ttl_seconds": spec["ttl_seconds"], "provider_secret_revision": label,
            "recipient_public_key": spec["recipient_public_key"]}


async def propose(spec: dict[str, Any], *, actor) -> dict[str, Any]:
    user_id, token_id = _service_identity(actor)
    kind = spec["kind"]
    alias = ops._canonical_alias(spec["provider_alias"])
    owner_id = ops.validate_owner_id(spec["owner_id"])
    if owner_id is None:
        # Releasing ownership is a human decision, never a request.
        raise _error("sso_provider_owner_invalid", 422)
    claim = grant = None
    if kind == "ownership_claim":
        claim = {"expected_revision": spec["expected_revision"], "expected_fingerprint": spec["expected_fingerprint"]}
    elif kind == "provider_grant":
        grant = _grant_terms(spec)
    else:
        raise _error("sso_operation_request_invalid", 422)
    pool = await get_pool()
    await _expire_due(pool)
    request_id = uuid.uuid4()
    async with pool.acquire() as conn, conn.transaction():
        await _live_requester(conn, user_id, token_id)
        target = await management_targets.resolve(conn)
        installation = await conn.fetchval("SELECT installation_id FROM sso_operation_installation WHERE singleton")
        if grant is not None:
            # An approved request must still be able to become this operation.
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                               f"sso-operation-id:{grant['operation_id']}")
            if await conn.fetchval("""SELECT EXISTS(SELECT 1 FROM sso_identity_operations WHERE operation_id=$1)
                OR EXISTS(SELECT 1 FROM sso_operation_requests WHERE operation_id=$1)""", uuid.UUID(grant["operation_id"])):
                raise _error("sso_operation_id_conflict")
        document = {"schema_version": 1, "request_id": str(request_id), "kind": kind,
                    "installation_id": str(installation), "target_id": str(target.target_id),
                    "provider_alias": alias, "owner_id": owner_id,
                    "requested_by": str(user_id), "requested_token_id": str(token_id),
                    "claim": claim, "grant": grant}
        digest = ops._digest(document)
        now = await conn.fetchval("SELECT clock_timestamp()")
        await conn.execute("""INSERT INTO sso_operation_requests
            (request_id,kind,request,request_sha256,target_id,provider_alias,requested_by,requested_token_id,
             operation_id,ttl_seconds,created_at,expires_at)
            VALUES($1,$2,$3::jsonb,$4,$5,$6,$7,$8,$9,$10,$11,$12)""",
            request_id, kind, ops._json(document), digest, target.target_id, alias, user_id, token_id,
            uuid.UUID(grant["operation_id"]) if grant else None, grant["ttl_seconds"] if grant else None,
            now, now + timedelta(seconds=PENDING_SECONDS))
        await emit_event(conn, "auth.sso_operation_request_proposed", actor_id=str(user_id),
                         payload={"request_id": str(request_id), "kind": kind, "provider_alias": alias,
                                  "owner_id": owner_id, "request_sha256": digest,
                                  "operation_id": grant["operation_id"] if grant else None,
                                  "verbs": grant["verbs"] if grant else None})
        return view(await conn.fetchrow(_SELECT + " WHERE r.request_id=$1", request_id))


async def list_requests(*, actor) -> dict[str, Any]:
    pool = await get_pool()
    await _expire_due(pool)
    # Open requests first, so a burst of closed ones cannot hide them.
    order = (" ORDER BY CASE r.status WHEN 'pending' THEN 0 WHEN 'approved' THEN 1 ELSE 2 END,"
             " r.created_at DESC, r.request_id LIMIT $1")
    if getattr(actor, "account_kind", "human") == "service":
        rows = await pool.fetch(_SELECT + " WHERE r.requested_token_id=$2 AND r.requested_by=$3" + order,
                                LIST_LIMIT, uuid.UUID(str(actor.token_id)), uuid.UUID(str(actor.user_id)))
    else:
        rows = await pool.fetch(_SELECT + order, LIST_LIMIT)
    return {"schema_version": 1, "requests": [view(row) for row in rows]}


async def read(request_id: uuid.UUID, *, actor) -> dict[str, Any]:
    pool = await get_pool()
    await _expire_due(pool)
    row = await pool.fetchrow(_SELECT + " WHERE r.request_id=$1", request_id)
    if row is None:
        raise _error("sso_operation_request_not_found", 404)
    if getattr(actor, "account_kind", "human") == "service" and not _is_requester(row, actor):
        raise _error("sso_operation_request_scope_forbidden", 403)
    return view(row)


async def _locked(conn, request_id: uuid.UUID):
    row = await conn.fetchrow("SELECT * FROM sso_operation_requests WHERE request_id=$1 FOR UPDATE", request_id)
    if row is None:
        raise _error("sso_operation_request_not_found", 404)
    return row


async def _decide(request_id: uuid.UUID, *, expected_request_sha256: str, human_id: uuid.UUID, approve: bool):
    pool = await get_pool()
    await _expire_due(pool)
    async with pool.acquire() as conn, conn.transaction():
        row = await _locked(conn, request_id)
        if not secrets.compare_digest(row["request_sha256"], expected_request_sha256):
            raise _error("sso_operation_request_digest_conflict")
        if row["status"] != "pending" or row["expires_at"] <= await conn.fetchval("SELECT clock_timestamp()"):
            raise _error("sso_operation_request_not_pending")
        document = ops._object(row["request"])
        if approve:
            # Never act on a proposal whose requesting key is no longer trusted.
            await _live_requester(conn, row["requested_by"], row["requested_token_id"])
        if approve and row["kind"] == "ownership_claim":
            claim = document["claim"]
            await ops.change_ownership_in(conn, document["provider_alias"], owner_id=document["owner_id"],
                                          expected_revision=claim["expected_revision"],
                                          expected_fingerprint=claim["expected_fingerprint"],
                                          clear_emergency=False, actor_id=human_id, target_id=row["target_id"])
        now = await conn.fetchval("SELECT clock_timestamp()")
        grant = approve and row["kind"] == "provider_grant"
        await conn.execute("""UPDATE sso_operation_requests SET status=$2,decided_by=$3,decided_at=$4::timestamptz,
            operation_expires_at=CASE WHEN $5::boolean THEN $4::timestamptz + ttl_seconds * INTERVAL '1 second' END,
            closed_at=CASE WHEN $5::boolean THEN NULL ELSE $4::timestamptz END WHERE request_id=$1""",
            request_id, "approved" if approve else "denied", human_id, now, grant)
        stored = await conn.fetchrow(_SELECT + " WHERE r.request_id=$1", request_id)
        await emit_event(conn, f"auth.sso_operation_request_{'approved' if approve else 'denied'}",
                         actor_id=str(human_id),
                         payload={"request_id": str(request_id), "kind": row["kind"],
                                  "provider_alias": row["provider_alias"], "request_sha256": row["request_sha256"],
                                  "operation_id": str(row["operation_id"]) if row["operation_id"] else None,
                                  "operation_expires_at": _iso(stored["operation_expires_at"])})
    # No provider read after the commit: a failed readback must not report a
    # committed approval as a failure. The caller reads ownership itself.
    return view(stored)


async def approve(request_id: uuid.UUID, *, expected_request_sha256: str, human_id: uuid.UUID) -> dict[str, Any]:
    return await _decide(request_id, expected_request_sha256=expected_request_sha256, human_id=human_id, approve=True)


async def deny(request_id: uuid.UUID, *, expected_request_sha256: str, human_id: uuid.UUID) -> dict[str, Any]:
    return await _decide(request_id, expected_request_sha256=expected_request_sha256, human_id=human_id, approve=False)


async def cancel(request_id: uuid.UUID, *, actor) -> dict[str, Any]:
    _service_identity(actor)
    pool = await get_pool()
    await _expire_due(pool)
    async with pool.acquire() as conn, conn.transaction():
        row = await _locked(conn, request_id)
        if not _is_requester(row, actor):
            raise _error("sso_operation_request_scope_forbidden", 403)
        if row["status"] not in {"pending", "approved"} or (row["status"] == "approved" and row["kind"] != "provider_grant"):
            raise _error("sso_operation_request_not_pending")
        await conn.execute("UPDATE sso_operation_requests SET status='cancelled',closed_at=clock_timestamp() WHERE request_id=$1",
                           request_id)
        await emit_event(conn, "auth.sso_operation_request_cancelled", actor_id=str(row["requested_by"]),
                         payload={"request_id": str(request_id), "kind": row["kind"],
                                  "provider_alias": row["provider_alias"], "request_sha256": row["request_sha256"]})
        return view(await conn.fetchrow(_SELECT + " WHERE r.request_id=$1", request_id))


async def collect(request_id: uuid.UUID, *, actor) -> dict[str, Any]:
    _service_identity(actor)
    pool = await get_pool()
    await _expire_due(pool)
    async with pool.acquire() as conn, conn.transaction():
        row = await _locked(conn, request_id)
        if not _is_requester(row, actor):
            raise _error("sso_operation_request_scope_forbidden", 403)
        if row["kind"] != "provider_grant":
            raise _error("sso_operation_request_kind_conflict")
        if row["status"] == "collected":
            raise _error("sso_operation_request_already_collected")
        if row["status"] == "expired":
            raise _error("sso_operation_request_expired", 403)
        if row["status"] != "approved":
            raise _error("sso_operation_request_not_approved")
        if row["operation_expires_at"] <= await conn.fetchval("SELECT clock_timestamp()"):
            raise _error("sso_operation_request_expired", 403)
        document = ops._object(row["request"])
        terms = document["grant"]
        spec = {"operation_id": uuid.UUID(terms["operation_id"]), "installation_id": uuid.UUID(document["installation_id"]),
                "actor_id": row["requested_by"], "actor_token_id": row["requested_token_id"],
                "provider_alias": document["provider_alias"], "owner_id": document["owner_id"],
                "expected_provider_revision": terms["expected_provider_revision"],
                "expected_source": terms["source"], "ttl_seconds": terms["ttl_seconds"],
                "verbs": terms["verbs"], "cohort": [], "provider_payload_sha256": terms["provider_payload_sha256"],
                "provider_secret_revision": terms["provider_secret_revision"]}
        issued = await ops.grant_in(conn, spec, granted_by=row["decided_by"], expires_at=row["operation_expires_at"],
                                    expected_target_id=row["target_id"], configure_summary=terms["configure_summary"])
        sealed = sealing.seal(issued.pop("credential"), terms["recipient_public_key"],
                              request_id=str(request_id), request_sha256=row["request_sha256"])
        await conn.execute("UPDATE sso_operation_requests SET status='collected',closed_at=clock_timestamp() WHERE request_id=$1",
                           request_id)
        await emit_event(conn, "auth.sso_operation_request_credential_issued", actor_id=str(row["requested_by"]),
                         payload={"request_id": str(request_id), "operation_id": terms["operation_id"],
                                  "provider_alias": row["provider_alias"], "request_sha256": row["request_sha256"],
                                  "approved_by": str(row["decided_by"]), "expires_at": issued["expires_at"],
                                  "recipient_public_key_sha256": hashlib.sha256(
                                      terms["recipient_public_key"].encode("ascii")).hexdigest()})
    return {"schema_version": 1, "request_id": str(request_id), "request_sha256": row["request_sha256"],
            "operation_id": terms["operation_id"], "expires_at": issued["expires_at"],
            "provider_revision": issued["provider_revision"], "sealed_credential": sealed,
            "seal": {"mode": "base", "suite": sealing.SUITE_NAME,
                     "info": sealing.info(str(request_id), row["request_sha256"]).decode("ascii")}}
