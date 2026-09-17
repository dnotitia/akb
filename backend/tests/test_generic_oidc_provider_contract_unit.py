"""Fail-closed contract for standards-based OIDC brokered through Keycloak."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from app.sso.models import ProviderConfigureSpec
from app.sso.providers import generic_oidc
from app.sso.providers.keycloak_oidc import ProviderDefinitionError


_BROKER_BASE_URL = "https://auth.akb.example.com"
_BROKER_ISSUER = f"{_BROKER_BASE_URL}/realms/akb"
_TENANT_ID = "ade9ac17-851e-48d0-ba36-ed99a8d8c07e"
_UPSTREAM_ISSUER = f"https://login.microsoftonline.com/{_TENANT_ID}/v2.0"
_CLIENT_ID = "6fd50bdd-9701-4244-a489-0059e8baba49"
_SECRET = "oidc-client-secret-must-not-leak"  # pragma: allowlist secret


def _spec(**changes: object) -> ProviderConfigureSpec:
    values: dict[str, object] = {
        "provider_type": "oidc",
        "alias": "entra-dn",
        "display_name": "Microsoft Teams",
        "issuer": _UPSTREAM_ISSUER,
        "discovery_url": f"{_UPSTREAM_ISSUER}/.well-known/openid-configuration",
        "client_id": _CLIENT_ID,
        "client_secret": _SECRET,
    }
    values.update(changes)
    return ProviderConfigureSpec(**values)  # type: ignore[arg-type]


def _imported(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "issuer": _UPSTREAM_ISSUER,
        "authorizationUrl": (
            f"https://login.microsoftonline.com/{_TENANT_ID}/oauth2/v2.0/authorize"
        ),
        "tokenUrl": (
            f"https://login.microsoftonline.com/{_TENANT_ID}/oauth2/v2.0/token"
        ),
        "jwksUrl": (
            f"https://login.microsoftonline.com/{_TENANT_ID}/discovery/v2.0/keys"
        ),
        "userInfoUrl": "https://graph.microsoft.com/oidc/userinfo",
        "logoutUrl": (
            f"https://login.microsoftonline.com/{_TENANT_ID}/oauth2/v2.0/logout"
        ),
    }
    values.update(changes)
    return values


def _oidc_readback(*, enabled: bool = False) -> dict[str, object]:
    rendered = generic_oidc.render_representation(
        _spec(),
        _imported(untrustedImportedField="must-not-pass-through"),
        preserve_secret=False,
    )
    config = rendered["config"]
    assert isinstance(config, dict)
    config["clientSecret"] = generic_oidc.MASKED_SECRET
    return generic_oidc.with_enabled(rendered, enabled=enabled)


def _readback(representation: dict[str, object]):
    return generic_oidc.readback(
        representation,
        broker_base_url=_BROKER_BASE_URL,
        broker_issuer=_BROKER_ISSUER,
        realm="akb",
    )


def test_entra_discovery_renders_a_generic_disabled_security_profile():
    spec = generic_oidc.validate_spec(_spec(), broker_issuer=_BROKER_ISSUER)
    rendered = generic_oidc.render_representation(
        spec,
        _imported(untrustedImportedField="must-not-pass-through"),
        preserve_secret=False,
    )

    assert rendered["providerId"] == "oidc"
    assert rendered["enabled"] is False
    assert rendered["hideOnLogin"] is True
    assert rendered["trustEmail"] is True
    config = rendered["config"]
    assert isinstance(config, dict)
    assert config["clientSecret"] == _SECRET
    assert config["clientAuthMethod"] == "client_secret_post"
    assert config["validateSignature"] == "true"
    assert config["useJwksUrl"] == "true"
    assert config["pkceEnabled"] == "true"
    assert config["pkceMethod"] == "S256"
    assert config["syncMode"] == "FORCE"
    assert config["akbProviderType"] == "oidc"
    assert config["userInfoUrl"] == "https://graph.microsoft.com/oidc/userinfo"
    assert "claimFilterName" not in config
    assert "untrustedImportedField" not in config
    assert len(config["akbDiscoveryFingerprint"]) == 64


def test_generic_oidc_accepts_keycloak_discovery_without_a_provider_branch():
    issuer = "https://identity.example.com/realms/workforce"
    spec = generic_oidc.validate_spec(
        _spec(
            issuer=issuer,
            discovery_url=f"{issuer}/.well-known/openid-configuration",
            client_id="akb-broker",
        ),
        broker_issuer=_BROKER_ISSUER,
    )
    rendered = generic_oidc.render_representation(
        spec,
        {
            "issuer": issuer,
            "authorizationUrl": f"{issuer}/protocol/openid-connect/auth",
            "tokenUrl": f"{issuer}/protocol/openid-connect/token",
            "jwksUrl": f"{issuer}/protocol/openid-connect/certs",
            "userInfoUrl": f"{issuer}/protocol/openid-connect/userinfo",
            "logoutUrl": f"{issuer}/protocol/openid-connect/logout",
        },
        preserve_secret=False,
    )

    assert rendered["enabled"] is False
    config = rendered["config"]
    assert isinstance(config, dict)
    assert config["issuer"] == issuer


def test_generic_oidc_preserves_standard_endpoint_query_components():
    spec = generic_oidc.validate_spec(_spec(), broker_issuer=_BROKER_ISSUER)

    rendered = generic_oidc.render_representation(
        spec,
        _imported(
            authorizationUrl=(
                f"https://login.microsoftonline.com/{_TENANT_ID}/oauth2/v2.0/authorize"
                "?slice=workforce"
            )
        ),
        preserve_secret=False,
    )

    config = rendered["config"]
    assert isinstance(config, dict)
    assert config["authorizationUrl"].endswith("?slice=workforce")


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"provider_type": "keycloak-oidc"}, "provider_type_mismatch"),
        ({"issuer": "http://identity.example.com"}, "provider_issuer_invalid"),
        (
            {
                "issuer": f"{_BROKER_ISSUER}/",
                "discovery_url": f"{_BROKER_ISSUER}/.well-known/openid-configuration",
            },
            "provider_issuer_is_broker",
        ),
        (
            {"discovery_url": "https://login.microsoftonline.com/custom"},
            "provider_discovery_url_mismatch",
        ),
        ({"client_id": "client id with spaces"}, "provider_client_id_invalid"),
    ],
)
def test_generic_configuration_rejects_invalid_authority_or_client(changes, code):
    with pytest.raises(ProviderDefinitionError) as captured:
        generic_oidc.validate_spec(_spec(**changes), broker_issuer=_BROKER_ISSUER)

    assert captured.value.code == code
    assert _SECRET not in f"{captured.value!s} {captured.value!r}"


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        (
            {"issuer": "https://different.example.com"},
            "provider_discovery_issuer_mismatch",
        ),
        (
            {"authorizationUrl": "http://identity.example.com/authorize"},
            "provider_discovery_authorization_url_invalid",
        ),
        (
            {"tokenUrl": "not-a-url"},
            "provider_discovery_token_url_invalid",
        ),
        (
            {"jwksUrl": "file:///keys"},
            "provider_discovery_jwks_url_invalid",
        ),
        (
            {"userInfoUrl": "http://graph.example.com/userinfo"},
            "provider_discovery_optional_url_invalid",
        ),
        (
            {"userInfoUrl": "https://graph.example.com/user info"},
            "provider_discovery_optional_url_invalid",
        ),
        (
            {"userInfoUrl": "https://graph.example.com/userinfo#fragment"},
            "provider_discovery_optional_url_invalid",
        ),
        (
            {"logoutUrl": 7},
            "provider_discovery_optional_url_invalid",
        ),
    ],
)
def test_generic_discovery_rejects_mismatched_issuer_or_non_https_endpoints(changes, code):
    spec = generic_oidc.validate_spec(_spec(), broker_issuer=_BROKER_ISSUER)

    with pytest.raises(ProviderDefinitionError) as captured:
        generic_oidc.render_representation(
            spec,
            _imported(**changes),
            preserve_secret=False,
        )

    assert captured.value.code == code


def test_generic_readback_detects_endpoint_drift_with_the_discovery_fingerprint():
    representation = _oidc_readback(enabled=True)
    config = representation["config"]
    assert isinstance(config, dict)
    config["tokenUrl"] = "https://tokens.example.com/oauth/token"

    assert _readback(representation).state == "configuration_error"


def test_generic_readback_recognizes_only_exact_disabled_and_enabled_profiles():
    disabled = _readback(_oidc_readback())
    enabled = _readback(_oidc_readback(enabled=True))

    assert disabled.state == "configured_disabled"
    assert enabled.state == "enabled"
    assert enabled.client_secret_configured is True
    assert enabled.redirect_uri == (
        "https://auth.akb.example.com/realms/akb/broker/entra-dn/endpoint"
    )
    assert enabled.post_logout_redirect_uri == (
        "https://auth.akb.example.com/realms/akb/broker/entra-dn/endpoint/logout_response"
    )
    assert enabled.supports_logout is True
    assert enabled.supports_identity_migration is False


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("providerId",), "saml"),
        (("trustEmail",), False),
        (("config", "clientAuthMethod"), "client_secret_basic"),
        (("config", "validateSignature"), "false"),
        (("config", "akbDiscoveryFingerprint"), "0" * 64),
        (("config", "clientSecret"), ""),
    ],
)
def test_generic_readback_fails_closed_on_profile_drift(path, value):
    representation = _oidc_readback(enabled=True)
    target = representation
    for key in path[:-1]:
        nested = target[key]
        assert isinstance(nested, dict)
        target = nested
    target[path[-1]] = value

    assert _readback(representation).state == "configuration_error"


def test_generic_reconfigure_preserves_only_keycloaks_masked_secret_sentinel():
    rendered = generic_oidc.render_representation(
        replace(_spec(), client_secret=None),
        _imported(),
        preserve_secret=True,
    )

    config = rendered["config"]
    assert isinstance(config, dict)
    assert config["clientSecret"] == generic_oidc.MASKED_SECRET


def test_generic_readback_is_secret_free_and_does_not_mutate_representation():
    representation = _oidc_readback(enabled=True)
    before = deepcopy(representation)

    provider = _readback(representation)
    rendered = repr(
        (
            provider.admin_view(),
            provider.public_view(login_url="/login"),
            provider.audit_view(),
        )
    )

    assert representation == before
    assert _SECRET not in rendered
    assert generic_oidc.MASKED_SECRET not in rendered
