"""Product-admin API for runtime SSO provider configuration."""

from __future__ import annotations

import hashlib
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, SkipValidation

from app.api.routes.admin_auth import (
    ProductAdminActor,
    get_current_product_admin,
    get_product_admin_mutation,
)
from app.config import settings
from app.services import audit_log
from app.sso.keycloak_admin import (
    ProviderControlError,
    get_keycloak_provider_control,
)
from app.sso.identity_migration import (
    IdentityMigrationError,
    IdentityMigrationReadback,
    apply_identity_migration,
    inspect_identity_migration,
    rollback_identity_migration,
)
from app.sso.models import (
    IdentityPrelinkReadback,
    ProviderConfigureSpec,
    ProviderMutationReadback,
    ProviderReadback,
)
from app.sso.registry import provider_types


class ConfigureProviderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_type: str = Field(min_length=1, max_length=80)
    display_name: str = Field(min_length=1, max_length=80)
    issuer: str = Field(min_length=1, max_length=2048)
    discovery_url: str = Field(min_length=1, max_length=2048)
    client_id: str = Field(min_length=1, max_length=255)
    # Skip Pydantic's value-echoing type error for this write-only field. The
    # authenticated route validates the raw value and returns a value-less
    # provider error, while OpenAPI still describes a nullable string.
    client_secret: SkipValidation[str] | None = Field(default=None, repr=False)


class IdentityMigrationRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        # Runtime defaults prevent Pydantic's missing-field error from echoing
        # sibling subjects.  Keep the public contract truthful: all three
        # strings are still semantically required and null is never accepted.
        json_schema_extra={
            "required": [
                "existing_user_id",
                "upstream_subject",
                "broker_subject",
            ],
            "properties": {
                "existing_user_id": {
                    "title": "Existing User Id",
                    "type": "string",
                },
                "upstream_subject": {
                    "title": "Upstream Subject",
                    "type": "string",
                },
                "broker_subject": {
                    "title": "Broker Subject",
                    "type": "string",
                },
            },
        },
    )

    # These identifiers are intentionally validated in the value-less domain
    # boundary.  Pydantic's ordinary type error includes rejected input, which
    # would reflect an opaque IdP subject into the HTTP response.
    # Defaults route missing fields through the same value-less domain errors;
    # a Pydantic `missing` error would otherwise echo the full input object and
    # therefore the other opaque subject.
    existing_user_id: SkipValidation[str] | None = Field(default=None, repr=False)
    upstream_subject: SkipValidation[str] | None = Field(default=None, repr=False)
    broker_subject: SkipValidation[str] | None = Field(default=None, repr=False)


_CONFLICT_CODES = frozenset(
    {
        "keycloak_provider_control_delegated",
        "provider_alias_conflict",
        "provider_disable_before_reconfigure",
        "provider_configuration_invalid",
        "keycloak_provider_readback_failed",
        "identity_prelink_user_mismatch",
        "identity_prelink_user_inactive",
        "identity_prelink_user_not_native",
        "identity_prelink_local_credential_present",
        "identity_prelink_missing",
        "identity_prelink_subject_mismatch",
        "identity_prelink_ambiguous",
        "identity_migration_provider_must_be_disabled",
        "identity_migration_prelink_changed",
    }
)
_NOT_FOUND_CODES = frozenset({"provider_not_found", "identity_prelink_user_not_found"})
_SAFE_ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
_INVALID_CODES = frozenset(
    {
        "unsupported_sso_provider_type",
        "provider_type_mismatch",
        "provider_alias_invalid",
        "provider_display_name_invalid",
        "provider_issuer_invalid",
        "provider_issuer_is_broker",
        "provider_discovery_url_invalid",
        "provider_discovery_url_mismatch",
        "provider_discovery_issuer_mismatch",
        "provider_discovery_authorization_url_invalid",
        "provider_discovery_token_url_invalid",
        "provider_discovery_jwks_url_invalid",
        "provider_discovery_optional_url_invalid",
        "provider_client_id_invalid",
        "provider_client_secret_invalid",
        "provider_client_secret_required",
        "identity_prelink_subject_invalid",
    }
)


def _require_sso_mode() -> None:
    if settings.require_auth_mode() != "sso":
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "SSO provider control is not enabled",
        )


router = APIRouter(dependencies=[Depends(_require_sso_mode)])


