import type { QueryClient } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/api";
import { normalizeIndexingResponse, summarizeIndexing, type VaultSearchObservation } from "./indexing-status";

export const SEARCH_STATUS_PREFIX = "workspace-search-status";
export const STATUS_FRESH_MS = 120_000;
const REQUEST_MS = 8_000;
const SWEEP_MS = 60_000;
type Vault = { id: string; name: string };
export interface SearchStatusSnapshot {
  observations: VaultSearchObservation[];
  verified: boolean;
  loading: boolean;
}
const empty = (): SearchStatusSnapshot => ({ observations: [], verified: false, loading: true });

export class StatusRequestError extends Error {
  constructor(public status: number, public retryAt = 0) { super(`Status request failed (${status})`); }
}

/** The authenticated carrier deliberately keeps the existing session on a failed
 * observation. Layout, not a background health read, re-verifies the identity. */
export async function readSearchStatus(url: string, signal: AbortSignal): Promise<unknown> {
  let response: Response;
  try { response = await authenticatedFetch(url, { signal }, { unauthorized: "preserve-session" }); }
  catch (error) {
    if (error instanceof Error && error.message === "Unauthorized") throw new StatusRequestError(401);
    throw error;
  }
  if (!response.ok) {
    const retry = response.headers.get("Retry-After");
    const seconds = retry && /^\d+$/.test(retry) ? Number(retry) : undefined;
    const parsed = retry ? Date.parse(retry) : NaN;
    throw new StatusRequestError(response.status, seconds !== undefined ? Date.now() + seconds * 1000 : Number.isFinite(parsed) ? parsed : 0);
  }
  return response.json();
}

function parseVaults(body: unknown): Vault[] {
  if (!body || typeof body !== "object" || !Array.isArray((body as { vaults?: unknown }).vaults)) throw new Error("Invalid vault scope");
  const seen = new Set<string>();
  return (body as { vaults: unknown[] }).vaults.map(value => {
    if (!value || typeof value !== "object") throw new Error("Invalid vault scope");
    const { id, name } = value as Partial<Vault>;
    if (typeof id !== "string" || !id || typeof name !== "string" || !name) throw new Error("Invalid vault scope");
    return { id, name };
  }).filter(vault => !seen.has(vault.id) && !!seen.add(vault.id));
}

const unavailable = (vault: Vault): VaultSearchObservation => ({
  vaultId: vault.id, vaultName: vault.name, observation: "checking", coverage: "partial",
  stages: [], core: "unknown", auxiliary: "unknown", historicalFailureReported: false,
});

/** One session-owned sweep serves the header, Home, Overview and Settings.
 * Cache keys include origin + identity + generation; no status is persisted. */
