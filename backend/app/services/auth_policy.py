"""Deployment-level authentication policy guards."""

from app.config import settings
from app.exceptions import LocalAuthDisabledError

# Code-owned staging capability. Phase 4 must replace the fail-closed browser
# routes with server-side token custody before changing this value.
SSO_BROWSER_SESSION_READY = False


def require_local_auth_enabled() -> None:
    """Reject every password/session lifecycle outside canonical local mode."""
    if not settings.local_human_auth_enabled:
        raise LocalAuthDisabledError()
