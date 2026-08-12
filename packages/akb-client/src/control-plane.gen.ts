/* Generated from the app control-plane OpenAPI projection. Do not edit manually. */

export type ControlPlaneJsonValue =
  | string
  | number
  | boolean
  | null
  | ControlPlaneJsonValue[]
  | { [key: string]: ControlPlaneJsonValue };
export type ControlPlaneJsonObject = { [key: string]: ControlPlaneJsonValue };
export type ControlPlaneUuid = string;
export type ControlPlaneDateTime = string;

export interface AppCreateRequest {
  app_key: string;
  display_name?: string | null;
  description?: string | null;
  metadata?: ControlPlaneJsonObject;
}

export interface AppUpdateRequest {
  display_name?: string | null;
  description?: string | null;
  metadata?: ControlPlaneJsonObject | null;
}

export interface ReleaseCreateRequest {
  version: string;
  manifest: ControlPlaneJsonObject;
  manifest_checksum: string;
}

export interface AppDefinitionProjection {
  id: ControlPlaneUuid;
  app_key: string;
  display_name?: string | null;
  description?: string | null;
  metadata: ControlPlaneJsonObject;
  created_at: ControlPlaneDateTime;
  updated_at: ControlPlaneDateTime;
  replayed?: boolean | null;
}

export interface AppReleaseProjection {
  id: ControlPlaneUuid;
  app_id: ControlPlaneUuid;
  version: string;
  manifest: ControlPlaneJsonObject;
  manifest_checksum: string;
  registered_at: ControlPlaneDateTime;
  replayed?: boolean | null;
}

export interface CredentialIssueRequest {
  deployment: string;
  expires_at?: ControlPlaneDateTime | null;
}

export interface CredentialRotateRequest {
  expires_at?: ControlPlaneDateTime | null;
}

export interface CredentialExchangeRequest {
  credential: string;
}

export interface AuthorizeRequest {
  vault_id: ControlPlaneUuid;
  capability: string;
  resource_kind?: string | null;
  resource_key?: string | null;
}

export interface CredentialMetadata {
  credential_id: ControlPlaneUuid;
  app_id: ControlPlaneUuid;
  deployment: string;
  prefix: string;
  status: "active" | "rotated" | "revoked";
  generation: number;
  expires_at?: ControlPlaneDateTime | null;
  overlap_until?: ControlPlaneDateTime | null;
  revoked_at?: ControlPlaneDateTime | null;
  last_exchanged_at?: ControlPlaneDateTime | null;
  created_at?: ControlPlaneDateTime | null;
  [key: string]: ControlPlaneJsonValue | undefined;
}

export interface CredentialIssueProjection extends CredentialMetadata {
  credential?: string | null;
  note?: string | null;
}

export interface CredentialListProjection {
  credentials: CredentialMetadata[];
}

export interface CredentialExchangeProjection {
  access_token: string;
  token_type: "Bearer";
  expires_in: number;
  expires_at: ControlPlaneDateTime;
  correlation_id: string;
}

export interface AuthorizeProjection {
  authorized: true;
  correlation_id: string;
}

export interface InstallationCommandRequest {
  release_id: ControlPlaneUuid;
  capabilities: string[];
  mode?: "install" | "restore" | "fresh";
}

export interface ReleaseReference {
  id?: ControlPlaneUuid | null;
  version?: string | null;
}

export interface GrantProjection {
  generation: number;
  status: "active" | "revoked";
  capabilities: string[];
}

export interface ObservedProjection {
  generation: number;
  observed_at?: ControlPlaneDateTime | null;
  release?: ReleaseReference | null;
  schema_fingerprint?: string | null;
  grant_generation?: number | null;
  checkpoint: ControlPlaneJsonObject;
  recent_error?: ControlPlaneJsonObject | null;
}

export interface OwnedResourceProjection {
  kind: string;
  key: string;
  status: "owned" | "retained";
}

export interface DriftDimension {
  status: "in_sync" | "mismatch" | "unknown";
  expected?: ControlPlaneJsonValue;
  actual?: ControlPlaneJsonValue;
}

export interface DriftProjection {
  release: DriftDimension;
  schema: DriftDimension;
  grant: DriftDimension;
  overall: "in_sync" | "drifted" | "unknown";
  reasons: string[];
  unknown_dimensions: string[];
}

