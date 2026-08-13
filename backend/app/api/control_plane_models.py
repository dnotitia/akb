"""Typed wire models for the app control-plane REST surface.

The control plane is intentionally modelled separately from the data-plane
envelopes.  The models describe the projections that the services already
return; they do not own any state or introduce a second response format.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, TypeAlias
import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.util.text import NFCModel


# Pydantic 2.13 eagerly expands recursive aliases while building OpenAPI
# schemas.  A bounded JSON leaf keeps the wire contract typed without making
# model import depend on recursion support in the installed Pydantic minor.
JsonValue: TypeAlias = str | int | float | bool | None | list[object] | dict[str, object]
JsonObject: TypeAlias = dict[str, object]


class ControlPlaneModel(BaseModel):
    """Base for public projections; unknown service fields remain bounded JSON."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class ControlPlaneRequest(NFCModel):
    model_config = ConfigDict(extra="forbid")


class AppCreateRequest(ControlPlaneRequest):
    app_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    display_name: str | None = Field(default=None, max_length=256)
    description: str | None = Field(default=None, max_length=4096)
    metadata: JsonObject = Field(default_factory=dict)


class AppUpdateRequest(ControlPlaneRequest):
    display_name: str | None = Field(default=None, max_length=256)
    description: str | None = Field(default=None, max_length=4096)
    metadata: JsonObject | None = None


class ReleaseManifest(BaseModel):
    """Published release manifest shape shared by registry and rollout APIs."""

    model_config = ConfigDict(extra="allow")

    steps: list[JsonObject]


class ReleaseCreateRequest(ControlPlaneRequest):
    version: str = Field(min_length=1, max_length=256)
    manifest: ReleaseManifest
    manifest_checksum: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$")


class AppDefinitionProjection(ControlPlaneModel):
    id: uuid.UUID
    app_key: str
    display_name: str | None = None
    description: str | None = None
    metadata: JsonObject = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    replayed: bool | None = None


class AppReleaseProjection(ControlPlaneModel):
    id: uuid.UUID
    app_id: uuid.UUID
    version: str
    manifest: ReleaseManifest
    manifest_checksum: str
    registered_at: datetime
    replayed: bool | None = None


class CredentialIssueRequest(ControlPlaneRequest):
    deployment: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    expires_at: datetime | None = None


class CredentialRotateRequest(ControlPlaneRequest):
    expires_at: datetime | None = None


class CredentialExchangeRequest(ControlPlaneRequest):
    credential: str = Field(min_length=1, max_length=512)


class CredentialMetadata(ControlPlaneModel):
    credential_id: uuid.UUID
    app_id: uuid.UUID
    deployment: str
    prefix: str
    status: Literal["active", "rotated", "revoked"]
    generation: int
    expires_at: datetime | None = None
    overlap_until: datetime | None = None
    revoked_at: datetime | None = None
    last_exchanged_at: datetime | None = None
    created_at: datetime | None = None


class CredentialIssueProjection(CredentialMetadata):
    credential: str | None = None
    note: str | None = None


class CredentialListProjection(ControlPlaneModel):
    credentials: list[CredentialMetadata]


class CredentialExchangeProjection(ControlPlaneModel):
    access_token: str
    token_type: Literal["Bearer"]
    expires_in: int
    expires_at: datetime
    correlation_id: str


class AuthorizeRequest(ControlPlaneRequest):
    vault_id: uuid.UUID
    capability: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    resource_kind: str | None = None
    resource_key: str | None = None


class AuthorizeProjection(ControlPlaneModel):
    authorized: Literal[True]
    correlation_id: str


class InstallationCommandRequest(ControlPlaneRequest):
    release_id: uuid.UUID
    capabilities: list[str] = Field(min_length=1, max_length=32)
    mode: Literal["install", "restore", "fresh"] = "install"


class ReleaseReference(ControlPlaneModel):
    id: uuid.UUID | None = None
    version: str | None = None


class GrantProjection(ControlPlaneModel):
    generation: int
    status: Literal["active", "revoked"]
    capabilities: list[str]


class ObservedProjection(ControlPlaneModel):
    generation: int
    observed_at: datetime | None = None
    release: ReleaseReference | None = None
    schema_fingerprint: str | None = None
    grant_generation: int | None = None
    checkpoint: JsonObject = Field(default_factory=dict)
    recent_error: JsonObject | None = None


class OwnedResourceProjection(ControlPlaneModel):
    kind: str
    key: str
    status: Literal["owned", "retained"]


class DriftDimension(ControlPlaneModel):
    status: Literal["in_sync", "mismatch", "unknown"]
    expected: JsonValue = None
    actual: JsonValue = None


class DriftProjection(ControlPlaneModel):
    release: DriftDimension
    schema_: DriftDimension = Field(alias="schema")
    grant: DriftDimension
    overall: Literal["in_sync", "drifted", "unknown"]
    reasons: list[str]
    unknown_dimensions: list[str]


