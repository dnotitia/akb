"""Explicit HOST-only preparation of an existing identity target.

Never imports AKB settings, storage, or database modules. Broad credentials and
the recovery journal stay on the host; only the narrow descriptor and historical
retirement attestation are handed to the runtime.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any, Callable
from typing import Literal
from urllib.parse import quote, urlsplit
from uuid import UUID, uuid4

import jwt
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from app.services.standalone_sso_bootstrap import (
    STANDALONE_SSO_RECEIPT_PROFILE,
    StandaloneSSOBootstrapError,
    StandaloneSSOBootstrapSpec,
    _validate_readback,
)
from app.services.standalone_sso_keycloak import KeycloakStandaloneSSOControl
from app.sso.brokered_account_guard import BROKERED_ACCOUNT_ROLE
from app.sso.keycloak_admin import KeycloakAdminConfig, KeycloakProviderControl
from app.sso.models import ProviderConfigureSpec
from app.sso.providers import keycloak_oidc


def fail(code: str) -> StandaloneSSOBootstrapError:
    return StandaloneSSOBootstrapError(code)


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def protected_read(path: Path) -> bytes:
    """Open the actual inode, rejecting symlinks, permissive modes and owners."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) not in {0o400, 0o600}
            or info.st_uid not in {0, os.geteuid()}
            or info.st_size > 1_048_576
        ):
            raise fail("target_protected_file_invalid")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            return stream.read(1_048_577)
    finally:
        os.close(fd)


def protected_write(path: Path, value: object) -> None:
    """Atomic, private output; never follow an existing output symlink."""
    temporary = path.with_name(path.name + "." + uuid4().hex + ".pending")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(canonical(value))
            stream.flush()
            os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)


