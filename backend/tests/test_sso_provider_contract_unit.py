"""Pure contracts for the built-in SSO provider contribution boundary."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from app.sso.models import ProviderConfigureSpec
from app.sso.providers import generic_oidc, keycloak_oidc
from app.sso.providers.keycloak_oidc import ProviderDefinitionError
from app.sso.registry import provider_definition, provider_types


_BROKER_BASE_URL = "https://auth.akb.example.com"
_BROKER_ISSUER = f"{_BROKER_BASE_URL}/realms/akb"
_UPSTREAM_ISSUER = "https://accounts.example.com/realms/workforce"
_SECRET = "upstream-client-secret-must-not-leak"  # pragma: allowlist secret


def _spec(**changes: object) -> ProviderConfigureSpec:
    values: dict[str, object] = {
        "provider_type": "keycloak-oidc",
        "alias": "workforce",
        "display_name": "Company SSO",
        "issuer": _UPSTREAM_ISSUER,
        "discovery_url": f"{_UPSTREAM_ISSUER}/.well-known/openid-configuration",
        "client_id": "akb-broker",
        "client_secret": _SECRET,
    }
    values.update(changes)
    return ProviderConfigureSpec(**values)  # type: ignore[arg-type]


def _imported(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "issuer": _UPSTREAM_ISSUER,
        "authorizationUrl": f"{_UPSTREAM_ISSUER}/protocol/openid-connect/auth",
        "tokenUrl": f"{_UPSTREAM_ISSUER}/protocol/openid-connect/token",
        "jwksUrl": f"{_UPSTREAM_ISSUER}/protocol/openid-connect/certs",
        "userInfoUrl": f"{_UPSTREAM_ISSUER}/protocol/openid-connect/userinfo",
        "logoutUrl": f"{_UPSTREAM_ISSUER}/protocol/openid-connect/logout",
    }
    values.update(changes)
    return values


def _keycloak_readback(*, enabled: bool = False) -> dict[str, object]:
    rendered = keycloak_oidc.render_representation(
        _spec(),
        _imported(untrustedImportedField="must-not-pass-through"),
        preserve_secret=False,
    )
    config = rendered["config"]
    assert isinstance(config, dict)
    config["clientSecret"] = keycloak_oidc.MASKED_SECRET
    return keycloak_oidc.with_enabled(rendered, enabled=enabled)


def _readback(representation: dict[str, object]):
    return keycloak_oidc.readback(
        representation,
        broker_base_url=_BROKER_BASE_URL,
        broker_issuer=_BROKER_ISSUER,
        realm="akb",
    )


def test_registry_is_explicit_and_contains_only_the_reference_provider():
    assert provider_types() == ("keycloak-oidc", "oidc")
    definition = provider_definition("keycloak-oidc")
    assert definition.provider_type == "keycloak-oidc"
    assert definition.module is keycloak_oidc

    oidc_definition = provider_definition("oidc")
    assert oidc_definition.provider_type == "oidc"
    assert oidc_definition.module is generic_oidc

    with pytest.raises(ValueError, match="unsupported_sso_provider_type"):
        provider_definition("arbitrary-keycloak-json")


def test_valid_spec_renders_a_disabled_allowlisted_representation():
    spec = keycloak_oidc.validate_spec(_spec(), broker_issuer=_BROKER_ISSUER)
    rendered = keycloak_oidc.render_representation(
        spec,
        _imported(untrustedImportedField="must-not-pass-through"),
        preserve_secret=False,
    )

    assert rendered["providerId"] == "oidc"
    assert rendered["enabled"] is False
    assert rendered["hideOnLogin"] is True
    assert rendered["trustEmail"] is True
    assert rendered["storeToken"] is False
    config = rendered["config"]
    assert isinstance(config, dict)
    assert config["clientSecret"] == _SECRET
    assert config["clientAuthMethod"] == "client_secret_basic"
    assert config["validateSignature"] == "true"
    assert config["useJwksUrl"] == "true"
    assert config["pkceEnabled"] == "true"
    assert config["pkceMethod"] == "S256"
    assert config["syncMode"] == "FORCE"
    assert config["filteredByClaim"] == "true"
    assert config["claimFilterName"] == "email_verified"
    assert config["claimFilterValue"] == "true"
    assert config["akbProviderType"] == "keycloak-oidc"
    assert "untrustedImportedField" not in config


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"provider_type": "oidc"}, "provider_type_mismatch"),
        ({"alias": "../workforce"}, "provider_alias_invalid"),
        ({"issuer": "http://accounts.example.com"}, "provider_issuer_invalid"),
        (
            {"issuer": f"{_UPSTREAM_ISSUER}?tenant=one"},
            "provider_issuer_invalid",
        ),
        (
            {
                "issuer": f"{_BROKER_ISSUER}/",
                "discovery_url": f"{_BROKER_ISSUER}/.well-known/openid-configuration",
            },
            "provider_issuer_is_broker",
        ),
        (
            {
                "issuer": "https://AUTH.AKB.EXAMPLE.COM:443/realms/akb",
                "discovery_url": (
                    "https://AUTH.AKB.EXAMPLE.COM:443/realms/akb/"
                    ".well-known/openid-configuration"
                ),
            },
            "provider_issuer_is_broker",
        ),
        (
            {"discovery_url": "https://accounts.example.com/custom-discovery"},
            "provider_discovery_url_mismatch",
        ),
        ({"client_id": "client id with spaces"}, "provider_client_id_invalid"),
    ],
)
def test_invalid_specs_are_rejected_with_value_less_codes(changes, code):
    with pytest.raises(ProviderDefinitionError) as captured:
        keycloak_oidc.validate_spec(_spec(**changes), broker_issuer=_BROKER_ISSUER)

    assert captured.value.code == code
    assert _SECRET not in f"{captured.value!s} {captured.value!r}"


def test_discovery_issuer_must_exactly_match_configured_issuer():
    spec = keycloak_oidc.validate_spec(_spec(), broker_issuer=_BROKER_ISSUER)

    with pytest.raises(ProviderDefinitionError) as captured:
        keycloak_oidc.render_representation(
            spec,
            _imported(issuer="https://different.example.com/realms/workforce"),
            preserve_secret=False,
        )

    assert captured.value.code == "provider_discovery_issuer_mismatch"


def test_keycloak_discovery_endpoints_must_match_the_exact_issuer():
    spec = keycloak_oidc.validate_spec(_spec(), broker_issuer=_BROKER_ISSUER)

    with pytest.raises(ProviderDefinitionError) as captured:
        keycloak_oidc.render_representation(
            spec,
            _imported(tokenUrl="https://tokens.example.net/oauth/token"),
            preserve_secret=False,
        )

    assert captured.value.code == "provider_discovery_token_url_invalid"


def test_trailing_slash_issuer_is_canonicalized_before_discovery_readback():
    spec = keycloak_oidc.validate_spec(
        _spec(
            issuer=f"{_UPSTREAM_ISSUER}/",
            discovery_url=f"{_UPSTREAM_ISSUER}/.well-known/openid-configuration",
        ),
        broker_issuer=_BROKER_ISSUER,
    )

    assert spec.issuer == _UPSTREAM_ISSUER
    assert keycloak_oidc.render_representation(
        spec,
        _imported(),
        preserve_secret=False,
    )["enabled"] is False


def test_reconfigure_uses_only_keycloaks_masked_secret_sentinel():
    spec_without_secret = replace(_spec(), client_secret=None)
    rendered = keycloak_oidc.render_representation(
        spec_without_secret,
        _imported(),
        preserve_secret=True,
    )

    config = rendered["config"]
    assert isinstance(config, dict)
    assert config["clientSecret"] == keycloak_oidc.MASKED_SECRET


def test_client_secret_is_validated_but_never_trimmed_or_unicode_normalized():
    opaque = " leading-e\u0301-trailing "

    validated = keycloak_oidc.validate_spec(
        _spec(client_secret=opaque),
        broker_issuer=_BROKER_ISSUER,
    )

    assert validated.client_secret == opaque


def test_client_secret_rejects_control_characters():
    with pytest.raises(ProviderDefinitionError) as captured:
        keycloak_oidc.validate_spec(
            _spec(client_secret="line-one\nline-two"),  # pragma: allowlist secret
            broker_issuer=_BROKER_ISSUER,
        )

    assert captured.value.code == "provider_client_secret_invalid"


def test_readback_recognizes_only_exact_disabled_and_enabled_profiles():
    disabled = _readback(_keycloak_readback())
    enabled = _readback(_keycloak_readback(enabled=True))

    assert disabled.state == "configured_disabled"
    assert enabled.state == "enabled"
    assert enabled.client_secret_configured is True
    assert enabled.redirect_uri == (
        "https://auth.akb.example.com/realms/akb/broker/workforce/endpoint"
    )
    assert enabled.post_logout_redirect_uri == (
        "https://auth.akb.example.com/realms/akb/broker/workforce/endpoint/logout_response"
    )


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("providerId",), "saml"),
        (("trustEmail",), False),
        (("hideOnLogin",), True),
        (("config", "validateSignature"), "false"),
        (("config", "syncMode"), "IMPORT"),
        (("config", "filteredByClaim"), "false"),
        (("config", "claimFilterName"), "email"),
        (("config", "claimFilterValue"), "false"),
        (("config", "authorizationUrl"), "http://accounts.example.com/auth"),
        (("config", "metadataDescriptorUrl"), "https://accounts.example.com/custom"),
        (("config", "clientSecret"), ""),
    ],
)
def test_readback_fails_closed_on_profile_drift(path, value):
    representation = _keycloak_readback(enabled=True)
    target = representation
    for key in path[:-1]:
        nested = target[key]
        assert isinstance(nested, dict)
        target = nested
    target[path[-1]] = value

    assert _readback(representation).state == "configuration_error"


def test_views_are_bounded_and_never_expose_the_client_secret():
    representation = _keycloak_readback(enabled=True)
    provider = _readback(representation)
    views = (
        provider.admin_view(),
        provider.public_view(login_url="/api/v1/auth/sso/workforce/login"),
        provider.audit_view(),
    )

    rendered = repr(views)
    assert _SECRET not in rendered
    assert keycloak_oidc.MASKED_SECRET not in rendered
    assert set(views[1]) == {
        "provider_type",
        "alias",
        "display_name",
        "login_url",
    }


def test_secret_bearing_specs_do_not_render_secrets_in_repr():
    assert _SECRET not in repr(_spec())


def test_readback_does_not_mutate_the_keycloak_representation():
    representation = _keycloak_readback(enabled=True)
    before = deepcopy(representation)

    _readback(representation)

    assert representation == before


def test_drifted_unvalidated_values_are_not_reflected_to_admin_or_audit_views():
    representation = _keycloak_readback(enabled=True)
    representation["displayName"] = "unsafe\nlabel"
    config = representation["config"]
    assert isinstance(config, dict)
    config["issuer"] = (
        "https://user:drift-secret@example.com/realms/workforce"  # pragma: allowlist secret
    )

    provider = _readback(representation)
    rendered = repr((provider.admin_view(), provider.audit_view()))

    assert provider.state == "configuration_error"
    assert provider.display_name == "workforce"
    assert provider.issuer is None
    assert "drift-secret" not in rendered
    assert "unsafe" not in rendered


def test_only_complete_bounded_keycloak_vault_references_count_as_secrets():
    representation = _keycloak_readback(enabled=True)
    config = representation["config"]
    assert isinstance(config, dict)
    config["clientSecret"] = "${vault.upstream-client}"
    assert _readback(representation).client_secret_configured is True

    config["clientSecret"] = "${vault.unclosed"
    assert _readback(representation).state == "configuration_error"
    assert _readback(representation).client_secret_configured is False
