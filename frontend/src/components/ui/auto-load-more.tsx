import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { InlineLoadingState } from "@/components/ui/loading-state";
import { cn } from "@/lib/utils";

/** Append only when the end approaches its own scroll viewport. The quiet
 * keyboard action is also a fallback for assistive readers without scrolling. */
export function AutoLoadMore({ hasMore, loading = false, paused = false, autoPaused = false, error = false, requestKey, onLoadMore,
  label = "Load more items", loadingLabel = "Loading more items", className,
}: {
  hasMore: boolean;
  loading?: boolean;
  paused?: boolean;
  /** Stop scroll demand while keeping the explicit keyboard action available. */
  autoPaused?: boolean;
  error?: boolean;
  requestKey: string | number;
  onLoadMore: () => void;
  label?: string;
  loadingLabel?: string;
  className?: string;
}) {
  const sentinel = useRef<HTMLDivElement>(null);
  const requested = useRef<string | number | null>(null);
  const latest = useRef({ hasMore, loading, paused, autoPaused, error, requestKey, onLoadMore });
  latest.current = { hasMore, loading, paused, autoPaused, error, requestKey, onLoadMore };
  const [showLoading, setShowLoading] = useState(false);
  const supported = typeof IntersectionObserver !== "undefined";

  const load = useCallback((retry = false) => {
    const state = latest.current;
    const element = sentinel.current;
    if (!state.hasMore || state.loading || state.paused || (!retry && (state.error || state.autoPaused))
      || !element?.isConnected || element.closest("[hidden], [inert]")) return;
    if (!retry && requested.current === state.requestKey) return;
    requested.current = state.requestKey;
    state.onLoadMore();
  }, []);

  useEffect(() => {
    if (!loading) return;
    const timer = setTimeout(() => setShowLoading(true), 150);
    return () => { clearTimeout(timer); setShowLoading(false); };
  }, [loading]);

  // A cancelled fetch may retain the same cached page. Release the latch on
  // settlement/scope changes so returning to it can request that page again.
  // Error gating below still requires an explicit retry after a failure.
  useEffect(() => {
    if (!loading) requested.current = null;
  }, [loading, paused, autoPaused, requestKey]);

  useEffect(() => {
    const element = sentinel.current;
    if (!element || !hasMore || loading || paused || autoPaused || error || !supported) return;
    let root = element.parentElement;
    while (root && root !== document.body && root !== document.documentElement && !/(auto|scroll)/.test(getComputedStyle(root).overflowY)) root = root.parentElement;
    if (root === document.body || root === document.documentElement) root = null;
    const observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) load();
    }, { root, rootMargin: "0px 0px 240px 0px" });
    observer.observe(element);
    return () => observer.disconnect();
  }, [hasMore, loading, paused, autoPaused, error, requestKey, supported, load]);

  if (!hasMore) return null;
  return <div ref={sentinel} data-slot="auto-load-more" className={cn("min-h-1", className)}>
    {loading && showLoading && loadingLabel && <InlineLoadingState label={loadingLabel} size="sm" className="py-2" />}
    <Button type="button" variant="ghost" size="sm" disabled={loading || paused}
      onClick={() => load(true)} className={cn("max-sm:min-h-11 pointer-coarse:min-h-11", supported && !error && "sr-only focus:not-sr-only")}>
      {error ? `Retry: ${label.toLocaleLowerCase()}` : label}
    </Button>
  </div>;
}
