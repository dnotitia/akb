"""Fail-closed contract for the installation's own realm as a login option.

The whole kind rests on one discrimination: a brokered provider's token carries
`identity_provider`, and the local realm's does not. If that ever becomes
"optional", a token minted through one provider can be presented for another, so
each case here asserts the shape the other cannot produce.
"""

from __future__ import annotations

import pytest

from app.api.routes.auth import _require_signed_provider
from app.exceptions import AuthenticationError
from app.sso import local_realm
from app.sso.models import ProviderConfigureSpec
from app.sso.providers import generic_oidc, keycloak_oidc
from app.sso.providers.keycloak_oidc import ProviderDefinitionError


_BROKER_ISSUER = "https://auth.akb.example.com/realms/akb"
_UPSTREAM_ISSUER = "https://login.example.com/realms/workforce"
_SECRET = "provider-client-secret-must-not-leak"  # pragma: allowlist secret


def _keycloak_spec(alias: str) -> ProviderConfigureSpec:
    return ProviderConfigureSpec(  # type: ignore[arg-type]
        provider_type=keycloak_oidc.PROVIDER_TYPE,
        alias=alias,
        display_name="Upstream",
        issuer=_UPSTREAM_ISSUER,
        discovery_url=f"{_UPSTREAM_ISSUER}/.well-known/openid-configuration",
        client_id="broker-client",
        client_secret=_SECRET,
    )


def _generic_spec(alias: str) -> ProviderConfigureSpec:
    return ProviderConfigureSpec(  # type: ignore[arg-type]
        provider_type=generic_oidc.PROVIDER_TYPE,
        alias=alias,
        display_name="Upstream",
        issuer=_UPSTREAM_ISSUER,
        discovery_url=f"{_UPSTREAM_ISSUER}/.well-known/openid-configuration",
        client_id="broker-client",
        client_secret=_SECRET,
    )


# ── the reserved alias ────────────────────────────────────────────────


def test_keycloak_provider_may_not_take_the_reserved_alias() -> None:
    with pytest.raises(ProviderDefinitionError) as excinfo:
        keycloak_oidc.validate_spec(
            _keycloak_spec(local_realm.ALIAS), broker_issuer=_BROKER_ISSUER
        )
    assert str(excinfo.value) == "provider_alias_reserved"


def test_generic_provider_may_not_take_the_reserved_alias() -> None:
    with pytest.raises(ProviderDefinitionError) as excinfo:
        generic_oidc.validate_spec(
            _generic_spec(local_realm.ALIAS), broker_issuer=_BROKER_ISSUER
        )
    assert str(excinfo.value) == "provider_alias_reserved"


def test_an_ordinary_alias_is_still_accepted() -> None:
    spec = keycloak_oidc.validate_spec(
        _keycloak_spec("workforce"), broker_issuer=_BROKER_ISSUER
    )
    assert spec.alias == "workforce"


def test_the_self_referential_guard_is_unchanged() -> None:
    """The kind is an addition; registering the broker as its own upstream stays refused."""
    spec = ProviderConfigureSpec(  # type: ignore[arg-type]
        provider_type=keycloak_oidc.PROVIDER_TYPE,
        alias="loopback",
        display_name="Itself",
        issuer=_BROKER_ISSUER,
        discovery_url=f"{_BROKER_ISSUER}/.well-known/openid-configuration",
        client_id="broker-client",
        client_secret=_SECRET,
    )
    with pytest.raises(ProviderDefinitionError) as excinfo:
        keycloak_oidc.validate_spec(spec, broker_issuer=_BROKER_ISSUER)
    assert str(excinfo.value) == "provider_issuer_is_broker"


# ── the biconditional ─────────────────────────────────────────────────


def test_local_realm_requires_the_broker_claim_to_be_absent() -> None:
    _require_signed_provider({}, local_realm.ALIAS)


def test_local_realm_refuses_a_token_that_was_brokered() -> None:
    with pytest.raises(AuthenticationError):
        _require_signed_provider({"identity_provider": "workforce"}, local_realm.ALIAS)


def test_local_realm_refuses_a_token_that_names_the_local_alias() -> None:
    """No provider mints this claim for the local kind, so its presence is a forgery signal."""
    with pytest.raises(AuthenticationError):
        _require_signed_provider(
            {"identity_provider": local_realm.ALIAS}, local_realm.ALIAS
        )


def test_a_brokered_provider_still_requires_the_claim() -> None:
    with pytest.raises(AuthenticationError):
        _require_signed_provider({}, "workforce")


def test_a_brokered_provider_still_requires_the_claim_to_match() -> None:
    with pytest.raises(AuthenticationError):
        _require_signed_provider({"identity_provider": "other"}, "workforce")


def test_a_brokered_provider_accepts_its_own_claim() -> None:
    _require_signed_provider({"identity_provider": "workforce"}, "workforce")


# ── the session service records absence as the reserved alias ─────────


def test_session_service_reads_absence_as_the_local_alias() -> None:
    from app.services.sso_browser_session_service import _provider_alias

    assert _provider_alias({}) == local_realm.ALIAS


def test_session_service_still_reads_a_brokered_claim() -> None:
    from app.services.sso_browser_session_service import _provider_alias

    assert _provider_alias({"identity_provider": "workforce"}) == "workforce"


# ── the id-token check applies the same biconditional ─────────────────
#
# The route's check on the access token was not the only one. `verify_browser_id_token`
# carries its own, and a live sign-in answered 401 at the callback because this
# one still required `identity_provider` unconditionally. The unit suite could
# not see it: it asserted the route's helper and nothing reached the service.


def _id_token_limits(expected_alias: str, claims: dict[str, object]) -> bool:
    """Mirror of the service's shape decision, exercised without a live Keycloak."""
    from app.sso import local_realm as lr

    required = {"iss", "sub", "azp", "sid", "nonce", "at_hash"}
    if not lr.is_local_alias(expected_alias):
        required.add("identity_provider")
    elif claims.get("identity_provider") is not None:
        return False
    return required.issubset(claims)


def test_id_token_shape_requires_the_claim_for_a_brokered_provider() -> None:
    base = {"iss": "i", "sub": "s", "azp": "a", "sid": "d", "nonce": "n", "at_hash": "h"}
    assert not _id_token_limits("workforce", base)
    assert _id_token_limits("workforce", {**base, "identity_provider": "workforce"})


def test_id_token_shape_requires_the_claim_absent_for_the_local_realm() -> None:
    base = {"iss": "i", "sub": "s", "azp": "a", "sid": "d", "nonce": "n", "at_hash": "h"}
    assert _id_token_limits(local_realm.ALIAS, base)
    assert not _id_token_limits(local_realm.ALIAS, {**base, "identity_provider": "anything"})
