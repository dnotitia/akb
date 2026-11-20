import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useVaultTree } from "@/hooks/use-vault-tree";
import type { ArchiveScope, BrowseOptions } from "@/lib/api";
import { AutoLoadMore } from "@/components/ui/auto-load-more";
import { Button } from "@/components/ui/button";
import { InlineLoadingState } from "@/components/ui/loading-state";

type BrowseState = ReturnType<typeof useVaultTree>;
export interface CollectionBranchPage {
  items: BrowseState["items"];
  loading: boolean;
  loadingMore: boolean;
  refreshing: boolean;
  hasMore: boolean;
  complete: boolean;
  error: boolean;
  pageCount: number;
  pastCollectionCatalog: boolean;
  loadMore: () => void;
  retry: () => void;
}

/** Each disclosed branch owns bounded direct-child pages, independent of the
 * Vault-wide cursor. Restored offscreen branches wait for scroll demand;
 * closing a branch removes its query observer and pending request. */
export function CollectionBranchLoader({ vault, path, archiveScope, contentType, onPage, eager = false }: {
  vault: string;
  path: string;
  archiveScope: ArchiveScope;
  contentType: BrowseOptions["content_type"];
  onPage: (path: string, page: CollectionBranchPage | null) => void;
  eager?: boolean;
}) {
  const marker = useRef<HTMLSpanElement>(null);
  const [requested, setRequested] = useState(() => eager || typeof IntersectionObserver === "undefined");
  useEffect(() => {
    const element = marker.current;
    if (requested || !element) return;
    let root = element.parentElement;
    while (root && root !== document.body && root !== document.documentElement && !/(auto|scroll)/.test(getComputedStyle(root).overflowY)) root = root.parentElement;
    if (root === document.body || root === document.documentElement) root = null;
    const observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting) && !element.closest("[hidden], [inert]")) {
        setRequested(true);
        observer.disconnect();
      }
    }, { root, rootMargin: "200px 0px" });
    observer.observe(element);
    return () => observer.disconnect();
  }, [requested]);
  const query = useVaultTree(vault, archiveScope, { collection: path, depth: 0, content_type: contentType, enabled: requested });
  const latest = useRef(query);
  latest.current = query;
  const loadMore = useCallback(() => latest.current.loadMore(), []);
  const retry = useCallback(() => latest.current.retry(), []);
  const wrongScope = Boolean(query.items && query.path !== path);
  const invalid = query.showingPreviousScope || query.unsupported || query.paginationUnsupported || wrongScope;
  const page = useMemo<CollectionBranchPage>(() => ({
    items: invalid ? null : query.items,
    loading: query.loading,
    loadingMore: query.loadingMore,
    refreshing: query.refreshing,
    hasMore: !invalid && query.hasMore,
    complete: !invalid && query.complete,
    error: Boolean(query.error || invalid),
    pageCount: query.pageCount,
    pastCollectionCatalog: query.pastCollectionCatalog,
    loadMore, retry,
  }), [query.items, query.loading, query.loadingMore, query.refreshing, query.hasMore,
    query.complete, query.error, query.pageCount, query.pastCollectionCatalog, invalid, loadMore, retry]);
  useEffect(() => onPage(path, page), [path, page, onPage]);
  useEffect(() => () => onPage(path, null), [path, onPage]);
  return <span ref={marker} aria-hidden className="pointer-events-none -mb-px block h-px" />;
}

export function CollectionBranchBoundary({ path, page, paused, autoPaused, hasChildren, depth }: {
  path: string;
  page?: CollectionBranchPage;
  paused: boolean;
  autoPaused: boolean;
  hasChildren: boolean;
  depth: number;
}) {
  const name = path.split("/").at(-1) || path;
  return <div data-collection-boundary={path} style={{ paddingLeft: `${depth * 12 + 8}px` }} className="min-w-0 pr-2">
    {!page || page.loading ? <InlineLoadingState label={`Loading ${name}`} size="sm" className="py-2" />
      : page.error && !page.items ? <div role="status" className="py-2 text-xs text-foreground-muted">
        Could not load this collection.
        <Button variant="link" size="sm" className="ml-1" aria-label={`Retry ${name}`} onClick={page.retry} disabled={paused}>Retry</Button>
      </div> : <>
        {page.refreshing && <InlineLoadingState label={`Updating ${name}`} size="sm" className="py-2" />}
        {page.complete && !page.items?.length && !hasChildren && <p className="py-2 text-xs text-foreground-muted">This collection is empty.</p>}
        <AutoLoadMore hasMore={page.hasMore} loading={page.loadingMore} paused={paused || page.refreshing}
          autoPaused={autoPaused} error={page.error} requestKey={`${path}:${page.pageCount}`}
          onLoadMore={page.loadMore} label={`Load more in ${name}`} loadingLabel={`Loading more in ${name}`} />
      </>}
  </div>;
}
