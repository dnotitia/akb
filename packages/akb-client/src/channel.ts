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
const DEFAULT_RECONNECT_DELAY_MS = 250;
const MAX_RECONNECT_DELAY_MS = 30_000;

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

  feed(chunk: string): SseParserResult {
    this.buffer += chunk;
    const frames: SseFrame[] = [];
    let newlineIndex = this.findNewline();
    while (newlineIndex >= 0) {
      let line = this.buffer.slice(0, newlineIndex);
      const lineEndingLength = this.buffer[newlineIndex] === "\r" && this.buffer[newlineIndex + 1] === "\n"
        ? 2
        : 1;
      this.buffer = this.buffer.slice(newlineIndex + lineEndingLength);
      if (line.endsWith("\r")) line = line.slice(0, -1);
      const frame = this.consumeLine(line);
      if (frame) frames.push(frame);
      newlineIndex = this.findNewline();
    }
    return this.takeResult(frames);
  }

  end(): SseParserResult {
    const frames: SseFrame[] = [];
    if (this.buffer.length > 0) {
      const frame = this.consumeLine(this.buffer);
      if (frame) frames.push(frame);
      this.buffer = "";
    }
    const frame = this.dispatch();
    if (frame) frames.push(frame);
    return this.takeResult(frames);
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
    if (line.startsWith(":")) return null;

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
    const retryDelayMs = this.retryDelayMs;
    this.retryDelayMs = null;
    return { frames, retryDelayMs };
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
    if (!(await waitForReconnect(state))) return subscription;
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

    const next = await reconnect(state, vault, openStream, registrations);
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
): Promise<Response | null> {
  while (!state.stopped) {
    if (!(await waitForReconnect(state))) return null;
    const result = await openConnection(state, vault, openStream, false, registrations);
    if (result.response) return result.response;
    if (!result.error || state.stopped) return null;
    if (!isRetryable(result.error)) {
      terminateSubscription(state, result.error);
      return null;
    }
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

async function consumeResponse(
  state: SubscriptionState,
  vault: string,
  registrations: readonly Registration[],
  response: Response,
): Promise<"retry" | "terminal"> {
  const parser = new SseParser();
  const reader = response.body!.getReader();
  const decoder = new TextDecoder();
  try {
    while (!state.stopped) {
      const chunk = await reader.read();
      if (chunk.done) {
        const tail = decoder.decode();
        if (tail.length > 0) {
          await consumeParserResult(state, vault, registrations, parser.feed(tail));
        }
        await consumeParserResult(state, vault, registrations, parser.end());
        return "retry";
      }
      const result = parser.feed(decoder.decode(chunk.value, { stream: true }));
      await consumeParserResult(state, vault, registrations, result);
    }
    return "retry";
  } catch (error) {
    if (state.stopped || isAbortError(error, state.signal)) return "retry";
    if (error instanceof ChannelProtocolError) {
      terminateSubscription(state, error);
      return "terminal";
    }
    if (error instanceof ListenerFailure) {
      terminateSubscription(state, error.cause);
      return "terminal";
    }
    return "retry";
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

async function waitForReconnect(state: SubscriptionState): Promise<boolean> {
  if (state.stopped) return false;
  return await new Promise<boolean>((resolve) => {
    const timer = setTimeout(() => {
      state.wakeWaiters.delete(wake);
      resolve(!state.stopped);
    }, state.retryDelayMs);
    const wake = () => {
      clearTimeout(timer);
      state.wakeWaiters.delete(wake);
      resolve(false);
    };
    state.wakeWaiters.add(wake);
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
