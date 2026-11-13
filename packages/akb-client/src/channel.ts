import { AKB_ERROR_CODES, AkbError, createLocalError, isAbortError } from "./errors.js";
import type { AkbResult } from "./index.js";
import type {
  ChangeEventEnvelopeV1,
  EventCursor,
  EventKind,
  TailCheckpointV1,
} from "./core/schema.gen.js";

const EVENT_KIND_PATTERN = /^[A-Za-z][A-Za-z0-9]*(?:[._:-][A-Za-z0-9]+)*$/u;
const EVENT_CURSOR_PATTERN = /^ec1\.[A-Za-z0-9_-]+$/u;
// The reconnection time until a stream's SSE `retry` field sets another, and
// the smallest step the backoff after a refused open doubles from.
const DEFAULT_RECONNECT_DELAY_MS = 250;
// The longest wait the channel chooses itself, by its backoff or by an SSE
// `retry` field. A refusal's Retry-After is waited in full, however long.
const MAX_RECONNECT_DELAY_MS = 30_000;
// Past this many doublings the cap above applies anyway.
const MAX_BACKOFF_DOUBLINGS = 16;
// On top of a Retry-After, each client waits a random extra of up to 20% of
// it, and at most a second, so that clients refused together come back apart.
const RETRY_AFTER_JITTER_RATIO = 0.2;
const RETRY_AFTER_JITTER_MAX_MS = 1_000;
// The longest delay one timer takes; setTimeout fires at once for a longer
// one, so a longer wait is a chain of timers.
const MAX_TIMER_DELAY_MS = 2 ** 31 - 1;

type StreamRequest = (
  path: string,
  init: RequestInit,
) => Promise<AkbResult<Response>>;

export interface AkbEventGapDetails {
  earliest_cursor: EventCursor;
  latest_cursor: EventCursor;
}

export interface AkbChangeEventListenerOptions {
  kinds?: readonly EventKind[];
}

export type AkbChangeEventListener = (
  event: ChangeEventEnvelopeV1,
) => void | PromiseLike<void>;

export type AkbTailCheckpointListener = (
  checkpoint: TailCheckpointV1,
) => void | PromiseLike<void>;

export interface AkbChangeEventSubscribeOptions {
  cursor?: EventCursor;
  start?: "earliest";
  signal?: AbortSignal | null;
}

export interface AkbChangeEventSubscription {
  readonly cursor: EventCursor | null;
  readonly closed: Promise<void>;
  unsubscribe(): Promise<void>;
}

export interface AkbChangeEventChannel {
  on(event: "change", listener: AkbChangeEventListener): AkbChangeEventChannel;
  on(
    event: "change",
    options: AkbChangeEventListenerOptions,
    listener: AkbChangeEventListener,
  ): AkbChangeEventChannel;
  on(event: "checkpoint", listener: AkbTailCheckpointListener): AkbChangeEventChannel;
  subscribe(
    options?: AkbChangeEventSubscribeOptions,
  ): Promise<AkbChangeEventSubscription>;
}

type ChangeRegistration = {
  readonly type: "change";
  readonly kinds: readonly EventKind[] | undefined;
  readonly listener: AkbChangeEventListener;
};

type CheckpointRegistration = {
  readonly type: "checkpoint";
  readonly listener: AkbTailCheckpointListener;
};

type Registration = ChangeRegistration | CheckpointRegistration;

interface SubscriptionState {
  cursor: EventCursor | null;
  readonly initialCursor: EventCursor | undefined;
  readonly initialStart: "earliest" | undefined;
  readonly signal: AbortSignal | null;
  requestController: AbortController | null;
  requestCleanup: (() => void) | null;
  externalCleanup: (() => void) | null;
  readonly wakeWaiters: Set<() => void>;
  stopped: boolean;
  closedSettled: boolean;
  retryDelayMs: number;
  /**
   * Opens in a row that were refused, failed, or gave a stream that ended
   * before its first event or comment. A stream's first event or comment
   * resets it.
   */
  failedOpens: number;
  readonly closed: Promise<void>;
  readonly resolveClosed: () => void;
  readonly rejectClosed: (reason: unknown) => void;
}

