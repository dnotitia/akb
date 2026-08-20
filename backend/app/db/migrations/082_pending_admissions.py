"""Record the arrival that ``invite_only`` refuses.

A workspace that owns its identity provider admits people by an exact
``(issuer, subject)`` pair, and the subject in that pair is assigned by this
workspace's own realm at exactly one moment: the person's first arrival through
the broker. Until then nobody can name it — not the platform, not an
administrator, and not the person. Only the upstream's owner could, and it is
not ours.

``invite_only`` refuses that arrival and, until now, discarded it. The broker
had just verified the person, checked a signed ``email_verified`` claim, and
minted them a stable realm subject; the one value an exact binding needs was
produced and thrown away in the same instant.

One row here is one such arrival. It is deliberately NOT a membership, an
account, or a grant: it authorizes nothing, and the refusal that creates it is
unchanged. It is a note saying "this exact identity presented itself", so that
an administrator can approve that specific arrival instead of guessing a
subject in advance.

``(issuer, subject)`` is unique, so a person who keeps trying updates one row
rather than adding rows. Nothing is keyed by email: the address is recorded
because an administrator reads it, never because anything matches on it.
"""

from __future__ import annotations


async def migrate(conn) -> None:
    async with conn.transaction():
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_admissions (
                id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                issuer TEXT NOT NULL,
                subject TEXT NOT NULL,
                email TEXT,
                display_name TEXT,
                first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                arrivals INTEGER NOT NULL DEFAULT 1,
                CONSTRAINT pending_admissions_issuer_subject_key
                    UNIQUE (issuer, subject)
            )
            """
        )
        # Retention and the cap both evict by least-recent arrival, and the
        # list an administrator reads is ordered the same way. One index
        # serves all three.
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS pending_admissions_last_seen_idx
                ON pending_admissions(last_seen_at DESC)
            """
        )
