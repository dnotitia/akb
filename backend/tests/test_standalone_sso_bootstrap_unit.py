"""Lifecycle contracts for the standalone SSO installation bootstrap."""

from __future__ import annotations

from dataclasses import asdict, replace

import pytest


pytestmark = pytest.mark.asyncio

_BOOTSTRAP_SECRET = "temporary-bootstrap-secret-must-not-leak"  # pragma: allowlist secret
_UPGRADE_SECRET = "temporary-upgrade-secret-must-not-leak"  # pragma: allowlist secret
_MANAGEMENT_SECRET = "permanent-management-secret-must-not-leak"  # pragma: allowlist secret
_ADMIN_CLIENT_SECRET = "admin-browser-secret-must-not-leak"  # pragma: allowlist secret
_PRODUCT_ADMIN_PASSWORD = "one-time-product-admin-password"  # pragma: allowlist secret


def _spec():
    from app.services.standalone_sso_bootstrap import StandaloneSSOBootstrapSpec

    return StandaloneSSOBootstrapSpec(
        keycloak_internal_url="http://keycloak:8080",
        keycloak_public_url="https://auth.akb.example.com",
        realm="akb",
        akb_public_url="https://akb.example.com",
        bootstrap_client_id="akb-bootstrap-temporary",
        bootstrap_client_secret=_BOOTSTRAP_SECRET,
        management_client_id="akb-sso-manager",
        management_client_secret=_MANAGEMENT_SECRET,
        api_client_id="akb-web",
        api_client_secret="api-browser-secret-must-not-leak",  # pragma: allowlist secret
        admin_client_id="akb-admin",
        admin_client_secret=_ADMIN_CLIENT_SECRET,
        product_admin_username="product-admin",
        product_admin_email="product-admin@example.com",
        product_admin_password=_PRODUCT_ADMIN_PASSWORD,
        upgrade_client_id="akb-bootstrap-upgrade-v2",
        upgrade_client_secret="",
    )


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
        management_roles=(
            "manage-identity-providers",
            "query-clients",
            "query-users",
            "view-clients",
            "view-realm",
            "view-users",
        ),
        management_scope_roles=(
            "manage-identity-providers",
            "query-clients",
            "query-users",
            "view-clients",
            "view-realm",
            "view-users",
        ),
        admin_native_amr="pwd",
        product_admin_federated_identities=0,
    )


def _receipt(
    *,
    user_id: str = "11111111-1111-4111-8111-111111111111",
    profile: str | None = None,
    retired_client_id: str | None = None,
):
    from app.services.standalone_sso_bootstrap import (
        STANDALONE_SSO_RECEIPT_PROFILE,
        StandaloneSSORetirementReceipt,
    )

    readback = _readback()
    return StandaloneSSORetirementReceipt(
        profile=profile or STANDALONE_SSO_RECEIPT_PROFILE,
        issuer=_spec().issuer,
        realm_id=readback.realm_id,
        bootstrap_client_id=retired_client_id or _spec().bootstrap_client_id,
        management_client_uuid=readback.management_client_uuid,
        admin_client_uuid=readback.admin_client_uuid,
        api_client_uuid=readback.api_client_uuid,
        product_admin_subject=readback.product_admin_subject,
        akb_user_id=user_id,
    )


