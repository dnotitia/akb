"""Bind a non-human identity-provider client to an AKB service account.

``external_identities`` means "an identity provider identity that belongs to a
person here". The whole human resolution path is built on that reading:
enrollment policy, email snapshots, profile refresh on every request, the
ordinary browser-session surface, and the product-admin surface all join it and
expect a human on the other end. Putting a machine there would make every one
of those a place where the invariant has to be re-established, forever.

So machine principals get their own table. One row is one configured authority:
an exact (issuer, client) pair, resolved to exactly one AKB service account.
``subject`` is the client's service-account user in that realm — recorded and
refreshed for audit and to keep a recreated client from silently becoming a
second administrator, not consulted as an authorization input.
"""

from __future__ import annotations


async def migrate(conn) -> None:
    async with conn.transaction():
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS service_identities (
                id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                issuer TEXT NOT NULL,
                client_id TEXT NOT NULL,
                subject TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT service_identities_issuer_client_key
                    UNIQUE (issuer, client_id),
                CONSTRAINT service_identities_issuer_subject_key
                    UNIQUE (issuer, subject)
            )
            """
        )
        # One AKB account is never shared by two authorities: a second binding
        # onto the same account would make revoking one revoke both.
        await conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS service_identities_user_key
                ON service_identities(user_id)
            """
        )
