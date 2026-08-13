"""HTTP contracts for product-admin SSO provider control."""

from __future__ import annotations

import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.routes import admin_auth, admin_sso
from app.config import settings
from app.exceptions import AuthenticationError
from app.services.admin_auth_service import ProductAdminIdentity
from app.sso.keycloak_admin import ProviderControlError
from app.sso.models import ProviderMutationReadback, ProviderReadback


_SECRET = "write-only-provider-secret-must-not-leak"  # pragma: allowlist secret


def _admin() -> ProductAdminIdentity:
    return ProductAdminIdentity(
        user_id=uuid.uuid4(),
        external_identity_id=uuid.uuid4(),
        username="product-admin",
        email="admin@example.com",
        display_name="Product Admin",
        auth_method="keycloak",
    )


def _provider(
    *,
    state: str = "configured_disabled",
    enabled: bool | None = None,
) -> ProviderReadback:
    is_enabled = state == "enabled" if enabled is None else enabled
    return ProviderReadback(
        provider_type="keycloak-oidc",
        alias="workforce",
        display_name="Company SSO",
        state=state,  # type: ignore[arg-type]
        enabled=is_enabled,
        issuer="https://accounts.example.com/realms/workforce",
        discovery_url=(
            "https://accounts.example.com/realms/workforce/"
            ".well-known/openid-configuration"
        ),
        client_id="akb-broker",
        client_secret_configured=True,
        redirect_uri=(
            "https://auth.akb.example.com/realms/akb/"
            "broker/workforce/endpoint"
        ),
        supports_logout=True,
        supports_identity_migration=False,
    )


class Control:
    control_mode = "direct"

    def __init__(self) -> None:
        self.providers = [_provider()]
        self.configured_secret: str | None = None
        self.toggles: list[tuple[str, bool]] = []

    async def list_providers(self, **_kwargs):
        return tuple(self.providers)

    async def configure(self, spec):
        self.configured_secret = spec.client_secret
        return ProviderMutationReadback(before=None, after=_provider())

    async def set_enabled(self, alias: str, *, enabled: bool):
        self.toggles.append((alias, enabled))
        return ProviderMutationReadback(
            before=_provider(),
            after=_provider(
                state="enabled" if enabled else "configured_disabled",
                enabled=enabled,
            ),
        )


def _client(monkeypatch, control: Control, *, admin=None) -> TestClient:
    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    monkeypatch.setattr(
        admin_sso,
        "get_keycloak_provider_control",
        lambda: control,
    )
    app = FastAPI()
    app.include_router(admin_sso.router, prefix="/api/v1")
    actor = admin or _admin()
    app.dependency_overrides[admin_auth.get_current_product_admin] = lambda: actor
    app.dependency_overrides[admin_auth.get_product_admin_mutation] = lambda: actor
    return TestClient(app)


def test_admin_catalog_is_versioned_bounded_and_secret_free(monkeypatch):
    response = _client(monkeypatch, Control()).get("/api/v1/admin/sso/providers")

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": 1,
        "auth_mode": "sso",
        "control_mode": "direct",
        "supported_provider_types": ["keycloak-oidc"],
        "providers": [_provider().admin_view()],
    }
    assert _SECRET not in response.text


def test_delegated_catalog_is_explicit_without_attempting_a_read(monkeypatch):
    class Delegated(Control):
        control_mode = "delegated"

        async def list_providers(self, **_kwargs):
            raise AssertionError("delegated control must not call Admin REST")

    response = _client(monkeypatch, Delegated()).get(
        "/api/v1/admin/sso/providers"
    )

    assert response.status_code == 200
    assert response.json()["control_mode"] == "delegated"
    assert response.json()["providers"] == []


def test_local_mode_hides_the_control_surface_before_admin_resolution(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)
    app = FastAPI()
    app.include_router(admin_sso.router, prefix="/api/v1")

    response = TestClient(app).get("/api/v1/admin/sso/providers")

    assert response.status_code == 404
    assert response.json()["detail"] == "SSO provider control is not enabled"


def test_configure_accepts_secret_once_and_audits_only_redacted_metadata(monkeypatch):
    control = Control()
    records: list[dict[str, object]] = []
    monkeypatch.setattr(admin_sso.audit_log, "record", lambda **value: records.append(value))
    response = _client(monkeypatch, control).put(
        "/api/v1/admin/sso/providers/workforce",
        json={
            "provider_type": "keycloak-oidc",
            "display_name": "Company SSO",
            "issuer": "https://accounts.example.com/realms/workforce",
            "discovery_url": (
                "https://accounts.example.com/realms/workforce/"
                ".well-known/openid-configuration"
            ),
            "client_id": "akb-broker",
            "client_secret": _SECRET,
        },
    )

    assert response.status_code == 200
    assert control.configured_secret == _SECRET
    assert _SECRET not in response.text
    assert _SECRET not in repr(records)
    assert [record["action"] for record in records] == [
        "admin.sso.provider.configure.requested",
        "admin.sso.provider.configure",
    ]
    assert records[-1]["meta"] == {
        "before": None,
        "after": _provider().audit_view(),
    }


