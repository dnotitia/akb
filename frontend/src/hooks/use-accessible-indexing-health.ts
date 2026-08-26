import { useEffect, useState } from "react";
import { authenticatedFetch, listVaults } from "@/lib/api";

interface PipelineStats {
  pending?: number;
  abandoned?: number;
  indexed?: number;
}

interface VaultHealthResponse {
  metadata_backfill?: PipelineStats;
  vector_store?: {
    backfill?: {
      upsert?: PipelineStats;
    };
  };
}

export interface AccessibleIndexingStatus {
  vaultCount: number;
  checkedVaultCount: number;
  pending: number;
  abandoned: number;
  indexed: number;
  incomplete: boolean;
}

const HEALTH_CONCURRENCY = 5;
const ACTIVE_INTERVAL = 15_000;
const IDLE_INTERVAL = 60_000;

function count(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value)
    ? Math.max(0, value)
    : 0;
}

async function fetchVaultHealth(name: string): Promise<VaultHealthResponse> {
  const response = await authenticatedFetch(
    `/health/vault/${encodeURIComponent(name)}`,
  );
  if (!response.ok) {
    throw new Error(`Vault health request failed: ${response.status}`);
  }
  return (await response.json()) as VaultHealthResponse;
}

/**
 * Builds a user-scoped indexing snapshot from the reader-gated Vault health
 * surface. The generic `/health` endpoint is intentionally not used: its
 * counters are system-wide and would misrepresent another tenant's work as the
 * current user's status.
 *
 * Requests are capped in small batches because one account can access many
 * Vaults. A partial response remains useful, but is explicitly marked
 * `incomplete` so consumers render lower-bound counts rather than exact totals.
 */
export async function loadAccessibleIndexingHealth(): Promise<AccessibleIndexingStatus> {
  const response = await listVaults();
  const names = Array.from(
    new Set(
      (response.vaults || [])
        .map((vault) => (typeof vault?.name === "string" ? vault.name : ""))
        .filter(Boolean),
    ),
  );

  const status: AccessibleIndexingStatus = {
    vaultCount: names.length,
    checkedVaultCount: 0,
    pending: 0,
    abandoned: 0,
    indexed: 0,
    incomplete: false,
  };

  for (let index = 0; index < names.length; index += HEALTH_CONCURRENCY) {
    const batch = names.slice(index, index + HEALTH_CONCURRENCY);
    const results = await Promise.allSettled(batch.map(fetchVaultHealth));
    results.forEach((result) => {
      if (result.status !== "fulfilled") return;
      const upsert = result.value.vector_store?.backfill?.upsert;
      const metadata = result.value.metadata_backfill;
      status.checkedVaultCount += 1;
      status.pending += count(upsert?.pending) + count(metadata?.pending);
      status.abandoned += count(upsert?.abandoned) + count(metadata?.abandoned);
      status.indexed += count(upsert?.indexed);
    });
  }

  status.incomplete = status.checkedVaultCount !== status.vaultCount;
  return status;
}

export function useAccessibleIndexingHealth(
  enabled: boolean,
  userId: string | undefined,
) {
  const [data, setData] = useState<AccessibleIndexingStatus | null>(null);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    if (!enabled || !userId) {
      setData(null);
      setError(null);
      return;
    }

    let cancelled = false;
    let timer: number | undefined;
    setData(null);
    setError(null);

    const schedule = (delay: number) => {
      if (!cancelled) timer = window.setTimeout(tick, delay);
    };

    const tick = async () => {
      try {
        const next = await loadAccessibleIndexingHealth();
        if (cancelled) return;
        setData(next);
        setError(null);
        schedule(
          next.pending > 0 || next.abandoned > 0 || next.incomplete
            ? ACTIVE_INTERVAL
            : IDLE_INTERVAL,
        );
      } catch (caught) {
        if (cancelled) return;
        setError(
          caught instanceof Error
            ? caught
            : new Error("Index status unavailable"),
        );
        schedule(ACTIVE_INTERVAL);
      }
    };

    void tick();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [enabled, userId]);

  return { data, error };
}
