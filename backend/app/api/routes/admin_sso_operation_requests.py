"""Operation requests: proposed by a service key, decided by a human administrator.

No route here returns an operation credential to a human. The only credential
in any response is the one the requesting token collects, sealed to the public
key it supplied with its request.
"""

from datetime import datetime
from typing import Annotated, Literal
import uuid

from fastapi import APIRouter, Body, Depends, Header, Request, Response
from pydantic import Field

from app.api.deps import get_sso_operation_request_service_key
from app.api.routes.admin_auth import ProductAdminActor, get_current_product_admin, get_product_admin_mutation
from app.api.routes.admin_sso_operations import RequestModel, SourceTuple, ValueLessRoute
from app.config import settings
from app.exceptions import AuthenticationError, ForbiddenError
from app.sso import operation_requests


router = APIRouter(route_class=ValueLessRoute)
_ALIAS = r"^[a-z0-9][a-z0-9._-]{0,62}$"
_SHA256 = r"^[a-f0-9]{64}$"


async def get_requesting_service_key(request: Request, authorization: str | None = Header(default=None)):
    """The dedicated request key, and nothing else: no session or cookie."""
    return await get_sso_operation_request_service_key(authorization=authorization)


async def get_request_reader(request: Request, authorization: str | None = Header(default=None)):
    """A human administrator, or the dedicated request key.

    The human is the product-admin session in SSO mode and the local
    product-admin bearer in local mode, resolved the way the existing
    operation metadata reader does it: a local human bearer first, then the
    dedicated request key. A bearer that is neither is refused.
    """
    if authorization:
        if settings.require_auth_mode() == "local":
            try:
                return await get_current_product_admin(request, authorization=authorization)
            except AuthenticationError:
                pass
        return await get_requesting_service_key(request, authorization=authorization)
    return await get_current_product_admin(request, authorization=None)


class ConfigureSummary(RequestModel):
    """Every non-secret configure field, and only whether a secret is supplied."""

    provider_type: str = Field(min_length=1, max_length=80)
    display_name: str = Field(min_length=1, max_length=80)
    issuer: str = Field(min_length=1, max_length=2048)
    discovery_url: str = Field(min_length=1, max_length=2048)
    client_id: str = Field(min_length=1, max_length=255)
    client_secret_supplied: bool = Field(strict=True)


ProviderVerb = Literal["configure", "enable", "disable"]


class OwnershipClaimProposal(RequestModel):
    kind: Literal["ownership_claim"]
    provider_alias: str = Field(pattern=_ALIAS)
    owner_id: str = Field(min_length=1, max_length=255)
    expected_revision: int = Field(ge=0, strict=True)
    expected_fingerprint: str = Field(pattern=_SHA256)


class ProviderGrantProposal(RequestModel):
    kind: Literal["provider_grant"]
    operation_id: uuid.UUID
    provider_alias: str = Field(pattern=_ALIAS)
    owner_id: str = Field(min_length=1, max_length=255)
    expected_provider_revision: int = Field(ge=1, strict=True)
    verbs: list[ProviderVerb] = Field(min_length=1, max_length=3)
    provider_payload_sha256: dict[ProviderVerb, Annotated[str, Field(pattern=_SHA256)]]
    configure_summary: ConfigureSummary | None = None
    ttl_seconds: int = Field(ge=1, le=600, strict=True)
    provider_secret_revision: str | None = Field(default=None, min_length=1, max_length=255)
    # Raw 32-byte X25519 public key, unpadded base64url.
    recipient_public_key: str = Field(min_length=43, max_length=43)


Proposal = Annotated[OwnershipClaimProposal | ProviderGrantProposal, Field(discriminator="kind")]


class ClaimTerms(RequestModel):
    expected_revision: int
    expected_fingerprint: str


class GrantTerms(RequestModel):
    operation_id: uuid.UUID
    source: SourceTuple
    expected_provider_revision: int
    verbs: list[ProviderVerb]
    provider_payload_sha256: dict[str, str]
    configure_summary: ConfigureSummary | None
    ttl_seconds: int
    provider_secret_revision: str | None
    recipient_public_key: str


