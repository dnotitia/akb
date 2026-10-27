import { createContext, useCallback, useContext, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useQueries, useQueryClient } from "@tanstack/react-query";
import { getDocument } from "@/lib/api";
import { readWithAccessBoundary } from "@/lib/query-access-boundary";
import { readingDocumentKey, type ReadingDocument } from "@/lib/reading-workspace";
import { useAccessVerification, useCurrentUser } from "@/contexts/current-user-context";

interface ReadingWorkspaceState {
  tabs: ReadingDocument[];
  closed: ReadingDocument[];
  remember: (doc: ReadingDocument) => void;
  forget: (key: string) => void;
  close: (key: string, active: string) => void;
  positions: Map<string, number>;
}
const ReadingWorkspaceContext = createContext<ReadingWorkspaceState | null>(null);

/** In-memory, access-proof-scoped state. Never persists private titles/body to
 * browser storage. Labels are verified again after each new ACL proof without
 * remounting the active editor or throwing away another document's position. */
export function ReadingWorkspaceProvider({ children }: { children: ReactNode }) {
  const [tabs, setTabs] = useState<ReadingDocument[]>([]);
  const [closed, setClosed] = useState<ReadingDocument[]>([]);
  const positions = useRef(new Map<string, number>()).current;
  const navigate = useNavigate();
  const location = useLocation();
  const user = useCurrentUser();
  const { checking, revision } = useAccessVerification();
  useLayoutEffect(() => {
    setTabs([]);
    setClosed([]);
    positions.clear();
  }, [user?.user_id, positions]);
  useLayoutEffect(() => { setClosed([]); }, [revision]);
  const queryClient = useQueryClient();
  const verified = useQueries({ queries: tabs.map(tab => ({
    queryKey: ["document", tab.vault, tab.path, tab.version, user?.user_id, revision],
    queryFn: ({ queryKey, signal }) => readWithAccessBoundary(queryClient, queryKey,
      () => getDocument(tab.vault, tab.path, tab.version), signal),
    enabled: !checking, retry: false, staleTime: Infinity,
  })) });
  const visibleTabs = tabs.flatMap((tab, index) => {
    const snapshot = verified[index]?.data;
    return snapshot ? [{ ...tab, title: snapshot.title || snapshot.path }] : [];
  });
  const remember = useCallback((doc: ReadingDocument) => {
    const key = readingDocumentKey(doc);
    setTabs(previous => {
      const index = previous.findIndex(item => readingDocumentKey(item) === key);
      if (index < 0) return [...previous, doc];
      if (previous[index].title === doc.title && previous[index].href === doc.href) return previous;
      return previous.map((item, i) => i === index ? doc : item);
    });
    setClosed(previous => previous.some(item => readingDocumentKey(item) === key)
      ? previous.filter(item => readingDocumentKey(item) !== key) : previous);
  }, []);
  const forget = useCallback((key: string) => {
    setTabs(previous => previous.filter(item => readingDocumentKey(item) !== key));
    setClosed(previous => previous.filter(item => readingDocumentKey(item) !== key));
    positions.delete(key + ":rendered");
    positions.delete(key + ":raw");
  }, [positions]);
  const remove = useCallback((key: string) => {
    const doc = tabs.find(item => readingDocumentKey(item) === key);
    if (doc) setClosed(previous => [doc, ...previous.filter(item => readingDocumentKey(item) !== key)].slice(0, 10));
    setTabs(previous => previous.filter(item => readingDocumentKey(item) !== key));
  }, [tabs]);
  const close = useCallback((key: string, active: string) => {
    if (key !== active) { remove(key); return; }
    const index = visibleTabs.findIndex(item => readingDocumentKey(item) === key);
    const next = visibleTabs[index + 1] ?? visibleTabs[index - 1];
    const href = next?.href ?? `/vault/${encodeURIComponent(visibleTabs[index]?.vault ?? "")}`;
    // The existing dirty-editor guard can defer/cancel this navigation. Remove
    // the tab only when the confirmed router transition actually arrives.
    navigate(href, { state: { readingCloseTab: key } });
  }, [navigate, remove, visibleTabs]);
  useLayoutEffect(() => {
    const key = (location.state as { readingCloseTab?: string } | null)?.readingCloseTab;
    if (key && tabs.some(item => readingDocumentKey(item) === key)) remove(key);
  }, [location.key, location.state, remove, tabs]);
  const value = useMemo(() => ({ tabs: checking ? [] : visibleTabs, closed: checking ? [] : closed, remember, forget, close, positions }), [visibleTabs, closed, checking, remember, forget, close, positions]);
  return <ReadingWorkspaceContext.Provider value={value}>{children}</ReadingWorkspaceContext.Provider>;
}

// eslint-disable-next-line react-refresh/only-export-components
export function useReadingWorkspace() { return useContext(ReadingWorkspaceContext); }
