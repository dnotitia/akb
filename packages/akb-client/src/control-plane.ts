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
  ObservedStateProjection,
  ObservedStateRequest,
  RolloutProjection,
  RolloutRequest,
  RolloutResumeRequest,
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
  EligibilityProjection,
  GrantProjection,
  InstallationCommandRequest,
  InstallationProjection,
  InventoryItem,
  InventoryProjection,
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
  fetch?: typeof fetch;
}

export interface ControlPlaneListInventoryOptions {
  limit?: number;
  cursor?: string | null;
  lifecycle?: string | null;
}

export interface ControlPlaneListCredentialsOptions {
  deployment?: string | null;
}

export type ControlPlaneClientError = AkbError;

type ControlPlaneResult<T> = Promise<AkbResult<T, ControlPlaneClientError>>;
type TokenSource = string | (() => string | null | undefined);

interface Requester {
  call<T>(method: string, path: string, body?: unknown, headers?: HeadersInit): ControlPlaneResult<T>;
}

export interface ControlPlaneAdminApps {
  create(input: AppCreateRequest): ControlPlaneResult<AppDefinitionProjection>;
  get(appId: string): ControlPlaneResult<AppDefinitionProjection>;
  update(appId: string, input: AppUpdateRequest): ControlPlaneResult<AppDefinitionProjection>;
}

export interface ControlPlaneAdminReleases {
  create(appId: string, input: ReleaseCreateRequest): ControlPlaneResult<AppReleaseProjection>;
  get(appId: string, releaseId: string): ControlPlaneResult<AppReleaseProjection>;
}

export interface ControlPlaneAdminCredentials {
  issue(appId: string, input: CredentialIssueRequest): ControlPlaneResult<CredentialIssueProjection>;
  list(appId: string, options?: ControlPlaneListCredentialsOptions): ControlPlaneResult<CredentialListProjection>;
  rotate(appId: string, credentialId: string, input?: CredentialRotateRequest): ControlPlaneResult<CredentialIssueProjection>;
  revoke(appId: string, credentialId: string): ControlPlaneResult<CredentialMetadata>;
}

export interface ControlPlaneAdminInstallations {
  apply(appId: string, vaultId: string, input: InstallationCommandRequest): ControlPlaneResult<InstallationProjection>;
  get(appId: string, vaultId: string): ControlPlaneResult<InstallationProjection>;
  uninstall(appId: string, vaultId: string): ControlPlaneResult<InstallationProjection>;
}

export interface ControlPlaneAdminInventory {
  list(appId: string, options?: ControlPlaneListInventoryOptions): ControlPlaneResult<InventoryProjection>;
  reportObserved(appId: string, input: ObservedStateRequest): ControlPlaneResult<ObservedStateProjection>;
}

export interface ControlPlaneAdminSnapshots {
  create(appId: string): ControlPlaneResult<SnapshotCreateProjection>;
  get(appId: string, snapshotId: string): ControlPlaneResult<SnapshotProjection>;
  evaluate(appId: string, snapshotId: string, targetId: string): ControlPlaneResult<EligibilityProjection>;
}

export interface ControlPlaneAdminRollouts {
  request(appId: string, input: RolloutRequest, idempotencyKey: string): ControlPlaneResult<RolloutProjection>;
  get(appId: string, rolloutId: string): ControlPlaneResult<RolloutProjection>;
  resume(appId: string, rolloutId: string, input: RolloutResumeRequest, idempotencyKey: string): ControlPlaneResult<RolloutProjection>;
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
  get(vaultId: string): ControlPlaneResult<InstallationProjection>;
}

export interface ControlPlaneAppInventory {
  list(options?: ControlPlaneListInventoryOptions): ControlPlaneResult<InventoryProjection>;
  reportObserved(input: ObservedStateRequest): ControlPlaneResult<ObservedStateProjection>;
}

export interface ControlPlaneAppSnapshots {
  create(): ControlPlaneResult<SnapshotCreateProjection>;
  get(snapshotId: string): ControlPlaneResult<SnapshotProjection>;
  evaluate(snapshotId: string, targetId: string): ControlPlaneResult<EligibilityProjection>;
}

export interface ControlPlaneAppRollouts {
  request(input: RolloutRequest, idempotencyKey: string): ControlPlaneResult<RolloutProjection>;
  get(rolloutId: string): ControlPlaneResult<RolloutProjection>;
  resume(rolloutId: string, input: RolloutResumeRequest, idempotencyKey: string): ControlPlaneResult<RolloutProjection>;
}

export interface ControlPlaneAppClient {
  readonly authorize: (input: AuthorizeRequest) => ControlPlaneResult<AuthorizeProjection>;
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
  );
}

