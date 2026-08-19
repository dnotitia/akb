"""Mark a delivered local credential as owed a replacement.

In SSO mode a credential handed to a person is temporary: the identity
provider arms ``UPDATE_PASSWORD`` when the account is created with a supplied
password and when that password is reset, so the first successful sign-in
forces a replacement. Local mode had no equivalent — a delivered credential
was simply the account's password until someone chose to change it.

The column is the local marker for exactly that state. It is set only where a
credential is issued to someone (administrative reset and local recovery-admin
provisioning), and cleared only where the holder replaces it. Self-service
registration never sets it: the person chose that password themselves, so
there is nothing to replace, and every pre-existing account keeps the ``false``
default.

Deliberately no CHECK tying the marker to ``auth_provider = 'local'``. The
services that set it already refuse non-local and non-human accounts, and a
constraint here would turn a local-to-SSO provider cutover on a row still
holding the marker into a migration failure.
"""

from __future__ import annotations


async def migrate(conn) -> None:
    async with conn.transaction():
        await conn.execute(
            """
            ALTER TABLE users
              ADD COLUMN IF NOT EXISTS credential_change_required
                  BOOLEAN NOT NULL DEFAULT false;
            """
        )
