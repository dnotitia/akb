"""Fail-closed lifecycle for a bundled standalone SSO installation.

This module owns ordering, not transport.  A deployment-specific Keycloak
control adapter performs the Admin REST calls while this state machine ensures
that the temporary bootstrap credential survives until all durable recovery
paths and the exact AKB product-admin projection have been read back.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
import re
from typing import Protocol

from app.services.sso_callback_urls import (
    BACKCHANNEL_LOGOUT_PATH,
    is_backchannel_logout_uri,
)


MANAGEMENT_REALM_ROLES = (
    "manage-identity-providers",
    "query-clients",
    "query-users",
    "view-clients",
    "view-realm",
    "view-users",
)
STANDALONE_SSO_RECEIPT_PROFILE_V1 = "bundled-keycloak-v1"
STANDALONE_SSO_RECEIPT_PROFILE_V2 = "bundled-keycloak-v2"
STANDALONE_SSO_RECEIPT_PROFILE = "bundled-keycloak-v3"
STANDALONE_SSO_RECEIPT_PROFILES = (
    STANDALONE_SSO_RECEIPT_PROFILE,
    STANDALONE_SSO_RECEIPT_PROFILE_V2,
    STANDALONE_SSO_RECEIPT_PROFILE_V1,
)
_BOOTSTRAP_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,255}$")


class StandaloneSSOBootstrapError(RuntimeError):
    """A value-free installation refusal suitable for operator output."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class StandaloneSSOBootstrapSpec:
    keycloak_internal_url: str
    keycloak_public_url: str
    realm: str
    akb_public_url: str
    bootstrap_client_id: str
    bootstrap_client_secret: str = field(repr=False)
    management_client_id: str
    management_client_secret: str = field(repr=False)
    api_client_id: str
    api_client_secret: str = field(repr=False)
    admin_client_id: str
    admin_client_secret: str = field(repr=False)
    product_admin_username: str
    product_admin_email: str
    product_admin_password: str = field(repr=False)
    backchannel_logout_uri: str = ""
    upgrade_client_id: str = "akb-bootstrap-upgrade-v2"
    upgrade_client_secret: str = field(default="", repr=False)

    @property
    def issuer(self) -> str:
        return f"{self.keycloak_public_url.rstrip('/')}/realms/{self.realm}"

    @property
    def admin_redirect_uri(self) -> str:
        return f"{self.akb_public_url.rstrip('/')}/api/v1/admin/auth/keycloak/callback"

    @property
    def admin_post_logout_redirect_uri(self) -> str:
        return f"{self.akb_public_url.rstrip('/')}/admin"

    @property
    def backchannel_logout_uri_effective(self) -> str:
        if self.backchannel_logout_uri:
            return self.backchannel_logout_uri
        return f"{self.akb_public_url.rstrip('/')}{BACKCHANNEL_LOGOUT_PATH}"

    @property
    def legacy_backchannel_logout_uri(self) -> str:
        """Public callback encoded by the retired v1/v2 profiles."""
        return f"{self.akb_public_url.rstrip('/')}{BACKCHANNEL_LOGOUT_PATH}"


@dataclass(frozen=True, slots=True)
class StandaloneSSOReadback:
    realm_id: str
    product_admin_subject: str
    admin_client_uuid: str
    management_client_uuid: str
    api_client_uuid: str
    active_signing_kid: str
    active_signing_bits: int
    passive_rs256_keys: int
    management_roles: tuple[str, ...]
    management_scope_roles: tuple[str, ...]
    admin_native_amr: str
    product_admin_federated_identities: int


@dataclass(frozen=True, slots=True)
class StandaloneSSORetirementReceipt:
    """Non-secret, durable proof binding bootstrap retirement to one install."""

    profile: str
    issuer: str
    realm_id: str
    bootstrap_client_id: str
    management_client_uuid: str
    admin_client_uuid: str
    api_client_uuid: str
    product_admin_subject: str
    akb_user_id: str
    backchannel_logout_uri: str | None = None


