export class AkbError extends Error {
  code: string;
  details: unknown;
  hint: string | null;
  status: number | null;
  /**
   * How long, in milliseconds, the response's `Retry-After` header asked the
   * caller to wait, or `null` when there is no response or no valid header.
   * An HTTP-date is measured from the response's `Date` header when it has a
   * valid one, otherwise from the local clock, and a past date gives `0`.
   */
  retryAfterMs: number | null;
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
    this.retryAfterMs = retryAfterMs(response);
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

const DELAY_SECONDS_PATTERN = /^\d+$/u;
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const MONTH = `(${MONTHS.join("|")})`;
const DAY_NAME = "(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)";
const TIME_OF_DAY = "(\\d{2}):(\\d{2}):(\\d{2})";
// The three HTTP-date forms a recipient accepts (RFC 9110, section 5.6.7).
const IMF_FIXDATE = new RegExp(`^${DAY_NAME}, (\\d{2}) ${MONTH} (\\d{4}) ${TIME_OF_DAY} GMT$`, "u");
const RFC850_DATE = new RegExp(
  `^(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day, (\\d{2})-${MONTH}-(\\d{2}) ${TIME_OF_DAY} GMT$`,
  "u",
);
const ASCTIME_DATE = new RegExp(`^${DAY_NAME} ${MONTH} ( \\d|\\d{2}) ${TIME_OF_DAY} (\\d{4})$`, "u");

/** The `Retry-After` of a fetch `Response`; plain status objects carry none. */
function retryAfterMs(response: unknown): number | null {
  const value = headerValue(response, "retry-after");
  if (value === null) return null;
  if (DELAY_SECONDS_PATTERN.test(value)) {
    return Math.min(Number(value) * 1000, Number.MAX_SAFE_INTEGER);
  }
  const retryAt = httpDate(value);
  if (retryAt === null) return null;
  const sentAt = headerValue(response, "date");
  const reference = (sentAt === null ? null : httpDate(sentAt)) ?? Date.now();
  return Math.max(0, retryAt - reference);
}

function headerValue(response: unknown, name: string): string | null {
  try {
    const headers = (response as { headers?: unknown } | null)?.headers;
    if (!headers || typeof (headers as Headers).get !== "function") return null;
    const value = (headers as Headers).get(name);
    return typeof value === "string" ? value.trim() : null;
  } catch {
    return null;
  }
}

function httpDate(value: string): number | null {
  let match = IMF_FIXDATE.exec(value);
  if (match) return utcTime(Number(match[3]), match[2], match[1], match[4], match[5], match[6]);
  match = RFC850_DATE.exec(value);
  if (match) return utcTime(rfc850Year(Number(match[3])), match[2], match[1], match[4], match[5], match[6]);
  match = ASCTIME_DATE.exec(value);
  if (match) return utcTime(Number(match[6]), match[1], match[2], match[3], match[4], match[5]);
  return null;
}

// A two-digit year more than 50 years ahead is the latest past year with those digits.
function rfc850Year(twoDigits: number): number {
  const current = new Date().getUTCFullYear();
  const year = current - (current % 100) + twoDigits;
  return year > current + 50 ? year - 100 : year;
}

function utcTime(
  year: number,
  monthName: string,
  dayText: string,
  hourText: string,
  minuteText: string,
  secondText: string,
): number | null {
  const month = MONTHS.indexOf(monthName);
  const day = Number(dayText.trim());
  const hour = Number(hourText);
  const minute = Number(minuteText);
  const second = Number(secondText);
  if (hour > 23 || minute > 59 || second > 60) return null;
  const date = new Date(0);
  date.setUTCFullYear(year, month, day);
  // A leap second (60) is read as the second before it.
  date.setUTCHours(hour, minute, Math.min(second, 59), 0);
  // An impossible day, such as 31 Feb, would otherwise roll into the next month.
  if (date.getUTCMonth() !== month || date.getUTCDate() !== day) return null;
  return date.getTime();
}
