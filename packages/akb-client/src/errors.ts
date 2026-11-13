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
    this.retryAfterMs = responseRetryAfterMs(response);
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

/**
 * An SDK-local error. It keeps only the plain status fields of the response it
 * failed on, so a caller that had that response passes the wait its
 * Retry-After asked for, `retryAfterMs`, separately.
 */
export function createLocalError(
  code: AkbLocalErrorCode,
  response: Pick<Response, "ok" | "status" | "statusText"> | null = null,
  retryAfterMs: number | null = null,
): AkbError {
  const error = new AkbError({ message: localErrorMessage(code), code }, response);
  if (retryAfterMs !== null) error.retryAfterMs = retryAfterMs;
  return error;
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
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTH = `(?<month>${MONTHS.join("|")})`;
const WEEKDAY = `(?<weekday>${WEEKDAYS.join("|")})`;
const TIME_OF_DAY = "(?<hour>\\d{2}):(?<minute>\\d{2}):(?<second>\\d{2})";
// The three HTTP-date forms a recipient accepts (RFC 9110, section 5.6.7).
const IMF_FIXDATE = new RegExp(`^${WEEKDAY}, (?<day>\\d{2}) ${MONTH} (?<year>\\d{4}) ${TIME_OF_DAY} GMT$`, "u");
const RFC850_DATE = new RegExp(
  `^(?<weekday>Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day, (?<day>\\d{2})-${MONTH}-(?<year>\\d{2}) ${TIME_OF_DAY} GMT$`,
  "u",
);
const ASCTIME_DATE = new RegExp(`^${WEEKDAY} ${MONTH} (?<day> \\d|\\d{2}) ${TIME_OF_DAY} (?<year>\\d{4})$`, "u");

type DateParts = Record<string, string>;

/** The wait the `Retry-After` of a fetch `Response` asks for; plain status objects carry none. */
export function responseRetryAfterMs(response: unknown): number | null {
  const value = headerValue(response, "retry-after");
  if (value === null) return null;
  if (DELAY_SECONDS_PATTERN.test(value)) {
    return Math.min(Number(value) * 1000, Number.MAX_SAFE_INTEGER);
  }
  const now = Date.now();
  const sentAt = headerValue(response, "date");
  // The response's own Date is the reference when it is valid: the wait is
  // measured on the clock that set the Retry-After, and an RFC 850 two-digit
  // year takes its century from it.
  const reference = (sentAt === null ? null : httpDate(sentAt, now)) ?? now;
  const retryAt = httpDate(value, reference);
  if (retryAt === null) return null;
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

function httpDate(value: string, reference: number): number | null {
  const imf = IMF_FIXDATE.exec(value)?.groups;
  if (imf) return utcTime(Number(imf.year), imf);
  const rfc850 = RFC850_DATE.exec(value)?.groups;
  if (rfc850) return utcTime(rfc850Year(Number(rfc850.year), rfc850, reference), rfc850);
  const asctime = ASCTIME_DATE.exec(value)?.groups;
  if (asctime) return utcTime(Number(asctime.year), asctime);
  return null;
}

/**
 * The year of an RFC 850 date: the latest year with those two digits that does
 * not put the timestamp more than 50 years after the reference. A timestamp
 * further ahead stands for the year a century earlier (RFC 9110, 5.6.7).
 */
function rfc850Year(twoDigits: number, parts: DateParts, reference: number): number {
  const limit = new Date(reference);
  limit.setUTCFullYear(limit.getUTCFullYear() + 50);
  const limitYear = limit.getUTCFullYear();
  const year = limitYear - ((((limitYear - twoDigits) % 100) + 100) % 100);
  if (year < limitYear) return year;
  // In the limit's own year, compare the rest of the timestamp with the limit.
  const candidate = [
    MONTHS.indexOf(parts.month),
    Number(parts.day),
    Number(parts.hour),
    Number(parts.minute),
    Number(parts.second),
  ];
  const bound = [
    limit.getUTCMonth(),
    limit.getUTCDate(),
    limit.getUTCHours(),
    limit.getUTCMinutes(),
    limit.getUTCSeconds(),
  ];
  const index = candidate.findIndex((part, at) => part !== bound[at]);
  return index >= 0 && candidate[index] > bound[index] ? year - 100 : year;
}

function utcTime(year: number, parts: DateParts): number | null {
  const month = MONTHS.indexOf(parts.month);
  const day = Number(parts.day.trim());
  const hour = Number(parts.hour);
  const minute = Number(parts.minute);
  const second = Number(parts.second);
  if (hour > 23 || minute > 59 || second > 60) return null;
  const date = new Date(0);
  date.setUTCFullYear(year, month, day);
  date.setUTCHours(hour, minute, Math.min(second, 59), 0);
  // An impossible day, such as 31 Feb, would otherwise roll into the next month.
  if (date.getUTCMonth() !== month || date.getUTCDate() !== day) return null;
  // The day name must be the date's own; RFC 850 spells it out in full.
  if (date.getUTCDay() !== WEEKDAYS.indexOf(parts.weekday.slice(0, 3))) return null;
  // A leap second (60) is the second after :59.
  return date.getTime() + (second === 60 ? 1000 : 0);
}