interface SseFrame {
  readonly event: string;
  readonly id: string | null;
  readonly data: string;
}

interface SseParserResult {
  readonly frames: SseFrame[];
  readonly retryDelayMs: number | null;
  /** Whether a comment line, such as a heartbeat, was read. */
  readonly commented: boolean;
}

class ChannelProtocolError extends AkbError {
  constructor() {
    super({
      message: "The AKB Change Event stream contained an invalid frame.",
      code: "protocol_error",
    });
  }
}

class ListenerFailure {
  constructor(readonly cause: unknown) {}
}

class SseParser {
  private buffer = "";
  private event = "";
  private id: string | null = null;
  private data: string[] = [];
  private hasFields = false;
  private retryDelayMs: number | null = null;
  private commented = false;
  // The last chunk ended on a CR, which already ended its line; an LF that
  // starts the next chunk is the rest of that CRLF, not an empty line.
  private endedOnCr = false;

  feed(chunk: string): SseParserResult {
    let text = chunk;
    if (this.endedOnCr && text.length > 0) {
      if (text.startsWith("\n")) text = text.slice(1);
      this.endedOnCr = false;
    }
    this.buffer += text;
    const frames: SseFrame[] = [];
    let newlineIndex = this.findNewline();
    while (newlineIndex >= 0) {
      let line = this.buffer.slice(0, newlineIndex);
      const endsOnCr = this.buffer[newlineIndex] === "\r";
      if (endsOnCr && newlineIndex === this.buffer.length - 1) this.endedOnCr = true;
      const lineEndingLength = endsOnCr && this.buffer[newlineIndex + 1] === "\n" ? 2 : 1;
      this.buffer = this.buffer.slice(newlineIndex + lineEndingLength);
      if (line.endsWith("\r")) line = line.slice(0, -1);
      const frame = this.consumeLine(line);
      if (frame) frames.push(frame);
      newlineIndex = this.findNewline();
    }
    return this.takeResult(frames);
  }

  /**
   * The stream ended. As the SSE parsing rules require, whatever is pending is
   * discarded: a last line without its line ending, and an event without the
   * blank line that ends it. A stream cut in the middle of an event therefore
   * dispatches nothing of it, and the channel resumes from the last cursor.
   */
  end(): SseParserResult {
    this.buffer = "";
    this.event = "";
    this.id = null;
    this.data = [];
    this.hasFields = false;
    return this.takeResult([]);
  }

  private findNewline(): number {
    const lf = this.buffer.indexOf("\n");
    const cr = this.buffer.indexOf("\r");
    if (lf < 0) return cr;
    if (cr < 0) return lf;
    return Math.min(lf, cr);
  }

  private consumeLine(line: string): SseFrame | null {
    if (line === "") return this.dispatch();
    if (line.startsWith(":")) {
      this.commented = true;
      return null;
    }

    const separator = line.indexOf(":");
    const field = separator < 0 ? line : line.slice(0, separator);
    let value = separator < 0 ? "" : line.slice(separator + 1);
    if (value.startsWith(" ")) value = value.slice(1);

    switch (field) {
      case "event":
        this.event = value;
        this.hasFields = true;
        break;
      case "id":
        this.id = value;
        this.hasFields = true;
        break;
      case "data":
        this.data.push(value);
        this.hasFields = true;
        break;
      case "retry":
        if (/^\d+$/u.test(value)) {
          this.retryDelayMs = Math.min(Number(value), MAX_RECONNECT_DELAY_MS);
        }
        break;
      default:
        break;
    }
    return null;
  }

