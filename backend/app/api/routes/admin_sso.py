"""Product-admin API for runtime SSO provider configuration."""

from __future__ import annotations

import hashlib
import re

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
from app.sso.models import (
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


_CONFLICT_CODES = frozenset(
    {
        "keycloak_provider_control_delegated",
        "provider_alias_conflict",
        "provider_disable_before_reconfigure",
        "provider_configuration_invalid",
        "keycloak_provider_readback_failed",
    }
)
_NOT_FOUND_CODES = frozenset({"provider_not_found"})
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
) -> None:
    meta = mutation.audit_view() if mutation is not None else None
    if meta is None and provider is not None:
        meta = provider.audit_view()
    if meta is None and provider_type is not None:
        meta = {"provider_type": provider_type}
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
        meta=meta,
    )


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