class _Control:
    def __init__(
        self,
        *,
        manager_available: bool,
        bootstrap_available: bool,
        upgrade_available: bool = False,
    ):
        self.manager_available = manager_available
        self.bootstrap_available = bootstrap_available
        self.upgrade_available = upgrade_available
        self.events: list[str] = []
        self._readback = _readback()

    async def acquire_management(self, _spec):
        self.events.append("acquire-management")
        return "manager-token" if self.manager_available else None

    async def acquire_bootstrap(self, _spec):
        self.events.append("acquire-bootstrap")
        return "bootstrap-token" if self.bootstrap_available else None

    async def acquire_upgrade(self, _spec):
        self.events.append("acquire-upgrade")
        return "upgrade-token" if self.upgrade_available else None

    async def reconcile(self, _spec, *, bootstrap_token: str):
        assert bootstrap_token == "bootstrap-token"
        self.events.append("reconcile-keycloak")
        self.manager_available = True
        return self._readback

    async def readback(self, _spec, *, management_token: str):
        assert management_token == "manager-token"
        self.events.append("readback-keycloak")
        return self._readback

    async def readback_legacy_v1(self, _spec, *, management_token: str):
        assert management_token == "manager-token"
        self.events.append("readback-keycloak-v1")
        return self._readback

    async def upgrade_v1_to_v2(self, _spec, *, upgrade_token: str):
        assert upgrade_token == "upgrade-token"
        self.events.append("upgrade-keycloak-v1-to-v2")
        return self._readback

    async def retire_bootstrap(self, _spec, *, bootstrap_token: str):
        assert bootstrap_token == "bootstrap-token"
        self.events.append("retire-bootstrap")
        self.bootstrap_available = False

    async def assert_bootstrap_retired(self, _spec, *, bootstrap_token: str):
        assert bootstrap_token == "bootstrap-token"
        self.events.append("assert-bootstrap-retired")
        assert self.bootstrap_available is False

    async def retire_upgrade(self, _spec, *, upgrade_token: str):
        assert upgrade_token == "upgrade-token"
        self.events.append("retire-upgrade")
        self.upgrade_available = False

    async def assert_upgrade_retired(self, _spec, *, upgrade_token: str):
        assert upgrade_token == "upgrade-token"
        self.events.append("assert-upgrade-retired")
        assert self.upgrade_available is False


class _ReceiptStore:
    def __init__(self, events: list[str], receipt=None):
        self.events = events
        self.receipt = receipt

    async def load(self):
        self.events.append("load-retirement-receipt")
        return self.receipt

    async def record(self, receipt):
        self.events.append("record-retirement-receipt")
        self.receipt = receipt


async def test_fresh_bootstrap_retires_temporary_admin_only_after_akb_projection():
    from app.services.standalone_sso_bootstrap import bootstrap_standalone_sso

    control = _Control(manager_available=False, bootstrap_available=True)
    receipts = _ReceiptStore(control.events)

    async def _provision(**kwargs):
        control.events.append("provision-akb-admin")
        assert kwargs == {
            "username": "product-admin",
            "email": "product-admin@example.com",
            "issuer": "https://auth.akb.example.com/realms/akb",
            "subject": "00000000-0000-4000-8000-000000000001",
        }
        return {
            "user_id": "11111111-1111-4111-8111-111111111111",
            "created": True,
            "is_admin": True,
            "is_recovery_admin": True,
        }

    report = await bootstrap_standalone_sso(
        _spec(),
        control=control,
        provision_admin=_provision,
        load_retirement_receipt=receipts.load,
        record_retirement_receipt=receipts.record,
    )

    assert control.events == [
        "load-retirement-receipt",
        "acquire-management",
        "acquire-bootstrap",
        "reconcile-keycloak",
        "provision-akb-admin",
        "acquire-management",
        "readback-keycloak",
        "retire-bootstrap",
        "assert-bootstrap-retired",
        "record-retirement-receipt",
        "load-retirement-receipt",
    ]
    assert receipts.receipt == _receipt()
    assert report["mode"] == "fresh"
    assert report["bootstrap_admin_retired"] is True
    assert report["product_admin_subject"] == _readback().product_admin_subject
    assert report["akb_user_id"] == "11111111-1111-4111-8111-111111111111"
    assert report["active_signing_bits"] == 3072
    serialized = str(report)
    for secret in (
        _BOOTSTRAP_SECRET,
        _MANAGEMENT_SECRET,
        _ADMIN_CLIENT_SECRET,
        _PRODUCT_ADMIN_PASSWORD,
        "api-browser-secret-must-not-leak",
    ):
        assert secret not in serialized


