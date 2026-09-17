import { AkbError, akbFetch } from "./index.js";
import type { AkbResult } from "./index.js";
import type {
  AppCreateRequest,
  AppDefinitionProjection,
  AppReleaseProjection,
  AppUpdateRequest,
  AuthorizeProjection,
  AuthorizeRequest,
  ControlPlaneDateTime,
  ControlPlaneJsonObject,
  ControlPlaneOperationId,
  CredentialExchangeProjection,
  CredentialExchangeRequest,
  CredentialIssueProjection,
  CredentialIssueRequest,
  CredentialListProjection,
  CredentialMetadata,
  CredentialRotateRequest,
  EligibilityProjection,
  InstallationCommandRequest,
  InstallationProjection,
  InventoryProjection,
  LegacyAdoptionCreateRequest,
  LegacyAdoptionProjection,
  LegacyAdoptionTargetProjection,
  LegacyAdoptionTargetRequest,
  ObservedStateProjection,
  ObservedStateRequest,
  RolloutProjection,
  RolloutRequest,
  RolloutResumeRequest,
  DesiredSchemaProjection,
  ManifestColumn,
  ManifestIndex,
  ManifestIndexColumn,
  ManifestTable,
  ManifestUniqueKey,
  ReleaseManifest,
  ReleaseCreateRequest,
  SnapshotCreateProjection,
  SnapshotProjection,
  SnapshotRequest,
} from "./control-plane.gen.js";

export type {
  AppCreateRequest,
  AppDefinitionProjection,
  AppReleaseProjection,
  AppUpdateRequest,
  AuthorizeProjection,
  AuthorizeRequest,
  ControlPlaneDateTime,
  ControlPlaneJsonObject,
  ControlPlaneJsonValue,
  ControlPlaneOperation,
  ControlPlaneOperationId,
  ControlPlaneOperations,
  ControlPlaneUuid,
  CredentialExchangeProjection,
  CredentialExchangeRequest,
  CredentialIssueProjection,
  CredentialIssueRequest,
  CredentialListProjection,
  CredentialMetadata,
  CredentialRotateRequest,
  DriftDimension,
  DriftProjection,
  DesiredSchemaProjection,
  EligibilityProjection,
  GrantProjection,
  InstallationCommandRequest,
  InstallationProjection,
  InventoryItem,
  InventoryProjection,
  LegacyAdoptionCreateRequest,
  LegacyAdoptionProjection,
  LegacyAdoptionTargetProjection,
  LegacyAdoptionTargetRequest,
  ManifestColumn,
  ManifestIndex,
  ManifestIndexColumn,
  ManifestTable,
  ManifestUniqueKey,
  ObservedProjection,
  ObservedStateProjection,
  ObservedStateRequest,
  OwnedResourceProjection,
  ReleaseCreateRequest,
  ReleaseManifest,
  ReleaseReference,
  RolloutProjection,
  RolloutRequest,
  RolloutResumeRequest,
  RolloutStepProjection,
  RolloutTargetProjection,
  SnapshotCreateProjection,
  SnapshotProjection,
  SnapshotRequest,
  SnapshotTargetProjection,
  TransitionPlan,
  TransitionSource,
} from "./control-plane.gen.js";

export interface ControlPlaneAdminClientConfig {
  baseUrl: string;
  adminToken: string | (() => string | null | undefined);
  fetch?: typeof fetch;
}

export interface ControlPlaneAppClientConfig {
  baseUrl: string;
  appToken: string | (() => string | null | undefined);
  fetch?: typeof fetch;
}

export interface AppCredentialExchangeConfig {
  baseUrl: string;
  credential: string;
  signal?: AbortSignal | null;
  fetch?: typeof fetch;
}

export interface ControlPlaneRequestOptions {
  signal?: AbortSignal | null;
}

export interface ControlPlaneListInventoryOptions extends ControlPlaneRequestOptions {
  limit?: number;
  cursor?: string | null;
  lifecycle?: string | null;
}

export interface ControlPlaneListCredentialsOptions extends ControlPlaneRequestOptions {
  deployment?: string | null;
}

export type ControlPlaneClientError = AkbError;

type ControlPlaneResult<T> = Promise<AkbResult<T, ControlPlaneClientError>>;
type TokenSource = string | (() => string | null | undefined);

interface Requester {
  call<T>(
    method: string,
    path: string,
    body?: unknown,
    headers?: HeadersInit,
    options?: ControlPlaneRequestOptions,
  ): ControlPlaneResult<T>;
}

