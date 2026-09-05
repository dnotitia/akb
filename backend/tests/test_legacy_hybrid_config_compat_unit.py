"""Compatibility coverage for the bounded 0.14.x rollback runtime."""

from app.config import Settings

_ADMIN_SECRET = "test-admin-client-secret"  # pragma: allowlist secret


def test_hybrid_runtime_accepts_newer_admin_client_secret_field() -> None:
    configured = Settings(
        keycloak_enabled=True,
        keycloak_sso_only=False,
        local_auth_enabled=True,
        keycloak_admin_client_secret=_ADMIN_SECRET,
    )

    assert configured.keycloak_enabled is True
    assert configured.keycloak_sso_only is False
    assert configured.local_auth_enabled is True
    assert configured.keycloak_admin_client_secret == _ADMIN_SECRET