export interface InstallationProjection {
  installation_id: ControlPlaneUuid;
  app_id: ControlPlaneUuid;
  vault_id: ControlPlaneUuid;
  lifecycle: "installing" | "active" | "upgrading" | "blocked" | "uninstalled";
  blocked_reason?: string | null;
  desired_release?: ReleaseReference | null;
  current_release?: ReleaseReference | null;
  observed?: ObservedProjection | null;
  desired_grant_generation?: number;
  latest_grant?: GrantProjection | null;
  active_grant?: GrantProjection | null;
  owned_resources?: OwnedResourceProjection[];
  checkpoint?: ControlPlaneJsonObject;
  recent_error?: ControlPlaneJsonObject | null;
  drift?: DriftProjection | null;
  drift_classification?: DriftProjection | null;
  created_at?: ControlPlaneDateTime | null;
  updated_at?: ControlPlaneDateTime | null;
  command_status?: "accepted" | "already_applied" | "not_applicable" | null;
  replayed?: boolean | null;
}

export interface ObservedStateRequest {
  installation_id: ControlPlaneUuid;
  observed_generation: number;
  observed_at?: ControlPlaneDateTime | null;
  observed_release_id?: ControlPlaneUuid | null;
  observed_release_version?: string | null;
  schema_fingerprint?: string | null;
  observed_grant_generation?: number | null;
  checkpoint?: ControlPlaneJsonObject;
  recent_error?: ControlPlaneJsonObject | null;
}

export interface ObservedStateProjection {
  accepted: boolean;
  installation_id: ControlPlaneUuid;
  observed_generation: number;
  observed_at: ControlPlaneDateTime;
}

export interface InventoryItem {
  installation_id: ControlPlaneUuid;
  app_id: ControlPlaneUuid;
  vault_id: ControlPlaneUuid;
  vault_name: string;
  lifecycle: InstallationProjection["lifecycle"];
  desired_release?: ReleaseReference | null;
  current_release?: ReleaseReference | null;
  observed?: ObservedProjection | null;
  latest_grant?: GrantProjection | null;
  latest_active_grant?: GrantProjection | null;
  grant_generation: number;
  checkpoint: ControlPlaneJsonObject;
  recent_error?: ControlPlaneJsonObject | null;
  drift: DriftProjection;
  drift_classification: DriftProjection;
  created_at: ControlPlaneDateTime;
  updated_at: ControlPlaneDateTime;
}

export interface InventoryProjection {
  items: InventoryItem[];
  next_cursor?: string | null;
}

export interface SnapshotCreateProjection {
  snapshot_id: ControlPlaneUuid;
  app_id?: ControlPlaneUuid | null;
  created_at?: ControlPlaneDateTime | null;
  sealed_at?: ControlPlaneDateTime | null;
  requested_by_kind?: "admin" | "app" | null;
  target_count?: number;
}

export interface SnapshotRequest {}

export interface SnapshotTargetProjection {
  target_id: ControlPlaneUuid;
  installation_id: ControlPlaneUuid;
  vault_id: ControlPlaneUuid;
  desired_release?: ReleaseReference | null;
  current_release?: ReleaseReference | null;
  baseline_grant_generation: number;
  state: "pending" | "running" | "denied" | "skipped" | "applied" | "replayed";
  reason_code?: string | null;
  created_at: ControlPlaneDateTime;
  updated_at: ControlPlaneDateTime;
}

export interface SnapshotProjection {
  snapshot_id: ControlPlaneUuid;
  app_id: ControlPlaneUuid;
  created_at: ControlPlaneDateTime;
  sealed_at?: ControlPlaneDateTime | null;
  requested_by_kind: "admin" | "app";
  target_count: number;
  targets: SnapshotTargetProjection[];
}

export interface EligibilityProjection {
  target_id: ControlPlaneUuid;
  eligible: boolean;
  executed: boolean;
  state: string;
  reason_code?: string | null;
}

export interface RolloutRequest {
  release_id: ControlPlaneUuid;
  manifest_checksum: string;
}

export interface RolloutResumeRequest extends RolloutRequest {}

export interface RolloutStepProjection {
  step_id: string;
  operation: string;
  state: string;
  checkpoint?: ControlPlaneJsonObject;
  reason?: string | null;
}

export interface RolloutTargetProjection {
  target_id: ControlPlaneUuid;
  installation_id: ControlPlaneUuid;
  vault_id: ControlPlaneUuid;
  ordinal: number;
  batch: number;
  canary: boolean;
  state: string;
  reason?: string | null;
  steps: RolloutStepProjection[];
}

export interface RolloutProjection {
  job_id: ControlPlaneUuid;
  app_id?: ControlPlaneUuid | null;
  release_id?: ControlPlaneUuid | null;
  manifest_checksum?: string | null;
  snapshot_id?: ControlPlaneUuid | null;
  status?: "pending" | "running" | "applied" | "blocked" | null;
  blocked_reason?: string | null;
  created_at?: ControlPlaneDateTime | null;
  updated_at?: ControlPlaneDateTime | null;
  completed_at?: ControlPlaneDateTime | null;
  targets: RolloutTargetProjection[];
  replayed?: boolean | null;
  source_rollout_id?: ControlPlaneUuid | null;
  resume_outcome?: "accepted" | "replayed" | "denied" | null;
  resume_reason?: string | null;
}