export interface ControlPlaneAdminApps {
  create(input: AppCreateRequest, options?: ControlPlaneRequestOptions): ControlPlaneResult<AppDefinitionProjection>;
  get(appId: string, options?: ControlPlaneRequestOptions): ControlPlaneResult<AppDefinitionProjection>;
  update(appId: string, input: AppUpdateRequest, options?: ControlPlaneRequestOptions): ControlPlaneResult<AppDefinitionProjection>;
}

export interface ControlPlaneAdminReleases {
  create(appId: string, input: ReleaseCreateRequest, options?: ControlPlaneRequestOptions): ControlPlaneResult<AppReleaseProjection>;
  get(appId: string, releaseId: string, options?: ControlPlaneRequestOptions): ControlPlaneResult<AppReleaseProjection>;
}

export interface ControlPlaneAdminCredentials {
  issue(appId: string, input: CredentialIssueRequest, options?: ControlPlaneRequestOptions): ControlPlaneResult<CredentialIssueProjection>;
  list(appId: string, options?: ControlPlaneListCredentialsOptions): ControlPlaneResult<CredentialListProjection>;
  rotate(appId: string, credentialId: string, input?: CredentialRotateRequest, options?: ControlPlaneRequestOptions): ControlPlaneResult<CredentialIssueProjection>;
  revoke(appId: string, credentialId: string, options?: ControlPlaneRequestOptions): ControlPlaneResult<CredentialMetadata>;
}

export interface ControlPlaneAdminInstallations {
  apply(appId: string, vaultId: string, input: InstallationCommandRequest, options?: ControlPlaneRequestOptions): ControlPlaneResult<InstallationProjection>;
  get(appId: string, vaultId: string, options?: ControlPlaneRequestOptions): ControlPlaneResult<InstallationProjection>;
  uninstall(appId: string, vaultId: string, options?: ControlPlaneRequestOptions): ControlPlaneResult<InstallationProjection>;
}

export interface ControlPlaneAdminInventory {
  list(appId: string, options?: ControlPlaneListInventoryOptions): ControlPlaneResult<InventoryProjection>;
  reportObserved(appId: string, input: ObservedStateRequest, options?: ControlPlaneRequestOptions): ControlPlaneResult<ObservedStateProjection>;
}

export interface ControlPlaneAdminSnapshots {
  create(appId: string, options?: ControlPlaneRequestOptions): ControlPlaneResult<SnapshotCreateProjection>;
  get(appId: string, snapshotId: string, options?: ControlPlaneRequestOptions): ControlPlaneResult<SnapshotProjection>;
  evaluate(appId: string, snapshotId: string, targetId: string, options?: ControlPlaneRequestOptions): ControlPlaneResult<EligibilityProjection>;
}

export interface ControlPlaneAdminRollouts {
  request(appId: string, input: RolloutRequest, idempotencyKey: string, options?: ControlPlaneRequestOptions): ControlPlaneResult<RolloutProjection>;
  get(appId: string, rolloutId: string, options?: ControlPlaneRequestOptions): ControlPlaneResult<RolloutProjection>;
  resume(appId: string, rolloutId: string, input: RolloutResumeRequest, idempotencyKey: string, options?: ControlPlaneRequestOptions): ControlPlaneResult<RolloutProjection>;
}

export interface ControlPlaneAdminClient {
  readonly apps: ControlPlaneAdminApps;
  readonly releases: ControlPlaneAdminReleases;
  readonly credentials: ControlPlaneAdminCredentials;
  readonly installations: ControlPlaneAdminInstallations;
  readonly inventory: ControlPlaneAdminInventory;
  readonly snapshots: ControlPlaneAdminSnapshots;
  readonly rollouts: ControlPlaneAdminRollouts;
}

export interface ControlPlaneAppInstallations {
  get(vaultId: string, options?: ControlPlaneRequestOptions): ControlPlaneResult<InstallationProjection>;
}

export interface ControlPlaneAppInventory {
  list(options?: ControlPlaneListInventoryOptions): ControlPlaneResult<InventoryProjection>;
  reportObserved(input: ObservedStateRequest, options?: ControlPlaneRequestOptions): ControlPlaneResult<ObservedStateProjection>;
}

export interface ControlPlaneAppSnapshots {
  create(options?: ControlPlaneRequestOptions): ControlPlaneResult<SnapshotCreateProjection>;
  get(snapshotId: string, options?: ControlPlaneRequestOptions): ControlPlaneResult<SnapshotProjection>;
  evaluate(snapshotId: string, targetId: string, options?: ControlPlaneRequestOptions): ControlPlaneResult<EligibilityProjection>;
}

