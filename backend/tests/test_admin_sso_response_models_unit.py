"""The declared shapes must equal the projections the handlers build.

`response_model` FILTERS. A field the model does not declare is dropped from
the response silently — in a browser and in the control plane at once — which
is why these payloads were left untyped until a second consumer appeared
(#455). Reading the models for plausibility is not enough: this compares each
one against the dict the code actually produces, key for key, so a model that
drifts from its projection fails here rather than in somebody's console.
"""

from __future__ import annotations

import uuid

from app.models.admin_sso import (
    SsoIdentityMigrationResponse,
    SsoIdentityMigrationView,
    SsoIdentityPrelinkView,
    SsoProviderCapabilities,
    SsoProviderCatalogResponse,
    SsoProviderMutationResponse,
    SsoProviderView,
)
from app.sso.identity_migration import IdentityMigrationReadback
from app.sso.models import IdentityPrelinkReadback, ProviderReadback


def _provider_readback() -> ProviderReadback:
    return ProviderReadback(
        provider_type="keycloak-oidc",
        alias="platform",
        display_name="Platform",
        state="enabled",
        enabled=True,
        issuer="https://id.example.com/realms/workforce",
        discovery_url="https://id.example.com/realms/workforce/.well-known/openid-configuration",
        client_id="akb-workspace-broker",
        client_secret_configured=True,
        redirect_uri="https://auth.example.com/realms/akb/broker/platform/endpoint",
        post_logout_redirect_uri="https://auth.example.com/realms/akb/broker/platform/endpoint/logout_response",
        supports_logout=True,
        supports_identity_migration=True,
    )


def test_the_provider_view_declares_exactly_what_admin_view_projects():
    projected = _provider_readback().admin_view()
    assert set(SsoProviderView.model_fields) == set(projected)
    assert set(SsoProviderCapabilities.model_fields) == set(projected["capabilities"])


def test_the_migration_view_declares_exactly_what_admin_view_projects():
    projected = IdentityMigrationReadback(
        user_id=uuid.uuid4(),
        state="linked",
        old_issuer="https://old.example.com/realms/akb",
        new_issuer="https://new.example.com/realms/akb",
    ).admin_view()
    assert set(SsoIdentityMigrationView.model_fields) == set(projected)


def test_the_prelink_view_withholds_the_subjects_and_declares_the_rest():
    prelink = IdentityPrelinkReadback(
        provider_alias="platform",
        provider_state="configured_disabled",
        upstream_issuer="https://id.example.com/realms/workforce",
        broker_issuer="https://auth.example.com/realms/akb",
        broker_subject="broker-subject",
        upstream_subject="upstream-subject",
        broker_username="someone",
    )
    declared = set(SsoIdentityPrelinkView.model_fields)
    # Withheld on purpose: echoing an opaque subject publishes it.
    assert "broker_subject" not in declared
    assert "upstream_subject" not in declared
    assert declared == {
        "provider_alias",
        "provider_state",
        "upstream_issuer",
        "broker_issuer",
        "broker_username",
    }
    for name in declared:
        assert hasattr(prelink, name), f"{name} is declared but the readback has no such value"


def test_a_full_catalog_survives_the_model_unchanged():
    """The end-to-end property that matters: serialising through the declared
    model returns the same document the handler built, not a subset of it."""
    provider = _provider_readback().admin_view()
    envelope = {
        "schema_version": 1,
        "auth_mode": "sso",
        "control_mode": "direct",
        "supported_provider_types": ["keycloak-oidc", "oidc"],
        "providers": [provider],
    }
    assert SsoProviderCatalogResponse.model_validate(envelope).model_dump() == envelope


def test_a_mutation_readback_survives_the_model_unchanged():
    envelope = {"provider": _provider_readback().admin_view()}
    assert SsoProviderMutationResponse.model_validate(envelope).model_dump() == envelope


def test_a_migration_response_survives_the_model_unchanged():
    envelope = {
        "schema_version": 1,
        "prelink": {
            "provider_alias": "platform",
            "provider_state": "configured_disabled",
            "upstream_issuer": "https://id.example.com/realms/workforce",
            "broker_issuer": "https://auth.example.com/realms/akb",
            "broker_username": "someone",
        },
        "migration": {
            "user_id": str(uuid.uuid4()),
            "state": "ready_to_link",
            "old_issuer": "https://old.example.com/realms/akb",
            "new_issuer": "https://new.example.com/realms/akb",
        },
    }
    assert SsoIdentityMigrationResponse.model_validate(envelope).model_dump() == envelope


def test_an_unconfigured_provider_keeps_its_absent_fields_absent():
    """A configuration_error readback carries no issuer. The model must accept
    that without inventing a value, and must not drop the keys either."""
    projected = ProviderReadback(
        provider_type="keycloak-oidc",
        alias="broken",
        display_name="Broken",
        state="configuration_error",
        enabled=False,
        issuer=None,
        discovery_url=None,
        client_id=None,
        client_secret_configured=False,
        redirect_uri="https://auth.example.com/realms/akb/broker/broken/endpoint",
        post_logout_redirect_uri="https://auth.example.com/realms/akb/broker/broken/endpoint/logout_response",
        supports_logout=True,
        supports_identity_migration=True,
    ).admin_view()
    assert SsoProviderView.model_validate(projected).model_dump() == projected


def test_a_field_the_model_does_not_know_is_refused_rather_than_dropped():
    """`extra="forbid"` is the runtime half of this file.

    Without it, a handler that grows a key ships a response missing that key
    and nobody is told — the silent filtering that made typing these routes
    risky in the first place. With it, the same mistake is a loud failure at
    response time, which is the trade this whole change is making.
    """
    import pytest
    from pydantic import ValidationError

    envelope = {
        "schema_version": 1,
        "auth_mode": "sso",
        "control_mode": "direct",
        "supported_provider_types": ["keycloak-oidc"],
        "providers": [],
        "something_a_handler_grew_later": True,
    }
    with pytest.raises(ValidationError):
        SsoProviderCatalogResponse.model_validate(envelope)


def test_every_route_on_this_surface_declares_a_response_model():
    """The next route added here is typed, or this fails.

    Typing six of seven would leave the surface in the state it was already in:
    the document advertises an operation and describes none of its payload, and
    the next consumer transcribes the shape by hand.
    """
    from fastapi.routing import APIRoute

    from app.api.routes.admin_sso import router

    untyped = [
        f"{sorted(route.methods)[0]} {route.path}"
        for route in router.routes
        if isinstance(route, APIRoute) and route.response_model is None
    ]
    assert untyped == [], f"routes on the managed SSO admin surface with no declared response: {untyped}"