export function createControlPlaneAdminClient(
  config: ControlPlaneAdminClientConfig,
): ControlPlaneAdminClient {
  const requester = makeRequester(config.baseUrl, config.adminToken, config.fetch);
  const client: ControlPlaneAdminClient = {
    apps: {
      create: (input) => requester.call("POST", "/apps", input),
      get: (appId) => requester.call("GET", `/apps/${segment(appId)}`),
      update: (appId, input) => requester.call("PATCH", `/apps/${segment(appId)}`, input),
    },
    releases: {
      create: (appId, input) => requester.call("POST", `/apps/${segment(appId)}/releases`, input),
      get: (appId, releaseId) => requester.call("GET", `/apps/${segment(appId)}/releases/${segment(releaseId)}`),
    },
    credentials: {
      issue: (appId, input) => requester.call("POST", `/apps/${segment(appId)}/credentials`, input),
      list: (appId, options) => requester.call("GET", withQuery(`/apps/${segment(appId)}/credentials`, {
        deployment: options?.deployment,
      })),
      rotate: (appId, credentialId, input = {}) => requester.call("POST", `/apps/${segment(appId)}/credentials/${segment(credentialId)}/rotate`, input),
      revoke: (appId, credentialId) => requester.call("DELETE", `/apps/${segment(appId)}/credentials/${segment(credentialId)}`),
    },
    installations: {
      apply: (appId, vaultId, input) => requester.call("PUT", `/apps/${segment(appId)}/installations/${segment(vaultId)}`, input),
      get: (appId, vaultId) => requester.call("GET", `/apps/${segment(appId)}/installations/${segment(vaultId)}`),
      uninstall: (appId, vaultId) => requester.call("DELETE", `/apps/${segment(appId)}/installations/${segment(vaultId)}`),
    },
    inventory: {
      list: (appId, options) => requester.call("GET", withQuery(`/apps/${segment(appId)}/inventory`, options)),
      reportObserved: (appId, input) => requester.call("POST", `/apps/${segment(appId)}/observed-state`, input),
    },
    snapshots: {
      create: (appId) => requester.call("POST", `/apps/${segment(appId)}/rollout-snapshots`),
      get: (appId, snapshotId) => requester.call("GET", `/apps/${segment(appId)}/rollout-snapshots/${segment(snapshotId)}`),
      evaluate: (appId, snapshotId, targetId) => requester.call("POST", `/apps/${segment(appId)}/rollout-snapshots/${segment(snapshotId)}/targets/${segment(targetId)}/eligibility`),
    },
    rollouts: {
      request: (appId, input, idempotencyKey) => requester.call("POST", `/apps/${segment(appId)}/rollouts`, input, idempotencyHeaders(idempotencyKey)),
      get: (appId, rolloutId) => requester.call("GET", `/apps/${segment(appId)}/rollouts/${segment(rolloutId)}`),
      resume: (appId, rolloutId, input, idempotencyKey) => requester.call("POST", `/apps/${segment(appId)}/rollouts/${segment(rolloutId)}/resume`, input, idempotencyHeaders(idempotencyKey)),
    },
  };
  return freezeClient(client);
}

export function createControlPlaneAppClient(
  config: ControlPlaneAppClientConfig,
): ControlPlaneAppClient {
  const requester = makeRequester(config.baseUrl, config.appToken, config.fetch);
  const client: ControlPlaneAppClient = {
    authorize: (input) => requester.call("POST", "/app/authorize", input),
    installations: {
      get: (vaultId) => requester.call("GET", `/app/installations/${segment(vaultId)}`),
    },
    inventory: {
      list: (options) => requester.call("GET", withQuery("/app/inventory", options)),
      reportObserved: (input) => requester.call("POST", "/app/observed-state", input),
    },
    snapshots: {
      create: () => requester.call("POST", "/app/rollout-snapshots"),
      get: (snapshotId) => requester.call("GET", `/app/rollout-snapshots/${segment(snapshotId)}`),
      evaluate: (snapshotId, targetId) => requester.call("POST", `/app/rollout-snapshots/${segment(snapshotId)}/targets/${segment(targetId)}/eligibility`),
    },
    rollouts: {
      request: (input, idempotencyKey) => requester.call("POST", "/app/rollouts", input, idempotencyHeaders(idempotencyKey)),
      get: (rolloutId) => requester.call("GET", `/app/rollouts/${segment(rolloutId)}`),
      resume: (rolloutId, input, idempotencyKey) => requester.call("POST", `/app/rollouts/${segment(rolloutId)}/resume`, input, idempotencyHeaders(idempotencyKey)),
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