async def test_completed_bootstrap_rerun_is_read_only_and_does_not_need_temp_admin():
    from app.services.standalone_sso_bootstrap import bootstrap_standalone_sso

    control = _Control(manager_available=True, bootstrap_available=False)
    receipts = _ReceiptStore(control.events, _receipt())

    async def _provision(**_kwargs):
        control.events.append("provision-akb-admin")
        return {
            "user_id": "11111111-1111-4111-8111-111111111111",
            "created": False,
            "is_admin": True,
            "is_recovery_admin": True,
        }

    report = await bootstrap_standalone_sso(
        replace(_spec(), bootstrap_client_secret="", product_admin_password=""),
        control=control,
        provision_admin=_provision,
        load_retirement_receipt=receipts.load,
        record_retirement_receipt=receipts.record,
    )

    assert control.events == [
        "load-retirement-receipt",
        "acquire-management",
        "acquire-bootstrap",
        "readback-keycloak",
        "provision-akb-admin",
        "acquire-management",
        "readback-keycloak",
    ]
    assert report["mode"] == "readback"
    assert report["keycloak_mutated"] is False
    assert report["akb_admin_created"] is False


async def test_legacy_v1_receipt_uses_one_time_upgrade_authority_then_retires_it():
    from app.services.standalone_sso_bootstrap import (
        STANDALONE_SSO_RECEIPT_PROFILE_V1,
        bootstrap_standalone_sso,
    )

    control = _Control(
        manager_available=True,
        bootstrap_available=False,
        upgrade_available=True,
    )
    receipts = _ReceiptStore(
        control.events,
        _receipt(profile=STANDALONE_SSO_RECEIPT_PROFILE_V1),
    )

    async def _provision(**_kwargs):
        control.events.append("provision-akb-admin")
        return {
            "user_id": "11111111-1111-4111-8111-111111111111",
            "created": False,
            "is_admin": True,
            "is_recovery_admin": True,
        }

    report = await bootstrap_standalone_sso(
        replace(
            _spec(),
            bootstrap_client_secret="",
            product_admin_password="",
            upgrade_client_secret=_UPGRADE_SECRET,
        ),
        control=control,
        provision_admin=_provision,
        load_retirement_receipt=receipts.load,
        record_retirement_receipt=receipts.record,
    )

    assert control.events == [
        "load-retirement-receipt",
        "acquire-management",
        "acquire-bootstrap",
        "acquire-upgrade",
        "readback-keycloak-v1",
        "provision-akb-admin",
        "upgrade-keycloak-v1-to-v2",
        "acquire-management",
        "readback-keycloak",
        "retire-upgrade",
        "assert-upgrade-retired",
        "record-retirement-receipt",
        "load-retirement-receipt",
    ]
    assert receipts.receipt == _receipt(
        retired_client_id=_spec().upgrade_client_id,
    )
    assert report["mode"] == "upgrade-v1-to-v2"
    assert report["keycloak_mutated"] is True
    assert report["receipt_profile"] == "bundled-keycloak-v2"
    assert control.upgrade_available is False


async def test_legacy_v1_receipt_without_upgrade_authority_fails_before_mutation():
    from app.services.standalone_sso_bootstrap import (
        STANDALONE_SSO_RECEIPT_PROFILE_V1,
        StandaloneSSOBootstrapError,
        bootstrap_standalone_sso,
    )

    control = _Control(manager_available=True, bootstrap_available=False)
    receipts = _ReceiptStore(
        control.events,
        _receipt(profile=STANDALONE_SSO_RECEIPT_PROFILE_V1),
    )

    async def _should_not_run(**_kwargs):
        raise AssertionError("legacy migration must not run without its authority")

    with pytest.raises(StandaloneSSOBootstrapError) as captured:
        await bootstrap_standalone_sso(
            replace(
                _spec(),
                bootstrap_client_secret="",
                product_admin_password="",
                upgrade_client_secret="",
            ),
            control=control,
            provision_admin=_should_not_run,
            load_retirement_receipt=receipts.load,
            record_retirement_receipt=receipts.record,
        )

    assert captured.value.code == "keycloak_upgrade_credential_required"
    assert "upgrade-keycloak-v1-to-v2" not in control.events
    assert "provision-akb-admin" not in control.events