export class SearchStatusStore {
  private snapshot = empty();
  private accessProven = false;
  private listeners = new Set<() => void>();
  private generation = 0;
  private active = false;
  private running = false;
  private timer?: ReturnType<typeof setTimeout>;
  private expiry?: ReturnType<typeof setTimeout>;
  private sweep?: AbortController;
  private cursor = 0;
  private priority?: string;
  private unsupported = new Set<string>();
  private retryAt = new Map<string, number>();
  private failures = new Map<string, number>();
  private accessRechecked = new Set<string>();
  private denied = new Set<string>();
  private lastManual = -Infinity;
  private unsubscribeCache?: () => void;
  private removing = false;
  private marker?: readonly unknown[];
  constructor(
    private client: QueryClient,
    private origin: string,
    private identity: string,
    private unauthorized: () => void,
    private read = readSearchStatus,
  ) {}
  private connect() {
    if (this.unsubscribeCache) return;
    this.unsubscribeCache = this.client.getQueryCache().subscribe(event => {
      if (event.type === "removed" && JSON.stringify(event.query.queryKey) === JSON.stringify(this.marker) && !this.removing) {
        this.reset();
        if (this.active) this.schedule(0);
      }
    });
  }
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  getSnapshot = () => this.snapshot;
  hasAccessProof = () => this.accessProven;
  private key(id: string) { return [SEARCH_STATUS_PREFIX, this.origin, this.identity, this.generation, id] as const; }
  private publish(snapshot: SearchStatusSnapshot) {
    this.snapshot = snapshot;
    this.listeners.forEach(listener => listener());
    clearTimeout(this.expiry);
    const expiry = Math.min(...snapshot.observations.filter(row => row.observation === "fresh" && row.receivedAt !== undefined).map(row => row.receivedAt! + STATUS_FRESH_MS));
    if (Number.isFinite(expiry)) this.expiry = setTimeout(() => this.publish({ ...this.snapshot, observations: this.expireRows() }), Math.max(1, expiry - Date.now()));
  }
  private purge() {
    this.removing = true;
    this.client.removeQueries({ queryKey: [SEARCH_STATUS_PREFIX, this.origin, this.identity] });
    this.removing = false;
  }
  private reset() {
    this.accessProven = false;
    this.sweep?.abort();
    clearTimeout(this.timer);
    this.generation++;
    this.running = false;
    this.unsupported.clear(); this.retryAt.clear();
    this.failures.clear();
    this.accessRechecked.clear();
    this.denied.clear();
    this.marker = undefined;
    this.purge();
    this.publish(empty());
  }
  setActive(active: boolean) {
    this.connect();
    if (this.active === active) return;
    this.active = active;
    if (!active) {
      this.accessProven = false;
      this.sweep?.abort(); clearTimeout(this.timer); this.generation++; this.running = false;
      this.publish({ ...this.snapshot, verified: false, loading: false,
        observations: this.snapshot.observations.map(row => ({ ...row, observation: row.receivedAt !== undefined ? "stale" : "unavailable" })) });
      this.purge(); this.marker = undefined;
    } else this.schedule(0);
  }
  setPriority(id?: string) { this.priority = id; }
  refresh = () => {
    if (!this.active) return;
    if (this.running || Date.now() - this.lastManual < 1_000) return;
    this.lastManual = Date.now();
    this.unsupported.clear();
    this.schedule(0);
  };
  dispose() { this.active = false; this.unsubscribeCache?.(); this.unsubscribeCache = undefined; this.reset(); clearTimeout(this.expiry); }
  private schedule(delay: number) {
    clearTimeout(this.timer);
    if (this.active) this.timer = setTimeout(() => void this.run(), delay);
  }
  private async request(url: string, parent: AbortSignal): Promise<unknown> {
    const controller = new AbortController();
    const abort = () => controller.abort();
    parent.addEventListener("abort", abort, { once: true });
    if (parent.aborted) controller.abort();
    const timeout = setTimeout(abort, REQUEST_MS);
    let rejectAborted: (() => void) | undefined;
    const aborted = new Promise<never>((_, reject) => {
      rejectAborted = () => reject(new DOMException("Status request cancelled", "AbortError"));
      controller.signal.addEventListener("abort", rejectAborted, { once: true });
      if (controller.signal.aborted) rejectAborted();
    });
    try { return await Promise.race([this.read(url, controller.signal), aborted]); }
    finally { clearTimeout(timeout); parent.removeEventListener("abort", abort); if (rejectAborted) controller.signal.removeEventListener("abort", rejectAborted); }
  }
  private expireRows() {
    return this.snapshot.observations.map(row => row.receivedAt !== undefined && Date.now() - row.receivedAt >= STATUS_FRESH_MS
      ? { ...row, observation: "stale" as const } : row);
  }
  private async run() {
    if (!this.active || this.running) return;
    const generation = this.generation;
    const current = () => this.active && this.generation === generation;
    const controller = new AbortController();
    this.sweep = controller; this.running = true;
    const deadline = setTimeout(() => controller.abort(), SWEEP_MS);
    if (!this.marker) {
      this.marker = this.key("scope");
      this.client.setQueryDefaults([SEARCH_STATUS_PREFIX], { gcTime: Infinity });
      this.client.setQueryData(this.marker, true);
    }
    this.publish({ ...this.snapshot, loading: true, observations: this.expireRows() });
    let scopeFailed = false;
    let recheckScope = false;
    const denied = this.denied;
    const checked = new Set<string>();
    try {
      const scopeRetryAt = this.retryAt.get("scope") ?? 0;
      if (scopeRetryAt > Date.now()) throw new StatusRequestError(429, scopeRetryAt);
      const vaults = parseVaults(await this.request("/api/v1/my/vaults", controller.signal));
      if (!current() || controller.signal.aborted) return;
      this.accessProven = true;
      this.failures.delete("scope"); this.retryAt.delete("scope");
      const previous = new Map(this.snapshot.observations.map(row => [row.vaultId, row]));
      const ids = new Set(vaults.map(vault => vault.id));
      for (const id of denied) if (!ids.has(id)) denied.delete(id);
      for (const row of this.snapshot.observations) {
        if (!ids.has(row.vaultId)) this.client.removeQueries({ queryKey: this.key(row.vaultId), exact: true });
      }
      this.publish({ verified: !vaults.some(vault => denied.has(vault.id)), loading: true, observations: vaults.filter(vault => !denied.has(vault.id)).map(vault => ({ ...(previous.get(vault.id) ?? unavailable(vault)), vaultName: vault.name })) });
      const ordered = [...vaults.slice(this.cursor % (vaults.length || 1)), ...vaults.slice(0, this.cursor % (vaults.length || 1))];
      const priority = ordered.findIndex(vault => vault.id === this.priority);
      if (priority > 0) ordered.unshift(...ordered.splice(priority, 1));
      let next = 0;
      const worker = async () => {
        while (current() && !controller.signal.aborted && next < ordered.length) {
          const vault = ordered[next++];
          this.cursor = (vaults.findIndex(item => item.id === vault.id) + 1) % (vaults.length || 1);
          if (this.unsupported.has(vault.id) || (this.retryAt.get(vault.id) ?? 0) > Date.now()) continue;
          try {
            const body = await this.request(`/health/vault/${encodeURIComponent(vault.name)}`, controller.signal);
            if (!current() || controller.signal.aborted) return;
            const row = normalizeIndexingResponse(body, vault, Date.now());
            checked.add(vault.id); this.failures.delete(vault.id); this.retryAt.delete(vault.id); this.accessRechecked.delete(vault.id);
            denied.delete(vault.id);
            this.client.setQueryData(this.key(vault.id), row);
            this.publish({ ...this.snapshot, verified: !vaults.some(vault => denied.has(vault.id)), observations: this.snapshot.observations.some(item => item.vaultId === vault.id)
              ? this.snapshot.observations.map(item => item.vaultId === vault.id ? row : item) : [...this.snapshot.observations, row] });
          } catch (error) {
            if (!current()) return;
            if (error instanceof StatusRequestError && error.status === 401) {
              this.setActive(false); this.reset(); this.unauthorized(); return;
            }
            if (error instanceof StatusRequestError && error.status === 403) {
              denied.add(vault.id);
              if (!this.accessRechecked.has(vault.id)) { this.accessRechecked.add(vault.id); recheckScope = true; }
              this.client.removeQueries({ queryKey: this.key(vault.id), exact: true });
              this.publish({ ...this.snapshot, verified: false, observations: this.snapshot.observations.filter(row => row.vaultId !== vault.id) });
              continue;
            }
            const unsupported = error instanceof StatusRequestError && [405, 501].includes(error.status);
            if (error instanceof StatusRequestError && error.status === 404 && !this.accessRechecked.has(vault.id)) { this.accessRechecked.add(vault.id); recheckScope = true; }
            if (unsupported) this.unsupported.add(vault.id);
            if (error instanceof StatusRequestError && error.status === 429) this.retryAt.set(vault.id, error.retryAt || Date.now() + 60_000);
            else if (!unsupported) this.backoff(vault.id);
            this.publish({ ...this.snapshot, observations: this.snapshot.observations.map(row => row.vaultId !== vault.id ? row : {
              ...row, observation: row.receivedAt !== undefined ? "stale" : "unavailable", attemptedAt: Date.now(),
              coverage: unsupported ? "unsupported" : row.coverage,
              error: unsupported ? "This server does not support search status." : "Could not verify the latest status.",
            }) });
          }
        }
      };
      await Promise.all(Array.from({ length: Math.min(5, ordered.length) }, worker));
      // A 404 can mean a rename/deletion, not an unsupported server. A 403 is
      // stronger than a cached directory grant; never reinstate its private row.
      if (recheckScope && current() && !controller.signal.aborted) {
        const refreshed = parseVaults(await this.request("/api/v1/my/vaults", controller.signal));
        if (!current() || controller.signal.aborted) return;
        const ids = new Set(refreshed.map(vault => vault.id));
        for (const id of denied) if (!ids.has(id)) denied.delete(id);
        const previous = new Map(this.snapshot.observations.map(row => [row.vaultId, row]));
        for (const row of this.snapshot.observations) if (!ids.has(row.vaultId)) this.client.removeQueries({ queryKey: this.key(row.vaultId), exact: true });
        this.publish({ verified: !refreshed.some(vault => denied.has(vault.id)), loading: true,
          observations: refreshed.filter(vault => !denied.has(vault.id)).map(vault => ({ ...(previous.get(vault.id) ?? { ...unavailable(vault), observation: "unavailable" as const }), vaultName: vault.name })) });
      }
    } catch (error) {
      if (!current()) return;
      scopeFailed = true;
      this.accessProven = false;
      if (error instanceof StatusRequestError && error.status === 401) {
        this.setActive(false); this.reset(); this.unauthorized(); return;
      }
      if (error instanceof StatusRequestError && error.status === 429) this.retryAt.set("scope", error.retryAt || Date.now() + 60_000);
      else this.backoff("scope");
      // A failed access proof never leaves cached private Vault rows on screen.
      this.purge(); this.marker = undefined;
      this.publish({ observations: [], verified: false, loading: false });
    } finally {
      clearTimeout(deadline);
      if (current()) {
        this.running = false;
        this.publish({ ...this.snapshot, loading: false, observations: this.expireRows().map(row => controller.signal.aborted && !checked.has(row.vaultId) ? { ...row, observation: row.receivedAt !== undefined ? "stale" : "unavailable", error: "Status check did not finish." } : row) });
        const summary = summarizeIndexing(this.snapshot.observations, { verified: this.snapshot.verified, loading: false });
        this.schedule(scopeFailed || !this.snapshot.verified || summary.affectedIds.length || summary.unknownIds.length ? 15_000 : 60_000);
      }
    }
  }
  private backoff(id: string) {
    const failures = (this.failures.get(id) ?? 0) + 1;
    this.failures.set(id, failures);
    this.retryAt.set(id, Date.now() + Math.min(120_000, 15_000 * 2 ** (failures - 1)));
  }
}