  private dispatch(): SseFrame | null {
    if (!this.hasFields) return null;
    const frame: SseFrame = {
      event: this.event,
      id: this.id,
      data: this.data.join("\n"),
    };
    this.event = "";
    this.id = null;
    this.data = [];
    this.hasFields = false;
    return frame;
  }

  private takeResult(frames: SseFrame[]): SseParserResult {
    const { retryDelayMs, commented } = this;
    this.retryDelayMs = null;
    this.commented = false;
    return { frames, retryDelayMs, commented };
  }
}

export function makeChangeEventChannel(
  vault: string,
  openStream: StreamRequest,
): AkbChangeEventChannel {
  return makeChannel(vault, openStream, []);
}

function makeChannel(
  vault: string,
  openStream: StreamRequest,
  registrations: readonly Registration[],
): AkbChangeEventChannel {
  const channel: AkbChangeEventChannel = {
    on(event: "change" | "checkpoint", optionsOrListener: unknown, maybeListener?: unknown) {
      if (event === "change") {
        const options = typeof optionsOrListener === "function" ? undefined : optionsOrListener;
        const listener = typeof optionsOrListener === "function" ? optionsOrListener : maybeListener;
        if (typeof listener !== "function") {
          throw new TypeError('"change" listeners must be functions.');
        }
        if (options !== undefined && !isObject(options)) {
          throw new TypeError('"change" listener options must be an object.');
        }
        const kinds = options === undefined || !Object.hasOwn(options, "kinds")
          ? undefined
          : normalizeKinds(options.kinds);
        const registration: ChangeRegistration = {
          type: "change",
          kinds,
          listener: listener as AkbChangeEventListener,
        };
        return makeChannel(vault, openStream, [...registrations, registration]);
      }

      if (event !== "checkpoint" || typeof optionsOrListener !== "function") {
        throw new TypeError('"checkpoint" listeners must be functions.');
      }
      const registration: CheckpointRegistration = {
        type: "checkpoint",
        listener: optionsOrListener as AkbTailCheckpointListener,
      };
      return makeChannel(vault, openStream, [...registrations, registration]);
    },
    subscribe(options: AkbChangeEventSubscribeOptions = {}) {
      validateSubscribeOptions(options);
      return startSubscription(
        vault,
        openStream,
        registrations,
        options,
      );
    },
  };
  return Object.freeze(channel);
}

function validateSubscribeOptions(options: AkbChangeEventSubscribeOptions): void {
  if (!isObject(options)) {
    throw new TypeError("Change Event subscription options must be an object.");
  }
  if (options.start !== undefined && options.start !== "earliest") {
    throw new TypeError('Change Event subscription start must be "earliest".');
  }
  if (options.cursor !== undefined && options.start !== undefined) {
    throw new TypeError("Change Event subscription cannot combine cursor and start.");
  }
}

function normalizeKinds(kinds: unknown): readonly EventKind[] {
  if (!Array.isArray(kinds)) {
    throw new TypeError("Change Event kinds must be an array of exact Event Kind strings.");
  }
  const unique = new Set<EventKind>();
  for (const kind of kinds) {
    if (typeof kind !== "string" || !EVENT_KIND_PATTERN.test(kind)) {
      throw new TypeError("Change Event kinds must be exact Event Kind strings.");
    }
    unique.add(kind);
  }
  return Object.freeze([...unique].sort());
}

function startSubscription(
  vault: string,
  openStream: StreamRequest,
  registrations: readonly Registration[],
  options: AkbChangeEventSubscribeOptions,
): Promise<AkbChangeEventSubscription> {
  const state = createSubscriptionState(options);
  const subscription: AkbChangeEventSubscription = {
    get cursor() {
      return state.cursor;
    },
    closed: state.closed,
    async unsubscribe() {
      stopSubscription(state);
      await state.closed;
    },
  };
  if (state.stopped) return Promise.resolve(subscription);
  return openInitialConnection(
    state,
    subscription,
    vault,
    openStream,
    registrations,
  );
}