export interface ControlPlaneOperation<Parameters, RequestBody, Success> {
  parameters: Parameters;
  requestBody: RequestBody;
  responses: { success: Success };
}

export interface ControlPlaneOperations {
  appsCreate: ControlPlaneOperation<never, AppCreateRequest, AppDefinitionProjection>;
  appsGet: ControlPlaneOperation<{ path: { app_id: string } }, never, AppDefinitionProjection>;
  appsUpdate: ControlPlaneOperation<{ path: { app_id: string } }, AppUpdateRequest, AppDefinitionProjection>;
  appsCreateRelease: ControlPlaneOperation<{ path: { app_id: string } }, ReleaseCreateRequest, AppReleaseProjection>;
  appsGetRelease: ControlPlaneOperation<{ path: { app_id: string; release_id: string } }, never, AppReleaseProjection>;
  appsIssueCredential: ControlPlaneOperation<{ path: { app_id: string } }, CredentialIssueRequest, CredentialIssueProjection>;
  appsListCredentials: ControlPlaneOperation<{ path: { app_id: string } }, never, CredentialListProjection>;
  appsRotateCredential: ControlPlaneOperation<{ path: { app_id: string; credential_id: string } }, CredentialRotateRequest, CredentialIssueProjection>;
  appsRevokeCredential: ControlPlaneOperation<{ path: { app_id: string; credential_id: string } }, never, CredentialMetadata>;
  authExchangeAppCredential: ControlPlaneOperation<never, CredentialExchangeRequest, CredentialExchangeProjection>;
  appAuthorize: ControlPlaneOperation<never, AuthorizeRequest, AuthorizeProjection>;
  appsApplyInstallation: ControlPlaneOperation<{ path: { app_id: string; vault_id: string } }, InstallationCommandRequest, InstallationProjection>;
  appsGetInstallation: ControlPlaneOperation<{ path: { app_id: string; vault_id: string } }, never, InstallationProjection>;
  appsUninstallInstallation: ControlPlaneOperation<{ path: { app_id: string; vault_id: string } }, never, InstallationProjection>;
  appGetInstallation: ControlPlaneOperation<{ path: { vault_id: string } }, never, InstallationProjection>;
  appsListInventory: ControlPlaneOperation<{ path: { app_id: string } }, never, InventoryProjection>;
  appListInventory: ControlPlaneOperation<never, never, InventoryProjection>;
  appsReportObservedState: ControlPlaneOperation<{ path: { app_id: string } }, ObservedStateRequest, ObservedStateProjection>;
  appReportObservedState: ControlPlaneOperation<never, ObservedStateRequest, ObservedStateProjection>;
  appsCreateRolloutSnapshot: ControlPlaneOperation<{ path: { app_id: string } }, SnapshotRequest | null, SnapshotCreateProjection>;
  appCreateRolloutSnapshot: ControlPlaneOperation<never, SnapshotRequest | null, SnapshotCreateProjection>;
  appsGetRolloutSnapshot: ControlPlaneOperation<{ path: { app_id: string; snapshot_id: string } }, never, SnapshotProjection>;
  appGetRolloutSnapshot: ControlPlaneOperation<{ path: { snapshot_id: string } }, never, SnapshotProjection>;
  appsEvaluateRolloutTarget: ControlPlaneOperation<{ path: { app_id: string; snapshot_id: string; target_id: string } }, never, EligibilityProjection>;
  appEvaluateRolloutTarget: ControlPlaneOperation<{ path: { snapshot_id: string; target_id: string } }, never, EligibilityProjection>;
  appsRequestRollout: ControlPlaneOperation<{ path: { app_id: string }; header: { "Idempotency-Key": string } }, RolloutRequest, RolloutProjection>;
  appRequestRollout: ControlPlaneOperation<{ header: { "Idempotency-Key": string } }, RolloutRequest, RolloutProjection>;
  appsGetRollout: ControlPlaneOperation<{ path: { app_id: string; rollout_id: string } }, never, RolloutProjection>;
  appGetRollout: ControlPlaneOperation<{ path: { rollout_id: string } }, never, RolloutProjection>;
  appsResumeRollout: ControlPlaneOperation<{ path: { app_id: string; rollout_id: string }; header: { "Idempotency-Key": string } }, RolloutResumeRequest, RolloutProjection>;
  appResumeRollout: ControlPlaneOperation<{ path: { rollout_id: string }; header: { "Idempotency-Key": string } }, RolloutResumeRequest, RolloutProjection>;
}

export type ControlPlaneOperationId = keyof ControlPlaneOperations;
