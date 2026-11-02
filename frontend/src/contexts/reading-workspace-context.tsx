import { createContext, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useQueries, useQueryClient } from "@tanstack/react-query";
import { ApiError, getDocument } from "@/lib/api";
import { readWithAccessBoundary } from "@/lib/query-access-boundary";
import { readingDocumentKey, type ReadingDocument } from "@/lib/reading-workspace";
import { emptyReadingSession, readReadingSession, writeReadingSession, READING_TAB_LIMIT, READING_CLOSED_LIMIT, type ReadingSession } from "@/lib/reading-session";
import { useAccessVerification, useCurrentUser } from "@/contexts/current-user-context";

interface ReadingWorkspaceState {
  tabs: ReadingDocument[];
  closed: ReadingDocument[];
  remember: (doc: ReadingDocument) => void;
  forget: (key: string) => void;
  close: (key: string, active: string) => void;
  reorder: (key: string, target: string) => void;
  positions: Map<string, number>;
  savePosition: (key: string, top: number) => void;
  activate: (key: string) => void;
  companion: ReadingDocument | null;
  openBeside: (doc: ReadingDocument) => void;
  closeBeside: () => void;
  sideRequest: number;
  restoring: boolean;
  restoreErrors: number;
  retryRestore: () => void;
}
const ReadingWorkspaceContext = createContext<ReadingWorkspaceState | null>(null);
function limitTabs(tabs: ReadingDocument[], active: string | null, companion: ReadingDocument | null) {
  const result = [...tabs];
  while (result.length > READING_TAB_LIMIT) {
    const index = result.findIndex(doc => readingDocumentKey(doc) !== active && (!companion || readingDocumentKey(doc) !== readingDocumentKey(companion)));
    result.splice(index < 0 ? 0 : index, 1);
  }
  return result;
}