def _raise_control_error(error: ProviderControlError) -> None:
    if error.code in _INVALID_CODES:
        status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
        message = "SSO provider configuration is invalid"
    elif error.code in _NOT_FOUND_CODES:
        status_code = status.HTTP_404_NOT_FOUND
        message = "SSO provider was not found"
    elif error.code in _CONFLICT_CODES:
        status_code = status.HTTP_409_CONFLICT
        message = "SSO provider state conflicts with this operation"
    else:
        status_code = status.HTTP_502_BAD_GATEWAY
        message = "SSO provider authority is unavailable"
    raise HTTPException(
        status_code,
        detail={"message": message, "code": error.code},
    ) from error


def _audit(
    action: str,
    actor: ProductAdminActor,
    alias: str,
    *,
    outcome: str = "ok",
    code: str | None = None,
    provider: ProviderReadback | None = None,
    mutation: ProviderMutationReadback | None = None,
    provider_type: str | None = None,
    meta: dict[str, object] | None = None,
) -> None:
    audit_meta = meta
    if audit_meta is None and mutation is not None:
        audit_meta = mutation.audit_view()
    if audit_meta is None and provider is not None:
        audit_meta = provider.audit_view()
    if audit_meta is None and provider_type is not None:
        audit_meta = {"provider_type": provider_type}
    if _SAFE_ALIAS_RE.fullmatch(alias):
        target = f"provider={alias}"
    else:
        digest = hashlib.sha256(alias.encode("utf-8")).hexdigest()[:16]
        target = f"provider=<invalid:sha256:{digest}>"
    audit_log.record(
        action=action,
        actor=actor.username,
        actor_id=str(actor.user_id),
        target=target,
        outcome=outcome,
        code=code,
        meta=audit_meta,
    )


def _subject_digest(value: object) -> str:
    if not isinstance(value, str):
        return "<invalid>"
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return "<invalid>"
    return hashlib.sha256(encoded).hexdigest()


def _canonical_user_id(value: object) -> str:
    try:
        return str(uuid.UUID(value))  # type: ignore[arg-type]
    except (AttributeError, TypeError, ValueError):
        return "<invalid>"


def _identity_audit_meta(
    request: IdentityMigrationRequest,
    *,
    prelink: IdentityPrelinkReadback | None = None,
    migration: IdentityMigrationReadback | None = None,
) -> dict[str, object]:
    meta: dict[str, object] = {
        "existing_user_id": _canonical_user_id(request.existing_user_id),
        "old_subject_sha256": _subject_digest(request.upstream_subject),
        "new_subject_sha256": _subject_digest(request.broker_subject),
    }
    if prelink is not None:
        meta.update(
            {
                "provider_state": prelink.provider_state,
                "old_issuer": prelink.upstream_issuer,
                "new_issuer": prelink.broker_issuer,
            }
        )
    if migration is not None:
        meta["migration_state"] = migration.state
    return meta


def _raise_identity_migration_error(error: IdentityMigrationError) -> None:
    if error.code in {
        "identity_migration_user_id_invalid",
        "identity_migration_issuer_invalid",
        "identity_migration_subject_invalid",
        "identity_migration_actor_invalid",
        "identity_migration_binding_unchanged",
    }:
        status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
        message = "Identity migration request is invalid"
    elif error.code == "identity_migration_target_not_found":
        status_code = status.HTTP_404_NOT_FOUND
        message = "Identity migration target was not found"
    else:
        status_code = status.HTTP_409_CONFLICT
        message = "Identity migration state conflicts with this operation"
    raise HTTPException(
        status_code,
        detail={"message": message, "code": error.code},
    ) from error


@router.get("/admin/sso/providers", summary="List managed SSO providers")
async def list_sso_providers(
    _actor: ProductAdminActor = Depends(get_current_product_admin),
):
    control = get_keycloak_provider_control()
    providers: tuple[ProviderReadback, ...] = ()
    if control.control_mode == "direct":
        try:
            providers = await control.list_providers(force_refresh=True)
        except ProviderControlError as error:
            _raise_control_error(error)
    return {
        "schema_version": 1,
        "auth_mode": "sso",
        "control_mode": control.control_mode,
        "supported_provider_types": list(provider_types()),
        "providers": [provider.admin_view() for provider in providers],
    }