async def test_partial_bootstrap_uses_remaining_temp_admin_then_retires_it():
    from app.services.standalone_sso_bootstrap import bootstrap_standalone_sso

    control = _Control(manager_available=True, bootstrap_available=True)
    receipts = _ReceiptStore(control.events)

    async def _provision(**_kwargs):
        control.events.append("provision-akb-admin")
        return {
            "user_id": "11111111-1111-4111-8111-111111111111",
            "created": False,
            "is_admin": True,
            "is_recovery_admin": True,
        }

    report = await bootstrap_standalone_sso(
        _spec(),
        control=control,
        provision_admin=_provision,
        load_retirement_receipt=receipts.load,
        record_retirement_receipt=receipts.record,
    )

    assert "reconcile-keycloak" in control.events
    assert control.events.index("retire-bootstrap") > control.events.index(
        "provision-akb-admin"
    )
    assert report["mode"] == "recovery"


async def test_akb_projection_failure_preserves_temporary_recovery_credential():
    from app.services.standalone_sso_bootstrap import bootstrap_standalone_sso

    control = _Control(manager_available=False, bootstrap_available=True)
    receipts = _ReceiptStore(control.events)

    async def _fail(**_kwargs):
        control.events.append("provision-akb-admin")
        raise RuntimeError("database unavailable")

    with pytest.raises(RuntimeError, match="database unavailable"):
        await bootstrap_standalone_sso(
            _spec(),
            control=control,
            provision_admin=_fail,
            load_retirement_receipt=receipts.load,
            record_retirement_receipt=receipts.record,
        )

    assert "retire-bootstrap" not in control.events
    assert control.bootstrap_available is True


async def test_no_authorized_install_credential_fails_closed():
    from app.services.standalone_sso_bootstrap import (
        StandaloneSSOBootstrapError,
        bootstrap_standalone_sso,
    )

    control = _Control(manager_available=False, bootstrap_available=False)
    receipts = _ReceiptStore(control.events)

    async def _should_not_run(**_kwargs):
        raise AssertionError("AKB projection must not run")

    with pytest.raises(StandaloneSSOBootstrapError) as captured:
        await bootstrap_standalone_sso(
            _spec(),
            control=control,
            provision_admin=_should_not_run,
            load_retirement_receipt=receipts.load,
            record_retirement_receipt=receipts.record,
        )

    assert captured.value.code == "keycloak_install_credential_unavailable"


async def test_master_realm_can_never_be_selected_as_the_akb_product_realm():
    from app.services.standalone_sso_bootstrap import (
        StandaloneSSOBootstrapError,
        bootstrap_standalone_sso,
    )

    control = _Control(manager_available=False, bootstrap_available=True)
    receipts = _ReceiptStore(control.events)

    async def _should_not_run(**_kwargs):
        raise AssertionError("master-realm refusal must precede projection")

    with pytest.raises(StandaloneSSOBootstrapError) as captured:
        await bootstrap_standalone_sso(
            replace(_spec(), realm="master"),
            control=control,
            provision_admin=_should_not_run,
            load_retirement_receipt=receipts.load,
            record_retirement_receipt=receipts.record,
        )

    assert captured.value.code == "keycloak_product_realm_invalid"
    assert control.events == []


