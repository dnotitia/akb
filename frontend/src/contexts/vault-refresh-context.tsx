import { createContext, ReactNode, useContext } from "react";

/**
 * Refresh handles for the vault list (left column) and the active vault's
 * tree (middle column). Mutation sites — collection create/delete, doc
 * delete/edit, file upload/delete, vault delete — call the appropriate
 * `refetch*` after success to invalidate the cached fetch.
 *
 * Default values are no-ops so consumers rendered outside the provider
 * (e.g. the `/auth` route, which doesn't mount `VaultShell`) don't crash
 * — they just get silent no-op calls.
 */
interface VaultRefreshContextValue {
  refetchVaults: () => void;
  refetchTree: () => void;
}

const noop = () => {};

const VaultRefreshContext = createContext<VaultRefreshContextValue>({
  refetchVaults: noop,
  refetchTree: noop,
});

export function VaultRefreshProvider({
  children,
  refetchVaults,
  refetchTree,
}: VaultRefreshContextValue & { children: ReactNode }) {
  return (
    <VaultRefreshContext.Provider value={{ refetchVaults, refetchTree }}>
      {children}
    </VaultRefreshContext.Provider>
  );
}

export const useVaultRefresh = () => useContext(VaultRefreshContext);
