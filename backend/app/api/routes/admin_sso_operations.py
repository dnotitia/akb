"""Explicit human grants and the separate bounded operation credential carrier."""

from datetime import datetime
from typing import Literal
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Response, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from app.api.routes.admin_auth import ProductAdminActor, get_current_product_admin, get_product_admin_mutation
from app.sso import identity_operations as operations
from app.sso.identity_migration import IdentityMigrationError
from app.sso.keycloak_admin import ProviderControlError
from app.models.admin_sso import SsoProviderView, SsoProviderCatalogResponse
from app.config import settings
from app.exceptions import AuthenticationError
from app.sso import management_targets
from app.sso.runtime_identity import source_tuple
from app.db.postgres import get_pool


class ValueLessRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe_handler(request):
            try:
                return await handler(request)
            except RequestValidationError:
                raise HTTPException(422, detail={"code": "sso_operation_request_invalid"}) from None
            except (IdentityMigrationError, ProviderControlError) as error:
                raise HTTPException(409, detail={"code": error.code}) from error
        return safe_handler


async def get_metadata_actor(request: Request, authorization: str | None = Header(default=None)):
    from app.api.routes.admin_sso import get_sso_provider_control_actor

    if settings.require_auth_mode() == "local":
        try:
            return await get_current_product_admin(request, authorization=authorization)
        except AuthenticationError:
            pass
    return await get_sso_provider_control_actor(request, authorization=authorization, csrf_header=None)


router = APIRouter(route_class=ValueLessRoute)


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OwnershipRequest(RequestModel):
    owner_id: str | None = Field(min_length=1, max_length=255)
    expected_revision: int = Field(ge=0, strict=True)
    expected_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    clear_emergency: bool = False


class CohortItem(RequestModel):
    existing_user_id: str
    upstream_subject: str = Field(repr=False)
    broker_subject: str = Field(repr=False)


Verb = Literal["preflight", "postcheck", "apply", "rollback", "admit", "configure", "enable", "disable", "login-evidence"]


class SourceTuple(RequestModel):
    auth_mode: Literal["local", "sso"]
    issuer: str | None
    runtime_generation: int = Field(ge=1, strict=True)
    sso_session_epoch: str | None
    source_revision: str | None = Field(pattern=r"^[a-f0-9]{40}$")


class GrantRequest(RequestModel):
    operation_id: uuid.UUID
    installation_id: uuid.UUID
    actor_id: uuid.UUID
    actor_token_id: uuid.UUID
    provider_alias: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,62}$")
    owner_id: str = Field(min_length=1, max_length=255)
    expected_provider_revision: int = Field(ge=1, strict=True)
    runtime_generation: int | None = Field(default=None, ge=1, strict=True)
    sso_session_epoch: uuid.UUID | None = None
    target_id: uuid.UUID | None = None
    expected_source: SourceTuple | None = None
    expected_target_realm_id: str | None = None
    target_session_epoch: uuid.UUID | None = None
    expected_target_runtime_generation: int | None = Field(default=None, ge=1, strict=True)
    expected_target_source_revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")
    ttl_seconds: int = Field(ge=1, le=600, strict=True)
    verbs: list[Verb] = Field(min_length=1, max_length=9)
    cohort: list[CohortItem] = Field(max_length=25, repr=False)
    provider_payload_sha256: dict[str, str] = Field(default_factory=dict)
    provider_secret_revision: str | None = Field(default=None, min_length=1, max_length=255)


class ExecuteRequest(RequestModel):
    nonce: uuid.UUID
    verb: Verb
    item_index: int = Field(ge=0, strict=True)
    issued_at: datetime
    payload: dict | None = Field(default=None, repr=False)


class CapabilitiesResponse(RequestModel):
    schema_version: Literal[1]
    installation_id: uuid.UUID
    capabilities: list[str]
    max_ttl_seconds: int
    max_nonce_age_seconds: int
    realm_identity: dict[str, str] | None
    realm_identity_error: str | None


class OwnershipResponse(RequestModel):
    schema_version: Literal[1]
    target_id: uuid.UUID
    provider_alias: str
    owner_id: str | None
    revision: int
    fingerprint: str
    provider_secret_revision: str | None
    drifted: bool
    emergency_disabled: bool


class GrantResponse(RequestModel):
    schema_version: Literal[1]
    operation_id: uuid.UUID
    credential: str = Field(repr=False)
    expires_at: datetime
    cohort_sha256: str
    provider_revision: int


class IdentityResultResponse(RequestModel):
    schema_version: Literal[1]
    operation_id: uuid.UUID
    nonce: uuid.UUID
    verb: Literal["preflight", "postcheck", "apply", "rollback", "admit"]
    item_index: int
    user_id: uuid.UUID
    state: Literal["ready_to_link", "linked"]
    binding_changed: bool


class ProviderResultResponse(RequestModel):
    schema_version: Literal[1]
    operation_id: uuid.UUID
    nonce: uuid.UUID
    verb: Literal["configure", "enable", "disable"]
    provider_revision: int
    provider: SsoProviderView


