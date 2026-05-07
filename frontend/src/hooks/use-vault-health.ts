import { useEffect, useState } from "react";
import { getToken } from "@/lib/api";
import type { HealthSnapshot } from "./use-health";

const ENDPOINT_BASE = "/health/vault";
const DEFAULT_INTERVAL = 15000;

interface VaultHealthSnapshot extends HealthSnapshot {
  vault?: string;
}

/**
 * Polls /health/vault/{name} every 15s. Authenticated — bails out if no
 * token. Returns null until the first response resolves; subsequent
 * failures keep the last good snapshot rather than flickering through
 * null. Cleans up on unmount or vaultName change.
 *
 * Endpoint is off-prefix (sibling of /health) — do not prepend /api/v1.
 */
export function useVaultHealth(
  vaultName: string | undefined,
  intervalMs: number = DEFAULT_INTERVAL,
): VaultHealthSnapshot | null {
  const [data, setData] = useState<VaultHealthSnapshot | null>(null);

  useEffect(() => {
    if (!vaultName || !getToken()) {
      setData(null);
      return;
    }
    let cancelled = false;
    const tick = async () => {
      const token = getToken();
      if (!token) return;
      try {
        const r = await fetch(
          `${ENDPOINT_BASE}/${encodeURIComponent(vaultName)}`,
          { headers: { Authorization: `Bearer ${token}` } },
        );
        if (!r.ok) return;
        const json = (await r.json()) as VaultHealthSnapshot;
        if (!cancelled) setData(json);
      } catch {
        /* silent — IndexingBadge falls back to placeholder */
      }
    };
    tick();
    const id = setInterval(tick, intervalMs);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [vaultName, intervalMs]);

  return data;
}