function createSubscriptionState(
  options: AkbChangeEventSubscribeOptions,
): SubscriptionState {
  let resolveClosed!: () => void;
  let rejectClosed!: (reason: unknown) => void;
  const closed = new Promise<void>((resolve, reject) => {
    resolveClosed = resolve;
    rejectClosed = reject;
  });
  void closed.catch(() => undefined);
  const state: SubscriptionState = {
    cursor: options.cursor ?? null,
    initialCursor: options.cursor,
    initialStart: options.start,
    signal: options.signal ?? null,
    requestController: null,
    requestCleanup: null,
    externalCleanup: null,
    wakeWaiters: new Set(),
    stopped: false,
    closedSettled: false,
    retryDelayMs: DEFAULT_RECONNECT_DELAY_MS,
    failedOpens: 0,
    closed,
    resolveClosed,
    rejectClosed,
  };
  if (state.signal?.aborted) {
    state.stopped = true;
    settleClosed(state);
  } else {
    if (state.signal) {
      const onAbort = () => stopSubscription(state);
      state.signal.addEventListener("abort", onAbort, { once: true });
      state.externalCleanup = () => state.signal?.removeEventListener("abort", onAbort);
    }
  }
  return state;
}

async function openInitialConnection(
  state: SubscriptionState,
  subscription: AkbChangeEventSubscription,
  vault: string,
  openStream: StreamRequest,
  registrations: readonly Registration[],
): Promise<AkbChangeEventSubscription> {
  let initial = true;
  while (!state.stopped) {
    const result = await openConnection(state, vault, openStream, initial, registrations);
    if (result.response) {
      void runSubscription(state, subscription, vault, openStream, registrations, result.response);
      return subscription;
    }
    initial = false;
    if (!result.error) break;
    if (state.stopped) {
      if (result.error.code === AKB_ERROR_CODES.aborted) return subscription;
      return subscription;
    }
    if (!isRetryable(result.error)) {
      terminateSubscription(state, result.error);
      throw result.error;
    }
    state.failedOpens += 1;
    if (!(await waitForReconnect(state, refusedOpenDelayMs(state, result.error.retryAfterMs)))) {
      return subscription;
    }
  }
  return subscription;
}

async function runSubscription(
  state: SubscriptionState,
  subscription: AkbChangeEventSubscription,
  vault: string,
  openStream: StreamRequest,
  registrations: readonly Registration[],
  response: Response,
): Promise<void> {
  let currentResponse = response;
  while (!state.stopped) {
    const streamResult = await consumeResponse(state, vault, registrations, currentResponse);
    if (state.stopped) return;
    if (streamResult === "terminal") return;

    const next = await reconnect(state, vault, openStream, registrations, streamResult === "dropped");
    if (!next) return;
    currentResponse = next;
  }
  void subscription;
}

async function reconnect(
  state: SubscriptionState,
  vault: string,
  openStream: StreamRequest,
  registrations: readonly Registration[],
  dropped: boolean,
): Promise<Response | null> {
  // A stream that sent an event or a comment is reopened after the
  // reconnection time. One that ended before either, as when a proxy accepts
  // the request and drops it, counts as another refused open.
  let delayMs = state.retryDelayMs;
  if (dropped) {
    state.failedOpens += 1;
    delayMs = refusedOpenDelayMs(state, null);
  }
  while (!state.stopped) {
    if (!(await waitForReconnect(state, delayMs))) return null;
    const result = await openConnection(state, vault, openStream, false, registrations);
    if (result.response) return result.response;
    if (!result.error || state.stopped) return null;
    if (!isRetryable(result.error)) {
      terminateSubscription(state, result.error);
      return null;
    }
    state.failedOpens += 1;
    delayMs = refusedOpenDelayMs(state, result.error.retryAfterMs);
  }
  return null;
}

