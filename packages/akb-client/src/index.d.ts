export type AkbJsonValue =
  | string
  | number
  | boolean
  | null
  | AkbJsonValue[]
  | { [key: string]: AkbJsonValue };

export interface AkbSuccessEnvelope {
  kind: string;
  [key: string]: AkbJsonValue | undefined;
}

export interface AkbErrorPayload {
  message?: string;
  error?: string;
  detail?: unknown;
  code?: string;
  details?: unknown;
  hint?: string | null;
  password_required?: boolean;
  slug?: string;
  [key: string]: unknown;
}

export class AkbError extends Error {
  code: string;
  details: unknown;
  hint: string | null;
  status: number;
  payload: Record<string, unknown>;
  response: Response | null;
  constructor(payload: unknown, response?: Response | null);
}

export interface AkbResult<T, E extends AkbError = AkbError> {
  data: T | null;
  error: E | null;
  response: Pick<Response, "ok" | "status" | "statusText"> | null;
  throwOnError(): AkbThrowingResult<T>;
}

export interface AkbThrowingResult<T> extends AkbResult<T, never> {
  data: T;
  error: null;
}

export interface AkbClientConfig {
  baseUrl: string;
  token?: string | null | (() => string | null | undefined);
  fetch?: typeof fetch;
}

export interface AkbClient {
  request<T = AkbSuccessEnvelope>(path: string | URL, init?: RequestInit): Promise<AkbResult<T>>;
}

export function unwrapAkbResponse<T = unknown>(
  response: Pick<Response, "ok" | "status" | "statusText"> | null,
  body: T | AkbErrorPayload | unknown,
): AkbResult<T>;

export function akbFetch<T = unknown>(
  input: RequestInfo | URL,
  init?: RequestInit,
  fetchImpl?: typeof fetch,
): Promise<AkbResult<T>>;

export function createClient(config: AkbClientConfig): AkbClient;
