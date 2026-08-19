"""Persistent account-state markers shared by lifecycle services."""

from __future__ import annotations


# An intentionally invalid credential tombstone, never a usable password.
RETIRED_RECOVERY_ADMIN_PASSWORD_SENTINEL = (  # nosec B105
    "!retired-recovery-admin:no-local-login!"
)

# The designated recovery administrator can be created before any credential
# exists for it. This marker records that state: it is not a bcrypt hash, so
# no candidate can ever verify against it and the account cannot be entered
# until a credential is issued on demand. Distinct from the retirement
# tombstone above — this account is live and holds recovery authority.
UNISSUED_RECOVERY_ADMIN_PASSWORD_SENTINEL = (  # nosec B105
    "!unissued-recovery-admin:no-local-login!"
)


def is_retired_recovery_admin_password(password_hash: str | None) -> bool:
    """Return whether an account is the durable retired-recovery tombstone."""
    return password_hash == RETIRED_RECOVERY_ADMIN_PASSWORD_SENTINEL


def is_unissued_recovery_admin_password(password_hash: str | None) -> bool:
    """Return whether a recovery administrator has no issued credential yet."""
    return password_hash == UNISSUED_RECOVERY_ADMIN_PASSWORD_SENTINEL
