"""Persistent account-state markers shared by lifecycle services."""

from __future__ import annotations


# An intentionally invalid credential tombstone, never a usable password.
RETIRED_RECOVERY_ADMIN_PASSWORD_SENTINEL = (  # nosec B105
    "!retired-recovery-admin:no-local-login!"
)


def is_retired_recovery_admin_password(password_hash: str | None) -> bool:
    """Return whether an account is the durable retired-recovery tombstone."""
    return password_hash == RETIRED_RECOVERY_ADMIN_PASSWORD_SENTINEL