class StandaloneSSOControl(Protocol):
    async def acquire_management(
        self,
        spec: StandaloneSSOBootstrapSpec,
    ) -> str | None: ...

    async def acquire_bootstrap(
        self,
        spec: StandaloneSSOBootstrapSpec,
    ) -> str | None: ...

    async def acquire_upgrade(
        self,
        spec: StandaloneSSOBootstrapSpec,
    ) -> str | None: ...

    async def reconcile(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        bootstrap_token: str,
    ) -> StandaloneSSOReadback: ...

    async def readback(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        management_token: str,
    ) -> StandaloneSSOReadback: ...

    async def readback_legacy_v1(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        management_token: str,
    ) -> StandaloneSSOReadback: ...

    async def readback_legacy_v2(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        management_token: str,
    ) -> StandaloneSSOReadback: ...

    async def readback_callback_migration(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        source_backchannel_logout_uri: str,
        management_token: str,
    ) -> StandaloneSSOReadback: ...

    async def upgrade_legacy_to_current(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        upgrade_token: str,
    ) -> StandaloneSSOReadback: ...

    async def upgrade_callback_to_current(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        source_backchannel_logout_uri: str,
        upgrade_token: str,
    ) -> StandaloneSSOReadback: ...

    async def retire_bootstrap(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        bootstrap_token: str,
    ) -> None: ...

    async def assert_bootstrap_retired(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        bootstrap_token: str,
    ) -> None: ...

    async def retire_upgrade(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        upgrade_token: str,
    ) -> None: ...

    async def assert_upgrade_retired(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        upgrade_token: str,
    ) -> None: ...


ProvisionAdmin = Callable[..., Awaitable[Mapping[str, object]]]
LoadRetirementReceipt = Callable[[], Awaitable[StandaloneSSORetirementReceipt | None]]


class RecordRetirementReceipt(Protocol):
    def __call__(
        self,
        receipt: StandaloneSSORetirementReceipt,
        *,
        previous_receipt: StandaloneSSORetirementReceipt | None = None,
    ) -> Awaitable[None]: ...


def _validate_readback(readback: StandaloneSSOReadback) -> None:
    required = (
        readback.realm_id,
        readback.product_admin_subject,
        readback.admin_client_uuid,
        readback.management_client_uuid,
        readback.api_client_uuid,
        readback.active_signing_kid,
    )
    if any(not value.strip() for value in required):
        raise StandaloneSSOBootstrapError("keycloak_readback_incomplete")
    if readback.active_signing_bits != 3072:
        raise StandaloneSSOBootstrapError("keycloak_signing_profile_mismatch")
    if readback.passive_rs256_keys < 1:
        raise StandaloneSSOBootstrapError("keycloak_rotation_window_missing")
    if tuple(sorted(readback.management_roles)) != MANAGEMENT_REALM_ROLES:
        raise StandaloneSSOBootstrapError("keycloak_management_roles_mismatch")
    if tuple(sorted(readback.management_scope_roles)) != MANAGEMENT_REALM_ROLES:
        raise StandaloneSSOBootstrapError("keycloak_management_scope_mismatch")
    if readback.admin_native_amr != "pwd":
        raise StandaloneSSOBootstrapError("keycloak_admin_native_amr_missing")
    if readback.product_admin_federated_identities != 0:
        raise StandaloneSSOBootstrapError("keycloak_admin_identity_is_federated")


def _validate_projection(result: Mapping[str, object]) -> str:
    user_id = result.get("user_id")
    if (
        not isinstance(user_id, str)
        or not user_id
        or result.get("is_admin") is not True
        or result.get("is_recovery_admin") is not True
    ):
        raise StandaloneSSOBootstrapError("akb_product_admin_readback_failed")
    return user_id


def _expected_retirement_receipt(
    spec: StandaloneSSOBootstrapSpec,
    readback: StandaloneSSOReadback,
    *,
    akb_user_id: str,
    profile: str = STANDALONE_SSO_RECEIPT_PROFILE,
    retired_client_id: str | None = None,
    backchannel_logout_uri: str | None = None,
) -> StandaloneSSORetirementReceipt:
    return StandaloneSSORetirementReceipt(
        profile=profile,
        issuer=spec.issuer,
        realm_id=readback.realm_id,
        bootstrap_client_id=retired_client_id or spec.bootstrap_client_id,
        management_client_uuid=readback.management_client_uuid,
        admin_client_uuid=readback.admin_client_uuid,
        api_client_uuid=readback.api_client_uuid,
        product_admin_subject=readback.product_admin_subject,
        akb_user_id=akb_user_id,
        backchannel_logout_uri=(
            backchannel_logout_uri
            if backchannel_logout_uri is not None
            else (spec.backchannel_logout_uri_effective if profile == STANDALONE_SSO_RECEIPT_PROFILE else None)
        ),
    )


