"""Deployment-level authentication policy guards."""

from app.config import settings
from app.exceptions import LocalAuthDisabledError


def require_local_auth_enabled() -> None:
    """Reject every password lifecycle operation under the managed profile."""
    if not settings.local_auth_enabled:
        raise LocalAuthDisabledError()
