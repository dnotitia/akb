import { createContext, useContext, useEffect, useMemo, useSyncExternalStore, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { summarizeIndexing } from "@/lib/indexing-status";
import { SearchStatusStore } from "@/lib/search-status-store";

function useController(identity: string, enabled: boolean) {
  const client = useQueryClient();
  const store = useMemo(() => new SearchStatusStore(client, window.location.origin, identity, () => {
    window.dispatchEvent(new Event("akb:revalidate-access"));
  }), [client, identity]);
  const snapshot = useSyncExternalStore(store.subscribe, store.getSnapshot, store.getSnapshot);
  useEffect(() => {
    const sync = () => store.setActive(enabled && document.visibilityState !== "hidden" && navigator.onLine !== false);
    sync();
    const hide = () => { if (document.visibilityState === "hidden") store.setActive(false); };
    const offline = () => store.setActive(false);
    const online = () => window.dispatchEvent(new Event("akb:revalidate-access"));
    document.addEventListener("visibilitychange", hide);
    window.addEventListener("offline", offline); window.addEventListener("online", online);
    return () => { store.setActive(false); document.removeEventListener("visibilitychange", hide); window.removeEventListener("offline", offline); window.removeEventListener("online", online); };
  }, [enabled, store]);
  useEffect(() => () => store.dispose(), [store]);
  return { store, snapshot };
}

function useValue(identity: string, enabled: boolean) {
  const { store, snapshot } = useController(identity, enabled);
  // Foreground verification is a rendering gate, not merely a polling hint.
  const observations = useMemo(() => enabled && store.hasAccessProof() ? snapshot.observations : [], [enabled, snapshot, store]);
  const summary = summarizeIndexing(observations, { verified: enabled && snapshot.verified, loading: !enabled || snapshot.loading });
  return { observations, summary, verified: enabled && snapshot.verified, loading: !enabled || snapshot.loading, refresh: store.refresh };
}
type SearchStatusContextValue = ReturnType<typeof useValue>;
const Context = createContext<SearchStatusContextValue | null>(null);
export function SearchStatusProvider({ identity, enabled, children }: { identity: string; enabled: boolean; children: ReactNode }) {
  const value = useValue(identity, enabled);
  return <Context.Provider value={value}>{children}</Context.Provider>;
}
// The context and its hooks intentionally share this module; HMR may reload it.
// eslint-disable-next-line react-refresh/only-export-components
export function useOptionalSearchStatus() { return useContext(Context); }
// eslint-disable-next-line react-refresh/only-export-components
export function useSearchStatus() {
  const value = useOptionalSearchStatus();
  if (!value) throw new Error("SearchStatusProvider is required");
  return value;
}