class RequestDocument(RequestModel):
    """The exact object whose canonical JSON SHA-256 is ``request_sha256``."""

    schema_version: Literal[1]
    request_id: uuid.UUID
    kind: Literal["ownership_claim", "provider_grant"]
    installation_id: uuid.UUID
    target_id: uuid.UUID
    provider_alias: str
    owner_id: str
    requested_by: uuid.UUID
    requested_token_id: uuid.UUID
    claim: ClaimTerms | None
    grant: GrantTerms | None


class OperationRequestView(RequestModel):
    schema_version: Literal[1]
    request_id: uuid.UUID
    request_sha256: str
    request: RequestDocument
    status: Literal["pending", "approved", "denied", "cancelled", "expired", "collected"]
    created_at: datetime
    expires_at: datetime
    requester_username: str | None
    decided_by: uuid.UUID | None
    decider_username: str | None
    decided_at: datetime | None
    operation_expires_at: datetime | None
    closed_at: datetime | None


class OperationRequestList(RequestModel):
    schema_version: Literal[1]
    requests: list[OperationRequestView]


class Decision(RequestModel):
    expected_request_sha256: str = Field(pattern=_SHA256)


class SealDescription(RequestModel):
    mode: Literal["base"]
    suite: str
    info: str


class SealedCredentialResponse(RequestModel):
    schema_version: Literal[1]
    request_id: uuid.UUID
    request_sha256: str
    operation_id: uuid.UUID
    expires_at: datetime
    provider_revision: int
    sealed_credential: str = Field(repr=False)
    seal: SealDescription


@router.post("/admin/sso/operation-requests", response_model=OperationRequestView, status_code=201)
async def propose_operation_request(proposal: Annotated[Proposal, Body()],
                                    actor=Depends(get_requesting_service_key)):
    return await operation_requests.propose(proposal.model_dump(), actor=actor)


@router.get("/admin/sso/operation-requests", response_model=OperationRequestList)
async def list_operation_requests(actor=Depends(get_request_reader)):
    return await operation_requests.list_requests(actor=actor)


@router.get("/admin/sso/operation-requests/{request_id}", response_model=OperationRequestView)
async def read_operation_request(request_id: uuid.UUID, actor=Depends(get_request_reader)):
    return await operation_requests.read(request_id, actor=actor)


@router.post("/admin/sso/operation-requests/{request_id}/approve", response_model=OperationRequestView)
async def approve_operation_request(request_id: uuid.UUID, decision: Decision,
                                    actor: ProductAdminActor = Depends(get_product_admin_mutation)):
    return await operation_requests.approve(request_id, expected_request_sha256=decision.expected_request_sha256,
                                            human_id=_human(actor))


@router.post("/admin/sso/operation-requests/{request_id}/deny", response_model=OperationRequestView)
async def deny_operation_request(request_id: uuid.UUID, decision: Decision,
                                 actor: ProductAdminActor = Depends(get_product_admin_mutation)):
    return await operation_requests.deny(request_id, expected_request_sha256=decision.expected_request_sha256,
                                         human_id=_human(actor))


@router.post("/admin/sso/operation-requests/{request_id}/cancel", response_model=OperationRequestView)
async def cancel_operation_request(request_id: uuid.UUID, actor=Depends(get_requesting_service_key)):
    return await operation_requests.cancel(request_id, actor=actor)


@router.post("/admin/sso/operation-requests/{request_id}/credential", response_model=SealedCredentialResponse)
async def collect_operation_credential(request_id: uuid.UUID, response: Response,
                                       actor=Depends(get_requesting_service_key)):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return await operation_requests.collect(request_id, actor=actor)


def _human(actor) -> uuid.UUID:
    # The product-admin mutation dependency only yields a human session here;
    # keep a service account out even if that dependency ever widens.
    if getattr(actor, "account_kind", "human") != "human":
        raise ForbiddenError("A human administrator must decide an operation request",
                             code="sso_operation_request_human_required")
    return uuid.UUID(str(actor.user_id))