def _validate_receipt_static_binding(
    receipt: StandaloneSSORetirementReceipt,
    spec: StandaloneSSOBootstrapSpec,
) -> None:
    if receipt.profile == STANDALONE_SSO_RECEIPT_PROFILE_V1:
        expected_client_ids = {spec.bootstrap_client_id}
    elif receipt.profile == STANDALONE_SSO_RECEIPT_PROFILE_V2:
        expected_client_ids = {
            spec.bootstrap_client_id,
            spec.upgrade_client_id,
        }
    elif receipt.profile == STANDALONE_SSO_RECEIPT_PROFILE:
        expected_client_ids = {
            spec.bootstrap_client_id,
            spec.upgrade_client_id,
        }
        if receipt.backchannel_logout_uri is None or not is_backchannel_logout_uri(receipt.backchannel_logout_uri):
            raise StandaloneSSOBootstrapError("keycloak_bootstrap_retirement_receipt_mismatch")
    else:
        expected_client_ids = set()
    if (
        receipt.issuer != spec.issuer
        or receipt.bootstrap_client_id not in expected_client_ids
        or (
            receipt.profile
            in {
                STANDALONE_SSO_RECEIPT_PROFILE_V1,
                STANDALONE_SSO_RECEIPT_PROFILE_V2,
            }
            and receipt.backchannel_logout_uri is not None
        )
    ):
        raise StandaloneSSOBootstrapError("keycloak_bootstrap_retirement_receipt_mismatch")


