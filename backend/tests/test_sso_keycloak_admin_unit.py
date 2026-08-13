"""State-transition and transport contracts for runtime IdP control."""

from __future__ import annotations

from copy import deepcopy
import json

import httpx
import pytest

from app.sso.keycloak_admin import (
    KeycloakAdminConfig,
    KeycloakProviderControl,
    ProviderControlError,
)
from app.sso.models import ProviderConfigureSpec
from app.sso.providers.keycloak_oidc import MASKED_SECRET


pytestmark = pytest.mark.asyncio

_SECRET = "upstream-client-secret-must-not-leak"  # pragma: allowlist secret
_NEW_SECRET = "rotated-client-secret-must-not-leak"  # pragma: allowlist secret
_MANAGEMENT_SECRET = "management-secret-must-not-leak"  # pragma: allowlist secret
_ISSUER = "https://accounts.example.com/realms/workforce"


def _config(*, management_secret: str = _MANAGEMENT_SECRET) -> KeycloakAdminConfig:
    return KeycloakAdminConfig(
        internal_base_url="https://keycloak.internal.example.com/auth",
        public_base_url="https://auth.akb.example.com",
        realm="akb",
        management_client_id="akb-sso-manager",
        management_client_secret=management_secret,
        verify_ssl=True,
    )


def _spec(**changes: object) -> ProviderConfigureSpec:
    values: dict[str, object] = {
        "provider_type": "keycloak-oidc",
        "alias": "workforce",
        "display_name": "Company SSO",
        "issuer": _ISSUER,
        "discovery_url": f"{_ISSUER}/.well-known/openid-configuration",
        "client_id": "akb-broker",
        "client_secret": _SECRET,
    }
    values.update(changes)
    return ProviderConfigureSpec(**values)  # type: ignore[arg-type]


class KeycloakFixture:
    def __init__(self) -> None:
        self.providers: dict[str, dict[str, object]] = {}
        self.requests: list[tuple[str, str]] = []
        self.fail_admin = False
        self.drift_readback_after_write = False
        self._drift_readback = False

    def _readback(self, provider: dict[str, object]) -> dict[str, object]:
        result = deepcopy(provider)
        config = result.get("config")
        assert isinstance(config, dict)
        if config.get("clientSecret"):
            config["clientSecret"] = MASKED_SECRET
        if self._drift_readback:
            result["displayName"] = "Unexpected SSO"
        return result

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path))
        if path == "/auth/realms/akb/protocol/openid-connect/token":
            form = dict(item.split("=", 1) for item in request.content.decode().split("&"))
            assert form["grant_type"] == "client_credentials"
            return httpx.Response(200, json={"access_token": "opaque-management-token"})
        if self.fail_admin:
            return httpx.Response(500, text=f"never expose {_SECRET} {_MANAGEMENT_SECRET}")
        if path == "/auth/admin/realms/akb/identity-provider/import-config":
            body = json.loads(request.content)
            assert body == {
                "providerId": "oidc",
                "fromUrl": f"{_ISSUER}/.well-known/openid-configuration",
            }
            return httpx.Response(
                200,
                json={
                    "issuer": _ISSUER,
                    "authorizationUrl": f"{_ISSUER}/protocol/openid-connect/auth",
                    "tokenUrl": f"{_ISSUER}/protocol/openid-connect/token",
                    "jwksUrl": f"{_ISSUER}/protocol/openid-connect/certs",
                    "userInfoUrl": f"{_ISSUER}/protocol/openid-connect/userinfo",
                    "logoutUrl": f"{_ISSUER}/protocol/openid-connect/logout",
                    "untrustedImportedField": "must-not-pass-through",
                },
            )
        collection = "/auth/admin/realms/akb/identity-provider/instances"
        if path == collection and request.method == "GET":
            return httpx.Response(
                200,
                json=[self._readback(provider) for provider in self.providers.values()],
            )
        if path == collection and request.method == "POST":
            provider = json.loads(request.content)
            alias = provider["alias"]
            if alias in self.providers:
                return httpx.Response(409)
            self.providers[alias] = provider
            if self.drift_readback_after_write:
                self._drift_readback = True
            return httpx.Response(201)
        prefix = f"{collection}/"
        if path.startswith(prefix):
            alias = path.removeprefix(prefix)
            provider = self.providers.get(alias)
            if request.method == "GET":
                return (
                    httpx.Response(404)
                    if provider is None
                    else httpx.Response(200, json=self._readback(provider))
                )
            if request.method == "PUT":
                if provider is None:
                    return httpx.Response(404)
                updated = json.loads(request.content)
                updated_config = updated.get("config")
                old_config = provider.get("config")
                assert isinstance(updated_config, dict)
                assert isinstance(old_config, dict)
                if updated_config.get("clientSecret") == MASKED_SECRET:
                    updated_config["clientSecret"] = old_config["clientSecret"]
                self.providers[alias] = updated
                if self.drift_readback_after_write:
                    self._drift_readback = True
                return httpx.Response(204)
        raise AssertionError(f"unexpected request: {request.method} {path}")


