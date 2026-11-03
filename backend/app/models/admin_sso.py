"""Declared response shapes for the managed SSO admin surface.

These routes have always been in this API's OpenAPI document — nothing here is
newly exposed — but they returned plain dicts, so the document advertised seven
operations and described none of their payloads. A second consumer in another
repository then transcribed the envelope by hand, including a list of the
provider types this installation supports, and four days later this side grew a
second one. The shape had been written down twice with nothing linking the
copies (dnotitia/akb#455).

So it is declared here, once, and the framework publishes it.

Every model forbids extra fields. `response_model` FILTERS: a key the model does
not know is dropped from the response silently, in browsers and in a control
plane at once, which is exactly why these payloads were left alone until now.
Forbidding extras converts that silence into a test failure, and
`test_admin_sso_response_models_unit.py` asserts each model's field set against
the projection the handlers actually build.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.sso.identity_migration import IdentityMigrationState
from app.sso.models import ProviderState


class _AdminSsoModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SsoProviderCapabilities(_AdminSsoModel):
    """What a provider of this kind can do, stated rather than inferred.

    A consumer that reads these needs no opinion about what `provider_type`
    means; one that switches on the type string is approximating them.
    """

    supports_logout: bool
    supports_identity_migration: bool


class SsoProviderView(_AdminSsoModel):
    """One provider as an administrator sees it. Never carries a secret."""

    provider_type: str
    alias: str
    display_name: str
    state: ProviderState
    enabled: bool
    # Absent until configured: a configuration_error readback carries the alias
    # and the redirect pair and little else.
    issuer: str | None = None
    discovery_url: str | None = None
    client_id: str | None = None
    client_secret_configured: bool
    redirect_uri: str
    post_logout_redirect_uri: str
    capabilities: SsoProviderCapabilities


class SsoProviderCatalogResponse(_AdminSsoModel):
    """The catalog. `supported_provider_types` is what THIS installation
    supports; it is not a statement about what a reader must accept."""

    schema_version: int
    auth_mode: str
    control_mode: str
    supported_provider_types: list[str]
    providers: list[SsoProviderView]


class SsoProviderMutationResponse(_AdminSsoModel):
    """The readback of exactly one configure, enable or disable."""

    provider: SsoProviderView


class SsoIdentityPrelinkView(_AdminSsoModel):
    """Link evidence with the opaque subjects withheld on purpose: the state
    proves the exact values in the authenticated request were read back from
    both authorities, and echoing them would publish them."""

    provider_alias: str
    provider_state: ProviderState
    upstream_issuer: str
    broker_issuer: str
    broker_username: str


class SsoIdentityMigrationView(_AdminSsoModel):
    user_id: str
    state: IdentityMigrationState
    old_issuer: str
    new_issuer: str


class SsoIdentityMigrationResponse(_AdminSsoModel):
    schema_version: int
    prelink: SsoIdentityPrelinkView
    migration: SsoIdentityMigrationView


class SsoRealmArrivalView(_AdminSsoModel):
    """A reference to a recorded arrival; the arrival itself is read from the
    pending-admissions surface by this id."""

    admission_id: str
    last_seen_at: str


class SsoRealmResidueAccountView(_AdminSsoModel):
    """One realm account with no AKB binding. `arrival` is null for residue:
    an account nothing in AKB refers to."""

    realm_user_id: str
    username: str
    created_at: str | None
    arrival: SsoRealmArrivalView | None


class SsoRealmAccountResidueResponse(_AdminSsoModel):
    """Realm accounts minus (exact bindings and recorded arrivals), read-only.

    `state` is `unavailable` when the realm could not be read; every count and
    the list are then null, never zero. When `truncated` is true the realm
    holds more than `scan_limit` accounts and the counts describe only those
    read. `accounts` is bounded and lists residue ahead of pending arrivals.
    """

    schema_version: int
    state: Literal["available", "unavailable"]
    unavailable_code: str | None
    scan_limit: int
    truncated: bool | None
    realm_account_count: int | None
    bound_count: int | None
    pending_arrival_count: int | None
    residue_count: int | None
    accounts: list[SsoRealmResidueAccountView] | None
