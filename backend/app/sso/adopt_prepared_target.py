"""Adopt HOST-prepared Keycloak after the explicit human-verifier switch.

The protected receipt attests historical master-authority retirement. Current
target realm/client/profile readback uses only the permanent six-role manager;
it neither observes nor acquires master authority.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import sys
import time
from typing import Any
from uuid import UUID
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from app.services.standalone_sso_bootstrap import (
    STANDALONE_SSO_RECEIPT_PROFILE,
    StandaloneSSOBootstrapError,
    StandaloneSSOBootstrapSpec,
    StandaloneSSOReadback,
    _expected_retirement_receipt,
    _validate_projection,
    _validate_readback,
)
from app.services.standalone_sso_keycloak import KeycloakStandaloneSSOControl
from app.sso.prepare_target_host import ExactCohortMember, SourceGuard, canonical, digest, fail, protected_read


class PreparedTargetReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: int = Field(ge=1, le=1)
    operation_id: UUID
    org_id: UUID
    target_id: UUID
    realm_id: UUID
    session_epoch: UUID
    realm_name: str
    issuer: str
    internal_url: str
    management_client_id: str
    api_client_id: str
    admin_client_id: str
    akb_public_url: str
    backchannel_logout_uri: str
    local_realm_self_registration: bool
    product_admin_username: str
    product_admin_email: str
    bootstrap_client_id: str
    config_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    active_credentials_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    input_binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    cohort: list[ExactCohortMember] = Field(max_length=25)
    cohort_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    profile: str
    readback: StandaloneSSOReadback
    prepared_at: datetime
    authority_retired_at: datetime
    retirement_evidence: dict[str, bool]
    provider: dict[str, object] | None = None
    source_guard: SourceGuard | None = None
    owned_assets: list[dict[str, object]] = Field(default_factory=list)
    preserved_broker_ids: list[str] = Field(default_factory=list)


def validate_runtime_binding(receipt: PreparedTargetReceipt, descriptor_bytes: bytes, settings: Any) -> StandaloneSSOBootstrapSpec:
    """Pure, fail-closed gate before any database initialization or writes."""
    if settings.require_auth_mode() != "sso" or not settings.keycloak_enabled or settings.keycloak_public_client:
        raise fail("prepared_target_verifier_not_active")
    if str(receipt.target_id) != getattr(settings, "sso_prepared_target_id", ""):
        raise fail("prepared_target_selector_mismatch")
    if getattr(settings, "sso_session_epoch", None) != receipt.session_epoch:
        raise fail("prepared_target_session_epoch_mismatch")
    if receipt.source_guard is not None and getattr(settings, "auth_runtime_generation", 0) != receipt.source_guard.runtime_generation + 1:
        raise fail("prepared_target_runtime_generation_mismatch")
    if digest(descriptor_bytes) != receipt.config_sha256:
        raise fail("prepared_target_descriptor_mismatch")
    installed = json.loads(descriptor_bytes)
    if set(installed) != {"schema_version", "targets"} or installed["schema_version"] != 1:
        raise fail("prepared_target_descriptor_mismatch")
    targets = installed["targets"]
    if not isinstance(targets, list):
        raise fail("prepared_target_descriptor_mismatch")
    matches = [entry for entry in targets if entry.get("target_id") == str(receipt.target_id)]
    if len(matches) != 1:
        raise fail("prepared_target_descriptor_mismatch")
    entry = matches[0]
    expected_descriptor = {
        "target_id": str(receipt.target_id), "realm_id": str(receipt.realm_id), "realm_name": receipt.realm_name,
        "issuer": receipt.issuer, "internal_url": receipt.internal_url,
        "management_client_id": receipt.management_client_id,
        "management_client_secret": settings.keycloak_management_client_secret,
        "session_epoch": str(receipt.session_epoch), "verify_ssl": settings.keycloak_verify_ssl}
    if entry != expected_descriptor:
        raise fail("prepared_target_descriptor_mismatch")
    expected_settings = {
        "keycloak_server_url": receipt.issuer.rsplit("/realms/", 1)[0],
        "keycloak_realm": receipt.realm_name, "keycloak_client_id": receipt.api_client_id,
        "keycloak_admin_client_id": receipt.admin_client_id, "keycloak_management_client_id": receipt.management_client_id,
        "public_base_url": receipt.akb_public_url, "sso_local_realm_self_registration": receipt.local_realm_self_registration,
        "keycloak_backchannel_logout_uri_effective": receipt.backchannel_logout_uri}
    if any(getattr(settings, key) != value for key, value in expected_settings.items()):
        raise fail("prepared_target_runtime_mismatch")
    if (settings.keycloak_internal_url or settings.keycloak_server_url) != receipt.internal_url:
        raise fail("prepared_target_runtime_mismatch")
    credentials = {"api_client_secret": settings.keycloak_client_secret,
                   "admin_client_secret": settings.keycloak_admin_client_secret,
                   "management_client_secret": settings.keycloak_management_client_secret,
                   "browser_session_key": settings.sso_browser_session_encryption_key}
    if digest(canonical(credentials)) != receipt.active_credentials_sha256:
        raise fail("prepared_target_active_credentials_mismatch")
    if (receipt.profile != STANDALONE_SSO_RECEIPT_PROFILE
            or receipt.product_admin_username != f"akb-recovery-{receipt.target_id.hex}"
            or receipt.readback.realm_id != str(receipt.realm_id)
            or receipt.retirement_evidence != {"new_token_denied": True, "prior_token_master_call_denied": True}
            or receipt.prepared_at.tzinfo is None or receipt.authority_retired_at.tzinfo is None
            or receipt.authority_retired_at < receipt.prepared_at
            or receipt.authority_retired_at.timestamp() > time.time() + 30
            or any(member.broker_subject is None for member in receipt.cohort)
            or digest(canonical([m.model_dump(mode="json") for m in receipt.cohort])) != receipt.cohort_sha256):
        raise fail("prepared_target_receipt_invalid")
    _validate_readback(receipt.readback)
    return StandaloneSSOBootstrapSpec(
        keycloak_internal_url=receipt.internal_url, keycloak_public_url=settings.keycloak_server_url,
        realm=receipt.realm_name, akb_public_url=receipt.akb_public_url,
        bootstrap_client_id=receipt.bootstrap_client_id, bootstrap_client_secret="",
        management_client_id=receipt.management_client_id,
        management_client_secret=settings.keycloak_management_client_secret,
        api_client_id=receipt.api_client_id, api_client_secret=settings.keycloak_client_secret,
        admin_client_id=receipt.admin_client_id, admin_client_secret=settings.keycloak_admin_client_secret,
        product_admin_username=receipt.product_admin_username, product_admin_email=receipt.product_admin_email,
        product_admin_password="", backchannel_logout_uri=receipt.backchannel_logout_uri,
        local_realm_self_registration=receipt.local_realm_self_registration)


async def fresh_target_readback(receipt: PreparedTargetReceipt, spec: StandaloneSSOBootstrapSpec,
                                control: KeycloakStandaloneSSOControl) -> None:
    token = await control.acquire_management(spec)
    if token is None:
        raise fail("prepared_target_management_unavailable")
    discovery = await control._client(spec).get(
        f"/realms/{quote(spec.realm, safe='')}/.well-known/openid-configuration")
    if discovery.status_code != 200 or discovery.json().get("issuer") != receipt.issuer:
        raise fail("prepared_target_actual_issuer_mismatch")
    current = await control.readback(spec, management_token=token)
    _validate_readback(current)
    identity_fields = ("realm_id", "product_admin_subject", "admin_client_uuid", "management_client_uuid", "api_client_uuid")
    if any(getattr(current, field) != getattr(receipt.readback, field) for field in identity_fields):
        raise fail("prepared_target_actual_identity_mismatch")
    if not await control.realm_events_converged(spec, management_token=token):
        raise fail("prepared_target_events_mismatch")
    await control.brokered_account_guard_readback(spec, management_token=token)
    if not await control.login_presentation_converged(spec, management_token=token):
        raise fail("prepared_target_login_mismatch")
    # Keycloak 26's GET client-secret requires the existing view-clients role;
    # mutation/rotation requires configure authority. Read secrets only for an
    # in-memory equality check and never include values in receipts or errors.
    for client_uuid, secret_value in ((current.api_client_uuid, spec.api_client_secret),
                                      (current.admin_client_uuid, spec.admin_client_secret)):
        actual = await control._json(spec,
            f"/admin/realms/{quote(spec.realm, safe='')}/clients/{quote(client_uuid, safe='')}/client-secret",
            token=token, code="prepared_target_client_credential_read_failed")
        if not isinstance(actual, dict) or actual.get("value") != secret_value:
            raise fail("prepared_target_client_credential_mismatch")


async def record_target_adoption(receipt: PreparedTargetReceipt, *, spec: StandaloneSSOBootstrapSpec,
                                  preparation_bytes: bytes) -> dict[str, object]:
    """Canonical exact admin projection, then an immutable target-scoped receipt.

    Provisioning and receipt inserts are independently idempotent. A crash
    between them resumes against the same target-specific principal; no source
    recovery account, binding, receipt, PAT, or ACL row is updated.
    """
    from app.db.postgres import get_pool
    from app.services.recovery_admin_service import provision_sso_recovery_admin

    pool = await get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchrow("SELECT preparation_sha256 FROM prepared_sso_target_adoptions WHERE target_id=$1",
                                       receipt.target_id)
        if existing is not None and existing["preparation_sha256"] != digest(preparation_bytes):
            raise fail("prepared_target_existing_adoption_mismatch")
    projection = await provision_sso_recovery_admin(
        username=receipt.product_admin_username, email=receipt.product_admin_email,
        issuer=receipt.issuer, subject=receipt.readback.product_admin_subject, target_id=receipt.target_id)
    user_id = _validate_projection(projection)
    retirement = _expected_retirement_receipt(spec, receipt.readback, akb_user_id=user_id)
    expected = (receipt.operation_id, str(receipt.realm_id), receipt.issuer, receipt.profile, receipt.config_sha256,
                digest(preparation_bytes), UUID(user_id), canonical(asdict(retirement)).decode(),
                canonical(receipt.model_dump(mode="json")).decode())
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1::bigint)", 0x414B4253534F5447)
            await conn.execute("""INSERT INTO prepared_sso_target_adoptions
                (target_id,operation_id,realm_id,issuer,profile,config_sha256,preparation_sha256,
                 akb_user_id,retirement_receipt,host_preparation)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10::jsonb)
                ON CONFLICT(target_id) DO NOTHING""", receipt.target_id, *expected)
            row = await conn.fetchrow("SELECT * FROM prepared_sso_target_adoptions WHERE target_id=$1", receipt.target_id)
            if row is None or any(row[key] != value for key, value in zip(
                    ("operation_id", "realm_id", "issuer", "profile", "config_sha256", "preparation_sha256", "akb_user_id"),
                    expected[:7], strict=True)) or json.loads(row["retirement_receipt"]) != asdict(retirement):
                raise fail("prepared_target_existing_adoption_mismatch")
    return {"adopted": True, "target_id": str(receipt.target_id), "user_id": user_id, "profile": receipt.profile}


async def load_target_adoption(*, target_id: UUID, realm_id: str, issuer: str) -> dict[str, object] | None:
    """Explicit selection only: no latest-row or global-receipt fallback."""
    from app.db.postgres import get_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM prepared_sso_target_adoptions WHERE target_id=$1", target_id)
    if row is None:
        return None
    if row["realm_id"] != realm_id or row["issuer"] != issuer:
        raise fail("prepared_target_existing_adoption_mismatch")
    return json.loads(row["retirement_receipt"])


async def adopt_from_file(input_file: Path) -> dict[str, object]:
    from app.config import settings
    from app.db.postgres import close_pool

    preparation_bytes = protected_read(input_file)
    receipt = PreparedTargetReceipt.model_validate_json(preparation_bytes)
    descriptor = protected_read(Path(getattr(settings, "sso_management_targets_file", "")))
    spec = validate_runtime_binding(receipt, descriptor, settings)
    control = KeycloakStandaloneSSOControl(verify_ssl=settings.keycloak_verify_ssl)
    try:
        await fresh_target_readback(receipt, spec, control)
        from app.cli import _initialize_operator_database

        await _initialize_operator_database()
        return await record_target_adoption(receipt, spec=spec, preparation_bytes=preparation_bytes)
    finally:
        await control.aclose()
        await close_pool()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-file", required=True, type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(adopt_from_file(args.input_file))))
        return 0
    except StandaloneSSOBootstrapError as exc:
        print(exc.code, file=sys.stderr)
        return 1
    except Exception:
        print("prepared_target_adoption_failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