def _control(fixture: KeycloakFixture, **kwargs) -> KeycloakProviderControl:
    return KeycloakProviderControl(
        _config(),
        transport=httpx.MockTransport(fixture.handler),
        **kwargs,
    )


async def test_management_secret_is_excluded_from_config_repr():
    assert _MANAGEMENT_SECRET not in repr(_config())


async def test_configure_creates_disabled_then_exactly_reads_back():
    fixture = KeycloakFixture()
    control = _control(fixture)

    mutation = await control.configure(_spec())
    provider = mutation.after

    assert mutation.before is None
    assert provider.state == "configured_disabled"
    assert provider.client_secret_configured is True
    stored = fixture.providers["workforce"]
    assert stored["enabled"] is False
    assert stored["hideOnLogin"] is True
    config = stored["config"]
    assert isinstance(config, dict)
    assert config["clientSecret"] == _SECRET
    assert "untrustedImportedField" not in config


async def test_configure_rejects_a_valid_but_non_exact_readback():
    fixture = KeycloakFixture()
    fixture.drift_readback_after_write = True
    control = _control(fixture)

    with pytest.raises(ProviderControlError) as captured:
        await control.configure(_spec())

    assert captured.value.code == "keycloak_provider_readback_failed"


async def test_new_provider_requires_a_write_only_secret_before_any_import():
    fixture = KeycloakFixture()
    control = _control(fixture)

    with pytest.raises(ProviderControlError) as captured:
        await control.configure(_spec(client_secret=None))

    assert captured.value.code == "provider_client_secret_required"
    assert all("import-config" not in path for _, path in fixture.requests)


async def test_existing_unmanaged_alias_is_never_adopted_or_overwritten():
    fixture = KeycloakFixture()
    fixture.providers["workforce"] = {
        "alias": "workforce",
        "providerId": "oidc",
        "enabled": True,
        "config": {"clientSecret": "pre-existing"},  # pragma: allowlist secret
    }
    control = _control(fixture)

    with pytest.raises(ProviderControlError) as captured:
        await control.configure(_spec())

    assert captured.value.code == "provider_alias_conflict"
    assert fixture.providers["workforce"]["enabled"] is True


async def test_enabled_provider_must_be_disabled_before_reconfiguration():
    fixture = KeycloakFixture()
    control = _control(fixture)
    await control.configure(_spec())
    await control.set_enabled("workforce", enabled=True)

    with pytest.raises(ProviderControlError) as captured:
        await control.configure(_spec(display_name="Renamed SSO", client_secret=None))

    assert captured.value.code == "provider_disable_before_reconfigure"


async def test_reconfigure_preserves_secret_or_rotates_it_explicitly():
    fixture = KeycloakFixture()
    control = _control(fixture)
    await control.configure(_spec())

    preserved = (await control.configure(
        _spec(display_name="Renamed SSO", client_secret=None)
    )).after
    config = fixture.providers["workforce"]["config"]
    assert isinstance(config, dict)
    assert config["clientSecret"] == _SECRET
    assert preserved.display_name == "Renamed SSO"

    rotated = (await control.configure(_spec(client_secret=_NEW_SECRET))).after
    config = fixture.providers["workforce"]["config"]
    assert isinstance(config, dict)
    assert config["clientSecret"] == _NEW_SECRET
    assert rotated.state == "configured_disabled"


async def test_enable_disable_are_read_back_and_idempotent():
    fixture = KeycloakFixture()
    control = _control(fixture)
    await control.configure(_spec())

    enabled_mutation = await control.set_enabled("workforce", enabled=True)
    enabled = enabled_mutation.after
    enabled_again = (await control.set_enabled("workforce", enabled=True)).after
    disabled = (await control.set_enabled("workforce", enabled=False)).after

    assert enabled_mutation.before is not None
    assert enabled_mutation.before.state == "configured_disabled"
    assert enabled.state == enabled_again.state == "enabled"
    assert disabled.state == "configured_disabled"
    assert fixture.providers["workforce"]["enabled"] is False
    assert fixture.providers["workforce"]["hideOnLogin"] is True


async def test_toggle_rejects_identity_configuration_drift_during_write():
    fixture = KeycloakFixture()
    control = _control(fixture)
    await control.configure(_spec())
    fixture.drift_readback_after_write = True

    with pytest.raises(ProviderControlError) as captured:
        await control.set_enabled("workforce", enabled=True)

    assert captured.value.code == "keycloak_provider_readback_failed"