/** Persist canonical identifiers and reading positions; titles and bodies always require a current access proof. */
export function ReadingWorkspaceProvider({ children }: { children: ReactNode }) {
  const user = useCurrentUser();
  const owner = user?.user_id ?? null;
  const { checking, revision } = useAccessVerification();
  const [owned, setOwned] = useState(() => ({ owner, session: readReadingSession(owner) }));
  const empty = useMemo(emptyReadingSession, []);
  const session = owned.owner === owner ? owned.session : empty;
  const latest = useRef(owned);
  useLayoutEffect(() => { latest.current = owned; }, [owned]);
  useLayoutEffect(() => {
    if (owned.owner !== owner) setOwned({ owner, session: readReadingSession(owner) });
  }, [owner, owned.owner]);
  const update = useCallback((change: (session: ReadingSession) => ReadingSession) => setOwned(previous => {
    const session = change(previous.owner === owner ? previous.session : readReadingSession(owner));
    return previous.owner === owner && session === previous.session ? previous : { owner, session };
  }), [owner]);
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  const entries = useMemo(() => [...new Map([...session.tabs, ...session.closed, ...(session.companion ? [session.companion] : [])].map(doc => [readingDocumentKey(doc), doc])).values()], [session.tabs, session.closed, session.companion]);
  const proof = `${owner}:${revision}`;
  const latestProof = useRef({ proof, checking });
  useLayoutEffect(() => { latestProof.current = { proof, checking }; }, [proof, checking]);
  const [queue, setQueue] = useState({ proof, finished: 0 });
  const finished = queue.proof === proof ? queue.finished : 0;
  const verified = useQueries({ queries: entries.map((tab, index) => ({
    queryKey: ["document", tab.vault, tab.path, tab.version, owner, revision],
    queryFn: ({ queryKey, signal }) => readWithAccessBoundary(queryClient, queryKey, () => getDocument(tab.vault, tab.path, tab.version), signal),
    enabled: !!owner && !checking && index < finished + 4, retry: false, staleTime: Infinity,
  })) });
  const completed = verified.filter(result => result.isFetched).length;
  let completedPrefix = 0;
  while (verified[completedPrefix]?.isFetched) completedPrefix += 1;
  useEffect(() => { setQueue(previous => previous.proof === proof && previous.finished === completedPrefix ? previous : { proof, finished: completedPrefix }); }, [completedPrefix, proof]);
  const snapshots = new Map(entries.map((doc, index) => [readingDocumentKey(doc), verified[index]]));
  const visible = (docs: ReadingDocument[]) => checking || !owner ? [] : docs.flatMap(doc => {
    const result = snapshots.get(readingDocumentKey(doc));
    const denied = result?.error instanceof ApiError && [401, 403, 404].includes(result.error.status);
    return result?.data && !denied ? [{ ...doc, title: result.data.title || result.data.path }] : [];
  });
  const visibleTabs = visible(session.tabs);
  const visibleClosed = visible(session.closed);
  const companion = session.companion ? visible([session.companion])[0] ?? null : null;
  const deniedKeys = entries.filter((_doc, index) => verified[index].error instanceof ApiError && [401, 403, 404].includes(verified[index].error.status)).map(readingDocumentKey).join("\n");
  useEffect(() => {
    if (!deniedKeys || checking || completed !== entries.length) return;
    const denied = new Set(deniedKeys.split("\n"));
    update(previous => ({ ...previous, tabs: previous.tabs.filter(doc => !denied.has(readingDocumentKey(doc))), closed: previous.closed.filter(doc => !denied.has(readingDocumentKey(doc))),
      companion: previous.companion && denied.has(readingDocumentKey(previous.companion)) ? null : previous.companion }));
  }, [deniedKeys, checking, completed, entries.length, update]);
  const flush = useCallback(() => {
    const current = latest.current;
    if (owner && current.owner === owner) writeReadingSession(owner, current.session);
  }, [owner]);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => { if (!checking) flush(); }, [owned, checking, flush]);
  useEffect(() => {
    const hide = () => { if (document.visibilityState === "hidden") flush(); };
    const clear = () => { const next = { owner, session: emptyReadingSession() }; latest.current = next; setOwned(next); };
    window.addEventListener("pagehide", flush); document.addEventListener("visibilitychange", hide);
    window.addEventListener("akb:clear-reading-session", clear);
    return () => { clearTimeout(timer.current); flush(); window.removeEventListener("pagehide", flush); document.removeEventListener("visibilitychange", hide); window.removeEventListener("akb:clear-reading-session", clear); };
  }, [flush, owner]);
  const savePosition = useCallback((key: string, top: number) => {
    if (latest.current.owner !== owner || !Number.isFinite(top)) return;
    latest.current.session.positions.delete(key);
    latest.current.session.positions.set(key, Math.max(0, top));
    while (latest.current.session.positions.size > 256) latest.current.session.positions.delete(latest.current.session.positions.keys().next().value!);
    clearTimeout(timer.current); timer.current = setTimeout(flush, 200);
  }, [owner, flush]);
  const remember = useCallback((doc: ReadingDocument) => update(previous => {
    const key = readingDocumentKey(doc);
    const found = previous.tabs.find(item => readingDocumentKey(item) === key);
    if (found?.title === doc.title && found.href === doc.href && !previous.closed.some(item => readingDocumentKey(item) === key)) return previous;
    const tabs = found ? previous.tabs.map(item => readingDocumentKey(item) === key ? doc : item) : [...previous.tabs, doc];
    return { ...previous, tabs: limitTabs(tabs, previous.active, previous.companion), closed: previous.closed.filter(item => readingDocumentKey(item) !== key) };
  }), [update]);
  const activate = useCallback((key: string) => update(previous => previous.active === key ? previous : { ...previous, active: key }), [update]);
  const forget = useCallback((key: string) => update(previous => {
    for (const mode of ["rendered", "raw", "side"]) previous.positions.delete(key + ":" + mode);
    return { ...previous, tabs: previous.tabs.filter(item => readingDocumentKey(item) !== key), closed: previous.closed.filter(item => readingDocumentKey(item) !== key),
      companion: previous.companion && readingDocumentKey(previous.companion) === key ? null : previous.companion };
  }), [update]);
  const remove = useCallback((key: string) => update(previous => {
    const doc = previous.tabs.find(item => readingDocumentKey(item) === key);
    if (!doc) return previous;
    return { ...previous, closed: [doc, ...previous.closed.filter(item => readingDocumentKey(item) !== key)].slice(0, READING_CLOSED_LIMIT), tabs: previous.tabs.filter(item => readingDocumentKey(item) !== key) };
  }), [update]);
  const close = useCallback((key: string, active: string) => {
    if (key !== active) { remove(key); return; }
    const index = visibleTabs.findIndex(item => readingDocumentKey(item) === key);
    const next = visibleTabs[index + 1] ?? visibleTabs[index - 1];
    navigate(next?.href ?? `/vault/${encodeURIComponent(visibleTabs[index]?.vault ?? "")}`, { state: { readingCloseTab: key } });
  }, [navigate, remove, visibleTabs]);
  const reorder = useCallback((key: string, target: string) => update(previous => {
    const from = previous.tabs.findIndex(tab => readingDocumentKey(tab) === key), to = previous.tabs.findIndex(tab => readingDocumentKey(tab) === target);
    if (from < 0 || to < 0 || from === to) return previous;
    const tabs = [...previous.tabs]; const [tab] = tabs.splice(from, 1); tabs.splice(to, 0, tab); return { ...previous, tabs };
  }), [update]);
  const [sideRequest, setSideRequest] = useState(0);
  const openBeside = useCallback((doc: ReadingDocument) => {
    update(previous => {
      const key = readingDocumentKey(doc);
      return { ...previous, companion: doc,
        tabs: previous.tabs.some(tab => readingDocumentKey(tab) === key) ? previous.tabs : limitTabs([...previous.tabs, doc], previous.active, doc),
        closed: previous.closed.filter(tab => readingDocumentKey(tab) !== key) };
    });
    setSideRequest(value => value + 1);
  }, [update]);
  const closeBeside = useCallback(() => update(previous => ({ ...previous, companion: null })), [update]);
  useLayoutEffect(() => {
    const key = (location.state as { readingCloseTab?: string } | null)?.readingCloseTab;
    if (key && session.tabs.some(item => readingDocumentKey(item) === key)) remove(key);
  }, [location.key, location.state, remove, session.tabs]);
  const retryRestore = useCallback(() => { void (async () => {
    if (verified.some(result => result.isPending)) return;
    const failed = verified.filter(result => result.isError);
    for (let index = 0; index < failed.length; index += 4) {
      if (latestProof.current.proof !== proof || latestProof.current.checking) return;
      await Promise.allSettled(failed.slice(index, index + 4).map(result => result.refetch()));
    }
  })(); }, [verified, proof]);
  const value = { tabs: visibleTabs, closed: visibleClosed, remember, forget, close, reorder, activate, positions: session.positions, savePosition,
    companion, openBeside, closeBeside, sideRequest, restoring: !!owner && !checking && verified.some(result => result.isPending),
    restoreErrors: verified.filter(result => result.isError && !(result.error instanceof ApiError && [401, 403, 404].includes(result.error.status))).length, retryRestore };
  return <ReadingWorkspaceContext.Provider value={value}>{children}</ReadingWorkspaceContext.Provider>;
}

// eslint-disable-next-line react-refresh/only-export-components
export function useReadingWorkspace() { return useContext(ReadingWorkspaceContext); }
