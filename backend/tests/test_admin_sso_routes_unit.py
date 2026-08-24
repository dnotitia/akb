"""HTTP contracts for product-admin SSO provider control."""

from __future__ import annotations

import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.routes import admin_auth, admin_sso
from app.config import settings
from app.exceptions import AuthenticationError, ForbiddenError
from app.services.admin_auth_service import ProductAdminIdentity
from app.services.auth_service import AuthenticatedUser
from app.sso.keycloak_admin import ProviderControlError
from app.sso.identity_migration import (
    IdentityMigrationError,
    IdentityMigrationReadback,
)
from app.sso.models import (
    IdentityPrelinkReadback,
    ProviderMutationReadback,
    ProviderReadback,
)


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
        discovery_url=("https://accounts.example.com/realms/workforce/.well-known/openid-configuration"),
        client_id="akb-broker",
        client_secret_configured=True,
        redirect_uri=("https://auth.akb.example.com/realms/akb/broker/workforce/endpoint"),
        supports_logout=True,
        supports_identity_migration=True,
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

    async def verify_identity_prelink(
        self,
        alias: str,
        *,
        broker_subject: str,
        upstream_subject: str,
    ) -> IdentityPrelinkReadback:
        return IdentityPrelinkReadback(
            provider_alias=alias,
            provider_state=self.providers[0].state,
            upstream_issuer="https://accounts.example.com/realms/workforce",
            broker_issuer="https://auth.akb.example.com/realms/akb",
            broker_subject=broker_subject,
            upstream_subject=upstream_subject,
            broker_username="alice",
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
    app.dependency_overrides[admin_sso.get_sso_provider_control_actor] = lambda: actor
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

    response = _client(monkeypatch, Delegated()).get("/api/v1/admin/sso/providers")

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
            "discovery_url": ("https://accounts.example.com/realms/workforce/.well-known/openid-configuration"),
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
    response = _client(monkeypatch, control).post(f"/api/v1/admin/sso/providers/workforce/{suffix}")

    assert response.status_code == 200
    assert control.toggles == [("workforce", enabled)]
    assert response.json()["provider"]["state"] == ("enabled" if enabled else "configured_disabled")


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
            "discovery_url": ("https://accounts.example.com/realms/workforce/.well-known/openid-configuration"),
            "client_id": "akb-broker",
            "client_secret": _SECRET,
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == ("provider_disable_before_reconfigure")
    assert _SECRET not in response.text


def test_invalid_secret_json_type_is_rejected_without_echoing_its_value(monkeypatch):
    leaked_value = "nested-secret-must-not-be-returned"  # pragma: allowlist secret

    response = _client(monkeypatch, Control()).put(
        "/api/v1/admin/sso/providers/workforce",
        json={
            "provider_type": "keycloak-oidc",
            "display_name": "Company SSO",
            "issuer": "https://accounts.example.com/realms/workforce",
            "discovery_url": ("https://accounts.example.com/realms/workforce/.well-known/openid-configuration"),
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
            "discovery_url": ("https://accounts.example.com/realms/workforce/.well-known/openid-configuration"),
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
                "discovery_url": ("https://accounts.example.com/realms/workforce/.well-known/openid-configuration"),
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
            "__Host-akb_admin_session": "opaque-session-value-that-is-long-enough",
            "__Host-akb_admin_csrf": "opaque-csrf-value-that-is-long-enough",
        }

    with pytest.raises(AuthenticationError, match="Invalid admin CSRF token"):
        await admin_auth.get_product_admin_mutation(  # type: ignore[arg-type]
            Request(),
            authorization=None,
            csrf_header=None,
        )


async def test_service_admin_controls_provider_without_browser_csrf(monkeypatch):
    """A service administrator controls SSO providers without a browser CSRF token."""
    actor = AuthenticatedUser(
        user_id=str(uuid.uuid4()),
        username="platform-bot",
        email="platform-bot@invalid.local",
        display_name="AKB Platform",
        is_admin=True,
        auth_method="pat",
        account_kind="service",
        token_id=str(uuid.uuid4()),
        key_class="service",
        token_scopes=frozenset({"read", "write", "admin"}),
    )

    async def resolve_service(_request):
        return actor

    monkeypatch.setattr(admin_sso, "get_current_user", resolve_service)

    class Request:
        method = "PUT"

    resolved = await admin_sso.get_sso_provider_control_actor(  # type: ignore[attr-defined]
        Request(),
        authorization="Bearer akb_secret_platform",  # pragma: allowlist secret
        csrf_header=None,
    )

    assert resolved is actor


@pytest.mark.parametrize(
    ("is_admin", "account_kind", "auth_method", "token_id", "key_class", "token_scopes"),
    [
        (False, "service", "pat", "token-id", "service", frozenset({"read", "write", "admin"})),
        (True, "human", "pat", "token-id", "service", frozenset({"read", "write", "admin"})),
        (True, "service", "oauth", "token-id", "service", frozenset({"read", "write", "admin"})),
        (True, "service", "pat", None, "service", frozenset({"read", "write", "admin"})),
        (True, "service", "pat", "token-id", "pat", frozenset({"read", "write", "admin"})),
        (True, "service", "pat", "token-id", "service", frozenset({"read", "write"})),
    ],
)
async def test_provider_service_control_requires_the_exact_machine_authority(
    monkeypatch,
    is_admin,
    account_kind,
    auth_method,
    token_id,
    key_class,
    token_scopes,
):
    actor = AuthenticatedUser(
        user_id=str(uuid.uuid4()),
        username="candidate",
        email="candidate@invalid.local",
        display_name=None,
        is_admin=is_admin,
        auth_method=auth_method,
        account_kind=account_kind,
        token_id=token_id,
        key_class=key_class,
        token_scopes=token_scopes,
    )

    async def resolve_candidate(_request):
        return actor

    monkeypatch.setattr(admin_sso, "get_current_user", resolve_candidate)

    class Request:
        method = "POST"

    with pytest.raises(ForbiddenError, match="administrator service key"):
        await admin_sso.get_sso_provider_control_actor(  # type: ignore[arg-type]
            Request(),
            authorization="Bearer candidate",
            csrf_header=None,
        )


async def test_provider_browser_mutation_still_requires_admin_csrf(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)

    class Request:
        method = "PUT"
        cookies = {
            "__Host-akb_admin_session": "opaque-session-value-that-is-long-enough",
            "__Host-akb_admin_csrf": "opaque-csrf-value-that-is-long-enough",
        }

    with pytest.raises(AuthenticationError, match="Invalid admin CSRF token"):
        await admin_sso.get_sso_provider_control_actor(  # type: ignore[arg-type]
            Request(),
            authorization=None,
            csrf_header=None,
        )


async def test_invalid_bearer_never_falls_through_to_admin_cookie(monkeypatch):
    browser_fallback_called = False

    async def reject_bearer(_request):
        raise AuthenticationError("invalid bearer")

    async def browser_fallback(*_args, **_kwargs):
        nonlocal browser_fallback_called
        browser_fallback_called = True
        return _admin()

    monkeypatch.setattr(admin_sso, "get_current_user", reject_bearer)
    monkeypatch.setattr(admin_sso, "get_product_admin_mutation", browser_fallback)

    class Request:
        method = "POST"

    with pytest.raises(AuthenticationError, match="invalid bearer"):
        await admin_sso.get_sso_provider_control_actor(  # type: ignore[arg-type]
            Request(),
            authorization="Bearer invalid",
            csrf_header="attacker-controlled",
        )
    assert browser_fallback_called is False


def _route_dependency_callables(route) -> set:
    return {
        dependency.call
        for dependency in route.dependant.dependencies
        if dependency.call is not None
    }


def test_only_provider_catalog_configuration_and_toggle_use_machine_control():
    provider_routes = {
        (route.path, method)
        for route in admin_sso.router.routes
        for method in getattr(route, "methods", set())
        if route.path.startswith("/admin/sso/providers")
    }
    machine_routes = {
        ("/admin/sso/providers", "GET"),
        ("/admin/sso/providers/{alias}", "PUT"),
        ("/admin/sso/providers/{alias}/enable", "POST"),
        ("/admin/sso/providers/{alias}/disable", "POST"),
    }
    assert machine_routes <= provider_routes

    for route in admin_sso.router.routes:
        if not route.path.startswith("/admin/sso/providers"):
            continue
        callables = _route_dependency_callables(route)
        keyed_methods = {(route.path, method) for method in route.methods}
        if keyed_methods & machine_routes:
            assert admin_sso.get_sso_provider_control_actor in callables
        else:
            assert admin_sso.get_sso_provider_control_actor not in callables
            assert (
                admin_auth.get_current_product_admin in callables
                or admin_auth.get_product_admin_mutation in callables
            )


def test_identity_migration_preflight_derives_both_issuers_server_side(monkeypatch):
    user_id = uuid.uuid4()
    captured: dict[str, object] = {}

    async def _inspect(**values):
        captured.update(values)
        return IdentityMigrationReadback(
            user_id=user_id,
            state="ready_to_link",
            old_issuer=values["old_issuer"],
            new_issuer=values["new_issuer"],
        )

    monkeypatch.setattr(admin_sso, "inspect_identity_migration", _inspect)
    response = _client(monkeypatch, Control()).post(
        "/api/v1/admin/sso/providers/workforce/identity-migrations/preflight",
        json={
            "existing_user_id": str(user_id),
            "upstream_subject": "upstream-subject",
            "broker_subject": "broker-subject",
        },
    )

    assert response.status_code == 200
    assert response.json()["schema_version"] == 1
    assert captured == {
        "existing_user_id": str(user_id),
        "old_issuer": "https://accounts.example.com/realms/workforce",
        "old_subject": "upstream-subject",
        "new_issuer": "https://auth.akb.example.com/realms/akb",
        "new_subject": "broker-subject",
    }
    assert response.json()["migration"]["state"] == "ready_to_link"


def test_identity_migration_apply_requires_provider_disabled_before_db_write(
    monkeypatch,
):
    control = Control()
    control.providers = [_provider(state="enabled")]

    async def _must_not_apply(**_values):
        raise AssertionError("enabled provider must not mutate AKB identity state")

    monkeypatch.setattr(admin_sso, "apply_identity_migration", _must_not_apply)
    response = _client(monkeypatch, control).post(
        "/api/v1/admin/sso/providers/workforce/identity-migrations/apply",
        json={
            "existing_user_id": str(uuid.uuid4()),
            "upstream_subject": "upstream-subject",
            "broker_subject": "broker-subject",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == ("identity_migration_provider_must_be_disabled")


def test_identity_migration_apply_is_audited_without_raw_subjects(monkeypatch):
    user_id = uuid.uuid4()
    records: list[dict[str, object]] = []
    applied: list[dict[str, object]] = []

    async def _apply(**values):
        applied.append(values)
        return IdentityMigrationReadback(
            user_id=user_id,
            state="linked",
            old_issuer=values["old_issuer"],
            new_issuer=values["new_issuer"],
        )

    monkeypatch.setattr(admin_sso, "apply_identity_migration", _apply)
    monkeypatch.setattr(admin_sso.audit_log, "record", lambda **value: records.append(value))
    response = _client(monkeypatch, Control()).post(
        "/api/v1/admin/sso/providers/workforce/identity-migrations/apply",
        json={
            "existing_user_id": str(user_id),
            "upstream_subject": "upstream-subject",
            "broker_subject": "broker-subject",
        },
    )

    assert response.status_code == 200
    assert response.json()["migration"] == {
        "user_id": str(user_id),
        "state": "linked",
        "old_issuer": "https://accounts.example.com/realms/workforce",
        "new_issuer": "https://auth.akb.example.com/realms/akb",
    }
    assert applied[0]["actor_id"] == "product-admin"
    assert [record["action"] for record in records] == [
        "admin.sso.identity_migration.apply.requested",
        "admin.sso.identity_migration.apply",
    ]
    assert "upstream-subject" not in repr(records)
    assert "broker-subject" not in repr(records)
    assert records[-1]["meta"]["old_subject_sha256"]
    assert records[-1]["meta"]["new_subject_sha256"]


def test_identity_migration_apply_compensates_fresh_binding_on_postcheck_drift(
    monkeypatch,
):
    user_id = uuid.uuid4()
    rollbacks: list[dict[str, object]] = []

    class DriftAfterApply(Control):
        def __init__(self) -> None:
            super().__init__()
            self.prelink_reads = 0

        async def verify_identity_prelink(self, *args, **kwargs):
            self.prelink_reads += 1
            if self.prelink_reads == 2:
                raise ProviderControlError("identity_prelink_missing")
            return await super().verify_identity_prelink(*args, **kwargs)

    async def _readback(**values):
        return IdentityMigrationReadback(
            user_id=user_id,
            state="ready_to_link",
            old_issuer=values["old_issuer"],
            new_issuer=values["new_issuer"],
        )

    async def _apply(**values):
        return IdentityMigrationReadback(
            user_id=user_id,
            state="linked",
            old_issuer=values["old_issuer"],
            new_issuer=values["new_issuer"],
            binding_changed=True,
        )

    async def _rollback(**values):
        rollbacks.append(values)
        return await _readback(**values)

    monkeypatch.setattr(admin_sso, "apply_identity_migration", _apply)
    monkeypatch.setattr(admin_sso, "rollback_identity_migration", _rollback)
    response = _client(monkeypatch, DriftAfterApply()).post(
        "/api/v1/admin/sso/providers/workforce/identity-migrations/apply",
        json={
            "existing_user_id": str(user_id),
            "upstream_subject": "upstream-subject",
            "broker_subject": "broker-subject",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "identity_prelink_missing"
    assert len(rollbacks) == 1


def test_identity_migration_postcheck_never_removes_a_preexisting_binding(
    monkeypatch,
):
    user_id = uuid.uuid4()

    class DriftAfterIdempotentApply(Control):
        def __init__(self) -> None:
            super().__init__()
            self.prelink_reads = 0

        async def verify_identity_prelink(self, *args, **kwargs):
            self.prelink_reads += 1
            if self.prelink_reads == 2:
                raise ProviderControlError("identity_prelink_missing")
            return await super().verify_identity_prelink(*args, **kwargs)

    async def _linked(**values):
        return IdentityMigrationReadback(
            user_id=user_id,
            state="linked",
            old_issuer=values["old_issuer"],
            new_issuer=values["new_issuer"],
        )

    async def _must_not_rollback(**_values):
        raise AssertionError("a binding owned by an earlier call must remain")

    monkeypatch.setattr(admin_sso, "apply_identity_migration", _linked)
    monkeypatch.setattr(
        admin_sso,
        "rollback_identity_migration",
        _must_not_rollback,
    )
    response = _client(monkeypatch, DriftAfterIdempotentApply()).post(
        "/api/v1/admin/sso/providers/workforce/identity-migrations/apply",
        json={
            "existing_user_id": str(user_id),
            "upstream_subject": "upstream-subject",
            "broker_subject": "broker-subject",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "identity_prelink_missing"


def test_identity_migration_postcheck_never_removes_a_concurrent_binding(
    monkeypatch,
):
    user_id = uuid.uuid4()
    rollbacks: list[dict[str, object]] = []

    class DriftAfterConcurrentApply(Control):
        def __init__(self) -> None:
            super().__init__()
            self.prelink_reads = 0

        async def verify_identity_prelink(self, *args, **kwargs):
            self.prelink_reads += 1
            if self.prelink_reads == 2:
                raise ProviderControlError("identity_prelink_missing")
            return await super().verify_identity_prelink(*args, **kwargs)

    async def _ready(**values):
        return IdentityMigrationReadback(
            user_id=user_id,
            state="ready_to_link",
            old_issuer=values["old_issuer"],
            new_issuer=values["new_issuer"],
        )

    async def _concurrently_linked(**values):
        return IdentityMigrationReadback(
            user_id=user_id,
            state="linked",
            old_issuer=values["old_issuer"],
            new_issuer=values["new_issuer"],
        )

    async def _rollback(**values):
        rollbacks.append(values)
        return await _ready(**values)

    monkeypatch.setattr(admin_sso, "apply_identity_migration", _concurrently_linked)
    monkeypatch.setattr(admin_sso, "rollback_identity_migration", _rollback)
    response = _client(monkeypatch, DriftAfterConcurrentApply()).post(
        "/api/v1/admin/sso/providers/workforce/identity-migrations/apply",
        json={
            "existing_user_id": str(user_id),
            "upstream_subject": "upstream-subject",
            "broker_subject": "broker-subject",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "identity_prelink_missing"
    assert rollbacks == []


def test_identity_migration_rollback_is_exact_audited_and_idempotent(monkeypatch):
    user_id = uuid.uuid4()
    records: list[dict[str, object]] = []
    rolled_back: list[dict[str, object]] = []

    async def _rollback(**values):
        rolled_back.append(values)
        return IdentityMigrationReadback(
            user_id=user_id,
            state="ready_to_link",
            old_issuer=values["old_issuer"],
            new_issuer=values["new_issuer"],
        )

    monkeypatch.setattr(admin_sso, "rollback_identity_migration", _rollback)
    monkeypatch.setattr(admin_sso.audit_log, "record", lambda **value: records.append(value))
    response = _client(monkeypatch, Control()).post(
        "/api/v1/admin/sso/providers/workforce/identity-migrations/rollback",
        json={
            "existing_user_id": str(user_id),
            "upstream_subject": "upstream-subject",
            "broker_subject": "broker-subject",
        },
    )

    assert response.status_code == 200
    assert response.json()["migration"]["state"] == "ready_to_link"
    assert rolled_back[0]["actor_id"] == "product-admin"
    assert [record["action"] for record in records] == [
        "admin.sso.identity_migration.rollback.requested",
        "admin.sso.identity_migration.rollback",
    ]
    assert "upstream-subject" not in repr(records)
    assert "broker-subject" not in repr(records)


def test_identity_migration_conflicts_are_value_less(monkeypatch):
    async def _inspect(**_values):
        raise IdentityMigrationError("identity_migration_old_binding_missing")

    monkeypatch.setattr(admin_sso, "inspect_identity_migration", _inspect)
    response = _client(monkeypatch, Control()).post(
        "/api/v1/admin/sso/providers/workforce/identity-migrations/preflight",
        json={
            "existing_user_id": str(uuid.uuid4()),
            "upstream_subject": "upstream-subject",
            "broker_subject": "broker-subject",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == ("identity_migration_old_binding_missing")


def test_identity_migration_malformed_subject_type_is_not_echoed(monkeypatch):
    leaked = "subject-value-must-not-be-reflected"
    response = _client(monkeypatch, Control()).post(
        "/api/v1/admin/sso/providers/workforce/identity-migrations/preflight",
        json={
            "existing_user_id": str(uuid.uuid4()),
            "upstream_subject": {"value": leaked},
            "broker_subject": "broker-subject",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "identity_migration_subject_invalid"
    assert leaked not in response.text


def test_identity_migration_missing_subject_does_not_echo_the_other_subject(
    monkeypatch,
):
    leaked = "present-subject-must-not-be-reflected"
    response = _client(monkeypatch, Control()).post(
        "/api/v1/admin/sso/providers/workforce/identity-migrations/preflight",
        json={
            "existing_user_id": str(uuid.uuid4()),
            "upstream_subject": leaked,
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "identity_migration_subject_invalid"
    assert leaked not in response.text


def test_identity_migration_audit_digest_handles_non_utf8_subject():
    assert admin_sso._subject_digest("\ud800") == "<invalid>"


def test_identity_migration_openapi_keeps_all_identifiers_required_strings():
    schema = admin_sso.IdentityMigrationRequest.model_json_schema()

    assert schema["required"] == [
        "existing_user_id",
        "upstream_subject",
        "broker_subject",
    ]
    assert {name: value["type"] for name, value in schema["properties"].items()} == {
        "existing_user_id": "string",
        "upstream_subject": "string",
        "broker_subject": "string",
    }