async def test_configuration_error_cannot_be_enabled_but_can_be_forced_hidden():
    fixture = KeycloakFixture()
    control = _control(fixture)
    await control.configure(_spec())
    config = fixture.providers["workforce"]["config"]
    assert isinstance(config, dict)
    config["validateSignature"] = "false"

    with pytest.raises(ProviderControlError) as captured:
        await control.set_enabled("workforce", enabled=True)
    assert captured.value.code == "provider_configuration_invalid"

    disabled = (await control.set_enabled("workforce", enabled=False)).after
    assert disabled.state == "configuration_error"
    assert fixture.providers["workforce"]["enabled"] is False
    assert fixture.providers["workforce"]["hideOnLogin"] is True


async def test_catalog_uses_bounded_stale_cache_but_admin_refresh_fails_closed():
    fixture = KeycloakFixture()
    now = [100.0]
    control = _control(fixture, monotonic=lambda: now[0])
    await control.configure(_spec())
    await control.set_enabled("workforce", enabled=True)
    assert [item.alias for item in await control.list_providers()] == ["workforce"]

    fixture.fail_admin = True
    now[0] += 16
    stale = await control.list_providers(allow_stale=True)
    assert [item.alias for item in stale] == ["workforce"]

    with pytest.raises(ProviderControlError) as captured:
        await control.list_providers(force_refresh=True)
    assert captured.value.code == "keycloak_provider_list_failed"

    now[0] += 61
    with pytest.raises(ProviderControlError):
        await control.list_providers(allow_stale=True)


async def test_catalog_accepts_exact_limit_and_rejects_truncation():
    fixture = KeycloakFixture()
    control = _control(fixture)
    await control.configure(_spec())
    template = fixture.providers.pop("workforce")
    for index in range(100):
        alias = f"workforce-{index:03d}"
        provider = deepcopy(template)
        provider["alias"] = alias
        provider["displayName"] = alias
        fixture.providers[alias] = provider

    assert len(await control.list_providers(force_refresh=True)) == 100

    overflow = deepcopy(template)
    overflow["alias"] = "workforce-overflow"
    overflow["displayName"] = "workforce-overflow"
    fixture.providers["workforce-overflow"] = overflow
    with pytest.raises(ProviderControlError) as captured:
        await control.list_providers(force_refresh=True)
    assert captured.value.code == "keycloak_provider_catalog_truncated"


async def test_catalog_failure_backoff_prevents_public_retry_stampede():
    fixture = KeycloakFixture()
    now = [100.0]
    control = _control(fixture, monotonic=lambda: now[0])
    fixture.fail_admin = True

    with pytest.raises(ProviderControlError):
        await control.list_providers()
    requests_after_first_failure = list(fixture.requests)

    with pytest.raises(ProviderControlError):
        await control.list_providers()
    assert fixture.requests == requests_after_first_failure

    now[0] += 6
    with pytest.raises(ProviderControlError):
        await control.list_providers()
    assert len(fixture.requests) > len(requests_after_first_failure)


async def test_keycloak_responses_are_bounded_while_streaming():
    def oversized_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/protocol/openid-connect/token"):
            return httpx.Response(200, json={"access_token": "management-token"})
        return httpx.Response(200, content=b"x" * 1_048_577)

    control = KeycloakProviderControl(
        _config(),
        transport=httpx.MockTransport(oversized_handler),
    )

    with pytest.raises(ProviderControlError) as captured:
        await control.list_providers(force_refresh=True)

    assert captured.value.code == "keycloak_provider_list_failed"


async def test_transport_errors_never_include_secrets_or_response_bodies():
    fixture = KeycloakFixture()
    fixture.fail_admin = True
    control = _control(fixture)

    with pytest.raises(ProviderControlError) as captured:
        await control.list_providers(force_refresh=True)

    rendered = f"{captured.value!s} {captured.value!r}"
    assert captured.value.code == "keycloak_provider_list_failed"
    assert all(
        value not in rendered
        for value in (_SECRET, _NEW_SECRET, _MANAGEMENT_SECRET)
    )


async def test_delegated_control_mode_never_attempts_keycloak_management():
    fixture = KeycloakFixture()
    control = KeycloakProviderControl(
        _config(management_secret=""),
        transport=httpx.MockTransport(fixture.handler),
    )

    assert control.control_mode == "delegated"
    with pytest.raises(ProviderControlError) as captured:
        await control.list_providers(force_refresh=True)
    assert captured.value.code == "keycloak_provider_control_delegated"
    assert fixture.requests == []
