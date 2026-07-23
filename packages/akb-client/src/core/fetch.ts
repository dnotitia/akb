import type { AkbResult, AkbSuccessEnvelope } from "../index.js";
import type { paths } from "./schema.gen.js";

const REQUIRED_HEADERS_BY_OPERATION: Readonly<Record<string, readonly string[]>> = {
  "post /api/v1/tables/{vault}/migrations": ["Idempotency-Key"],
};

export type AkbPath = string & keyof paths;
export type AkbPathMethod<Path extends AkbPath> = string & keyof paths[Path];

export interface AkbOperation<
  Method extends string,
  Path extends string,
  Parameters,
  RequestBody,
  Success,
> {
  method: Method;
  path: Path;
  parameters: Parameters;
  requestBody: RequestBody;
  responses: {
    success: Success;
  };
}

export type AkbOperationResponse<Operation> =
  Operation extends { responses: { success: infer Success } } ? Success : AkbSuccessEnvelope;

type AkbOperationParameters<Operation> =
  Operation extends { parameters: infer Parameters } ? Parameters : never;

type AkbOperationRequestBody<Operation> =
  Operation extends { requestBody: infer RequestBody } ? RequestBody : never;

type AkbOperationPathInit<Operation> =
  [AkbOperationParameters<Operation>] extends [never]
    ? { path?: Record<string, string | number | boolean> }
    : AkbOperationParameters<Operation> extends { path: infer PathParams }
      ? { path: PathParams }
      : { path?: Record<string, string | number | boolean> };

type AkbOperationHeaderInit<Operation> =
  [AkbOperationParameters<Operation>] extends [never]
    ? { headers?: HeadersInit }
    : AkbOperationParameters<Operation> extends { header: infer HeaderParams }
      ? { headers: Headers | string[][] | (Record<string, string> & HeaderParams) }
      : { headers?: HeadersInit };

type AkbOperationBodyInit<Operation> =
  unknown extends AkbOperationRequestBody<Operation>
    ? { json?: unknown }
    : [AkbOperationRequestBody<Operation>] extends [never]
      ? { json?: never }
      : { json: AkbOperationRequestBody<Operation> };

export type AkbTypedFetchOptions<Operation> =
  Omit<AkbTypedFetchInit, "path" | "headers" | "json">
  & AkbOperationPathInit<Operation>
  & AkbOperationHeaderInit<Operation>
  & AkbOperationBodyInit<Operation>;

export type AkbTypedFetchArgs<Operation> =
  [AkbOperationParameters<Operation>] extends [never]
    ? unknown extends AkbOperationRequestBody<Operation>
      ? [init?: AkbTypedFetchOptions<Operation>]
      : [AkbOperationRequestBody<Operation>] extends [never]
        ? [init?: AkbTypedFetchOptions<Operation>]
        : [init: AkbTypedFetchOptions<Operation>]
    : AkbOperationParameters<Operation> extends { path: unknown }
      ? [init: AkbTypedFetchOptions<Operation>]
      : AkbOperationParameters<Operation> extends { header: unknown }
        ? [init: AkbTypedFetchOptions<Operation>]
        : [init?: AkbTypedFetchOptions<Operation>];

export interface AkbTypedFetchInit extends Omit<RequestInit, "method" | "body"> {
  path?: Record<string, string | number | boolean>;
  query?: Record<
    string,
    string | number | boolean | null | undefined | Array<string | number | boolean>
  >;
  json?: unknown;
  body?: BodyInit | null;
}

export interface AkbTypedFetch {
  <Path extends AkbPath, Method extends AkbPathMethod<Path>>(
    method: Method,
    path: Path,
    ...args: AkbTypedFetchArgs<paths[Path][Method]>
  ): Promise<AkbResult<AkbOperationResponse<paths[Path][Method]>>>;
}

/**
 * Create a typed fetch wrapper over an AKB client request function.
 *
 * Runtime work stays deliberately small: the parent client owns bearer auth,
 * X-Akb-Claims, and {data,error}; this helper substitutes OpenAPI path
 * parameters and forwards the final route through that boundary.
 */
export function createTypedFetch(client: {
  request<T = AkbSuccessEnvelope>(path: string | URL, init?: RequestInit): Promise<AkbResult<T>>;
}): AkbTypedFetch {
  const typedFetch = async (method: string, path: string, init: AkbTypedFetchInit = {}) => {
    const { path: pathParams, query, json, ...requestInit } = init;
    validateRequiredHeaders(method, path, requestInit.headers);
    const route = appendQuery(stripApiPrefix(resolvePathTemplate(String(path), pathParams)), query);
    const body = json === undefined ? requestInit.body : JSON.stringify(json);
    return await client.request(route, {
      ...requestInit,
      method: String(method).toUpperCase(),
      body,
    });
  };
  return typedFetch as unknown as AkbTypedFetch;
}

function validateRequiredHeaders(
  method: string,
  path: string,
  headers: HeadersInit | undefined,
): void {
  const required = REQUIRED_HEADERS_BY_OPERATION[`${method.toLowerCase()} ${path}`] ?? [];
  if (required.length === 0) return;
  const supplied = new Headers(headers);
  for (const name of required) {
    if (!supplied.has(name)) throw new TypeError(`Missing required header: ${name}`);
  }
}

function resolvePathTemplate(
  path: string,
  params: Record<string, string | number | boolean> | undefined,
): string {
  return path.replace(/\{([^}]+)\}/g, (_match, key) => {
    const value = params?.[key];
    if (value === undefined || value === null) {
      throw new TypeError(`Missing path parameter: ${key}`);
    }
    return encodeURIComponent(String(value));
  });
}

function stripApiPrefix(path: string): string {
  return path.startsWith("/api/v1/") ? path.slice("/api/v1".length) : path;
}

function appendQuery(
  path: string,
  query:
    | Record<string, string | number | boolean | null | undefined | Array<string | number | boolean>>
    | undefined,
): string {
  if (!query) return path;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === null || value === undefined) continue;
    const values = Array.isArray(value) ? value : [value];
    for (const item of values) params.append(key, String(item));
  }
  const suffix = params.toString();
  return suffix ? `${path}${path.includes("?") ? "&" : "?"}${suffix}` : path;
}
