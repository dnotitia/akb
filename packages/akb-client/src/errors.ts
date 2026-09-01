export class AkbError extends Error {
  code: string;
  details: unknown;
  hint: string | null;
  status: number | null;
  payload: Record<string, unknown>;
  response: Pick<Response, "ok" | "status" | "statusText"> | null;

  constructor(payload: unknown, response: Pick<Response, "ok" | "status" | "statusText"> | null = null) {
    const body = objectPayload(payload);
    const message = stringValue(body.message)
      ?? stringValue(body.error)
      ?? stringValue(body.detail)
      ?? responseStatusMessage(response)
      ?? "AKB request failed";
    super(message);
    this.name = "AkbError";
    this.code = stringValue(body.code) ?? "unknown";
    this.details = body.details ?? null;
    this.hint = stringValue(body.hint);
    this.status = typeof response?.status === "number" ? response.status : null;
    this.payload = body;
    this.response = response;
  }
}

export const AKB_ERROR_CODES = {
  transport: "transport_error",
  aborted: "request_aborted",
  responseRead: "response_read_error",
  invalidJson: "invalid_json",
} as const;

export type AkbLocalErrorCode = typeof AKB_ERROR_CODES[keyof typeof AKB_ERROR_CODES];

export function createLocalError(
  code: AkbLocalErrorCode,
  response: Pick<Response, "ok" | "status" | "statusText"> | null = null,
): AkbError {
  return new AkbError({ message: localErrorMessage(code), code }, response);
}

export function isAbortError(error: unknown, signal: AbortSignal | null | undefined): boolean {
  if (signal?.aborted) return true;
  return Boolean(
    error
    && typeof error === "object"
    && "name" in error
    && (error as { name?: unknown }).name === "AbortError",
  );
}

function objectPayload(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : {};
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function responseStatusMessage(
  response: Pick<Response, "ok" | "status" | "statusText"> | null,
): string | null {
  if (!response || typeof response.status !== "number") return null;
  return `${response.status} ${response.statusText || "Request failed"}`;
}

function localErrorMessage(code: AkbLocalErrorCode): string {
  switch (code) {
    case AKB_ERROR_CODES.transport:
      return "AKB request failed before receiving a response.";
    case AKB_ERROR_CODES.aborted:
      return "AKB request was aborted.";
    case AKB_ERROR_CODES.responseRead:
      return "AKB response could not be read.";
    case AKB_ERROR_CODES.invalidJson:
      return "AKB response was not valid JSON.";
  }
}
