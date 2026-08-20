"""Install must leave the recovery administrator reachable by someone.

The account exists so that an installation can be recovered. That is only
true if, when the installer finishes, at least one authority in the system
can either authenticate as it or mint a credential for it. "Holds a
credential" is not the property — an account with no credential is fine so
long as something can still issue one, and an account with a credential is
still lost if nobody holds the plaintext and nothing can reset it.

Each authority below is measured, not asserted from the design: the SSO
authorities come from running the install path against a recording
transport and from reading the management client's own role tuple, and the
local ones from the guards the provisioning and reset paths actually apply.
The assertion is a disjunction, so it fails only when every door is shut.

Limits, stated rather than implied: this measures the authorities THIS
codebase knows about. It cannot see a Keycloak operator acting outside AKB,
and it cannot prove an operator still holds a password the installer
printed once.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.services.standalone_sso_bootstrap import (
    MANAGEMENT_REALM_ROLES,
    StandaloneSSOBootstrapSpec,
    bootstrap_standalone_sso,
)
from app.services.standalone_sso_keycloak import KeycloakStandaloneSSOControl


pytestmark = pytest.mark.asyncio

_PRODUCT_ADMIN_PASSWORD = "one-time-product-admin-password"  # pragma: allowlist secret


def _spec() -> StandaloneSSOBootstrapSpec:
    values = {
        "keycloak_internal_url": "http://keycloak:8080",
        "keycloak_public_url": "https://auth.akb.example.com",
        "realm": "akb",
        "akb_public_url": "https://akb.example.com",
        "bootstrap_client_id": "akb-bootstrap-temporary",
        "bootstrap_client_secret": "bootstrap-secret",  # pragma: allowlist secret
        "management_client_id": "akb-sso-manager",
        "management_client_secret": "management-secret",  # pragma: allowlist secret
        "api_client_id": "akb-web",
        "api_client_secret": "api-secret",  # pragma: allowlist secret
        "admin_client_id": "akb-admin",
        "admin_client_secret": "admin-secret",  # pragma: allowlist secret
        "product_admin_username": "product-admin",
        "product_admin_email": "product-admin@example.com",
    }
    fields = StandaloneSSOBootstrapSpec.__dataclass_fields__
    if "product_admin_password" in fields:
        values["product_admin_password"] = _PRODUCT_ADMIN_PASSWORD
    return StandaloneSSOBootstrapSpec(**values)


async def _install_writes_a_product_admin_credential() -> bool:
    """Run the install's product-admin reconcile against a fake realm.

    The fake stores exactly what the installer sends and answers reads from
    that, so it models an install that mints a credential and one that does
    not, without assuming which.
    """
    realm: dict[str, object] = {"user": None, "credentials": []}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/admin/realms/akb/users" and request.method == "GET":
            user = realm["user"]
            return httpx.Response(200, json=[] if user is None else [user])
        if path == "/admin/realms/akb/users" and request.method == "POST":
            body = json.loads(request.content)
            for credential in body.pop("credentials", None) or ():
                if credential.get("type") == "password" and credential.get("value"):
                    realm["credentials"].append(credential)
            body["id"] = "product-admin-uuid"
            body.setdefault("requiredActions", [])
            realm["user"] = body
            return httpx.Response(201)
        if path.endswith("/reset-password") and request.method == "PUT":
            body = json.loads(request.content)
            if body.get("type") == "password" and body.get("value"):
                realm["credentials"].append(body)
                user = realm["user"]
                if isinstance(user, dict) and body.get("temporary"):
                    user["requiredActions"] = ["UPDATE_PASSWORD"]
            return httpx.Response(204)
        if path.endswith("/credentials"):
            return httpx.Response(
                200,
                json=[{"type": "password"} for _ in realm["credentials"]],
            )
        if path.endswith("/federated-identity"):
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected request: {request.method} {path}")

    control = KeycloakStandaloneSSOControl()
    spec = _spec()
    control._clients[spec.keycloak_internal_url] = httpx.AsyncClient(  # noqa: SLF001
        base_url=spec.keycloak_internal_url,
        transport=httpx.MockTransport(handler),
    )
    try:
        await control._reconcile_product_admin(  # noqa: SLF001
            spec,
            token="opaque-token",  # pragma: allowlist secret
        )
    finally:
        await control.aclose()
    return bool(realm["credentials"])


class _Control:
    """Records which one-time authorities the install leaves usable."""

    def __init__(self, readback):
        self._value = readback
        self.bootstrap_alive = True

    async def acquire_management(self, _spec):
        return "manager-token"

    async def acquire_bootstrap(self, _spec):
        return "bootstrap-token" if self.bootstrap_alive else None

    async def acquire_upgrade(self, _spec):
        return None

    async def reconcile(self, _spec, *, bootstrap_token):
        return self._value

    async def readback(self, _spec, *, management_token):
        return self._value

    async def readback_legacy_v1(self, _spec, *, management_token):
        return self._value

    async def readback_legacy_v2(self, _spec, *, management_token):
        return self._value

    async def readback_callback_migration(self, _spec, **_kwargs):
        return self._value

    async def upgrade_legacy_to_current(self, _spec, *, upgrade_token):
        return self._value

    async def upgrade_callback_to_current(self, _spec, **_kwargs):
        return self._value

    async def retire_bootstrap(self, _spec, *, bootstrap_token):
        self.bootstrap_alive = False

    async def assert_bootstrap_retired(self, _spec, *, bootstrap_token):
        assert self.bootstrap_alive is False

    async def retire_upgrade(self, _spec, *, upgrade_token):
        return None

    async def assert_upgrade_retired(self, _spec, *, upgrade_token):
        return None


def _readback():
    from app.services.standalone_sso_bootstrap import StandaloneSSOReadback

    return StandaloneSSOReadback(
        realm_id="akb-realm-id",
        product_admin_subject="00000000-0000-4000-8000-000000000001",
        admin_client_uuid="admin-client-uuid",
        management_client_uuid="management-client-uuid",
        api_client_uuid="api-client-uuid",
        active_signing_kid="rsa-3072-active-kid",
        active_signing_bits=3072,
        passive_rs256_keys=1,
        management_roles=MANAGEMENT_REALM_ROLES,
        management_scope_roles=MANAGEMENT_REALM_ROLES,
        admin_native_amr="pwd",
        product_admin_federated_identities=0,
    )


async def _install_leaves_the_bootstrap_client_usable() -> bool:
    """Run one fresh install and see whether its temporary door survives."""
    control = _Control(_readback())
    stored: list[object] = []

    async def _load():
        return stored[-1] if stored else None

    async def _record(receipt, *, previous_receipt=None):
        stored.append(receipt)

    async def _provision(**_kwargs):
        return {
            "user_id": "11111111-1111-4111-8111-111111111111",
            "created": True,
            "is_admin": True,
            "is_recovery_admin": True,
        }

    await bootstrap_standalone_sso(
        _spec(),
        control=control,
        provision_admin=_provision,
        load_retirement_receipt=_load,
        record_retirement_receipt=_record,
    )
    return control.bootstrap_alive


async def test_sso_install_leaves_at_least_one_authority_able_to_reach_the_admin():
    authorities = {
        # The installer put a credential on the account and handed it over.
        "install_wrote_a_credential": await _install_writes_a_product_admin_credential(),
        # The permanent client AKB keeps running could mint a new one.
        "standing_client_can_mint": "manage-users" in MANAGEMENT_REALM_ROLES,
        # The temporary install authority is still usable afterwards.
        "install_authority_survives": await _install_leaves_the_bootstrap_client_usable(),
    }

    assert any(authorities.values()), (
        "a fresh SSO install left the recovery administrator unreachable: "
        f"{authorities}"
    )


async def test_local_install_leaves_at_least_one_authority_able_to_reach_the_admin(monkeypatch):
    from app.config import settings
    from app.exceptions import LocalAuthDisabledError
    from app.services import password_service, recovery_admin_service

    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)

    async def _unreachable_pool():
        raise AssertionError("provisioning reached the database without a credential")

    monkeypatch.setattr(recovery_admin_service, "get_pool", _unreachable_pool)
    provisioning_demands_a_credential = False
    try:
        await recovery_admin_service.provision_local_recovery_admin(
            username="recovery-admin",
            email="recovery-admin@example.com",
        )
    except TypeError:
        # The signature itself refuses: no credential, no account.
        provisioning_demands_a_credential = True
    except AssertionError:
        provisioning_demands_a_credential = False

    # A local administrator can be reset by an authority that already exists,
    # which is why local was never at risk here. Probe the real path rather
    # than the mode flag: drive `reset_password` with an unusable pool and see
    # whether it gets past its own gate before reaching the database.
    class _Reached(Exception):
        pass

    async def _reached_pool():
        raise _Reached()

    monkeypatch.setattr(password_service, "get_pool", _reached_pool)
    reset_authority_available = False
    try:
        await password_service.reset_password(
            username="recovery-admin",
            actor_id=None,
            method="cli",
        )
    except _Reached:
        reset_authority_available = True
    except LocalAuthDisabledError:
        reset_authority_available = False

    authorities = {
        "install_wrote_a_credential": provisioning_demands_a_credential,
        "reset_authority_available": reset_authority_available,
    }

    assert any(authorities.values()), (
        "a fresh local install left the recovery administrator unreachable: "
        f"{authorities}"
    )
