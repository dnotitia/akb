"""Durable, non-secret evidence for standalone SSO bootstrap retirement."""

from __future__ import annotations

import uuid

from app.db.postgres import get_pool
from app.services.standalone_sso_bootstrap import (
    STANDALONE_SSO_RECEIPT_PROFILE,
    STANDALONE_SSO_RECEIPT_PROFILES,
    StandaloneSSOBootstrapError,
    StandaloneSSORetirementReceipt,
)
from app.services.sso_callback_urls import is_backchannel_logout_uri


# Stable signed-bigint key ("AKBSSORP") serializes the singleton receipt write.
_RECEIPT_LOCK_ID = 0x414B4253534F5250

_SELECT_RECEIPT = """
    SELECT profile, issuer, realm_id, bootstrap_client_id,
           management_client_uuid, admin_client_uuid, api_client_uuid,
           product_admin_subject, akb_user_id, backchannel_logout_uri
      FROM standalone_sso_bootstrap_retirements
     WHERE profile = $1
"""


def _error() -> StandaloneSSOBootstrapError:
    return StandaloneSSOBootstrapError("keycloak_bootstrap_retirement_receipt_mismatch")


def _validated_user_id(receipt: StandaloneSSORetirementReceipt) -> uuid.UUID:
    fields = (
        receipt.issuer,
        receipt.realm_id,
        receipt.bootstrap_client_id,
        receipt.management_client_uuid,
        receipt.admin_client_uuid,
        receipt.api_client_uuid,
        receipt.product_admin_subject,
        receipt.akb_user_id,
    )
    if (
        receipt.profile != STANDALONE_SSO_RECEIPT_PROFILE
        or any(not value.strip() for value in fields)
        or receipt.backchannel_logout_uri is None
        or not is_backchannel_logout_uri(receipt.backchannel_logout_uri)
    ):
        raise _error()
    try:
        return uuid.UUID(receipt.akb_user_id)
    except ValueError, AttributeError:
        raise _error() from None


def _from_row(row) -> StandaloneSSORetirementReceipt:
    return StandaloneSSORetirementReceipt(
        profile=row["profile"],
        issuer=row["issuer"],
        realm_id=row["realm_id"],
        bootstrap_client_id=row["bootstrap_client_id"],
        management_client_uuid=row["management_client_uuid"],
        admin_client_uuid=row["admin_client_uuid"],
        api_client_uuid=row["api_client_uuid"],
        product_admin_subject=row["product_admin_subject"],
        akb_user_id=str(row["akb_user_id"]),
        backchannel_logout_uri=row["backchannel_logout_uri"],
    )


def _same_installation(
    source: StandaloneSSORetirementReceipt,
    target: StandaloneSSORetirementReceipt,
) -> bool:
    return (
        source.profile == target.profile == STANDALONE_SSO_RECEIPT_PROFILE
        and source.issuer == target.issuer
        and source.realm_id == target.realm_id
        and source.management_client_uuid == target.management_client_uuid
        and source.admin_client_uuid == target.admin_client_uuid
        and source.api_client_uuid == target.api_client_uuid
        and source.product_admin_subject == target.product_admin_subject
        and source.akb_user_id == target.akb_user_id
        and source.backchannel_logout_uri != target.backchannel_logout_uri
    )


async def load_standalone_sso_retirement_receipt() -> StandaloneSSORetirementReceipt | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        for profile in STANDALONE_SSO_RECEIPT_PROFILES:
            row = await conn.fetchrow(
                _SELECT_RECEIPT,
                profile,
            )
            if row is not None:
                return _from_row(row)
    return None


async def record_standalone_sso_retirement_receipt(
    receipt: StandaloneSSORetirementReceipt,
    *,
    previous_receipt: StandaloneSSORetirementReceipt | None = None,
) -> None:
    """Insert once, or replace one exact current callback receipt by CAS."""

    akb_user_id = _validated_user_id(receipt)
    if previous_receipt is not None:
        _validated_user_id(previous_receipt)
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock($1::bigint)",
                _RECEIPT_LOCK_ID,
            )
            existing = await conn.fetchrow(
                _SELECT_RECEIPT,
                STANDALONE_SSO_RECEIPT_PROFILE,
            )
            if existing is not None:
                current = _from_row(existing)
                if current == receipt:
                    return
                if (
                    previous_receipt is None
                    or current != previous_receipt
                    or not _same_installation(previous_receipt, receipt)
                ):
                    raise _error()
                update_result = await conn.execute(
                    """
                    UPDATE standalone_sso_bootstrap_retirements
                       SET issuer = $2,
                           realm_id = $3,
                           bootstrap_client_id = $4,
                           management_client_uuid = $5,
                           admin_client_uuid = $6,
                           api_client_uuid = $7,
                           product_admin_subject = $8,
                           akb_user_id = $9,
                           backchannel_logout_uri = $10,
                           retired_at = NOW()
                     WHERE profile = $1
                       AND backchannel_logout_uri = $11
                    """,
                    receipt.profile,
                    receipt.issuer,
                    receipt.realm_id,
                    receipt.bootstrap_client_id,
                    receipt.management_client_uuid,
                    receipt.admin_client_uuid,
                    receipt.api_client_uuid,
                    receipt.product_admin_subject,
                    akb_user_id,
                    receipt.backchannel_logout_uri,
                    previous_receipt.backchannel_logout_uri,
                )
                if update_result != "UPDATE 1":
                    raise _error()
                updated = await conn.fetchrow(
                    _SELECT_RECEIPT,
                    STANDALONE_SSO_RECEIPT_PROFILE,
                )
                if updated is None or _from_row(updated) != receipt:
                    raise _error()
                return
            if previous_receipt is not None:
                raise _error()
            await conn.execute(
                """
                INSERT INTO standalone_sso_bootstrap_retirements (
                    profile, issuer, realm_id, bootstrap_client_id,
                    management_client_uuid, admin_client_uuid, api_client_uuid,
                    product_admin_subject, akb_user_id, backchannel_logout_uri
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                """,
                receipt.profile,
                receipt.issuer,
                receipt.realm_id,
                receipt.bootstrap_client_id,
                receipt.management_client_uuid,
                receipt.admin_client_uuid,
                receipt.api_client_uuid,
                receipt.product_admin_subject,
                akb_user_id,
                receipt.backchannel_logout_uri,
            )
