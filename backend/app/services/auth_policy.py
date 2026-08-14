"""Deployment-level authentication policy guards."""

from app.config import settings
from app.exceptions import LocalAuthDisabledError
from app.services.sso_browser_session_crypto import (
    BrowserSessionCipher,
    BrowserSessionKeyError,
)


def sso_browser_session_ready() -> bool:
    """Return whether the complete ordinary-browser custody profile is usable.

    A blank encryption key is an intentional expand/contract state: bearer
    resource-server auth and `/admin` stay live while ordinary browser SSO is
    omitted from the public capability document. Invalid configured material
    is handled as a startup error by lifecycle validation.
    """
    if (
        settings.require_auth_mode() != "sso"
        or not settings.keycloak_enabled
        or settings.sso_session_epoch is None
        or not settings.keycloak_client_id.strip()
        or not settings.sso_browser_session_encryption_key
        or (not settings.keycloak_public_client and not settings.keycloak_client_secret)
    ):
        return False
    try:
        BrowserSessionCipher.from_encoded_key(settings.sso_browser_session_encryption_key)
    except BrowserSessionKeyError:
        return False
    return True


def require_local_auth_enabled() -> None:
    """Reject every password/session lifecycle outside canonical local mode."""
    if not settings.local_human_auth_enabled:
        raise LocalAuthDisabledError()
