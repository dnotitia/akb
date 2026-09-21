import { QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/api";
import {
  readSearchStatus,
  SEARCH_STATUS_PREFIX,
  SearchStatusStore,
  StatusRequestError,
} from "../search-status-store";

vi.mock("@/lib/api", () => ({ authenticatedFetch: vi.fn() }));

const NOW = 1_800_000_000_000;
const SCOPE_URL = "/api/v1/my/vaults";
const VAULT = { id: "a", name: "Private research" };
type Read = (url: string, signal: AbortSignal) => Promise<unknown>;

function good(pending = 0, abandoned = 0) {
  return { search_update_status: { version: 1, stages: {
    search_index: { mode: "enabled", unit: "chunks", scope: "stored_chunks", pending, retrying: 0, abandoned },
    file_projection: { mode: "not_applicable", unit: "file_updates", scope: "latest_file_intents" },
    content_preparation: { mode: "not_applicable", unit: "revision_updates", scope: "current_heads" },
    metadata: { mode: "disabled", unit: "documents", scope: "external_git_documents" },
  } } };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function untilAborted(signal: AbortSignal): Promise<never> {
  return new Promise((_, reject) => {
    const abort = () => reject(new DOMException("Aborted", "AbortError"));
    if (signal.aborted) abort();
    else signal.addEventListener("abort", abort, { once: true });
  });
}

const stores: SearchStatusStore[] = [];
const clients: QueryClient[] = [];

function setup(read: Read, identity = "user-a", client?: QueryClient) {
  const cache = client ?? new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
  const unauthorized = vi.fn();
  const store = new SearchStatusStore(cache, "https://akb.example", identity, unauthorized, read);
  stores.push(store);
  if (!clients.includes(cache)) clients.push(cache);
  return { store, client: cache, unauthorized };
}

async function start(store: SearchStatusStore) {
  store.setActive(true);
  await vi.advanceTimersByTimeAsync(0);
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  vi.clearAllMocks();
});

afterEach(() => {
  for (const store of stores.splice(0)) store.dispose();
  for (const client of clients.splice(0)) client.clear();
  vi.useRealTimers();
});

describe("SearchStatusStore", () => {
  it("does not read before activation and requests only reader-scoped routes", async () => {
    const read = vi.fn<Read>().mockImplementation(async (url) => url === SCOPE_URL ? { vaults: [VAULT, VAULT] } : good());
    const { store, client } = setup(read);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(read).not.toHaveBeenCalled();
    await start(store);
    expect(read.mock.calls.map(([url]) => url)).toEqual([SCOPE_URL, "/health/vault/Private%20research"]);
    expect(store.getSnapshot()).toMatchObject({ verified: true, loading: false, observations: [{ vaultId: "a", core: "clear" }] });
    const keys = client.getQueryCache().getAll().map((query) => query.queryKey);
    expect(keys).toContainEqual([SEARCH_STATUS_PREFIX, "https://akb.example", "user-a", 0, "a"]);
  });

  it("never exceeds five simultaneous health requests or duplicates an in-flight Vault", async () => {
    const vaults = Array.from({ length: 12 }, (_, index) => ({ id: String(index), name: `vault-${index}` }));
    const pending: ReturnType<typeof deferred<unknown>>[] = [];
    let active = 0;
    let peak = 0;
    const read = vi.fn<Read>().mockImplementation(async (url) => {
      if (url === SCOPE_URL) return { vaults };
      const wait = deferred<unknown>();
      pending.push(wait);
      peak = Math.max(peak, ++active);
      try { return await wait.promise; }
      finally { active--; }
    });
    const { store } = setup(read);
    await start(store);
    expect(active).toBe(5);
    for (let index = 0; index < vaults.length; index++) {
      pending[index].resolve(good(1));
      await vi.advanceTimersByTimeAsync(0);
      expect(active).toBeLessThanOrEqual(5);
    }
    expect(peak).toBe(5);
    expect(new Set(read.mock.calls.filter(([url]) => url !== SCOPE_URL).map(([url]) => url)).size).toBe(12);
    expect(store.getSnapshot().observations.every((row) => row.observation === "fresh")).toBe(true);
    expect(store.getSnapshot().loading).toBe(false);
  });

  it("manual refresh joins a running sweep instead of scheduling duplicate completed work", async () => {
    const wait = deferred<unknown>();
    const read = vi.fn<Read>().mockImplementation(async (url) => url === SCOPE_URL ? { vaults: [VAULT] } : wait.promise);
    const { store } = setup(read);
    await start(store);
    store.refresh(); store.refresh(); store.refresh();
    await vi.advanceTimersByTimeAsync(0);
    expect(read).toHaveBeenCalledTimes(2);
    wait.resolve(good());
    await vi.advanceTimersByTimeAsync(1);
    expect(read).toHaveBeenCalledTimes(2);
    expect(store.getSnapshot().loading).toBe(false);
  });

  it("rate-limits repeated manual refreshes after a fast completed sweep", async () => {
    const read = vi.fn<Read>().mockImplementation(async (url) => url === SCOPE_URL ? { vaults: [VAULT] } : good());
    const { store } = setup(read);
    await start(store);
    store.refresh();
    await vi.advanceTimersByTimeAsync(0);
    expect(read).toHaveBeenCalledTimes(4);
    store.refresh();
    await vi.advanceTimersByTimeAsync(500);
    store.refresh();
    await vi.advanceTimersByTimeAsync(0);
    expect(read).toHaveBeenCalledTimes(4);
    await vi.advanceTimersByTimeAsync(500);
    store.refresh();
    await vi.advanceTimersByTimeAsync(0);
    expect(read).toHaveBeenCalledTimes(6);
  });

  it("a failed refresh preserves last-good receipt time but immediately marks it stale", async () => {
    let failing = false;
    const read = vi.fn<Read>().mockImplementation(async (url) => {
      if (url === SCOPE_URL) return { vaults: [VAULT] };
      if (failing) throw new Error("Network failure");
      return good(2, 1);
    });
    const { store } = setup(read);
    await start(store);
    failing = true;
    await vi.advanceTimersByTimeAsync(2_000);
    store.refresh();
    await vi.advanceTimersByTimeAsync(0);
    expect(store.getSnapshot().observations[0]).toMatchObject({
      observation: "stale", core: "attention", receivedAt: NOW, attemptedAt: NOW + 2_000,
    });
  });

  it("clears private rows and cache immediately on 401 and asks Layout to revalidate", async () => {
    let fail = false;
    const read = vi.fn<Read>().mockImplementation(async (url) => {
      if (url === SCOPE_URL) return { vaults: [VAULT] };
      if (fail) throw new StatusRequestError(401);
      return good();
    });
    const { store, client, unauthorized } = setup(read);
    await start(store);
    fail = true;
    store.refresh();
    await vi.advanceTimersByTimeAsync(0);
    expect(unauthorized).toHaveBeenCalledTimes(1);
    expect(store.getSnapshot()).toMatchObject({ verified: false, observations: [] });
    expect(client.getQueryCache().getAll()).toHaveLength(0);
    const attempts = read.mock.calls.length;
    await vi.advanceTimersByTimeAsync(120_000);
    expect(read).toHaveBeenCalledTimes(attempts);
  });

  it("removes revoked Vault detail and promptly rechecks directory after 403", async () => {
    let scopes = 0;
    let healthReads = 0;
    const read = vi.fn<Read>().mockImplementation(async (url) => {
      if (url === SCOPE_URL) return { vaults: ++scopes >= 3 ? [] : [VAULT] };
      if (++healthReads > 1) throw new StatusRequestError(403);
      return good();
    });
    const { store, client } = setup(read);
    await start(store);
    store.refresh();
    await vi.advanceTimersByTimeAsync(1);
    expect(store.getSnapshot().observations).toEqual([]);
    expect(client.getQueryCache().getAll().some((query) => query.queryKey.at(-1) === VAULT.id)).toBe(false);
    expect(scopes).toBe(3);
    expect(store.getSnapshot().verified).toBe(true);
  });

  it("rechecks directory after 404 before deciding a Vault remains unavailable", async () => {
    let scopes = 0;
    const read = vi.fn<Read>().mockImplementation(async (url) => {
      if (url === SCOPE_URL) return { vaults: ++scopes > 1 ? [] : [VAULT] };
      throw new StatusRequestError(404);
    });
    const { store } = setup(read);
    await start(store);
    await vi.advanceTimersByTimeAsync(1);
    expect(scopes).toBe(2);
    expect(store.getSnapshot()).toMatchObject({ verified: true, observations: [] });
  });

  it("does not restore a 403-revoked private row from a stale directory during the next health check", async () => {
    let healthReads = 0;
    const wait = deferred<unknown>();
    const read = vi.fn<Read>().mockImplementation(async (url) => {
      if (url === SCOPE_URL) return { vaults: [VAULT] };
      if (++healthReads === 1) return good();
      if (healthReads === 2) throw new StatusRequestError(403);
      return wait.promise;
    });
    const { store } = setup(read);
    await start(store);
    store.refresh();
    await vi.advanceTimersByTimeAsync(0);
    expect(store.getSnapshot().observations).toEqual([]);
    await vi.advanceTimersByTimeAsync(15_000);
    expect(store.getSnapshot().observations).toEqual([]);
    expect(store.getSnapshot().verified).toBe(false);
    wait.resolve(good());
    await vi.advanceTimersByTimeAsync(0);
    expect(store.getSnapshot().observations).toMatchObject([{ vaultId: VAULT.id, observation: "fresh" }]);
  });

  it("does not repeatedly add directory rechecks for a still-listed unavailable endpoint", async () => {
    let scopes = 0;
    const read = vi.fn<Read>().mockImplementation(async (url) => {
      if (url === SCOPE_URL) { scopes++; return { vaults: [VAULT] }; }
      throw new StatusRequestError(404);
    });
    const { store } = setup(read);
    await start(store);
    expect(scopes).toBe(2);
    expect(store.getSnapshot().observations[0]).toMatchObject({ observation: "unavailable", coverage: "partial" });
    await vi.advanceTimersByTimeAsync(15_000);
    expect(scopes).toBe(3);
  });

  it("scope discovery failure is unknown rather than a complete empty account", async () => {
    const read = vi.fn<Read>().mockRejectedValue(new Error("Network down"));
    const { store } = setup(read);
    await start(store);
    expect(store.getSnapshot()).toEqual({ observations: [], verified: false, loading: false });
  });

  it("a malformed Vault response does not suppress valid observations for other Vaults", async () => {
    const other = { id: "b", name: "Valid vault" };
    const read = vi.fn<Read>().mockImplementation(async (url) => {
      if (url === SCOPE_URL) return { vaults: [VAULT, other] };
      return url.endsWith("Valid%20vault") ? good(3) : { status: "ok" };
    });
    const { store } = setup(read);
    await start(store);
    expect(store.getSnapshot()).toMatchObject({ verified: true, loading: false });
    expect(store.getSnapshot().observations).toMatchObject([
      { vaultId: VAULT.id, observation: "unavailable", core: "unknown" },
      { vaultId: other.id, observation: "fresh", core: "updating" },
    ]);
  });

  it("directory removal deletes formerly authorized cached detail before other reads finish", async () => {
    let scopes = 0;
    const other = { id: "b", name: "Remaining vault" };
    const wait = deferred<unknown>();
    const read = vi.fn<Read>().mockImplementation(async (url) => {
      if (url === SCOPE_URL) return { vaults: ++scopes > 1 ? [other] : [VAULT] };
      return url.endsWith("Remaining%20vault") ? wait.promise : good();
    });
    const { store, client } = setup(read);
    await start(store);
    store.refresh();
    await vi.advanceTimersByTimeAsync(0);
    expect(store.getSnapshot().observations.map((row) => row.vaultId)).toEqual([other.id]);
    expect(client.getQueryCache().getAll().some((query) => query.queryKey.at(-1) === VAULT.id)).toBe(false);
    wait.resolve(good());
    await vi.advanceTimersByTimeAsync(0);
  });

  it("prioritizes the inspected Vault while retaining the rest of accessible scope", async () => {
    const vaults = Array.from({ length: 8 }, (_, index) => ({ id: String(index), name: `vault-${index}` }));
    const read = vi.fn<Read>().mockImplementation(async (url) => url === SCOPE_URL ? { vaults } : good());
    const { store } = setup(read);
    store.setPriority("7");
    await start(store);
    expect(read.mock.calls[1][0]).toBe("/health/vault/vault-7");
    expect(store.getSnapshot().observations.map((row) => row.vaultId)).toEqual(vaults.map((vault) => vault.id));
    expect(read.mock.calls.filter(([url]) => url !== SCOPE_URL)).toHaveLength(8);
  });

  it.each([405, 501])("suppresses background health retry for %s until explicit refresh", async (status) => {
    const read = vi.fn<Read>().mockImplementation(async (url) => {
      if (url === SCOPE_URL) return { vaults: [VAULT] };
      throw new StatusRequestError(status);
    });
    const { store } = setup(read);
    await start(store);
    const healthReads = () => read.mock.calls.filter(([url]) => url !== SCOPE_URL).length;
    expect(store.getSnapshot().observations[0]).toMatchObject({ observation: "unavailable", coverage: "unsupported" });
    await vi.advanceTimersByTimeAsync(45_000);
    expect(healthReads()).toBe(1);
    store.refresh();
    await vi.advanceTimersByTimeAsync(0);
    expect(healthReads()).toBe(2);
  });

  it("honors a Vault Retry-After deadline even for manual refresh", async () => {
    let attempts = 0;
    const read = vi.fn<Read>().mockImplementation(async (url) => {
      if (url === SCOPE_URL) return { vaults: [VAULT] };
      if (++attempts === 1) throw new StatusRequestError(429, NOW + 45_000);
      return good();
    });
    const { store } = setup(read);
    await start(store);
    await vi.advanceTimersByTimeAsync(15_000);
    store.refresh();
    await vi.advanceTimersByTimeAsync(0);
    expect(attempts).toBe(1);
    await vi.advanceTimersByTimeAsync(29_999);
    expect(attempts).toBe(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(attempts).toBe(2);
  });

  it("honors a directory Retry-After without disclosing cached names", async () => {
    const read = vi.fn<Read>().mockRejectedValue(new StatusRequestError(429, NOW + 60_000));
    const { store } = setup(read);
    await start(store);
    await vi.advanceTimersByTimeAsync(59_999);
    expect(read).toHaveBeenCalledTimes(1);
    expect(store.getSnapshot()).toMatchObject({ verified: false, observations: [] });
    await vi.advanceTimersByTimeAsync(1);
    expect(read).toHaveBeenCalledTimes(2);
  });

  it("backs off repeated server failures rather than requesting every 15 seconds forever", async () => {
    let attempts = 0;
    const read = vi.fn<Read>().mockImplementation(async (url) => {
      if (url === SCOPE_URL) return { vaults: [VAULT] };
      attempts++;
      throw new StatusRequestError(503);
    });
    const { store } = setup(read);
    await start(store);
    await vi.advanceTimersByTimeAsync(15_000);
    expect(attempts).toBe(2);
    await vi.advanceTimersByTimeAsync(15_000);
    expect(attempts).toBe(2);
  });

  it("enforces the 8-second request bound even when an injected reader ignores abort", async () => {
    const wait = deferred<unknown>();
    let signal: AbortSignal | undefined;
    const read = vi.fn<Read>().mockImplementation(async (url, requestSignal) => {
      if (url === SCOPE_URL) return { vaults: [VAULT] };
      signal = requestSignal;
      return wait.promise;
    });
    const { store } = setup(read);
    await start(store);
    await vi.advanceTimersByTimeAsync(8_000);
    expect(signal?.aborted).toBe(true);
    expect(store.getSnapshot()).toMatchObject({ loading: false, observations: [{ observation: "unavailable" }] });
    wait.resolve(good(9));
    await vi.advanceTimersByTimeAsync(0);
    expect(store.getSnapshot().observations[0].observation).toBe("unavailable");
  });

  it("caps a sweep at 60 seconds and leaves unchecked Vaults explicitly unavailable", async () => {
    const vaults = Array.from({ length: 100 }, (_, index) => ({ id: String(index), name: `vault-${index}` }));
    const read = vi.fn<Read>().mockImplementation(async (url, signal) => {
      if (url === SCOPE_URL) return { vaults };
      return untilAborted(signal);
    });
    const { store } = setup(read);
    await start(store);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(store.getSnapshot().loading).toBe(false);
    expect(store.getSnapshot().observations).toHaveLength(100);
    expect(store.getSnapshot().observations.every((row) => row.observation === "unavailable")).toBe(true);
  });

  it("expires individual fresh rows at 120 seconds during another long-running sweep", async () => {
    const vaults = Array.from({ length: 100 }, (_, index) => ({ id: String(index), name: `vault-${index}` }));
    const read = vi.fn<Read>().mockImplementation(async (url, signal) => {
      if (url === SCOPE_URL) return { vaults };
      if (url === "/health/vault/vault-0") return good(2);
      return untilAborted(signal);
    });
    const { store } = setup(read);
    await start(store);
    expect(store.getSnapshot().observations[0]).toMatchObject({ observation: "fresh", receivedAt: NOW });
    await vi.advanceTimersByTimeAsync(120_000);
    expect(store.getSnapshot().observations[0]).toMatchObject({ observation: "stale", receivedAt: NOW });
  });

  it("pauses background polling, aborts old reads, and revalidates scope on resume", async () => {
    const wait = deferred<unknown>();
    let attempts = 0;
    let signal: AbortSignal | undefined;
    const read = vi.fn<Read>().mockImplementation(async (url, requestSignal) => {
      if (url === SCOPE_URL) return { vaults: [VAULT] };
      signal = requestSignal;
      return ++attempts === 1 ? wait.promise : good();
    });
    const { store } = setup(read);
    await start(store);
    store.setActive(false);
    expect(signal?.aborted).toBe(true);
    const calls = read.mock.calls.length;
    await vi.advanceTimersByTimeAsync(60_000);
    expect(read).toHaveBeenCalledTimes(calls);
    store.setActive(true);
    await vi.advanceTimersByTimeAsync(0);
    wait.resolve(good(7, 3));
    await vi.advanceTimersByTimeAsync(0);
    expect(store.getSnapshot().observations[0]).toMatchObject({ core: "clear", observation: "fresh" });
    expect(read.mock.calls.filter(([url]) => url === SCOPE_URL)).toHaveLength(2);
  });

  it("QueryClient.clear aborts old work and late responses cannot repopulate its generation", async () => {
    const wait = deferred<unknown>();
    let attempts = 0;
    let oldSignal: AbortSignal | undefined;
    const read = vi.fn<Read>().mockImplementation(async (url, signal) => {
      if (url === SCOPE_URL) return { vaults: [VAULT] };
      if (++attempts === 1) { oldSignal = signal; return wait.promise; }
      return good();
    });
    const { store, client } = setup(read);
    await start(store);
    client.clear();
    expect(oldSignal?.aborted).toBe(true);
    expect(store.getSnapshot().observations).toEqual([]);
    await vi.advanceTimersByTimeAsync(0);
    wait.resolve(good(0, 12));
    await vi.advanceTimersByTimeAsync(0);
    expect(store.getSnapshot().observations[0]).toMatchObject({ core: "clear", observation: "fresh" });
    expect(client.getQueryCache().getAll().every((query) => query.queryKey[3] === 1)).toBe(true);
  });

  it("an account switch purges old private cache and rejects late prior-identity results", async () => {
    const wait = deferred<unknown>();
    const oldRead = vi.fn<Read>().mockImplementation(async (url) => url === SCOPE_URL ? { vaults: [VAULT] } : wait.promise);
    const first = setup(oldRead);
    await start(first.store);
    first.store.dispose();
    const other = { id: "b", name: "Other account" };
    const newRead = vi.fn<Read>().mockImplementation(async (url) => url === SCOPE_URL ? { vaults: [other] } : good());
    const second = setup(newRead, "user-b", first.client);
    await start(second.store);
    wait.resolve(good(0, 20));
    await vi.advanceTimersByTimeAsync(0);
    expect(second.store.getSnapshot().observations.map((row) => row.vaultName)).toEqual([other.name]);
    expect(first.client.getQueryCache().getAll().every((query) => query.queryKey[2] === "user-b")).toBe(true);
  });
});

describe("readSearchStatus", () => {
  it.each([
    ["45", NOW + 45_000],
    [new Date(NOW + 60_000).toUTCString(), NOW + 60_000],
  ])("parses Retry-After %s on a failed response", async (retryAfter, retryAt) => {
    vi.mocked(authenticatedFetch).mockResolvedValue(new Response("", { status: 429, headers: { "Retry-After": retryAfter } }));
    const signal = new AbortController().signal;
    await expect(readSearchStatus("/health/vault/a", signal)).rejects.toMatchObject({ status: 429, retryAt });
    expect(authenticatedFetch).toHaveBeenCalledWith("/health/vault/a", { signal }, { unauthorized: "preserve-session" });
  });

  it("maps the existing carrier Unauthorized error to session revalidation", async () => {
    vi.mocked(authenticatedFetch).mockRejectedValue(new Error("Unauthorized"));
    await expect(readSearchStatus("/health/vault/a", new AbortController().signal)).rejects.toMatchObject({ status: 401 });
  });

  it("rejects malformed JSON rather than returning a successful empty status", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValue(new Response("<html>Sign in</html>", { status: 200 }));
    await expect(readSearchStatus("/health/vault/a", new AbortController().signal)).rejects.toThrow();
  });
});
