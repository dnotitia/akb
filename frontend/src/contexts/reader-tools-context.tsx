import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useLocation } from "react-router-dom";

import { READER_PREFERENCES_KEY as storageKey, readReaderPreferences, type ReaderPreferences } from "@/lib/reader-preferences";

interface ReaderTools {
  preferences: ReaderPreferences;
  setPreferences: (value: ReaderPreferences) => void;
  findOpen: boolean; openFind: (opener?: HTMLElement) => void; closeFind: () => void;
  findRequest: number;
  settingsOpen: boolean; setSettingsOpen: (open: boolean) => void;
  query: string; setQuery: (query: string) => void;
  current: number; move: (direction: number) => void;
  result: { count: number; limited: boolean };
  report: (result: { count: number; limited: boolean }) => void;
}
const ReaderToolsContext = createContext<ReaderTools | null>(null);

export function ReaderToolsProvider({ children }: { children: ReactNode }) {
  const location = useLocation();
  const identity = location.pathname + (new URLSearchParams(location.search).get("commit") || "");
  const [preferences, updatePreferences] = useState(readReaderPreferences);
  const setPreferences = useCallback((next: ReaderPreferences) => {
    updatePreferences(next);
    try { localStorage.setItem(storageKey, JSON.stringify(next)); } catch { /* Reading remains available without storage. */ }
  }, []);
  const [search, setSearch] = useState({ identity, open: false, query: "", current: 0, count: 0, limited: false });
  const [settings, setSettings] = useState({ identity, open: false });
  const setSettingsOpen = useCallback((open: boolean) => setSettings({ identity, open }), [identity]);
  const openerRef = useRef<HTMLElement | null>(null);
  const [findRequest, setFindRequest] = useState(0);
  const scoped = search.identity === identity ? search : { identity, open: false, query: "", current: 0, count: 0, limited: false };
  const openFind = useCallback((opener?: HTMLElement) => {
    if (opener || !(document.activeElement instanceof HTMLElement) || !document.activeElement.closest(".reader-find-bar")) {
      openerRef.current = opener ?? (document.activeElement instanceof HTMLElement ? document.activeElement : null);
    }
    setFindRequest(value => value + 1);
    setSearch(previous => ({ ...(previous.identity === identity ? previous : { identity, query: "", current: 0, count: 0, limited: false }), open: true }));
  }, [identity]);
  const closeFind = useCallback(() => { setSearch(previous => ({ ...previous, open: false })); if (openerRef.current?.isConnected) openerRef.current.focus({ preventScroll: true }); }, []);
  const setQuery = useCallback((query: string) => setSearch(previous => ({ ...previous, identity, query, current: 0, count: 0, limited: false })), [identity]);
  const move = useCallback((direction: number) => setSearch(previous => ({ ...previous, current: previous.count ? (previous.current + direction + previous.count) % previous.count : 0 })), []);
  const report = useCallback((result: { count: number; limited: boolean }) => setSearch(previous => {
    if (previous.identity !== identity || (previous.count === result.count && previous.limited === result.limited)) return previous;
    return { ...previous, ...result, current: result.count ? Math.min(previous.current, result.count - 1) : 0 };
  }), [identity]);
  useEffect(() => {
    const sync = (event: StorageEvent) => { if (event.key === storageKey || event.key === null) updatePreferences(readReaderPreferences()); };
    window.addEventListener("storage", sync); return () => window.removeEventListener("storage", sync);
  }, []);
  const { open: findOpen, query, current: currentMatch, count, limited } = scoped;
  const value = useMemo(() => ({ preferences, setPreferences, findOpen, findRequest, query, current: currentMatch,
    settingsOpen: settings.identity === identity && settings.open, setSettingsOpen,
    result: { count, limited }, openFind, closeFind, setQuery, move, report }),
  [preferences, setPreferences, findOpen, findRequest, query, currentMatch, count, limited, settings, identity, setSettingsOpen, openFind, closeFind, setQuery, move, report]);
  return <ReaderToolsContext.Provider value={value}>{children}</ReaderToolsContext.Provider>;
}

// eslint-disable-next-line react-refresh/only-export-components
export function useReaderTools() { return useContext(ReaderToolsContext); }
