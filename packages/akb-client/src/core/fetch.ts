import type { AkbResult, AkbSuccessEnvelope } from "../index.js";
import type { paths } from "./schema.gen.js";

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

export type AkbTypedFetchOptions<Operation> =
  Operation extends { parameters: { path: infer PathParams } }
    ? Omit<AkbTypedFetchInit, "path"> & { path: PathParams }
    : AkbTypedFetchInit;

export type AkbTypedFetchArgs<Operation> =
  Operation extends { parameters: { path: infer PathParams } }
    ? [init: Omit<AkbTypedFetchInit, "path"> & { path: PathParams }]
    : [init?: AkbTypedFetchInit];

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
