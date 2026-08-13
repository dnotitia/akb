"""Add durable evidence for verified standalone SSO bootstrap retirement."""

from __future__ import annotations


async def migrate(conn) -> None:
    async with conn.transaction():
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS standalone_sso_bootstrap_retirements (
                profile TEXT PRIMARY KEY
                    CHECK (char_length(profile) BETWEEN 1 AND 128),
                issuer TEXT NOT NULL
                    CHECK (char_length(issuer) BETWEEN 1 AND 2048),
                realm_id TEXT NOT NULL
                    CHECK (char_length(realm_id) BETWEEN 1 AND 255),
                bootstrap_client_id TEXT NOT NULL
                    CHECK (char_length(bootstrap_client_id) BETWEEN 1 AND 255),
                management_client_uuid TEXT NOT NULL
                    CHECK (char_length(management_client_uuid) BETWEEN 1 AND 255),
                admin_client_uuid TEXT NOT NULL
                    CHECK (char_length(admin_client_uuid) BETWEEN 1 AND 255),
                api_client_uuid TEXT NOT NULL
                    CHECK (char_length(api_client_uuid) BETWEEN 1 AND 255),
                product_admin_subject TEXT NOT NULL
                    CHECK (char_length(product_admin_subject) BETWEEN 1 AND 1024),
                akb_user_id UUID NOT NULL,
                retired_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