@router.put(
    "/admin/sso/providers/{alias}",
    summary="Configure a disabled SSO provider",
)
async def configure_sso_provider(
    alias: str,
    request: ConfigureProviderRequest,
    actor: ProductAdminActor = Depends(get_product_admin_mutation),
):
    _audit(
        "admin.sso.provider.configure.requested",
        actor,
        alias,
        provider_type=request.provider_type,
    )
    try:
        raw_secret: object = request.client_secret
        if raw_secret is not None and not isinstance(raw_secret, str):
            raise ProviderControlError("provider_client_secret_invalid")
        spec = ProviderConfigureSpec(
            provider_type=request.provider_type,
            alias=alias,
            display_name=request.display_name,
            issuer=request.issuer,
            discovery_url=request.discovery_url,
            client_id=request.client_id,
            client_secret=raw_secret,
        )
        mutation = await get_keycloak_provider_control().configure(spec)
    except ProviderControlError as error:
        _audit(
            "admin.sso.provider.configure",
            actor,
            alias,
            outcome="error",
            code=error.code,
            provider_type=request.provider_type,
        )
        _raise_control_error(error)
    except Exception:
        _audit(
            "admin.sso.provider.configure",
            actor,
            alias,
            outcome="error",
            code="internal_error",
            provider_type=request.provider_type,
        )
        raise
    _audit("admin.sso.provider.configure", actor, alias, mutation=mutation)
    return {"provider": mutation.after.admin_view()}


async def _toggle_sso_provider(
    alias: str,
    *,
    enabled: bool,
    actor: ProductAdminActor,
) -> dict[str, object]:
    verb = "enable" if enabled else "disable"
    _audit(f"admin.sso.provider.{verb}.requested", actor, alias)
    try:
        mutation = await get_keycloak_provider_control().set_enabled(
            alias,
            enabled=enabled,
        )
    except ProviderControlError as error:
        _audit(
            f"admin.sso.provider.{verb}",
            actor,
            alias,
            outcome="error",
            code=error.code,
        )
        _raise_control_error(error)
    except Exception:
        _audit(
            f"admin.sso.provider.{verb}",
            actor,
            alias,
            outcome="error",
            code="internal_error",
        )
        raise
    _audit(f"admin.sso.provider.{verb}", actor, alias, mutation=mutation)
    return {"provider": mutation.after.admin_view()}


@router.post(
    "/admin/sso/providers/{alias}/enable",
    summary="Enable a configured SSO provider",
)
async def enable_sso_provider(
    alias: str,
    actor: ProductAdminActor = Depends(get_product_admin_mutation),
):
    return await _toggle_sso_provider(alias, enabled=True, actor=actor)


@router.post(
    "/admin/sso/providers/{alias}/disable",
    summary="Disable an SSO provider",
)
async def disable_sso_provider(
    alias: str,
    actor: ProductAdminActor = Depends(get_product_admin_mutation),
):
    return await _toggle_sso_provider(alias, enabled=False, actor=actor)


def _prelink_response(
    prelink: IdentityPrelinkReadback,
    migration: IdentityMigrationReadback,
) -> dict[str, object]:
    # Do not reflect opaque subjects; the state proves the exact values in the
    # authenticated request were read back from both authorities.
    return {
        "schema_version": 1,
        "prelink": {
            "provider_alias": prelink.provider_alias,
            "provider_state": prelink.provider_state,
            "upstream_issuer": prelink.upstream_issuer,
            "broker_issuer": prelink.broker_issuer,
            "broker_username": prelink.broker_username,
        },
        "migration": migration.admin_view(),
    }


def _same_identity_prelink(
    before: IdentityPrelinkReadback,
    after: IdentityPrelinkReadback,
) -> bool:
    return (
        before.provider_alias,
        before.upstream_issuer,
        before.broker_issuer,
        before.upstream_subject,
        before.broker_subject,
    ) == (
        after.provider_alias,
        after.upstream_issuer,
        after.broker_issuer,
        after.upstream_subject,
        after.broker_subject,
    ) and after.provider_state == "configured_disabled"


async def _verify_and_inspect_identity_migration(
    alias: str,
    request: IdentityMigrationRequest,
) -> tuple[IdentityPrelinkReadback, IdentityMigrationReadback]:
    prelink = await get_keycloak_provider_control().verify_identity_prelink(
        alias,
        broker_subject=request.broker_subject,  # type: ignore[arg-type]
        upstream_subject=request.upstream_subject,  # type: ignore[arg-type]
    )
    migration = await inspect_identity_migration(
        existing_user_id=request.existing_user_id,  # type: ignore[arg-type]
        old_issuer=prelink.upstream_issuer,
        old_subject=request.upstream_subject,  # type: ignore[arg-type]
        new_issuer=prelink.broker_issuer,
        new_subject=request.broker_subject,  # type: ignore[arg-type]
    )
    return prelink, migration


