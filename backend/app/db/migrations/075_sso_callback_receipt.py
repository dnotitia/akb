"""Bind current standalone SSO receipts to the effective callback URI."""

from __future__ import annotations


async def migrate(conn) -> None:
    async with conn.transaction():
        await conn.execute(
            """
            ALTER TABLE standalone_sso_bootstrap_retirements
                ADD COLUMN IF NOT EXISTS backchannel_logout_uri TEXT
                    CHECK (
                        backchannel_logout_uri IS NULL OR
                        char_length(backchannel_logout_uri) BETWEEN 1 AND 2048
                    )
            """
        )