class StatusResponse(RequestModel):
    schema_version: Literal[1]
    target_id: uuid.UUID
    parent_operation_id: uuid.UUID | None
    intent_sha256: str
    authority_attempt: int
    owned_binding_ids: list[uuid.UUID]
    owned_binding_count: int
    owned_binding_item_indices: list[int]
    rollback_results: list[IdentityResultResponse]
    provider_state: str | None
    provider_fingerprint: str | None
    provider_readback_error: str | None
    provider_secret_revision: str | None
    source_tuple: SourceTuple | None
    target_epoch: str | None
    phase: Literal["granted", "preparing", "prepared", "retired"]
    prepared_identity_ids_hash: str | None
    existing_identity_ids_hash: str | None
    operation_id: uuid.UUID
    provider_alias: str
    provider_revision: int
    cohort_sha256: str
    expires_at: datetime
    retired_at: datetime | None
    verbs: list[Verb]


@router.get("/admin/sso/operation-capabilities", response_model=CapabilitiesResponse)
async def operation_capabilities(_actor: ProductAdminActor = Depends(get_metadata_actor)):
    return await operations.capabilities()


@router.get("/admin/sso/providers/{alias}/ownership", response_model=OwnershipResponse)
async def read_ownership(alias: str, _actor: ProductAdminActor = Depends(get_metadata_actor)):
    return await operations.ownership(alias)


@router.put("/admin/sso/providers/{alias}/ownership", response_model=OwnershipResponse)
async def change_ownership(alias: str, request: OwnershipRequest,
                           actor: ProductAdminActor = Depends(get_product_admin_mutation)):
    return await operations.set_ownership(alias, **request.model_dump(), actor_id=uuid.UUID(str(actor.user_id)))


@router.post("/admin/sso/identity-operations", response_model=GrantResponse)
async def grant_operation(request: GrantRequest, response: Response, actor: ProductAdminActor = Depends(get_product_admin_mutation)):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return await operations.grant(request.model_dump(exclude_unset=True), granted_by=uuid.UUID(str(actor.user_id)))


@router.post("/admin/sso/identity-operations/{operation_id}/execute", response_model=IdentityResultResponse | ProviderResultResponse)
async def execute_operation(operation_id: uuid.UUID, request: ExecuteRequest,
                            authorization: str | None = Header(default=None)):
    # This carrier is local to this one route. Cookies and regular bearer PATs
    # cannot become a bounded-operation credential, even when both are present.
    prefix = "AKB-Operation "
    if authorization is None or not authorization.startswith(prefix):
        raise HTTPException(401, detail={"code": "sso_operation_credential_invalid"})
    return await operations.execute(operation_id, authorization[len(prefix):], request.model_dump())


def operation_credential(authorization: str | None) -> str | None:
    return authorization[len("AKB-Operation "):] if authorization and authorization.startswith("AKB-Operation ") else None


@router.get("/admin/sso/identity-operations/{operation_id}", response_model=StatusResponse)
async def operation_status(operation_id: uuid.UUID, request: Request, authorization: str | None = Header(default=None)):
    credential = operation_credential(authorization)
    actor = None
    if credential is None:
        resolved = await get_metadata_actor(request, authorization=authorization)
        actor = resolved if getattr(resolved, "account_kind", "human") == "service" else None
    return await operations.status(operation_id, credential=credential, actor=actor)


@router.post("/admin/sso/identity-operations/{operation_id}/retire", response_model=StatusResponse)
async def retire_operation(operation_id: uuid.UUID, request: Request, authorization: str | None = Header(default=None),
                           csrf_header: str | None = Header(default=None, alias="X-AKB-Admin-CSRF")):
    credential = operation_credential(authorization)
    actor = None
    if credential is None:
        actor = await get_product_admin_mutation(request, authorization=authorization, csrf_header=csrf_header)
    return await operations.retire(operation_id, actor_id=uuid.UUID(str(actor.user_id)) if actor else None, credential=credential)


class TargetView(RequestModel):
    target_id: uuid.UUID
    realm_identity: dict[str, str] | None
    realm_identity_error: str | None
    session_epoch: uuid.UUID | None
    active: bool


class TargetsResponse(RequestModel):
    schema_version: Literal[1]
    installation_id: uuid.UUID
    active_target_id: uuid.UUID | None
    source: SourceTuple
    targets: list[TargetView]


class TargetProvidersResponse(SsoProviderCatalogResponse):
    target_id: uuid.UUID


@router.get("/admin/sso/targets", response_model=TargetsResponse)
async def list_targets(_actor: ProductAdminActor = Depends(get_metadata_actor)):
    pool = await get_pool()
    async with pool.acquire() as conn:
        installation = await conn.fetchval("SELECT installation_id FROM sso_operation_installation WHERE singleton")
        installed = management_targets.installed_targets()
        active = await management_targets.resolve(conn) if settings.require_auth_mode() == "sso" else None
    targets = {target.target_id: target for target in installed}
    if active:
        targets[active.target_id] = active
    views = []
    for target in targets.values():
        realm, error = None, None
        try:
            realm = await management_targets.read_realm(target)
        except ProviderControlError as exc:
            error = exc.code
        except Exception as exc:
            from app.exceptions import AKBError
            if not isinstance(exc, AKBError):
                raise
            error = exc.code
        views.append({"target_id": str(target.target_id), "realm_identity": realm, "realm_identity_error": error,
                      "session_epoch": str(target.session_epoch) if target.session_epoch else None,
                      "active": bool(active and active.target_id == target.target_id)})
    return {"schema_version": 1, "installation_id": str(installation), "active_target_id": str(active.target_id) if active else None,
            "source": source_tuple(), "targets": views}


