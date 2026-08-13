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


# Stable signed-bigint key ("AKBSSORP") serializes the singleton receipt write.
_RECEIPT_LOCK_ID = 0x414B4253534F5250

_SELECT_RECEIPT = """
    SELECT profile, issuer, realm_id, bootstrap_client_id,
           management_client_uuid, admin_client_uuid, api_client_uuid,
           product_admin_subject, akb_user_id
      FROM standalone_sso_bootstrap_retirements
     WHERE profile = $1
"""


def _error() -> StandaloneSSOBootstrapError:
    return StandaloneSSOBootstrapError(
        "keycloak_bootstrap_retirement_receipt_mismatch"
    )


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
    if receipt.profile != STANDALONE_SSO_RECEIPT_PROFILE or any(
        not value.strip() for value in fields
    ):
        raise _error()
    try:
        return uuid.UUID(receipt.akb_user_id)
    except (ValueError, AttributeError):
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
    )


async def load_standalone_sso_retirement_receipt(
) -> StandaloneSSORetirementReceipt | None:
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
) -> None:
    """Insert once after verified deletion; never replace conflicting proof."""

    akb_user_id = _validated_user_id(receipt)
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
                if _from_row(existing) != receipt:
                    raise _error()
                return
            await conn.execute(
                """
                INSERT INTO standalone_sso_bootstrap_retirements (
                    profile, issuer, realm_id, bootstrap_client_id,
                    management_client_uuid, admin_client_uuid, api_client_uuid,
                    product_admin_subject, akb_user_id
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
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
            )