async function openConnection(
  state: SubscriptionState,
  vault: string,
  openStream: StreamRequest,
  initial: boolean,
  registrations: readonly Registration[],
): Promise<{ response: Response | null; error: AkbError | null }> {
  const controller = new AbortController();
  const forwardAbort = () => controller.abort();
  state.requestController = controller;
  state.requestCleanup = () => {
    state.signal?.removeEventListener("abort", forwardAbort);
    if (state.requestController === controller) state.requestController = null;
    state.requestCleanup = null;
  };
  state.signal?.addEventListener("abort", forwardAbort, { once: true });

  const kinds = normalizedChannelKinds(registrations);
  const params = new URLSearchParams();
  if (initial) {
    if (state.initialCursor !== undefined) params.set("cursor", state.initialCursor);
    if (state.initialStart !== undefined) params.set("start", state.initialStart);
  } else if (state.cursor !== null) {
    // Reconnects use the header so the server's Last-Event-ID precedence is explicit.
  } else if (state.initialStart !== undefined) {
    params.set("start", state.initialStart);
  }
  for (const kind of kinds) params.append("kind", kind);
  const query = params.toString();
  const path = `/events/${encodeURIComponent(vault)}${query ? `?${query}` : ""}`;
  const headers = new Headers({ Accept: "text/event-stream" });
  if (!initial && state.cursor !== null) headers.set("Last-Event-ID", state.cursor);

  let result: AkbResult<Response>;
  try {
    result = await openStream(path, { method: "GET", headers, signal: controller.signal });
  } catch (error) {
    state.requestCleanup?.();
    if (state.stopped || isAbortError(error, state.signal)) {
      return { response: null, error: createLocalError(AKB_ERROR_CODES.aborted) };
    }
    return { response: null, error: createLocalError(AKB_ERROR_CODES.transport) };
  }

  if (result.error) {
    state.requestCleanup?.();
    return { response: null, error: result.error };
  }
  const response = result.data;
  if (!response) {
    state.requestCleanup?.();
    return {
      response: null,
      error: new AkbError({
        message: "The AKB Change Event stream did not return a response.",
        code: "protocol_error",
      }),
    };
  }
  if (!response.ok) {
    const error = await responseError(response);
    state.requestCleanup?.();
    return { response: null, error };
  }
  if (!response.body) {
    state.requestCleanup?.();
    return {
      response: null,
      error: new AkbError({
        message: "The AKB Change Event stream did not include a body.",
        code: "protocol_error",
      }, response),
    };
  }
  return { response, error: null };
}

/**
 * Reads a stream until it ends: "ended" if an event or a comment arrived
 * first, "dropped" if none did, "terminal" if the subscription stopped on it.
 */
async function consumeResponse(
  state: SubscriptionState,
  vault: string,
  registrations: readonly Registration[],
  response: Response,
): Promise<"ended" | "dropped" | "terminal"> {
  const parser = new SseParser();
  const reader = response.body!.getReader();
  const decoder = new TextDecoder();
  let live = false;
  const consume = async (result: SseParserResult): Promise<void> => {
    if (!live && (result.frames.length > 0 || result.commented)) {
      // The first event or comment shows the open reached a live stream.
      live = true;
      state.failedOpens = 0;
    }
    await consumeParserResult(state, vault, registrations, result);
  };
  const ended = (): "ended" | "dropped" => (live ? "ended" : "dropped");
  try {
    while (!state.stopped) {
      const chunk = await reader.read();
      if (chunk.done) {
        const tail = decoder.decode();
        if (tail.length > 0) await consume(parser.feed(tail));
        await consume(parser.end());
        return ended();
      }
      await consume(parser.feed(decoder.decode(chunk.value, { stream: true })));
    }
    return ended();
  } catch (error) {
    if (state.stopped || isAbortError(error, state.signal)) return ended();
    if (error instanceof ChannelProtocolError) {
      terminateSubscription(state, error);
      return "terminal";
    }
    if (error instanceof ListenerFailure) {
      terminateSubscription(state, error.cause);
      return "terminal";
    }
    return ended();
  } finally {
    try {
      reader.releaseLock();
    } catch {
      // The stream may already have released the reader after an abort.
    }
    state.requestCleanup?.();
  }
}

