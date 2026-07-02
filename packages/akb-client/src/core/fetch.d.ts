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

export function createTypedFetch(client: {
  request<T = AkbSuccessEnvelope>(path: string | URL, init?: RequestInit): Promise<AkbResult<T>>;
}): AkbTypedFetch;