export interface ControlPlaneAppRollouts {
  request(input: RolloutRequest, idempotencyKey: string, options?: ControlPlaneRequestOptions): ControlPlaneResult<RolloutProjection>;
  get(rolloutId: string, options?: ControlPlaneRequestOptions): ControlPlaneResult<RolloutProjection>;
  resume(rolloutId: string, input: RolloutResumeRequest, idempotencyKey: string, options?: ControlPlaneRequestOptions): ControlPlaneResult<RolloutProjection>;
}

export interface ControlPlaneAppClient {
  readonly authorize: (input: AuthorizeRequest, options?: ControlPlaneRequestOptions) => ControlPlaneResult<AuthorizeProjection>;
  readonly installations: ControlPlaneAppInstallations;
  readonly inventory: ControlPlaneAppInventory;
  readonly snapshots: ControlPlaneAppSnapshots;
  readonly rollouts: ControlPlaneAppRollouts;
}

/** Exchange a deployment credential without retaining it in the returned client state. */
export function exchangeAppCredential(
  config: AppCredentialExchangeConfig,
): ControlPlaneResult<CredentialExchangeProjection> {
  requireNonEmpty(config.credential, "credential");
  const requester = makeRequester(config.baseUrl, undefined, config.fetch);
  return requester.call<CredentialExchangeProjection>(
    "POST",
    "/auth/app-token",
    { credential: config.credential } satisfies CredentialExchangeRequest,
    undefined,
    { signal: config.signal },
  );
}

export function createControlPlaneAdminClient(
  config: ControlPlaneAdminClientConfig,
): ControlPlaneAdminClient {
  const requester = makeRequester(config.baseUrl, config.adminToken, config.fetch);
  const client: ControlPlaneAdminClient = {
    apps: {
      create: (input, options) => requester.call("POST", "/apps", input, undefined, options),
      get: (appId, options) => requester.call("GET", `/apps/${segment(appId)}`, undefined, undefined, options),
      update: (appId, input, options) => requester.call("PATCH", `/apps/${segment(appId)}`, input, undefined, options),
    },
    releases: {
      create: (appId, input, options) => requester.call("POST", `/apps/${segment(appId)}/releases`, input, undefined, options),
      get: (appId, releaseId, options) => requester.call("GET", `/apps/${segment(appId)}/releases/${segment(releaseId)}`, undefined, undefined, options),
    },
    credentials: {
      issue: (appId, input, options) => requester.call("POST", `/apps/${segment(appId)}/credentials`, input, undefined, options),
      list: (appId, options) => requester.call("GET", withQuery(`/apps/${segment(appId)}/credentials`, {
        deployment: options?.deployment,
      }), undefined, undefined, options),
      rotate: (appId, credentialId, input = {}, options) => requester.call("POST", `/apps/${segment(appId)}/credentials/${segment(credentialId)}/rotate`, input, undefined, options),
      revoke: (appId, credentialId, options) => requester.call("DELETE", `/apps/${segment(appId)}/credentials/${segment(credentialId)}`, undefined, undefined, options),
    },
    installations: {
      apply: (appId, vaultId, input, options) => requester.call("PUT", `/apps/${segment(appId)}/installations/${segment(vaultId)}`, input, undefined, options),
      get: (appId, vaultId, options) => requester.call("GET", `/apps/${segment(appId)}/installations/${segment(vaultId)}`, undefined, undefined, options),
      uninstall: (appId, vaultId, options) => requester.call("DELETE", `/apps/${segment(appId)}/installations/${segment(vaultId)}`, undefined, undefined, options),
    },
    inventory: {
      list: (appId, options) => requester.call("GET", withQuery(`/apps/${segment(appId)}/inventory`, options), undefined, undefined, options),
      reportObserved: (appId, input, options) => requester.call("POST", `/apps/${segment(appId)}/observed-state`, input, undefined, options),
    },
    snapshots: {
      create: (appId, options) => requester.call("POST", `/apps/${segment(appId)}/rollout-snapshots`, undefined, undefined, options),
      get: (appId, snapshotId, options) => requester.call("GET", `/apps/${segment(appId)}/rollout-snapshots/${segment(snapshotId)}`, undefined, undefined, options),
      evaluate: (appId, snapshotId, targetId, options) => requester.call("POST", `/apps/${segment(appId)}/rollout-snapshots/${segment(snapshotId)}/targets/${segment(targetId)}/eligibility`, undefined, undefined, options),
    },
    rollouts: {
      request: (appId, input, idempotencyKey, options) => requester.call("POST", `/apps/${segment(appId)}/rollouts`, input, idempotencyHeaders(idempotencyKey), options),
      get: (appId, rolloutId, options) => requester.call("GET", `/apps/${segment(appId)}/rollouts/${segment(rolloutId)}`, undefined, undefined, options),
      resume: (appId, rolloutId, input, idempotencyKey, options) => requester.call("POST", `/apps/${segment(appId)}/rollouts/${segment(rolloutId)}/resume`, input, idempotencyHeaders(idempotencyKey), options),
    },
  };
  return freezeClient(client);
}