async function consumeParserResult(
  state: SubscriptionState,
  vault: string,
  registrations: readonly Registration[],
  result: SseParserResult,
): Promise<void> {
  if (result.retryDelayMs !== null) state.retryDelayMs = result.retryDelayMs;
  for (const frame of result.frames) {
    await dispatchFrame(state, vault, registrations, frame);
  }
}

async function dispatchFrame(
  state: SubscriptionState,
  vault: string,
  registrations: readonly Registration[],
  frame: SseFrame,
): Promise<void> {
  if (frame.event !== "change" && frame.event !== "checkpoint") {
    throw new ChannelProtocolError();
  }
  if (frame.id === null || frame.id.length === 0) throw new ChannelProtocolError();
  let payload: unknown;
  try {
    payload = JSON.parse(frame.data);
  } catch {
    throw new ChannelProtocolError();
  }

  if (frame.event === "change") {
    const envelope = validateChangeEvent(payload, vault);
    if (envelope.cursor !== frame.id) throw new ChannelProtocolError();
    try {
      for (const registration of registrations) {
        if (registration.type !== "change") continue;
        if (registration.kinds !== undefined && !registration.kinds.includes(envelope.kind)) continue;
        await registration.listener(envelope);
      }
    } catch (error) {
      throw new ListenerFailure(error);
    }
    state.cursor = envelope.cursor;
    return;
  }

  const checkpoint = validateCheckpoint(payload);
  if (checkpoint.cursor !== frame.id) throw new ChannelProtocolError();
  try {
    for (const registration of registrations) {
      if (registration.type === "checkpoint") await registration.listener(checkpoint);
    }
  } catch (error) {
    throw new ListenerFailure(error);
  }
  state.cursor = checkpoint.cursor;
}

function validateChangeEvent(value: unknown, vault: string): ChangeEventEnvelopeV1 {
  if (!isObject(value) || !hasOnlyKeys(value, [
    "version",
    "cursor",
    "occurred_at",
    "vault",
    "kind",
    "resource_uri",
    "actor",
    "payload",
  ])) throw new ChannelProtocolError();
  if (value.version !== 1 || !isCursor(value.cursor) || typeof value.occurred_at !== "string"
    || Number.isNaN(Date.parse(value.occurred_at)) || value.vault !== vault
    || typeof value.kind !== "string" || !EVENT_KIND_PATTERN.test(value.kind)
    || !isObject(value.payload)) {
    throw new ChannelProtocolError();
  }
  if (value.resource_uri !== undefined && value.resource_uri !== null && typeof value.resource_uri !== "string") {
    throw new ChannelProtocolError();
  }
  if (value.actor !== undefined && value.actor !== null && typeof value.actor !== "string") {
    throw new ChannelProtocolError();
  }
  return value as unknown as ChangeEventEnvelopeV1;
}

function validateCheckpoint(value: unknown): TailCheckpointV1 {
  if (!isObject(value) || !hasOnlyKeys(value, ["version", "cursor"])
    || value.version !== 1 || !isCursor(value.cursor)) {
    throw new ChannelProtocolError();
  }
  return value as unknown as TailCheckpointV1;
}

function isCursor(value: unknown): value is EventCursor {
  return typeof value === "string" && EVENT_CURSOR_PATTERN.test(value);
}

function hasOnlyKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const allowed = new Set(keys);
  return Object.keys(value).every((key) => allowed.has(key));
}

function normalizedChannelKinds(registrations: readonly Registration[]): readonly EventKind[] {
  const changeRegistrations = registrations.filter(
    (registration): registration is ChangeRegistration => registration.type === "change",
  );
  if (changeRegistrations.some((registration) => registration.kinds === undefined)) return [];
  return normalizeKinds(changeRegistrations.flatMap((registration) => registration.kinds ?? []));
}