class ExactCohortMember(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    akb_user_id: UUID
    broker_subject: UUID | None
    broker_username: str = Field(min_length=1, max_length=255)
    provider_alias: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    upstream_issuer: str = Field(min_length=1, max_length=2048)
    upstream_subject: str = Field(min_length=1, max_length=1024)


class ApprovedProviderBootstrap(BaseModel):
    """One platform-owned broker, approved by the protected host input."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    provider_type: Literal["keycloak-oidc"] = "keycloak-oidc"
    alias: Literal["platform"] = "platform"
    display_name: str = Field(min_length=1, max_length=80)
    issuer: str
    discovery_url: str
    client_id: str
    client_secret: SecretStr
    callback: str
    broker_secret_version: str = Field(min_length=1, max_length=512)

    def spec(self) -> ProviderConfigureSpec:
        return ProviderConfigureSpec(self.provider_type, self.alias, self.display_name, self.issuer,
                                     self.discovery_url, self.client_id, self.client_secret.get_secret_value())


class SourceGuard(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    auth_mode: Literal["local", "sso"]
    issuer: str | None = None
    runtime_generation: int = Field(ge=1)
    sso_session_epoch: UUID | None = None
    source_revision: str = Field(pattern=r"^[0-9a-f]{40}$")

    @model_validator(mode="after")
    def validate_source(self) -> SourceGuard:
        if self.auth_mode == "local" and (self.issuer or self.sso_session_epoch):
            raise ValueError("target_source_guard_invalid")
        if self.auth_mode == "sso" and (not self.issuer or self.sso_session_epoch is None):
            raise ValueError("target_source_guard_invalid")
        return self


class HostPreparationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: int = Field(default=1, ge=1, le=1)
    operation_id: UUID
    org_id: UUID
    target_id: UUID
    realm_id: UUID
    session_epoch: UUID
    realm_name: str = Field(min_length=1, max_length=255)
    issuer: str
    internal_url: str
    akb_public_url: str
    backchannel_logout_uri: str
    verify_ssl: bool = True
    local_realm_self_registration: bool = False
    bootstrap_client_id: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,255}$")
    bootstrap_client_secret: SecretStr
    management_client_id: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,255}$")
    management_client_secret: SecretStr
    api_client_id: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,255}$")
    api_client_secret: SecretStr
    admin_client_id: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,255}$")
    admin_client_secret: SecretStr
    browser_session_key: SecretStr
    product_admin_subject: UUID | None
    product_admin_email: str
    product_admin_password: SecretStr
    cohort: list[ExactCohortMember] = Field(default_factory=list, max_length=25)
    provider: ApprovedProviderBootstrap | None = None
    source_guard: SourceGuard | None = None

    @property
    def product_admin_username(self) -> str:
        return f"akb-recovery-{self.target_id.hex}"

    @model_validator(mode="after")
    def validate_target(self) -> HostPreparationInput:
        if self.realm_name.lower() == "master" or self.realm_name.strip() != self.realm_name:
            raise ValueError("target_realm_invalid")
        suffix = "/realms/" + quote(self.realm_name, safe="")
        if not self.issuer.endswith(suffix):
            raise ValueError("target_issuer_invalid")
        for value in (self.issuer, self.internal_url, self.akb_public_url):
            parsed = urlsplit(value)
            if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("target_url_invalid")
            if parsed.query or parsed.fragment or value.endswith("/"):
                raise ValueError("target_url_invalid")
        secrets = [
            self.bootstrap_client_secret.get_secret_value(), self.management_client_secret.get_secret_value(),
            self.api_client_secret.get_secret_value(), self.admin_client_secret.get_secret_value(),
            self.browser_session_key.get_secret_value(), self.product_admin_password.get_secret_value(),
        ]
        if any(len(value) < 12 for value in secrets) or len(set(secrets)) != len(secrets):
            raise ValueError("target_credentials_invalid")
        if len(self.browser_session_key.get_secret_value()) < 32:
            raise ValueError("target_browser_key_invalid")
        if len({self.bootstrap_client_id, self.management_client_id, self.api_client_id, self.admin_client_id}) != 4:
            raise ValueError("target_client_ids_reused")
        for key in ("akb_user_id", "broker_subject", "broker_username"):
            actual = [getattr(member, key) for member in self.cohort if getattr(member, key) is not None]
            if len(set(actual)) != len(actual):
                raise ValueError("target_cohort_duplicate")
        identities = {(m.upstream_issuer, m.upstream_subject) for m in self.cohort}
        if len(identities) != len(self.cohort) or (self.product_admin_subject is not None and
                self.product_admin_subject in {m.broker_subject for m in self.cohort}):
            raise ValueError("target_cohort_duplicate")
        if any(m.broker_username == self.product_admin_username for m in self.cohort):
            raise ValueError("target_cohort_recovery_collision")
        if self.provider is not None:
            keycloak_oidc.validate_spec(self.provider.spec(), broker_issuer=self.issuer)
            if self.provider.client_secret.get_secret_value() in secrets:
                raise ValueError("target_provider_credential_reused")
            expected_callback = self.issuer + "/broker/platform/endpoint"
            if self.provider.callback != expected_callback:
                raise ValueError("target_provider_callback_mismatch")
            if any(m.provider_alias != "platform" or m.upstream_issuer != self.provider.issuer for m in self.cohort):
                raise ValueError("target_provider_cohort_mismatch")
        return self

    def spec(self) -> StandaloneSSOBootstrapSpec:
        return StandaloneSSOBootstrapSpec(
            keycloak_internal_url=self.internal_url,
            keycloak_public_url=self.issuer.rsplit("/realms/", 1)[0], realm=self.realm_name,
            akb_public_url=self.akb_public_url, bootstrap_client_id=self.bootstrap_client_id,
            bootstrap_client_secret=self.bootstrap_client_secret.get_secret_value(),
            management_client_id=self.management_client_id,
            management_client_secret=self.management_client_secret.get_secret_value(),
            api_client_id=self.api_client_id, api_client_secret=self.api_client_secret.get_secret_value(),
            admin_client_id=self.admin_client_id, admin_client_secret=self.admin_client_secret.get_secret_value(),
            product_admin_username=self.product_admin_username, product_admin_email=self.product_admin_email,
            product_admin_password=self.product_admin_password.get_secret_value(),
            backchannel_logout_uri=self.backchannel_logout_uri,
            local_realm_self_registration=self.local_realm_self_registration,
        )


class ExistingTargetControl(KeycloakStandaloneSSOControl):
    """The shared profile adapter, with realm creation and admin adoption removed."""

    def __init__(self, preparation: HostPreparationInput) -> None:
        super().__init__(verify_ssl=preparation.verify_ssl)
        self.preparation = preparation
        self.resolved_cohort = list(preparation.cohort)

    async def acquire_bootstrap(self, spec: StandaloneSSOBootstrapSpec) -> str | None:
        token = await super().acquire_bootstrap(spec)
        if token is None:
            return None
        # Only inspect a token returned by our configured master token endpoint.
        # This bounds HOST authority; it does not authenticate a supplied JWT.
        # Keycloak still verifies its signature/authorization on every API call.
        try:
            claims = jwt.decode(token, options={"verify_signature": False})
        except jwt.InvalidTokenError:
            raise fail("target_bootstrap_token_invalid") from None
        issued, expires = claims.get("iat"), claims.get("exp")
        public_root = self.preparation.issuer.rsplit("/realms/", 1)[0]
        now = datetime.now(timezone.utc).timestamp()
        if (
            spec.keycloak_public_url != public_root
            or claims.get("iss") != public_root + "/realms/master"
            or claims.get("typ") != "Bearer"
            or type(issued) is not int
            or type(expires) is not int
            or not 0 < expires - issued <= 600
            or not issued <= now < expires
        ):
            raise fail("target_bootstrap_token_invalid")
        return token

    async def configure_approved_provider(self, spec: StandaloneSSOBootstrapSpec) -> None:
        approved = self.preparation.provider
        if approved is None:
            return
        manager = KeycloakProviderControl(KeycloakAdminConfig(
            spec.keycloak_internal_url, spec.keycloak_public_url, spec.realm,
            spec.management_client_id, spec.management_client_secret, self.preparation.verify_ssl))
        # Use the same one-time protected owner marker on every retry. A
        # foreign/customer alias is never taken over, even when AKB-marked.
        token = await self.acquire_management(spec)
        if token is None:
            raise fail("target_management_unavailable")
        path = f"/admin/realms/{quote(spec.realm, safe='')}/identity-provider/instances/platform"
        response = await self._request(spec, "GET", path, token=token, expected=frozenset({200, 404}))
        marker = {"akbTargetBootstrapOperation": str(self.preparation.operation_id),
                  "akbTargetBootstrapTarget": str(self.preparation.target_id)}
        if response.status_code == 200:
            existing = response.json()
            if any(existing.get("config", {}).get(key) != value for key, value in marker.items()):
                raise fail("target_provider_foreign_alias")
        # Persist ownership as part of the creating request, so a lost create
        # response can be recovered by exact marker readback on the next run.
        original_request = manager._request

        async def marked_request(client, method, request_path, **kwargs):
            body = kwargs.get("json_body")
            if method in {"POST", "PUT"} and isinstance(body, dict) and body.get("alias") == "platform":
                kwargs["json_body"] = {**body, "config": {**body.get("config", {}), **marker}}
            return await original_request(client, method, request_path, **kwargs)

        manager._request = marked_request  # type: ignore[method-assign,assignment]
        readback = await manager.configure(approved.spec())
        if readback.after.enabled or readback.after.redirect_uri != approved.callback:
            raise fail("target_provider_readback_mismatch")

    async def require_realm(self, spec: StandaloneSSOBootstrapSpec, *, token: str) -> dict[str, Any]:
        realm = await self._realm(spec, token=token)
        if realm is None or realm.get("id") != str(self.preparation.realm_id) or realm.get("realm") != spec.realm:
            raise fail("target_realm_identity_mismatch")
        discovery = await self._client(spec).get(
            f"/realms/{quote(spec.realm, safe='')}/.well-known/openid-configuration")
        if discovery.status_code != 200 or discovery.json().get("issuer") != self.preparation.issuer:
            raise fail("target_realm_issuer_mismatch")
        return realm

    async def _reconcile_realm(self, spec: StandaloneSSOBootstrapSpec, *, token: str) -> dict[str, Any]:
        realm = await self.require_realm(spec, token=token)
        await self._request(spec, "PUT", f"/admin/realms/{quote(spec.realm, safe='')}", token=token,
                            json_body={**realm, **self._realm_profile(spec)}, expected=frozenset({204}))
        # Keycloak 26 rejects undeclared attributes under its default user
        # profile. Declare only our admin-owned recovery marker; do not enable
        # arbitrary unmanaged attributes or alter human profile permissions.
        path = f"/admin/realms/{quote(spec.realm, safe='')}/users/profile"
        profile = await self._json(spec, path, token=token, code="target_user_profile_read_failed")
        if not isinstance(profile, dict) or not isinstance(profile.get("attributes"), list):
            raise fail("target_user_profile_read_failed")
        desired = {"name": "akb.preparation.operation", "displayName": "AKB preparation operation",
                   "permissions": {"view": ["admin"], "edit": ["admin"]}, "multivalued": False}
        owners = [item for item in profile["attributes"] if item.get("name") == desired["name"]]
        if owners and (len(owners) != 1 or any(owners[0].get(key) != value for key, value in desired.items())
                       or owners[0].get("validations", {}) != {}):
            raise fail("target_user_profile_marker_conflict")
        if not owners:
            profile["attributes"].append(desired)
            await self._request(spec, "PUT", path, token=token, json_body=profile, expected=frozenset({200, 204}))
        return await self.require_realm(spec, token=token)

    async def _exact_user(self, spec: StandaloneSSOBootstrapSpec, *, token: str) -> dict[str, Any] | None:
        user = await super()._exact_user(spec, token=token)
        if user is None:
            return None
        response = await self._request(spec, "GET", f"/admin/realms/{quote(spec.realm, safe='')}/users/{user['id']}", token=token)
        return response.json()

    async def _reconcile_product_admin(self, spec: StandaloneSSOBootstrapSpec, *, token: str) -> dict[str, Any]:
        existing = await self._exact_user(spec, token=token)
        if existing is None:
            if self.preparation.product_admin_subject is not None:
                raise fail("target_recovery_expected_user_missing")
            await self._request(spec, "POST", f"/admin/realms/{quote(spec.realm, safe='')}/users", token=token,
                                json_body={"username": spec.product_admin_username, "email": spec.product_admin_email,
                                           "attributes": {"akb.preparation.operation": [str(self.preparation.operation_id)]},
                                           "enabled": True, "emailVerified": True, "requiredActions": ["UPDATE_PASSWORD"],
                                           "credentials": [{"type": "password", "value": spec.product_admin_password,
                                                            "temporary": True}]}, expected=frozenset({201}))
            existing = await self._exact_user(spec, token=token)
        if (existing is None or (self.preparation.product_admin_subject is not None and
                                existing.get("id") != str(self.preparation.product_admin_subject))
                or (self.preparation.product_admin_subject is None and
                    existing.get("attributes", {}).get("akb.preparation.operation") != [str(self.preparation.operation_id)])
                or not self._product_admin_is_native(existing) or not self._product_admin_matches(spec, existing)
                or await self._federated_identity_count(spec, existing["id"], token=token)):
            raise fail("target_recovery_identity_mismatch")
        return existing

    async def cohort_readback(self, spec: StandaloneSSOBootstrapSpec, member: ExactCohortMember, *, token: str,
                              allow_missing: bool = False) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        base = f"/admin/realms/{quote(spec.realm, safe='')}"
        provider = await self._json(spec, f"{base}/identity-provider/instances/{quote(member.provider_alias, safe='')}",
                                    token=token, code="target_cohort_provider_missing")
        if not isinstance(provider, dict) or provider.get("config", {}).get("issuer") != member.upstream_issuer:
            raise fail("target_cohort_upstream_issuer_mismatch")
        # A complete target census catches a tuple already owned by another
        # user; neither email nor username is used as identity authority.
        for offset in range(0, 10001, 100):
            users = await self._json(spec, f"{base}/users", token=token,
                                     params={"first": offset, "max": 100}, code="target_cohort_census_failed")
            if not isinstance(users, list) or offset == 10000:
                raise fail("target_cohort_census_limit")
            for user in users:
                links = await self._json(spec, f"{base}/users/{quote(user['id'], safe='')}/federated-identity",
                                         token=token, code="target_cohort_census_failed")
                if not isinstance(links, list):
                    raise fail("target_cohort_census_failed")
                if any(link.get("identityProvider") == member.provider_alias and link.get("userId") == member.upstream_subject
                       for link in links) and user["id"] != str(member.broker_subject):
                    raise fail("target_cohort_multi_owner")
            if len(users) < 100:
                break
        path = f"{base}/users/{member.broker_subject}"
        response = await self._request(spec, "GET", path, token=token, expected=frozenset({200, 404}))
        if response.status_code == 404:
            if allow_missing:
                return None, []
            raise fail("target_cohort_user_missing")
        user = response.json()
        if (user.get("id") != str(member.broker_subject) or user.get("username") != member.broker_username
                or user.get("enabled") is not True or user.get("federationLink") not in {None, ""}
                or user.get("requiredActions") not in (None, [])):
            raise fail("target_cohort_user_conflict")
        credentials = await self._json(spec, path + "/credentials", token=token, code="target_cohort_read_failed")
        if credentials != []:
            raise fail("target_cohort_local_credential")
        links = await self._json(spec, path + "/federated-identity", token=token, code="target_cohort_read_failed")
        expected = {"identityProvider": member.provider_alias, "userId": member.upstream_subject,
                    "userName": member.broker_username}
        if links != [expected] and not (allow_missing and links == []):
            raise fail("target_cohort_federation_conflict")
        return user, links

    def restore_cohort(self, values: list[dict[str, Any]]) -> None:
        """A private journal may resolve UUIDs, never change the approved intent."""
        resolved = [ExactCohortMember(**value) for value in values]
        if len(resolved) != len(self.preparation.cohort):
            raise fail("target_cohort_journal_mismatch")
        for original, actual in zip(self.preparation.cohort, resolved, strict=True):
            if (original.model_dump(exclude={"broker_subject"}) != actual.model_dump(exclude={"broker_subject"})
                    or (original.broker_subject is not None and original.broker_subject != actual.broker_subject)):
                raise fail("target_cohort_journal_mismatch")
        subjects = [member.broker_subject for member in resolved if member.broker_subject is not None]
        if len(set(subjects)) != len(subjects):
            raise fail("target_cohort_journal_mismatch")
        self.resolved_cohort = resolved

    async def resolve_cohort(self, spec: StandaloneSSOBootstrapSpec, *, token: str,
                             checkpoint: Callable[[], None] | None = None) -> None:
        # An interrupted POST may not yet have a journal UUID. Resolve only the
        # exact username carrying this operation marker, then pin its actual ID
        # before any link/delete. A durable UUID is never replaced by a lookup.
        for index, member in enumerate(self.resolved_cohort):
            if member.broker_subject is None:
                users = await self._json(spec, f"/admin/realms/{quote(spec.realm, safe='')}/users", token=token,
                                         params={"username": member.broker_username, "exact": "true"},
                                         code="target_cohort_read_failed")
                if not isinstance(users, list) or len(users) > 1:
                    raise fail("target_cohort_user_conflict")
                if users:
                    if (users[0].get("username") != member.broker_username or
                            users[0].get("attributes", {}).get("akb.preparation.operation") != [str(self.preparation.operation_id)]):
                        raise fail("target_cohort_unowned_user")
                    self.resolved_cohort[index] = member.model_copy(update={"broker_subject": UUID(users[0]["id"])})
                    if checkpoint is not None:
                        checkpoint()

    async def seed_cohort(self, spec: StandaloneSSOBootstrapSpec, *, token: str,
                          checkpoint: Callable[[], None] | None = None) -> None:
        await self.resolve_cohort(spec, token=token, checkpoint=checkpoint)
        resolved = self.resolved_cohort
        # Validate the whole cohort before creating anything.
        before = [await self.cohort_readback(spec, m, token=token, allow_missing=True) for m in resolved]
        base = f"/admin/realms/{quote(spec.realm, safe='')}"
        marker = {"akb.preparation.operation": [str(self.preparation.operation_id)]}
        for member, (user, links) in zip(resolved, before, strict=True):
            if user is None and member.broker_subject is not None:
                raise fail("target_cohort_expected_user_missing")
            if user is not None and links == [] and user.get("attributes", {}).get("akb.preparation.operation") != marker["akb.preparation.operation"]:
                raise fail("target_cohort_unowned_unlinked_user")
        for index, (member, (user, links)) in enumerate(zip(resolved, before, strict=True)):
            if user is None:
                try:
                    await self._request(spec, "POST", base + "/users", token=token,
                                        json_body={"username": member.broker_username,
                                                   "enabled": True, "emailVerified": False, "attributes": marker,
                                                   "requiredActions": [], "credentials": []}, expected=frozenset({201}))
                except StandaloneSSOBootstrapError:
                    # A lost response is recovered by exact ownership readback;
                    # never send another POST against an uncertain result.
                    pass
                actuals = await self._json(spec, base + "/users", token=token,
                                           params={"username": member.broker_username, "exact": "true"},
                                           code="target_cohort_read_failed")
                if not isinstance(actuals, list) or len(actuals) != 1 or actuals[0].get("attributes", {}).get("akb.preparation.operation") != marker["akb.preparation.operation"]:
                    raise fail("target_cohort_create_uncertain")
                member = member.model_copy(update={"broker_subject": UUID(actuals[0]["id"])})
                self.resolved_cohort[index] = member
                if checkpoint is not None:
                    checkpoint()
            path = f"{base}/users/{member.broker_subject}"
            if not links:
                try:
                    await self._request(spec, "POST", path + "/federated-identity/" + quote(member.provider_alias, safe=''),
                                        token=token, json_body={"identityProvider": member.provider_alias,
                                                               "userId": member.upstream_subject,
                                                               "userName": member.broker_username}, expected=frozenset({204}))
                except StandaloneSSOBootstrapError:
                    await self.cohort_readback(spec, member, token=token)
            role = await self._json(spec, base + "/roles/" + quote(BROKERED_ACCOUNT_ROLE, safe=''), token=token,
                                   code="target_cohort_guard_missing")
            await self._request(spec, "POST", path + "/role-mappings/realm", token=token,
                                json_body=[role], expected=frozenset({204}))
            await self.cohort_readback(spec, member, token=token)

    async def compensate_cohort(self, spec: StandaloneSSOBootstrapSpec, *, token: str,
                                checkpoint: Callable[[], None] | None = None) -> int:
        """Delete only exact users marked as created by this operation.

        Existing prelinked users are never marked or removed. Added credentials,
        changed usernames, and foreign links stop compensation rather than
        destroying state another actor has begun to use.
        """
        await self.resolve_cohort(spec, token=token, checkpoint=checkpoint)
        deleted = 0
        for member in self.resolved_cohort:
            if member.broker_subject is None:
                continue
            user, _ = await self.cohort_readback(spec, member, token=token, allow_missing=True)
            if user is None:
                continue
            if user.get("attributes", {}).get("akb.preparation.operation") != [str(self.preparation.operation_id)]:
                continue
            await self._request(spec, "DELETE", f"/admin/realms/{quote(spec.realm, safe='')}/users/{member.broker_subject}",
                                token=token, expected=frozenset({204}))
            deleted += 1
        return deleted

    async def user_fingerprint(self, spec: StandaloneSSOBootstrapSpec, subject: str, *, token: str) -> dict[str, object] | None:
        base = f"/admin/realms/{quote(spec.realm, safe='')}/users/{quote(subject, safe='')}"
        response = await self._request(spec, "GET", base, token=token, expected=frozenset({200, 404}))
        if response.status_code == 404:
            return None
        user = response.json()
        selected = {key: user.get(key) for key in ("id", "username", "email", "enabled", "requiredActions", "federationLink", "attributes")}
        credentials = await self._json(spec, base + "/credentials", token=token, code="target_cleanup_read_failed")
        links = await self._json(spec, base + "/federated-identity", token=token, code="target_cleanup_read_failed")
        roles = await self._json(spec, base + "/role-mappings", token=token, code="target_cleanup_read_failed")
        return {"subject": subject,
                "owned": user.get("attributes", {}).get("akb.preparation.operation") == [str(self.preparation.operation_id)],
                "fingerprint_sha256": digest(canonical({"user": selected, "credentials": credentials, "links": links, "roles": roles}))}


async def prepare_target(preparation: HostPreparationInput, output_dir: Path, *,
                         control: ExistingTargetControl | None = None,
                         compensate_only: bool = False) -> dict[str, object]:
    """Prepare target only; a private journal makes retirement retry read-only."""
    output_dir.mkdir(mode=0o700, parents=False, exist_ok=True)
    if stat.S_IMODE(output_dir.stat().st_mode) != 0o700 or output_dir.is_symlink():
        raise fail("target_output_directory_invalid")
    lock = output_dir / ".preparation.lock"
    lock_fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        os.close(lock_fd)
        raise fail("target_preparation_already_running") from None
    own_control = control is None
    control = control or ExistingTargetControl(preparation)
    spec = preparation.spec()
    journal_path = output_dir / "host-authority-journal.json"
    nonsecret = preparation.model_dump(mode="json", exclude={
        "bootstrap_client_secret", "product_admin_password", "management_client_secret", "api_client_secret",
        "admin_client_secret", "browser_session_key"})
    if preparation.provider is not None:
        nonsecret["provider"] = preparation.provider.model_dump(mode="json", exclude={"client_secret"})
        nonsecret["provider"]["client_secret_sha256"] = digest(preparation.provider.client_secret.get_secret_value().encode())
    binding = digest(canonical({**nonsecret, "credential_binding": digest(canonical({
        **asdict(spec), "browser_session_key": preparation.browser_session_key.get_secret_value()}))}))
    try:
        journal = json.loads(protected_read(journal_path)) if journal_path.exists() else None
        if journal is not None and journal.get("binding") != binding:
            raise fail("target_preparation_journal_mismatch")
        if journal is not None and "cohort" in journal:
            control.restore_cohort(journal["cohort"])

        def checkpoint_cohort() -> None:
            assert journal is not None
            values = [member.model_dump(mode="json") for member in control.resolved_cohort]
            control.restore_cohort(values)
            journal["cohort"] = values
            protected_write(journal_path, journal)

        bootstrap = await control.acquire_bootstrap(spec)
        if compensate_only:
            if bootstrap is None or journal is None or journal["phase"] != "preparing":
                raise fail("target_cohort_compensation_unavailable")
            await control.require_realm(spec, token=bootstrap)
            count = await control.compensate_cohort(spec, token=bootstrap, checkpoint=checkpoint_cohort)
            return {"compensated": True, "deleted_owned_users": count}
        if bootstrap is not None:
            await control.require_realm(spec, token=bootstrap)
            if journal is None:
                journal = {"binding": binding, "bootstrap_token": bootstrap, "phase": "preparing"}
                protected_write(journal_path, journal)
            if journal["phase"] == "preparing":
                readback = await control.reconcile(spec, bootstrap_token=bootstrap)
                _validate_readback(readback)
                for client_uuid, secret_value in (
                    (readback.api_client_uuid, spec.api_client_secret),
                    (readback.admin_client_uuid, spec.admin_client_secret),
                    (readback.management_client_uuid, spec.management_client_secret),
                ):
                    secret_readback = await control._json(spec,
                        f"/admin/realms/{quote(spec.realm, safe='')}/clients/{quote(client_uuid, safe='')}/client-secret",
                        token=bootstrap, code="target_client_secret_read_failed")
                    if not isinstance(secret_readback, dict) or secret_readback.get("value") != secret_value:
                        raise fail("target_client_secret_readback_mismatch")
                await control.configure_approved_provider(spec)
                await control.seed_cohort(spec, token=bootstrap, checkpoint=checkpoint_cohort)
                snapshots = [await control.user_fingerprint(spec, str(member.broker_subject), token=bootstrap)
                             for member in control.resolved_cohort]
                snapshots.append(await control.user_fingerprint(spec, readback.product_admin_subject, token=bootstrap))
                journal["owned_assets"] = [snapshot for snapshot in snapshots if snapshot and snapshot["owned"]]
                journal["preserved_broker_ids"] = [snapshot["subject"] for snapshot in snapshots if snapshot and not snapshot["owned"]]
                journal["cohort"] = [m.model_dump(mode="json") for m in control.resolved_cohort]
                journal["phase"] = "retiring"
                protected_write(journal_path, journal)
            try:
                await control.retire_bootstrap(spec, bootstrap_token=bootstrap)
            except StandaloneSSOBootstrapError:
                await control.assert_bootstrap_retired(spec, bootstrap_token=journal["bootstrap_token"])
        elif journal is None:
            # Successful replay uses the published attestation, never restores
            # a credential or blindly recreates a retired authority.
            receipt = json.loads(protected_read(output_dir / "target-preparation.json"))
            if receipt.get("input_binding_sha256") != binding:
                raise fail("target_preparation_receipt_mismatch")
            journal = {"phase": "published"}
        elif journal["phase"] != "retiring":
            raise fail("target_preparation_authority_lost")
        if journal["phase"] != "published":
            await control.assert_bootstrap_retired(spec, bootstrap_token=journal["bootstrap_token"])
        management = await control.acquire_management(spec)
        if management is None:
            raise fail("target_management_unavailable")
        await control.require_realm(spec, token=management)
        readback = await control.readback(spec, management_token=management)
        _validate_readback(readback)
        if not await control.realm_events_converged(spec, management_token=management):
            raise fail("target_events_profile_mismatch")
        await control.brokered_account_guard_readback(spec, management_token=management)
        if not await control.login_presentation_converged(spec, management_token=management):
            raise fail("target_login_profile_mismatch")
        if journal["phase"] in {"published", "retiring"}:
            resolved_values = receipt.get("cohort", []) if journal["phase"] == "published" else journal.get("cohort", [])
            if resolved_values:
                control.restore_cohort(resolved_values)
        for member in control.resolved_cohort:
            await control.cohort_readback(spec, member, token=management)
        if journal["phase"] == "published":
            return receipt
        descriptor = {"schema_version": 1, "targets": [{
            "target_id": str(preparation.target_id), "realm_id": str(preparation.realm_id),
            "realm_name": preparation.realm_name, "issuer": preparation.issuer, "internal_url": preparation.internal_url,
            "management_client_id": preparation.management_client_id,
            "management_client_secret": preparation.management_client_secret.get_secret_value(),
            "session_epoch": str(preparation.session_epoch), "verify_ssl": preparation.verify_ssl}]}
        active_credentials = {
            "api_client_secret": preparation.api_client_secret.get_secret_value(),
            "admin_client_secret": preparation.admin_client_secret.get_secret_value(),
            "management_client_secret": preparation.management_client_secret.get_secret_value(),
            "browser_session_key": preparation.browser_session_key.get_secret_value()}
        cohort = [member.model_dump(mode="json") for member in control.resolved_cohort]
        now = datetime.now(timezone.utc).isoformat()
        receipt = {"schema_version": 1, "operation_id": str(preparation.operation_id), "org_id": str(preparation.org_id),
                   "target_id": str(preparation.target_id), "realm_id": str(preparation.realm_id),
                   "session_epoch": str(preparation.session_epoch), "realm_name": preparation.realm_name,
                   "issuer": preparation.issuer, "internal_url": preparation.internal_url,
                   "management_client_id": preparation.management_client_id,
                   "api_client_id": preparation.api_client_id, "admin_client_id": preparation.admin_client_id,
                   "akb_public_url": preparation.akb_public_url, "backchannel_logout_uri": preparation.backchannel_logout_uri,
                   "local_realm_self_registration": preparation.local_realm_self_registration,
                   "product_admin_username": preparation.product_admin_username,
                   "product_admin_email": preparation.product_admin_email,
                   "bootstrap_client_id": preparation.bootstrap_client_id,
                   "config_sha256": digest(canonical(descriptor)), "active_credentials_sha256": digest(canonical(active_credentials)),
                   "input_binding_sha256": binding, "cohort": cohort, "cohort_sha256": digest(canonical(cohort)),
                   "profile": STANDALONE_SSO_RECEIPT_PROFILE, "readback": asdict(readback),
                   "provider": nonsecret.get("provider"),
                   "source_guard": preparation.source_guard.model_dump(mode="json") if preparation.source_guard else None,
                   "owned_assets": journal.get("owned_assets", []),
                   "preserved_broker_ids": journal.get("preserved_broker_ids", []),
                   "prepared_at": now, "authority_retired_at": now,
                   "retirement_evidence": {"new_token_denied": True, "prior_token_master_call_denied": True}}
        receipt = json.loads(canonical(receipt))
        protected_write(output_dir / "sso-management-targets.json", descriptor)
        protected_write(output_dir / "active-credentials.json", active_credentials)
        protected_write(output_dir / "target-preparation.json", receipt)
        journal_path.unlink(missing_ok=True)
        return receipt
    finally:
        if own_control:
            await control.aclose()
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--compensate-cohort", action="store_true")
    args = parser.parse_args()
    try:
        preparation = HostPreparationInput.model_validate_json(protected_read(args.input_file))
        receipt = asyncio.run(prepare_target(preparation, args.output_dir, compensate_only=args.compensate_cohort))
        print(json.dumps({"compensated": True, "deleted_owned_users": receipt["deleted_owned_users"]}
                         if args.compensate_cohort else
                         {"prepared": True, "target_id": receipt["target_id"], "profile": receipt["profile"]}))
        return 0
    except StandaloneSSOBootstrapError as exc:
        print(exc.code, file=sys.stderr)
        return 1
    except Exception:
        # Pydantic/HTTP errors may retain credential-bearing input.
        print("target_preparation_failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
