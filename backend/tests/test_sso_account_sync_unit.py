"""Only complete, exact-authority management reads can drive account sync."""
import httpx
import pytest

from app.sso.keycloak_admin import KeycloakAdminConfig, KeycloakProviderControl, ProviderControlError


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["active", "disabled", "missing", "wrong_realm", "realm404", "denied", "wrong_user", "malformed"])
async def test_exact_managed_account_read_is_fail_closed(mode):
    calls = []

    def respond(request):
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "fixture-token"})
        if request.url.path.endswith("/realms/akb"):
            if mode == "realm404":
                return httpx.Response(404)
            return httpx.Response(200, json={"realm": "wrong" if mode == "wrong_realm" else "akb", "enabled": True})
        assert request.url.path.endswith("/users/subject")
        if mode in {"missing", "denied"}:
            return httpx.Response(404 if mode == "missing" else 403)
        return httpx.Response(200, json={"id": "other" if mode == "wrong_user" else "subject",
            "enabled": "false" if mode == "malformed" else mode != "disabled"})

    config = KeycloakAdminConfig("https://internal.example.test", "https://sso.example.test", "akb",
        "manager", "fixture-secret", True)  # pragma: allowlist secret
    control = KeycloakProviderControl(config, transport=httpx.MockTransport(respond))
    if mode in {"active", "disabled", "missing"}:
        assert await control.read_managed_account_states(("subject",)) == {"subject": mode}
    else:
        with pytest.raises(ProviderControlError):
            await control.read_managed_account_states(("subject",))
    assert all(method == "GET" or path.endswith("/token") for method, path in calls)
    if mode in {"realm404", "wrong_realm"}:
        assert not any("/users/" in path for _, path in calls)


@pytest.mark.asyncio
async def test_failed_second_user_discards_partial_page():
    def respond(request):
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "fixture-token"})
        if request.url.path.endswith("/realms/akb"):
            return httpx.Response(200, json={"realm": "akb", "enabled": True})
        return httpx.Response(404 if request.url.path.endswith("/first") else 503)
    control = KeycloakProviderControl(KeycloakAdminConfig("https://internal.example.test",
        "https://sso.example.test", "akb", "manager", "fixture-secret", True),  # pragma: allowlist secret
        transport=httpx.MockTransport(respond))
    with pytest.raises(ProviderControlError):
        await control.read_managed_account_states(("first", "second"))