class InstallationProjection(ControlPlaneModel):
    installation_id: uuid.UUID
    app_id: uuid.UUID
    vault_id: uuid.UUID
    lifecycle: Literal["installing", "active", "upgrading", "blocked", "uninstalled"]
    blocked_reason: str | None = None
    desired_release: ReleaseReference | None = None
    current_release: ReleaseReference | None = None
    observed: ObservedProjection | None = None
    desired_grant_generation: int = 0
    latest_grant: GrantProjection | None = None
    active_grant: GrantProjection | None = None
    owned_resources: list[OwnedResourceProjection] = Field(default_factory=list)
    checkpoint: JsonObject = Field(default_factory=dict)
    recent_error: JsonObject | None = None
    drift: DriftProjection | None = None
    drift_classification: DriftProjection | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    command_status: Literal["accepted", "already_applied", "not_applicable"] | None = None
    replayed: bool | None = None


class ObservedStateRequest(ControlPlaneRequest):
    installation_id: uuid.UUID
    observed_generation: int = Field(ge=0)
    observed_at: datetime | None = None
    observed_release_id: uuid.UUID | None = None
    observed_release_version: str | None = Field(default=None, max_length=256)
    schema_fingerprint: str | None = Field(default=None, max_length=256)
    observed_grant_generation: int | None = Field(default=None, ge=0)
    checkpoint: JsonObject = Field(default_factory=dict)
    recent_error: JsonObject | None = None


class ObservedStateProjection(ControlPlaneModel):
    accepted: bool
    installation_id: uuid.UUID
    observed_generation: int
    observed_at: datetime


class InventoryItem(ControlPlaneModel):
    installation_id: uuid.UUID
    app_id: uuid.UUID
    vault_id: uuid.UUID
    vault_name: str
    lifecycle: Literal["installing", "active", "upgrading", "blocked", "uninstalled"]
    desired_release: ReleaseReference | None = None
    current_release: ReleaseReference | None = None
    observed: ObservedProjection | None = None
    latest_grant: GrantProjection | None = None
    latest_active_grant: GrantProjection | None = None
    grant_generation: int
    checkpoint: JsonObject
    recent_error: JsonObject | None = None
    drift: DriftProjection
    drift_classification: DriftProjection
    created_at: datetime
    updated_at: datetime


class InventoryProjection(ControlPlaneModel):
    items: list[InventoryItem]
    next_cursor: str | None = None


class SnapshotRequest(ControlPlaneRequest):
    """Reserved empty request body for future snapshot labels."""


class SnapshotCreateProjection(ControlPlaneModel):
    snapshot_id: uuid.UUID
    app_id: uuid.UUID | None = None
    created_at: datetime | None = None
    sealed_at: datetime | None = None
    requested_by_kind: Literal["admin", "app"] | None = None
    target_count: int = 0


class SnapshotTargetProjection(ControlPlaneModel):
    target_id: uuid.UUID
    installation_id: uuid.UUID
    vault_id: uuid.UUID
    desired_release: ReleaseReference | None = None
    current_release: ReleaseReference | None = None
    baseline_grant_generation: int
    state: Literal["pending", "running", "denied", "skipped", "applied", "replayed"]
    reason_code: str | None = None
    created_at: datetime
    updated_at: datetime


class SnapshotProjection(ControlPlaneModel):
    snapshot_id: uuid.UUID
    app_id: uuid.UUID
    created_at: datetime
    sealed_at: datetime | None = None
    requested_by_kind: Literal["admin", "app"]
    target_count: int
    targets: list[SnapshotTargetProjection]


class EligibilityProjection(ControlPlaneModel):
    target_id: uuid.UUID
    eligible: bool
    executed: bool
    state: str
    reason_code: str | None = None


class RolloutRequest(ControlPlaneRequest):
    release_id: uuid.UUID
    manifest_checksum: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$")


class RolloutResumeRequest(ControlPlaneRequest):
    release_id: uuid.UUID
    manifest_checksum: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$")


class RolloutStepProjection(ControlPlaneModel):
    step_id: str
    operation: str
    state: str
    checkpoint: JsonObject = Field(default_factory=dict)
    reason: str | None = None


class RolloutTargetProjection(ControlPlaneModel):
    target_id: uuid.UUID
    installation_id: uuid.UUID
    vault_id: uuid.UUID
    ordinal: int
    batch: int
    canary: bool
    state: str
    reason: str | None = None
    steps: list[RolloutStepProjection]


class RolloutProjection(ControlPlaneModel):
    job_id: uuid.UUID
    app_id: uuid.UUID | None = None
    release_id: uuid.UUID | None = None
    manifest_checksum: str | None = None
    snapshot_id: uuid.UUID | None = None
    status: Literal["pending", "running", "applied", "blocked"] | None = None
    blocked_reason: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None
    targets: list[RolloutTargetProjection] = Field(default_factory=list)
    replayed: bool | None = None
    source_rollout_id: uuid.UUID | None = None
    resume_outcome: Literal["accepted", "replayed", "denied"] | None = None
    resume_reason: str | None = None