async def bootstrap_standalone_sso(
    spec: StandaloneSSOBootstrapSpec,
    *,
    control: StandaloneSSOControl,
    provision_admin: ProvisionAdmin,
    load_retirement_receipt: LoadRetirementReceipt,
    record_retirement_receipt: RecordRetirementReceipt,
) -> dict[str, object]:
    """Converge one standalone SSO install without a recovery-credential gap."""

    if not spec.realm or spec.realm != spec.realm.strip() or spec.realm.casefold() == "master":
        # The master realm owns emergency bootstrap authority. Treating it as
        # AKB's product realm would mutate Keycloak's control plane and could
        # make the subsequent exact-client retirement destructive.
        raise StandaloneSSOBootstrapError("keycloak_product_realm_invalid")
    if _BOOTSTRAP_CLIENT_ID_RE.fullmatch(spec.bootstrap_client_id) is None:
        raise StandaloneSSOBootstrapError("keycloak_bootstrap_client_id_invalid")
    if _BOOTSTRAP_CLIENT_ID_RE.fullmatch(spec.upgrade_client_id) is None:
        raise StandaloneSSOBootstrapError("keycloak_upgrade_client_id_invalid")
    if spec.bootstrap_client_id == spec.upgrade_client_id:
        raise StandaloneSSOBootstrapError("keycloak_one_time_client_ids_reused")
    if not is_backchannel_logout_uri(spec.backchannel_logout_uri_effective):
        raise StandaloneSSOBootstrapError("keycloak_backchannel_logout_uri_invalid")

    retirement_receipt = await load_retirement_receipt()
    if retirement_receipt is not None:
        _validate_receipt_static_binding(retirement_receipt, spec)
    source_profile = retirement_receipt.profile if retirement_receipt else None
    legacy_v1 = source_profile == STANDALONE_SSO_RECEIPT_PROFILE_V1
    legacy_v2 = source_profile == STANDALONE_SSO_RECEIPT_PROFILE_V2
    legacy_profile = legacy_v1 or legacy_v2
    legacy_mutation_required = legacy_v1 or (
        legacy_v2 and spec.backchannel_logout_uri_effective != spec.legacy_backchannel_logout_uri
    )
    source_callback_uri: str | None = None
    if source_profile == STANDALONE_SSO_RECEIPT_PROFILE:
        assert retirement_receipt is not None
        assert retirement_receipt.backchannel_logout_uri is not None
        source_callback_uri = retirement_receipt.backchannel_logout_uri
    callback_migration_required = (
        source_callback_uri is not None and source_callback_uri != spec.backchannel_logout_uri_effective
    )
    mutation_upgrade_required = legacy_mutation_required or callback_migration_required

    management_token = await control.acquire_management(spec)
    bootstrap_token = await control.acquire_bootstrap(spec)
    upgrade_token = (
        await control.acquire_upgrade(spec) if mutation_upgrade_required or spec.upgrade_client_secret else None
    )
    if management_token is None and bootstrap_token is None and upgrade_token is None:
        raise StandaloneSSOBootstrapError("keycloak_install_credential_unavailable")

    if retirement_receipt is None:
        if bootstrap_token is None:
            raise StandaloneSSOBootstrapError("keycloak_bootstrap_retirement_receipt_missing")
        if upgrade_token is not None:
            raise StandaloneSSOBootstrapError("keycloak_upgrade_client_unexpected")
        if not spec.product_admin_password:
            # Validate the one-time recovery input before reconcile creates or
            # changes any realm resource. Readback and profile-upgrade runs do
            # not need the original product-admin password.
            raise StandaloneSSOBootstrapError("keycloak_product_admin_password_unavailable")
        if (
            len(spec.product_admin_password) < 12
            or spec.product_admin_password == spec.product_admin_username
            or spec.product_admin_password == spec.product_admin_email
        ):
            raise StandaloneSSOBootstrapError("keycloak_product_admin_password_policy")
        mode = "fresh" if management_token is None else "recovery"
        keycloak_mutated = True
        readback = await control.reconcile(
            spec,
            bootstrap_token=bootstrap_token,
        )
    elif legacy_profile:
        if bootstrap_token is not None:
            raise StandaloneSSOBootstrapError("keycloak_bootstrap_client_reactivated")
        if management_token is None:
            raise StandaloneSSOBootstrapError("keycloak_management_credential_unavailable")
        if legacy_mutation_required and upgrade_token is None:
            raise StandaloneSSOBootstrapError("keycloak_upgrade_credential_required")
        if not legacy_mutation_required and upgrade_token is not None:
            raise StandaloneSSOBootstrapError("keycloak_upgrade_client_unexpected")
        mode = (
            "upgrade-v1-to-v3"
            if legacy_v1
            else ("upgrade-v2-to-v3" if legacy_mutation_required else "upgrade-v2-to-v3-readback")
        )
        keycloak_mutated = False
        readback = await (
            control.readback_legacy_v1(
                spec,
                management_token=management_token,
            )
            if legacy_v1
            else control.readback_legacy_v2(
                spec,
                management_token=management_token,
            )
        )
    elif callback_migration_required:
        if bootstrap_token is not None:
            raise StandaloneSSOBootstrapError("keycloak_bootstrap_client_reactivated")
        if management_token is None:
            raise StandaloneSSOBootstrapError("keycloak_management_credential_unavailable")
        if upgrade_token is None:
            raise StandaloneSSOBootstrapError("keycloak_upgrade_credential_required")
        assert source_callback_uri is not None
        mode = "upgrade-v3-callback"
        keycloak_mutated = False
        readback = await control.readback_callback_migration(
            spec,
            source_backchannel_logout_uri=source_callback_uri,
            management_token=management_token,
        )
    else:
        if bootstrap_token is not None:
            raise StandaloneSSOBootstrapError("keycloak_bootstrap_client_reactivated")
        if upgrade_token is not None:
            raise StandaloneSSOBootstrapError("keycloak_upgrade_client_reactivated")
        mode = "readback"
        if management_token is None:
            raise StandaloneSSOBootstrapError("keycloak_management_credential_unavailable")
        keycloak_mutated = False
        readback = await control.readback(
            spec,
            management_token=management_token,
        )
    _validate_readback(readback)

    projection = await provision_admin(
        username=spec.product_admin_username,
        email=spec.product_admin_email,
        issuer=spec.issuer,
        subject=readback.product_admin_subject,
    )
    user_id = _validate_projection(projection)

    if legacy_profile or callback_migration_required:
        assert retirement_receipt is not None
        expected_source_receipt = _expected_retirement_receipt(
            spec,
            readback,
            akb_user_id=user_id,
            profile=retirement_receipt.profile,
            retired_client_id=retirement_receipt.bootstrap_client_id,
            backchannel_logout_uri=retirement_receipt.backchannel_logout_uri,
        )
        if retirement_receipt != expected_source_receipt:
            raise StandaloneSSOBootstrapError("keycloak_bootstrap_retirement_receipt_mismatch")
        if legacy_mutation_required:
            assert upgrade_token is not None
            upgraded_readback = await control.upgrade_legacy_to_current(
                spec,
                upgrade_token=upgrade_token,
            )
            _validate_readback(upgraded_readback)
            if upgraded_readback != readback:
                raise StandaloneSSOBootstrapError("keycloak_upgrade_readback_changed")
            readback = upgraded_readback
            keycloak_mutated = True
        elif callback_migration_required:
            assert upgrade_token is not None
            assert source_callback_uri is not None
            upgraded_readback = await control.upgrade_callback_to_current(
                spec,
                source_backchannel_logout_uri=source_callback_uri,
                upgrade_token=upgrade_token,
            )
            _validate_readback(upgraded_readback)
            if upgraded_readback != readback:
                raise StandaloneSSOBootstrapError("keycloak_upgrade_readback_changed")
            readback = upgraded_readback
            keycloak_mutated = True

    # Re-authenticate through the permanent path after projection.  A client
    # merely present in a prior read-back is not yet a proven recovery path.
    permanent_token = await control.acquire_management(spec)
    if permanent_token is None:
        raise StandaloneSSOBootstrapError("keycloak_management_credential_unavailable")
    final_readback = await control.readback(
        spec,
        management_token=permanent_token,
    )
    _validate_readback(final_readback)
    if final_readback != readback:
        raise StandaloneSSOBootstrapError("keycloak_readback_changed")

    expected_receipt = _expected_retirement_receipt(
        spec,
        final_readback,
        akb_user_id=user_id,
        retired_client_id=(
            spec.upgrade_client_id
            if mutation_upgrade_required
            else (
                retirement_receipt.bootstrap_client_id if retirement_receipt is not None else spec.bootstrap_client_id
            )
        ),
    )

    if retirement_receipt is None:
        assert bootstrap_token is not None
        await control.retire_bootstrap(
            spec,
            bootstrap_token=bootstrap_token,
        )
        await control.assert_bootstrap_retired(
            spec,
            bootstrap_token=bootstrap_token,
        )
        await record_retirement_receipt(expected_receipt)
        if await load_retirement_receipt() != expected_receipt:
            raise StandaloneSSOBootstrapError("keycloak_bootstrap_retirement_receipt_write_failed")
    elif mutation_upgrade_required:
        assert upgrade_token is not None
        await control.retire_upgrade(
            spec,
            upgrade_token=upgrade_token,
        )
        await control.assert_upgrade_retired(
            spec,
            upgrade_token=upgrade_token,
        )
        await record_retirement_receipt(
            expected_receipt,
            previous_receipt=(retirement_receipt if callback_migration_required else None),
        )
        if await load_retirement_receipt() != expected_receipt:
            raise StandaloneSSOBootstrapError("keycloak_upgrade_retirement_receipt_write_failed")
    elif legacy_profile:
        await record_retirement_receipt(expected_receipt)
        if await load_retirement_receipt() != expected_receipt:
            raise StandaloneSSOBootstrapError("keycloak_profile_retirement_receipt_write_failed")
    elif retirement_receipt != expected_receipt:
        raise StandaloneSSOBootstrapError("keycloak_bootstrap_retirement_receipt_mismatch")

    created = projection.get("created")
    return {
        "mode": mode,
        "keycloak_mutated": keycloak_mutated,
        "bootstrap_admin_retired": True,
        "receipt_profile": STANDALONE_SSO_RECEIPT_PROFILE,
        "realm_id": final_readback.realm_id,
        "product_admin_subject": final_readback.product_admin_subject,
        "akb_user_id": user_id,
        "akb_admin_created": created is True,
        "active_signing_kid": final_readback.active_signing_kid,
        "active_signing_bits": final_readback.active_signing_bits,
        "passive_rs256_keys": final_readback.passive_rs256_keys,
        "management_roles": list(final_readback.management_roles),
    }