@router.post(
    "/admin/sso/providers/{alias}/identity-migrations/preflight",
    summary="Verify an exact broker identity migration without writing",
)
async def preflight_sso_identity_migration(
    alias: str,
    request: IdentityMigrationRequest,
    _actor: ProductAdminActor = Depends(get_current_product_admin),
):
    try:
        prelink, migration = await _verify_and_inspect_identity_migration(
            alias,
            request,
        )
    except ProviderControlError as error:
        _raise_control_error(error)
    except IdentityMigrationError as error:
        _raise_identity_migration_error(error)
    return _prelink_response(prelink, migration)


async def _mutate_sso_identity_migration(
    alias: str,
    request: IdentityMigrationRequest,
    actor: ProductAdminActor,
    *,
    operation: str,
) -> dict[str, object]:
    if operation not in {"apply", "rollback"}:
        raise RuntimeError("unsupported identity migration operation")
    action = f"admin.sso.identity_migration.{operation}"
    _audit(
        f"{action}.requested",
        actor,
        alias,
        meta=_identity_audit_meta(request),
    )
    prelink: IdentityPrelinkReadback | None = None
    try:
        control = get_keycloak_provider_control()
        prelink = await control.verify_identity_prelink(
            alias,
            broker_subject=request.broker_subject,  # type: ignore[arg-type]
            upstream_subject=request.upstream_subject,  # type: ignore[arg-type]
        )
        if prelink.provider_state != "configured_disabled":
            raise ProviderControlError(
                "identity_migration_provider_must_be_disabled"
            )
        arguments = {
            "existing_user_id": request.existing_user_id,
            "old_issuer": prelink.upstream_issuer,
            "old_subject": request.upstream_subject,
            "new_issuer": prelink.broker_issuer,
            "new_subject": request.broker_subject,
            "actor_id": actor.username,
        }
        if operation == "apply":
            migration = await apply_identity_migration(**arguments)  # type: ignore[arg-type]
            try:
                confirmed = await control.verify_identity_prelink(
                    alias,
                    broker_subject=request.broker_subject,  # type: ignore[arg-type]
                    upstream_subject=request.upstream_subject,  # type: ignore[arg-type]
                )
                if not _same_identity_prelink(prelink, confirmed):
                    raise ProviderControlError("identity_migration_prelink_changed")
            except ProviderControlError as original_error:
                if migration.binding_changed:
                    try:
                        await rollback_identity_migration(**arguments)  # type: ignore[arg-type]
                    except Exception as compensation_error:
                        raise ProviderControlError(
                            "identity_migration_compensation_failed"
                        ) from compensation_error
                raise original_error
            prelink = confirmed
        else:
            migration = await rollback_identity_migration(**arguments)  # type: ignore[arg-type]
    except ProviderControlError as error:
        _audit(
            action,
            actor,
            alias,
            outcome="error",
            code=error.code,
            meta=_identity_audit_meta(request, prelink=prelink),
        )
        _raise_control_error(error)
    except IdentityMigrationError as error:
        _audit(
            action,
            actor,
            alias,
            outcome="error",
            code=error.code,
            meta=_identity_audit_meta(request, prelink=prelink),
        )
        _raise_identity_migration_error(error)
    except Exception:
        _audit(
            action,
            actor,
            alias,
            outcome="error",
            code="internal_error",
            meta=_identity_audit_meta(request, prelink=prelink),
        )
        raise
    _audit(
        action,
        actor,
        alias,
        meta=_identity_audit_meta(
            request,
            prelink=prelink,
            migration=migration,
        ),
    )
    if prelink is None:  # pragma: no cover - assigned before every success path
        raise RuntimeError("identity migration prelink read-back missing")
    return _prelink_response(prelink, migration)


@router.post(
    "/admin/sso/providers/{alias}/identity-migrations/apply",
    summary="Bind an operator-prelinked broker identity to an AKB user",
)
async def apply_sso_identity_migration(
    alias: str,
    request: IdentityMigrationRequest,
    actor: ProductAdminActor = Depends(get_product_admin_mutation),
):
    return await _mutate_sso_identity_migration(
        alias,
        request,
        actor,
        operation="apply",
    )


@router.post(
    "/admin/sso/providers/{alias}/identity-migrations/rollback",
    summary="Remove an AKB broker identity binding before operator cleanup",
)
async def rollback_sso_identity_migration(
    alias: str,
    request: IdentityMigrationRequest,
    actor: ProductAdminActor = Depends(get_product_admin_mutation),
):
    return await _mutate_sso_identity_migration(
        alias,
        request,
        actor,
        operation="rollback",
    )