@router.get("/admin/sso/targets/{target_id}/providers", response_model=TargetProvidersResponse)
async def target_providers(target_id: uuid.UUID, _actor: ProductAdminActor = Depends(get_metadata_actor)):
    pool = await get_pool()
    async with pool.acquire() as conn:
        target = await management_targets.resolve(conn, target_id)
    await management_targets.read_realm(target)
    from app.sso.registry import provider_types

    return {"schema_version": 1, "target_id": str(target_id), "auth_mode": "sso",
            "control_mode": target.control.control_mode, "supported_provider_types": list(provider_types()),
            "providers": [provider.admin_view() for provider in await target.control.list_providers(force_refresh=True)]}


@router.get("/admin/sso/targets/{target_id}/providers/{alias}/ownership", response_model=OwnershipResponse)
async def target_ownership(target_id: uuid.UUID, alias: str, _actor: ProductAdminActor = Depends(get_metadata_actor)):
    return await operations.ownership(alias, target_id=target_id)


@router.put("/admin/sso/targets/{target_id}/providers/{alias}/ownership", response_model=OwnershipResponse)
async def change_target_ownership(target_id: uuid.UUID, alias: str, request: OwnershipRequest,
                                  actor: ProductAdminActor = Depends(get_product_admin_mutation)):
    return await operations.set_ownership(alias, target_id=target_id, **request.model_dump(), actor_id=uuid.UUID(str(actor.user_id)))


class LoginChallengeResponse(RequestModel):
    schema_version: Literal[1]
    operation_id: uuid.UUID
    challenge_id: uuid.UUID
    akb_user_id: uuid.UUID
    login_url: str = Field(repr=False)
    expires_at: datetime


class LoginEvent(RequestModel):
    event_id: uuid.UUID
    operation_id: uuid.UUID
    challenge_id: uuid.UUID
    installation_id: uuid.UUID
    target_id: uuid.UUID
    akb_user_id: uuid.UUID
    issuer: str
    provider_alias: str
    subject: str
    runtime_generation: int
    sso_session_epoch: uuid.UUID
    source_revision: str | None
    cohort_sha256: str
    provider_revision: int | None
    provider_secret_revision: str | None
    occurred_at: datetime


class LoginEventsResponse(RequestModel):
    schema_version: Literal[1]
    operation_id: uuid.UUID
    challenge_id: uuid.UUID
    events: list[LoginEvent]


class LoginChallengeRequest(RequestModel):
    akb_user_id: uuid.UUID
    request_id: uuid.UUID | None = None


@router.post("/admin/sso/identity-operations/{operation_id}/login-challenges", response_model=LoginChallengeResponse)
async def operation_login_challenge(operation_id: uuid.UUID, request: LoginChallengeRequest,
                                    response: Response, authorization: str | None = Header(default=None)):
    from app.sso.callback_evidence import issue_challenge
    credential = operation_credential(authorization)
    if credential is None:
        raise HTTPException(401, detail={"code": "sso_operation_credential_invalid"})
    response.headers["Cache-Control"] = "no-store"
    return await issue_challenge(operation_id, credential, request.akb_user_id, request.request_id)


@router.get("/admin/sso/identity-operations/{operation_id}/login-events", response_model=LoginEventsResponse)
async def operation_login_events(operation_id: uuid.UUID, akb_user_id: uuid.UUID, challenge_id: uuid.UUID,
                                 request: Request, response: Response, authorization: str | None = Header(default=None)):
    from app.sso.callback_evidence import events
    credential = operation_credential(authorization)
    actor = None if credential else await get_metadata_actor(request, authorization=authorization)
    response.headers["Cache-Control"] = "no-store"
    return await events(operation_id, challenge_id, akb_user_id, credential=credential, actor=actor)


class AuthorityGrantRequest(RequestModel):
    operation_id: uuid.UUID
    expected_intent_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    ttl_seconds: int = Field(ge=1, le=600, strict=True)


class AuthorityGrantResponse(GrantResponse):
    parent_operation_id: uuid.UUID
    intent_sha256: str
    authority_attempt: int
    authority_scope: Literal["source-recovery", "target-evidence"]


@router.post("/admin/sso/identity-operations/{parent_operation_id}/authority-grants", response_model=AuthorityGrantResponse)
async def reauthorize_operation(parent_operation_id: uuid.UUID, request: AuthorityGrantRequest, response: Response,
                                 actor: ProductAdminActor = Depends(get_product_admin_mutation)):
    from app.sso.operation_attempts import reauthorize
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return await reauthorize(parent_operation_id, **request.model_dump(), granted_by=uuid.UUID(str(actor.user_id)))
