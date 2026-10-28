import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, getDocumentHead, type DocumentHead } from "@/lib/api";
import { useDocumentHead, type DocumentHeadOptions } from "../use-document-head";

vi.mock("@/lib/api", async () => ({ ...await vi.importActual("@/lib/api"), getDocumentHead: vi.fn() }));

const initial: DocumentHeadOptions = {
  vault: "team", document: "d-guide", scope: "account-a:writer", baseCommit: "base-commit", enabled: true,
};
const head: DocumentHead = {
  kind: "document_head", uri: "akb://team/coll/notes/doc/guide.md", vault: "team", path: "notes/guide.md",
  current_commit: "remote-commit", updated_at: "2026-10-20T00:00:00Z",
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

async function flush() {
  await act(async () => {});
}

function setVisibility(state: DocumentVisibilityState) {
  vi.spyOn(document, "visibilityState", "get").mockReturnValue(state);
  act(() => document.dispatchEvent(new Event("visibilitychange")));
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
  vi.mocked(getDocumentHead).mockReset().mockResolvedValue(head);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("document head observer", () => {
  it("checks initially and every 15 seconds only while the editor is enabled", async () => {
    const { result, rerender } = renderHook(useDocumentHead, { initialProps: initial });
    expect(result.current.checking).toBe(true);
    await flush();
    expect(result.current.head).toEqual(head);
    expect(result.current.checking).toBe(false);
    await act(() => vi.advanceTimersByTimeAsync(14_999));
    expect(getDocumentHead).toHaveBeenCalledTimes(1);
    await act(() => vi.advanceTimersByTimeAsync(1));
    expect(getDocumentHead).toHaveBeenCalledTimes(2);

    rerender({ ...initial, enabled: false });
    expect(result.current.head).toEqual(head);
    expect(await result.current.check()).toBeNull();
    await act(() => vi.advanceTimersByTimeAsync(45_000));
    expect(getDocumentHead).toHaveBeenCalledTimes(2);
    rerender(initial);
    await flush();
    expect(getDocumentHead).toHaveBeenCalledTimes(3);
  });

  it.each(["scope", "vault", "document"] as const)("does not request an editor without its %s identity", async field => {
    const { result } = renderHook(() => useDocumentHead({ ...initial, [field]: "" }));
    expect(await result.current.check()).toBeNull();
    await act(() => vi.advanceTimersByTimeAsync(45_000));
    expect(getDocumentHead).not.toHaveBeenCalled();
  });

  it("cancels an in-flight check as soon as the editor becomes inactive", async () => {
    const pending = deferred<DocumentHead>();
    vi.mocked(getDocumentHead).mockReturnValueOnce(pending.promise);
    const { result, rerender } = renderHook(useDocumentHead, { initialProps: initial });
    const check = result.current.check();
    const signal = vi.mocked(getDocumentHead).mock.calls[0][2]!;

    rerender({ ...initial, enabled: false });
    expect(signal.aborted).toBe(true);
    expect(result.current.checking).toBe(false);
    await act(async () => pending.resolve(head));
    expect(await check).toBeNull();
    expect(result.current.head).toBeNull();
    expect(result.current.error).toBeNull();
  });

  it("preserves a known remote head through pause, pending resume, and transient failure", async () => {
    const { result, rerender } = renderHook(useDocumentHead, { initialProps: initial });
    await flush();
    expect(result.current.head).toEqual(head);
    rerender({ ...initial, enabled: false });
    expect(result.current.head).toEqual(head);
    expect(result.current.checking).toBe(false);
    await act(() => vi.advanceTimersByTimeAsync(30_000));
    expect(getDocumentHead).toHaveBeenCalledTimes(1);

    const pending = deferred<DocumentHead>();
    vi.mocked(getDocumentHead).mockReturnValueOnce(pending.promise);
    rerender(initial);
    expect(result.current.head).toEqual(head);
    expect(result.current.checking).toBe(true);
    const unavailable = new TypeError("Failed to fetch");
    await act(async () => pending.reject(unavailable));
    expect(result.current.head).toEqual(head);
    expect(result.current.error).toBe(unavailable);
    expect(result.current.checking).toBe(false);
  });

  it("keeps a terminal failure while a recovery check is pending", async () => {
    const denied = new ApiError("Forbidden", 403, null);
    vi.mocked(getDocumentHead).mockRejectedValueOnce(denied);
    const { result } = renderHook(useDocumentHead, { initialProps: initial });
    await flush();
    expect(result.current.error).toBe(denied);
    const pending = deferred<DocumentHead>();
    vi.mocked(getDocumentHead).mockReturnValueOnce(pending.promise);
    act(() => { void result.current.check(); });
    expect(result.current.checking).toBe(true);
    expect(result.current.error).toBe(denied);
    await act(async () => pending.resolve(head));
    expect(result.current.head).toEqual(head);
    expect(result.current.error).toBeNull();
  });

  it.each([401, 403, 404])("retains status %s through transient failures until an authorized head succeeds", async status => {
    const unavailable = new ApiError("Access unavailable", status, null);
    vi.mocked(getDocumentHead).mockRejectedValueOnce(unavailable);
    const { result } = renderHook(useDocumentHead, { initialProps: initial });
    await flush();
    for (const failure of [new ApiError("Unavailable", 503, null), new TypeError("Failed to fetch")]) {
      vi.mocked(getDocumentHead).mockRejectedValueOnce(failure);
      await act(async () => { await result.current.check(); });
      expect(result.current.error).toBe(unavailable);
      expect(result.current.head).toBeNull();
    }
    const pending = deferred<DocumentHead>();
    vi.mocked(getDocumentHead).mockReturnValueOnce(pending.promise);
    act(() => { void result.current.check(); });
    expect(result.current.error).toBe(unavailable);
    await act(async () => pending.resolve(head));
    expect(result.current.error).toBeNull();
    expect(result.current.head).toEqual(head);
  });

  it.each([{ scope: "another-account" }, { baseCommit: "another-base" }])("does not carry terminal failure into a new generation %j", async change => {
    vi.mocked(getDocumentHead).mockRejectedValueOnce(new ApiError("Unavailable", 403, null));
    const { result, rerender } = renderHook(useDocumentHead, { initialProps: initial });
    await flush();
    expect(result.current.error).toBeInstanceOf(ApiError);
    const pending = deferred<DocumentHead>();
    vi.mocked(getDocumentHead).mockReturnValueOnce(pending.promise);
    rerender({ ...initial, ...change });
    expect(result.current.error).toBeNull();
    expect(result.current.head).toBeNull();
    await act(async () => pending.resolve(head));
    expect(result.current.head).toEqual(head);
  });

  it("starts when visibility returns and cancels a flight when the page becomes hidden", async () => {
    setVisibility("hidden");
    const { result } = renderHook(useDocumentHead, { initialProps: initial });
    await act(() => vi.advanceTimersByTimeAsync(30_000));
    expect(getDocumentHead).not.toHaveBeenCalled();
    setVisibility("visible");
    await flush();
    expect(result.current.head).toEqual(head);

    const pending = deferred<DocumentHead>();
    vi.mocked(getDocumentHead).mockReturnValueOnce(pending.promise);
    await act(() => vi.advanceTimersByTimeAsync(15_000));
    const signal = vi.mocked(getDocumentHead).mock.calls[1][2]!;
    expect(result.current.checking).toBe(true);
    setVisibility("hidden");
    expect(signal.aborted).toBe(true);
    expect(result.current.checking).toBe(false);
    await act(async () => pending.resolve({ ...head, current_commit: "discard-hidden" }));
    expect(result.current.head).toEqual(head);
    expect(await result.current.check()).toBeNull();
    await act(() => vi.advanceTimersByTimeAsync(45_000));
    act(() => window.dispatchEvent(new Event("online")));
    expect(getDocumentHead).toHaveBeenCalledTimes(2);

    setVisibility("visible");
    await flush();
    expect(getDocumentHead).toHaveBeenCalledTimes(3);
  });

  it("shares an overlapping initial, manual, timer, and online check", async () => {
    const pending = deferred<DocumentHead>();
    vi.mocked(getDocumentHead).mockReturnValueOnce(pending.promise);
    const { result } = renderHook(useDocumentHead, { initialProps: initial });
    let first!: Promise<DocumentHead | null>;
    let second!: Promise<DocumentHead | null>;
    act(() => {
      first = result.current.check();
      second = result.current.check();
      window.dispatchEvent(new Event("online"));
    });
    await act(() => vi.advanceTimersByTimeAsync(30_000));
    expect(first).toBe(second);
    expect(getDocumentHead).toHaveBeenCalledTimes(1);
    await act(async () => pending.resolve(head));
    expect(await first).toEqual(head);
    expect(result.current.head).toEqual(head);
  });

  it.each([
    { scope: "account-b:writer" },
    { scope: "account-a:reader" },
    { vault: "other-vault" },
    { document: "d-other" },
    { baseCommit: "saved-commit" },
    { baseCommit: null },
  ])("aborts and discards a late response after identity/base change %j", async patch => {
    const pending = deferred<DocumentHead>();
    vi.mocked(getDocumentHead).mockReturnValueOnce(pending.promise);
    const { result, rerender } = renderHook(useDocumentHead, { initialProps: initial });
    const previousCheck = result.current.check();
    const previousSignal = vi.mocked(getDocumentHead).mock.calls[0][2]!;

    rerender({ ...initial, ...patch });
    expect(previousSignal.aborted).toBe(true);
    expect(result.current.head).toBeNull();
    await flush();
    expect(result.current.head).toEqual(head);
    await act(async () => pending.resolve({ ...head, current_commit: "discard-old-scope" }));
    expect(await previousCheck).toBeNull();
    expect(result.current.head).toEqual(head);
  });

  it("clears a last-good observation immediately when a save establishes a new base", async () => {
    const { result, rerender } = renderHook(useDocumentHead, { initialProps: initial });
    await flush();
    expect(result.current.head).toEqual(head);
    const pending = deferred<DocumentHead>();
    vi.mocked(getDocumentHead).mockReturnValueOnce(pending.promise);

    rerender({ ...initial, baseCommit: head.current_commit });
    expect(result.current.head).toBeNull();
    expect(result.current.error).toBeNull();
    expect(result.current.checking).toBe(true);
    await act(async () => pending.resolve(head));
    expect(result.current.head).toEqual(head);
  });

  it.each([401, 403, 404])("clears the last private head for status %s and resolves failed manual checks", async status => {
    const { result } = renderHook(useDocumentHead, { initialProps: initial });
    await flush();
    const error = new ApiError("Lost access or missing document", status, null);
    vi.mocked(getDocumentHead).mockRejectedValueOnce(error);
    let checked!: DocumentHead | null;
    await act(async () => { checked = await result.current.check(); });

    expect(checked).toBeNull();
    expect(result.current.error).toBe(error);
    expect(result.current.head).toBeNull();
    expect(result.current.checking).toBe(false);
    await act(async () => { checked = await result.current.check(); });
    expect(checked).toEqual(head);
    expect(result.current.error).toBeNull();
  });

  it.each([new TypeError("Failed to fetch"), new ApiError("Unavailable", 503, null)])("keeps a known remote head through transient failure and recovers online: %s", async error => {
    const { result } = renderHook(useDocumentHead, { initialProps: initial });
    await flush();
    vi.mocked(getDocumentHead).mockRejectedValueOnce(error);
    await act(() => vi.advanceTimersByTimeAsync(15_000));
    expect(result.current.head).toEqual(head);
    expect(result.current.error).toBe(error);
    expect(result.current.checking).toBe(false);

    const recovered = { ...head, current_commit: "newest-commit" };
    vi.mocked(getDocumentHead).mockResolvedValueOnce(recovered);
    act(() => window.dispatchEvent(new Event("online")));
    await flush();
    expect(result.current.head).toEqual(recovered);
    expect(result.current.error).toBeNull();
  });

  it("aborts on unmount and does not reuse an observation in a later editor", async () => {
    const pending = deferred<DocumentHead>();
    vi.mocked(getDocumentHead).mockReturnValueOnce(pending.promise);
    const first = renderHook(useDocumentHead, { initialProps: initial });
    const check = first.result.current.check();
    const signal = vi.mocked(getDocumentHead).mock.calls[0][2]!;
    first.unmount();
    expect(signal.aborted).toBe(true);
    await act(async () => pending.resolve({ ...head, current_commit: "discard-unmounted" }));
    expect(await check).toBeNull();

    const next = renderHook(useDocumentHead, { initialProps: initial });
    expect(next.result.current.head).toBeNull();
    await flush();
    expect(getDocumentHead).toHaveBeenCalledTimes(2);
    expect(next.result.current.head).toEqual(head);
  });
});
