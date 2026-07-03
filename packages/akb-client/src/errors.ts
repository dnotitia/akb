export class AkbError extends Error {
  code: string;
  details: unknown;
  hint: string | null;
  status: number;
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
    this.status = typeof response?.status === "number" ? response.status : 0;
    this.payload = body;
    this.response = response;
  }
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