async def test_invalid_bootstrap_client_id_stops_before_external_calls():
    from app.services.standalone_sso_bootstrap import (
        StandaloneSSOBootstrapError,
        bootstrap_standalone_sso,
    )

    control = _Control(manager_available=False, bootstrap_available=True)
    receipts = _ReceiptStore(control.events)

    async def _should_not_run(**_kwargs):
        raise AssertionError("invalid destructive target must fail during preflight")

    with pytest.raises(StandaloneSSOBootstrapError) as captured:
        await bootstrap_standalone_sso(
            replace(_spec(), bootstrap_client_id="../other-client"),
            control=control,
            provision_admin=_should_not_run,
            load_retirement_receipt=receipts.load,
            record_retirement_receipt=receipts.record,
        )

    assert captured.value.code == "keycloak_bootstrap_client_id_invalid"
    assert control.events == []


async def test_removed_bootstrap_secret_without_retirement_receipt_fails_closed():
    from app.services.standalone_sso_bootstrap import (
        StandaloneSSOBootstrapError,
        bootstrap_standalone_sso,
    )

    control = _Control(manager_available=True, bootstrap_available=False)
    receipts = _ReceiptStore(control.events)
    spec = replace(_spec(), bootstrap_client_secret="", product_admin_password="")

    async def _should_not_run(**_kwargs):
        raise AssertionError("AKB projection must not run without retirement evidence")

    with pytest.raises(StandaloneSSOBootstrapError) as captured:
        await bootstrap_standalone_sso(
            spec,
            control=control,
            provision_admin=_should_not_run,
            load_retirement_receipt=receipts.load,
            record_retirement_receipt=receipts.record,
        )

    assert captured.value.code == "keycloak_bootstrap_retirement_receipt_missing"
    assert control.events == [
        "load-retirement-receipt",
        "acquire-management",
        "acquire-bootstrap",
    ]


async def test_missing_product_admin_password_stops_before_keycloak_mutation():
    from app.services.standalone_sso_bootstrap import (
        StandaloneSSOBootstrapError,
        bootstrap_standalone_sso,
    )

    control = _Control(manager_available=False, bootstrap_available=True)
    receipts = _ReceiptStore(control.events)

    async def _should_not_run(**_kwargs):
        raise AssertionError("projection must not run without the one-time password")

    with pytest.raises(StandaloneSSOBootstrapError) as captured:
        await bootstrap_standalone_sso(
            replace(_spec(), product_admin_password=""),
            control=control,
            provision_admin=_should_not_run,
            load_retirement_receipt=receipts.load,
            record_retirement_receipt=receipts.record,
        )

    assert captured.value.code == "keycloak_product_admin_password_unavailable"
    assert control.events == [
        "load-retirement-receipt",
        "acquire-management",
        "acquire-bootstrap",
    ]


@pytest.mark.parametrize(
    "password",
    ["short", "product-admin", "product-admin@example.com"],
)
async def test_invalid_product_admin_password_stops_before_keycloak_mutation(password):
    from app.services.standalone_sso_bootstrap import (
        StandaloneSSOBootstrapError,
        bootstrap_standalone_sso,
    )

    control = _Control(manager_available=False, bootstrap_available=True)
    receipts = _ReceiptStore(control.events)

    async def _should_not_run(**_kwargs):
        raise AssertionError("invalid password must fail before reconciliation")

    with pytest.raises(StandaloneSSOBootstrapError) as captured:
        await bootstrap_standalone_sso(
            replace(_spec(), product_admin_password=password),
            control=control,
            provision_admin=_should_not_run,
            load_retirement_receipt=receipts.load,
            record_retirement_receipt=receipts.record,
        )

    assert captured.value.code == "keycloak_product_admin_password_policy"
    assert "reconcile-keycloak" not in control.events