function isRetryable(error: AkbError): boolean {
  return error.code === AKB_ERROR_CODES.transport
    || (error.status !== null && error.status >= 500 && error.status <= 599);
}

async function responseError(response: Response): Promise<AkbError> {
  let body: unknown = null;
  try {
    const text = await response.text();
    if (text.length > 0) {
      const mediaType = (response.headers.get("content-type") ?? "")
        .toLowerCase()
        .split(";", 1)[0]
        .trim();
      if (mediaType.endsWith("json") || mediaType.length === 0) {
        try {
          body = JSON.parse(text);
        } catch {
          body = text;
        }
      } else {
        body = text;
      }
    }
  } catch {
    body = null;
  }
  return new AkbError(body, response);
}

/**
 * The wait before reopening after the n-th unsuccessful open in a row
 * (n = `state.failedOpens`).
 *
 * The channel's own wait is the longer of the reconnection time and a random
 * time in [c/2, c], where c = min(30000, max(reconnection time, 250) × 2^n) ms.
 * A refusal's Retry-After is a floor on that, not capped, plus a random
 * extra of up to min(20% of it, 1000) ms.
 */
function refusedOpenDelayMs(state: SubscriptionState, retryAfterMs: number | null): number {
  const step = Math.max(state.retryDelayMs, DEFAULT_RECONNECT_DELAY_MS);
  const doublings = Math.min(state.failedOpens, MAX_BACKOFF_DOUBLINGS);
  const ceiling = Math.min(MAX_RECONNECT_DELAY_MS, step * 2 ** doublings);
  const backoff = Math.round(ceiling / 2 + Math.random() * (ceiling / 2));
  const own = Math.max(state.retryDelayMs, backoff);
  if (retryAfterMs === null) return own;
  const spread = Math.min(retryAfterMs * RETRY_AFTER_JITTER_RATIO, RETRY_AFTER_JITTER_MAX_MS);
  return Math.max(own, retryAfterMs + Math.round(Math.random() * spread));
}

async function waitForReconnect(state: SubscriptionState, delayMs: number): Promise<boolean> {
  if (state.stopped) return false;
  return await new Promise<boolean>((resolve) => {
    let remainingMs = delayMs;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const wake = () => {
      clearTimeout(timer);
      state.wakeWaiters.delete(wake);
      resolve(false);
    };
    const arm = () => {
      const stepMs = Math.min(remainingMs, MAX_TIMER_DELAY_MS);
      remainingMs -= stepMs;
      timer = setTimeout(() => {
        if (remainingMs > 0 && !state.stopped) {
          arm();
          return;
        }
        state.wakeWaiters.delete(wake);
        resolve(!state.stopped);
      }, stepMs);
    };
    state.wakeWaiters.add(wake);
    arm();
  });
}

function stopSubscription(state: SubscriptionState): void {
  if (state.stopped) {
    settleClosed(state);
    return;
  }
  state.stopped = true;
  state.requestController?.abort();
  state.requestCleanup?.();
  state.externalCleanup?.();
  state.externalCleanup = null;
  for (const wake of state.wakeWaiters) wake();
  state.wakeWaiters.clear();
  settleClosed(state);
}

function terminateSubscription(state: SubscriptionState, error: unknown): void {
  if (state.stopped) return;
  state.stopped = true;
  state.requestController?.abort();
  state.requestCleanup?.();
  state.externalCleanup?.();
  state.externalCleanup = null;
  for (const wake of state.wakeWaiters) wake();
  state.wakeWaiters.clear();
  if (!state.closedSettled) {
    state.closedSettled = true;
    state.rejectClosed(error);
  }
}

function settleClosed(state: SubscriptionState): void {
  if (state.closedSettled) return;
  state.closedSettled = true;
  state.resolveClosed();
}

function isObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
