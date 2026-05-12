import { useCallback, useEffect, useState } from "react";
import { listVaults } from "@/lib/api";

export interface VaultSummary {
  id: string;
  name: string;
  role?: string;
  is_pinned?: boolean;
}

/**
 * Fetches the current user's vault list. Exposes a stable `refetch` so
 * mutation sites (vault create/delete) can invalidate after success via
 * `VaultRefreshContext`. Errors silently fall back to an empty list —
 * the picker is decorative, not load-bearing for the rest of the shell.
 */
export function useVaults() {
  const [vaults, setVaults] = useState<VaultSummary[]>([]);
  const [loading, setLoading] = useState(true);

  const refetch = useCallback(() => {
    setLoading(true);
    listVaults()
      .then((d) => setVaults((d.vaults as VaultSummary[]) || []))
      .catch(() => setVaults([]))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    refetch();
  }, [refetch]);

  return { vaults, loading, refetch };
}