@pytest.mark.parametrize(
    ("suffix", "enabled"),
    [("enable", True), ("disable", False)],
)
def test_enable_disable_are_explicit_mutations(monkeypatch, suffix, enabled):
    control = Control()
    response = _client(monkeypatch, control).post(
        f"/api/v1/admin/sso/providers/workforce/{suffix}"
    )

    assert response.status_code == 200
    assert control.toggles == [("workforce", enabled)]
    assert response.json()["provider"]["state"] == (
        "enabled" if enabled else "configured_disabled"
    )


def test_provider_errors_are_status_mapped_without_leaking_values(monkeypatch):
    class Broken(Control):
        async def configure(self, spec):
            assert spec.client_secret == _SECRET
            raise ProviderControlError("provider_disable_before_reconfigure")

    response = _client(monkeypatch, Broken()).put(
        "/api/v1/admin/sso/providers/workforce",
        json={
            "provider_type": "keycloak-oidc",
            "display_name": "Company SSO",
            "issuer": "https://accounts.example.com/realms/workforce",
            "discovery_url": (
                "https://accounts.example.com/realms/workforce/"
                ".well-known/openid-configuration"
            ),
            "client_id": "akb-broker",
            "client_secret": _SECRET,
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == (
        "provider_disable_before_reconfigure"
    )
    assert _SECRET not in response.text


def test_invalid_secret_json_type_is_rejected_without_echoing_its_value(monkeypatch):
    leaked_value = "nested-secret-must-not-be-returned"  # pragma: allowlist secret

    response = _client(monkeypatch, Control()).put(
        "/api/v1/admin/sso/providers/workforce",
        json={
            "provider_type": "keycloak-oidc",
            "display_name": "Company SSO",
            "issuer": "https://accounts.example.com/realms/workforce",
            "discovery_url": (
                "https://accounts.example.com/realms/workforce/"
                ".well-known/openid-configuration"
            ),
            "client_id": "akb-broker",
            "client_secret": {"value": leaked_value},
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "provider_client_secret_invalid"
    assert leaked_value not in response.text


def test_invalid_alias_is_hashed_in_audit_instead_of_reflected(monkeypatch):
    class InvalidAlias(Control):
        async def configure(self, _spec):
            raise ProviderControlError("provider_alias_invalid")

    records: list[dict[str, object]] = []
    monkeypatch.setattr(admin_sso.audit_log, "record", lambda **value: records.append(value))

    response = _client(monkeypatch, InvalidAlias()).put(
        "/api/v1/admin/sso/providers/unsafe%0Aalias",
        json={
            "provider_type": "keycloak-oidc",
            "display_name": "Company SSO",
            "issuer": "https://accounts.example.com/realms/workforce",
            "discovery_url": (
                "https://accounts.example.com/realms/workforce/"
                ".well-known/openid-configuration"
            ),
            "client_id": "akb-broker",
            "client_secret": _SECRET,
        },
    )

    assert response.status_code == 422
    assert all(record["target"].startswith("provider=<invalid:sha256:") for record in records)
    assert "unsafe" not in repr(records)


def test_unexpected_mutation_failures_still_emit_a_result_audit(monkeypatch):
    class Broken(Control):
        async def configure(self, _spec):
            raise RuntimeError("unexpected control failure")

    records: list[dict[str, object]] = []
    monkeypatch.setattr(admin_sso.audit_log, "record", lambda **value: records.append(value))

    with pytest.raises(RuntimeError, match="unexpected control failure"):
        _client(monkeypatch, Broken(), admin=_admin()).put(
            "/api/v1/admin/sso/providers/workforce",
            json={
                "provider_type": "keycloak-oidc",
                "display_name": "Company SSO",
                "issuer": "https://accounts.example.com/realms/workforce",
                "discovery_url": (
                    "https://accounts.example.com/realms/workforce/"
                    ".well-known/openid-configuration"
                ),
                "client_id": "akb-broker",
                "client_secret": _SECRET,
            },
        )

    assert records[-1]["outcome"] == "error"
    assert records[-1]["code"] == "internal_error"
    assert _SECRET not in repr(records)


async def test_sso_mutation_dependency_rejects_missing_csrf_before_resolution(
    monkeypatch,
):
    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)

    class Request:
        cookies = {
            "akb_admin_session": "opaque-session-value-that-is-long-enough",
            "akb_admin_csrf": "opaque-csrf-value-that-is-long-enough",
        }

    with pytest.raises(AuthenticationError, match="Invalid admin CSRF token"):
        await admin_auth.get_product_admin_mutation(  # type: ignore[arg-type]
            Request(),
            authorization=None,
            csrf_header=None,
        )
