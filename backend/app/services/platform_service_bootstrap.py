"""Trusted, fixed-purpose Native bootstrap. Never an HTTP token-issuer policy.

The caller already holds this installation's database authority. Input secrets
stay in a protected file; only hashes and immutable ownership enter the ledger.
The ledger deliberately has no user/token foreign keys: deletion is a tombstone,
not permission to bootstrap another administrator.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid
from urllib.parse import urlsplit
from typing import NoReturn

import asyncpg

from app.config import settings
from app.db.postgres import get_pool
from app.repositories.events_repo import emit_event
from app.services.role_authority import role_authority_transaction
from app.services.role_sync import RoleSync
from app.sso.runtime_identity import source_tuple

PURPOSE = "platform-service-v1"
CAPABILITY = "auth.platform-service-bootstrap.v1"
SCOPES = ["read", "write", "admin"]
SENTINEL = "!service-account:no-local-login!"
FIELDS = {"schema_version", "purpose", "operation_id", "org_id", "tenant_slug",
          "namespace", "owner_uid", "token_id", "credential"}


class BootstrapRefused(Exception):
    """Value-free diagnostic safe for CLI stderr."""


def _refuse(code: str) -> NoReturn:
    raise BootstrapRefused(code)


def _canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class Intent:
    operation_id: uuid.UUID
    org_id: uuid.UUID
    tenant_slug: str
    namespace: str
    owner_uid: uuid.UUID
    token_id: uuid.UUID
    credential: str = field(repr=False)
    intent_sha256: str

    @property
    def credential_sha256(self) -> str:
        return _digest(self.credential)


def parse_intent(value: object) -> Intent:
    if not isinstance(value, dict) or value.keys() != FIELDS:
        _refuse("platform_bootstrap_invalid_intent")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1 or value["purpose"] != PURPOSE:
        _refuse("platform_bootstrap_invalid_intent")
    slug = value["tenant_slug"]
    if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,57}[a-z0-9])?", slug):
        _refuse("platform_bootstrap_invalid_intent")
    if value["namespace"] != "akb-" + slug:
        _refuse("platform_bootstrap_invalid_intent")
    ids = {}
    for key in ("operation_id", "org_id", "owner_uid", "token_id"):
        try:
            raw = value[key]
            parsed = uuid.UUID(raw)
            if str(parsed) != raw or parsed.int == 0:
                raise ValueError
            ids[key] = parsed
        except ValueError, AttributeError, TypeError:
            _refuse("platform_bootstrap_invalid_intent")
    credential = value["credential"]
    if not isinstance(credential, str) or not re.fullmatch(r"akb_secret_[A-Za-z0-9_-]{43}", credential):
        _refuse("platform_bootstrap_invalid_credential")
    decoded = base64.urlsafe_b64decode(credential[11:] + "=")
    if len(decoded) != 32 or base64.urlsafe_b64encode(decoded).decode().rstrip("=") != credential[11:]:
        _refuse("platform_bootstrap_invalid_credential")
    return Intent(**ids, tenant_slug=slug, namespace=value["namespace"], credential=credential,
                  intent_sha256=_digest(_canonical(value)))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _refuse("platform_bootstrap_invalid_intent")
        result[key] = value
    return result


def read_intent(path: str) -> Intent:
    """Permit atomic Secret projections only beneath the given mount directory."""
    try:
        supplied = Path(path).absolute()
        parent = supplied.parent.resolve(strict=True)
        resolved = supplied.resolve(strict=True)
        if not resolved.is_relative_to(parent):
            _refuse("platform_bootstrap_input_file")
        fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) not in (0o400, 0o600)
                    or before.st_uid not in (0, os.geteuid()) or before.st_size > 16384):
                _refuse("platform_bootstrap_input_file")
            raw = stream.read(16385)
            after = os.fstat(stream.fileno())
            current = resolved.stat()
            def stable(s):
                return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            if len(raw) > 16384 or stable(before) != stable(after) or stable(before) != stable(current):
                _refuse("platform_bootstrap_input_file")
        return parse_intent(json.loads(raw, object_pairs_hook=_unique_object))
    except OSError, ValueError, UnicodeError, TypeError:
        _refuse("platform_bootstrap_input_file")


def runtime_identity() -> dict:
    try:
        runtime = source_tuple()
        if (runtime["auth_mode"] != "sso" or not settings.keycloak_enabled
                or not settings.keycloak_service_admin_client_id_effective
                or not isinstance(settings.sso_session_epoch, uuid.UUID) or settings.sso_session_epoch.int == 0
                or type(runtime["runtime_generation"]) is not int or runtime["runtime_generation"] <= 0
                or not runtime["issuer"].startswith("https://")):
            _refuse("platform_bootstrap_runtime_conflict")
    except ValueError:
        _refuse("platform_bootstrap_runtime_conflict")
    parsed = urlsplit(runtime["issuer"])
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        _refuse("platform_bootstrap_runtime_conflict")
    return runtime


async def provision(intent: Intent, *, replay_only: bool = False) -> dict:
    runtime = runtime_identity()
    pool = await get_pool()
    role_sync = RoleSync(pool)
    try:
        async with pool.acquire() as conn:
            async with role_authority_transaction(conn):
                # Role-authority lock first, then runtime/ledger/user/token rows.
                active = await conn.fetchval("""SELECT TRUE FROM auth_runtime_state WHERE singleton
                    AND runtime_generation=$1 AND auth_mode=$2 AND sso_session_epoch=$3::uuid FOR SHARE""",
                    runtime["runtime_generation"], runtime["auth_mode"], runtime["sso_session_epoch"])
                if active is not True or source_tuple() != runtime:
                    _refuse("platform_bootstrap_runtime_conflict")
                row = await conn.fetchrow("SELECT * FROM platform_service_bootstrap WHERE purpose=$1 FOR UPDATE", PURPOSE)
                if row is None:
                    if replay_only:
                        _refuse("platform_bootstrap_not_prepared")
                    if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM users WHERE username=$1 OR email=$2)",
                                           "platform-bot", f"platform-bot@{intent.tenant_slug}.local"):
                        _refuse("platform_bootstrap_account_exists")
                    user_id = uuid.uuid4()
                    await conn.execute("""INSERT INTO users(id,username,email,password_hash,display_name,
                        is_admin,auth_provider,account_status,account_kind)
                        VALUES($1,'platform-bot',$2,$3,'Platform service',true,'service','active','service')""",
                                       user_id, f"platform-bot@{intent.tenant_slug}.local", SENTINEL)
                    await conn.execute("""INSERT INTO tokens(id,user_id,name,token_hash,token_prefix,scopes,key_class)
                        VALUES($1,$2,'platform',$3,$4,$5,'service')""", intent.token_id, user_id,
                                       intent.credential_sha256, intent.credential[:12], SCOPES)
                    await conn.execute("""INSERT INTO platform_service_bootstrap(purpose,operation_id,org_id,tenant_slug,
                        namespace,owner_uid,user_id,token_id,intent_sha256,credential_sha256,runtime_identity,service_authority_client_id)
                        VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,$12)""", PURPOSE, intent.operation_id,
                                       intent.org_id, intent.tenant_slug, intent.namespace, intent.owner_uid, user_id,
                                       intent.token_id, intent.intent_sha256, intent.credential_sha256, _canonical(runtime),
                                       settings.keycloak_service_admin_client_id_effective)
                    await role_sync.on_user_create_in_conn(conn, user_id)
                    await emit_event(conn, "auth.platform_service_bootstrapped", payload={
                        "purpose": PURPOSE, "operation_id": str(intent.operation_id), "user_id": str(user_id),
                        "token_id": str(intent.token_id), "org_id": str(intent.org_id), "owner_uid": str(intent.owner_uid)})
                    status = "issued"
                else:
                    if (row["intent_sha256"] != intent.intent_sha256 or row["credential_sha256"] != intent.credential_sha256
                            or json.loads(row["runtime_identity"]) != runtime
                            or row["service_authority_client_id"] != settings.keycloak_service_admin_client_id_effective):
                        _refuse("platform_bootstrap_intent_conflict")
                    user_id = row["user_id"]
                    user = await conn.fetchrow("""SELECT u.*,EXISTS(SELECT 1 FROM external_identities e WHERE e.user_id=u.id)
                        AS has_external,EXISTS(SELECT 1 FROM service_identities s WHERE s.user_id=u.id) AS has_service_identity
                        FROM users u WHERE id=$1 FOR UPDATE""", user_id)
                    if (user is None or user["username"] != "platform-bot" or user["email"] != f"platform-bot@{intent.tenant_slug}.local"
                            or not user["is_admin"] or user["is_recovery_admin"] or user["auth_provider"] != "service"
                            or user["account_status"] != "active" or user["account_kind"] != "service" or user["has_external"]
                            or user["has_service_identity"] or user["password_hash"] != SENTINEL or user["credential_change_required"]):
                        _refuse("platform_bootstrap_account_changed")
                    token = await conn.fetchrow("SELECT *,expires_at IS NULL OR expires_at>clock_timestamp() AS live FROM tokens WHERE id=$1 FOR UPDATE", intent.token_id)
                    if (token is None or token["user_id"] != user_id or token["name"] != "platform"
                            or token["token_hash"] != intent.credential_sha256 or token["token_prefix"] != intent.credential[:12]
                            or token["key_class"] != "service" or token["scopes"] != SCOPES or token["vault_scope"] is not None
                            or token["expires_at"] is not None or not token["live"] or token["created_at"] < user["tokens_revoked_before"]):
                        _refuse("platform_bootstrap_credential_retired")
                    if not await role_sync.user_role_projected_in_conn(conn, user_id):
                        _refuse("platform_bootstrap_role_projection_changed")
                    status = "replayed"
    except asyncpg.UniqueViolationError:
        _refuse("platform_bootstrap_identity_conflict")
    return {"schema_version": 1, "purpose": PURPOSE, "operation_id": str(intent.operation_id),
            "org_id": str(intent.org_id), "tenant_slug": intent.tenant_slug, "namespace": intent.namespace,
            "owner_uid": str(intent.owner_uid), "token_id": str(intent.token_id), "user_id": str(user_id),
            "credential_sha256": intent.credential_sha256, "intent_sha256": intent.intent_sha256,
            "username": "platform-bot", "email": f"platform-bot@{intent.tenant_slug}.local", "account_kind": "service",
            "auth_provider": "service", "is_admin": True, "is_active": True,
            "key_class": "service", "scopes": list(SCOPES), "vault_scope": None,
            "runtime_identity": runtime, "status": status}


def write_receipt(path: str, report: dict) -> None:
    """Never follow an output symlink; support the regular termination-log file."""
    raw = (_canonical(report) + "\n").encode()
    if len(raw) > 4096:
        _refuse("platform_bootstrap_receipt_size")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        with os.fdopen(fd, "wb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.geteuid()):
                _refuse("platform_bootstrap_receipt_file")
            os.fchmod(stream.fileno(), 0o600)
            stream.truncate()
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        _refuse("platform_bootstrap_receipt_file")