async def test_readback_rejects_receipt_bound_to_another_installation():
    from app.services.standalone_sso_bootstrap import (
        StandaloneSSOBootstrapError,
        bootstrap_standalone_sso,
    )

    control = _Control(manager_available=True, bootstrap_available=False)
    receipts = _ReceiptStore(
        control.events,
        replace(_receipt(), realm_id="different-realm-id"),
    )

    async def _provision(**_kwargs):
        control.events.append("provision-akb-admin")
        return {
            "user_id": "11111111-1111-4111-8111-111111111111",
            "created": False,
            "is_admin": True,
            "is_recovery_admin": True,
        }

    with pytest.raises(StandaloneSSOBootstrapError) as captured:
        await bootstrap_standalone_sso(
            replace(_spec(), bootstrap_client_secret="", product_admin_password=""),
            control=control,
            provision_admin=_provision,
            load_retirement_receipt=receipts.load,
            record_retirement_receipt=receipts.record,
        )

    assert captured.value.code == "keycloak_bootstrap_retirement_receipt_mismatch"


async def test_recorded_retirement_rejects_a_reactivated_bootstrap_client():
    from app.services.standalone_sso_bootstrap import (
        StandaloneSSOBootstrapError,
        bootstrap_standalone_sso,
    )

    control = _Control(manager_available=True, bootstrap_available=True)
    receipts = _ReceiptStore(control.events, _receipt())

    async def _should_not_run(**_kwargs):
        raise AssertionError("a reactivated bootstrap client must stop convergence")

    with pytest.raises(StandaloneSSOBootstrapError) as captured:
        await bootstrap_standalone_sso(
            _spec(),
            control=control,
            provision_admin=_should_not_run,
            load_retirement_receipt=receipts.load,
            record_retirement_receipt=receipts.record,
        )

    assert captured.value.code == "keycloak_bootstrap_client_reactivated"
    assert "reconcile-keycloak" not in control.events


async def test_current_receipt_rejects_a_reactivated_upgrade_client():
    from app.services.standalone_sso_bootstrap import (
        StandaloneSSOBootstrapError,
        bootstrap_standalone_sso,
    )

    control = _Control(
        manager_available=True,
        bootstrap_available=False,
        upgrade_available=True,
    )
    receipts = _ReceiptStore(control.events, _receipt())

    async def _should_not_run(**_kwargs):
        raise AssertionError("a reactivated upgrade client must stop convergence")

    with pytest.raises(StandaloneSSOBootstrapError) as captured:
        await bootstrap_standalone_sso(
            replace(_spec(), upgrade_client_secret=_UPGRADE_SECRET),
            control=control,
            provision_admin=_should_not_run,
            load_retirement_receipt=receipts.load,
            record_retirement_receipt=receipts.record,
        )

    assert captured.value.code == "keycloak_upgrade_client_reactivated"
    assert "readback-keycloak" not in control.events


async def test_spec_repr_and_mapping_never_expose_secret_values():
    spec = replace(_spec(), upgrade_client_secret=_UPGRADE_SECRET)
    rendered = repr(spec)
    # asdict is intentionally not used by production reporting; this assertion
    # documents why the state machine emits an explicit allowlisted report.
    assert asdict(spec)["bootstrap_client_secret"] == _BOOTSTRAP_SECRET
    assert asdict(spec)["upgrade_client_secret"] == _UPGRADE_SECRET
    for secret in (
        _BOOTSTRAP_SECRET,
        _UPGRADE_SECRET,
        _MANAGEMENT_SECRET,
        _ADMIN_CLIENT_SECRET,
        _PRODUCT_ADMIN_PASSWORD,
        "api-browser-secret-must-not-leak",
    ):
        assert secret not in rendered


async def test_management_role_profile_is_least_privilege_and_exact():
    from app.services.standalone_sso_bootstrap import MANAGEMENT_REALM_ROLES

    assert MANAGEMENT_REALM_ROLES == (
        "manage-identity-providers",
        "query-clients",
        "query-users",
        "view-clients",
        "view-realm",
        "view-users",
    )
    assert "realm-admin" not in MANAGEMENT_REALM_ROLES
    assert "manage-clients" not in MANAGEMENT_REALM_ROLES
    assert "manage-users" not in MANAGEMENT_REALM_ROLES