export function createControlPlaneAppClient(
  config: ControlPlaneAppClientConfig,
): ControlPlaneAppClient {
  const requester = makeRequester(config.baseUrl, config.appToken, config.fetch);
  const client: ControlPlaneAppClient = {
    authorize: (input, options) => requester.call("POST", "/app/authorize", input, undefined, options),
    installations: {
      get: (vaultId, options) => requester.call("GET", `/app/installations/${segment(vaultId)}`, undefined, undefined, options),
    },
    inventory: {
      list: (options) => requester.call("GET", withQuery("/app/inventory", options), undefined, undefined, options),
      reportObserved: (input, options) => requester.call("POST", "/app/observed-state", input, undefined, options),
    },
    snapshots: {
      create: (options) => requester.call("POST", "/app/rollout-snapshots", undefined, undefined, options),
      get: (snapshotId, options) => requester.call("GET", `/app/rollout-snapshots/${segment(snapshotId)}`, undefined, undefined, options),
      evaluate: (snapshotId, targetId, options) => requester.call("POST", `/app/rollout-snapshots/${segment(snapshotId)}/targets/${segment(targetId)}/eligibility`, undefined, undefined, options),
    },
    rollouts: {
      request: (input, idempotencyKey, options) => requester.call("POST", "/app/rollouts", input, idempotencyHeaders(idempotencyKey), options),
      get: (rolloutId, options) => requester.call("GET", `/app/rollouts/${segment(rolloutId)}`, undefined, undefined, options),
      resume: (rolloutId, input, idempotencyKey, options) => requester.call("POST", `/app/rollouts/${segment(rolloutId)}/resume`, input, idempotencyHeaders(idempotencyKey), options),
    },
  };
  return freezeClient(client);
}

function makeRequester(
  baseUrl: string,
  token: TokenSource | undefined,
  fetchImpl: typeof fetch | undefined,
): Requester {
  const normalizedBaseUrl = normalizeBaseUrl(baseUrl);
  return {
    async call<T>(
      method: string,
      path: string,
      body: unknown = undefined,
      extraHeaders: HeadersInit | undefined = undefined,
      options: ControlPlaneRequestOptions | undefined = undefined,
    ): ControlPlaneResult<T> {
      const headers = new Headers(extraHeaders);
      if (body !== undefined) {
        headers.set("content-type", "application/json");
      }
      const resolvedToken = token === undefined ? null : resolveToken(token);
      if (resolvedToken !== null && !headers.has("authorization")) {
        headers.set("authorization", `Bearer ${resolvedToken}`);
      }
      return akbFetch<T>(`${normalizedBaseUrl}${path}`, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        ...(options?.signal === undefined ? {} : { signal: options.signal }),
      }, fetchImpl);
    },
  };
}

function normalizeBaseUrl(value: string): string {
  const trimmed = value.trim().replace(/\/+$/u, "");
  if (!trimmed) throw new TypeError("baseUrl must not be empty");
  return trimmed.endsWith("/api/v1") ? trimmed : `${trimmed}/api/v1`;
}

function resolveToken(source: TokenSource): string | null {
  const value = typeof source === "function" ? source() : source;
  return typeof value === "string" && value.length > 0 ? value : null;
}

function segment(value: string): string {
  requireNonEmpty(value, "path parameter");
  return encodeURIComponent(value);
}

function requireNonEmpty(value: string, label: string): void {
  if (typeof value !== "string" || value.trim().length === 0) {
    throw new TypeError(`${label} must not be empty`);
  }
}

function idempotencyHeaders(value: string): HeadersInit {
  requireNonEmpty(value, "Idempotency-Key");
  return { "Idempotency-Key": value };
}

function withQuery(
  path: string,
  options: ControlPlaneListInventoryOptions | ControlPlaneListCredentialsOptions | undefined,
): string {
  if (!options) return path;
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(options)) {
    if (key === "signal") continue;
    if (value !== undefined && value !== null) query.set(key, String(value));
  }
  const encoded = query.toString();
  return encoded ? `${path}?${encoded}` : path;
}

function freezeClient<T extends object>(client: T): T {
  for (const value of Object.values(client)) {
    if (value && typeof value === "object") Object.freeze(value);
  }
  return Object.freeze(client);
}
