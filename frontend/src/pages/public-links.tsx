import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Boxes, RefreshCw, Search, Share2 } from "lucide-react";
import { useAccessVerification, useCurrentUser } from "@/contexts/current-user-context";
import { useVaults } from "@/hooks/use-vaults";
import { useDebounce } from "@/hooks/use-debounce";
import { ApiError, deletePublication, listPublicationDirectory, type DirectoryPublication, type PublicLinkExpiry, type PublicLinkStatus } from "@/lib/api";
import { PublicationPolicySummary, PublicationRow, PublicationSkeleton } from "@/pages/publications";
import { Alert } from "@/components/ui/alert";
import { AutoLoadMore } from "@/components/ui/auto-load-more";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Input } from "@/components/ui/input";
import { WorkspacePageHeader } from "@/components/ui/workspace-page-header";
import { InlineLoadingState } from "@/components/ui/loading-state";
import { Panel } from "@/components/ui/panel";
import { SelectMenu } from "@/components/ui/select-menu";

export default function PublicLinksPage() {
  const user = useCurrentUser();
  const { checking, revision } = useAccessVerification();
  return <PublicLinksWorkspace key={JSON.stringify([user?.user_id, revision, checking])} />;
}

function PublicLinksWorkspace() {
  const user = useCurrentUser();
  const { checking, revision } = useAccessVerification();
  const [params, setParams] = useSearchParams();
  const vault = params.get("vault") || "";
  const text = params.get("q") || "";
  const q = useDebounce(text.trim());
  const status: PublicLinkStatus = params.get("status") === "all" ? "all" : params.get("status") === "unavailable" ? "unavailable" : "active";
  const expiry: PublicLinkExpiry = (["none", "soon", "long-running"] as const).find(value => value === params.get("expiry")) ?? "any";
  const vaultDirectory = useVaults();
  const client = useQueryClient();
  const searchRef = useRef<HTMLInputElement>(null);
  const alive = useRef(true);
  const [copied, setCopied] = useState<string | null>(null);
  const [copyError, setCopyError] = useState("");
  const [pending, setPending] = useState<DirectoryPublication | null>(null);
  const [revoking, setRevoking] = useState<string | null>(null);
  const feed = useInfiniteQuery({
    queryKey: ["publication-directory", user?.user_id, revision, vault, status, expiry, q],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam, signal }) => listPublicationDirectory({ vault: vault || undefined, q, status, expiry, page_size: 30, cursor: pageParam, signal }),
    getNextPageParam: last => last.next_cursor ?? undefined,
    enabled: !!user && !checking,
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: 0,
    placeholderData: (previous, previousQuery) => previousQuery?.queryKey.slice(0, 4).every((value, i) => value === ["publication-directory", user?.user_id, revision, vault][i]) ? previous : undefined,
  });
  const denied = feed.error instanceof ApiError && [401, 403].includes(feed.error.status);
  const pagingNeedsRefresh = feed.isFetchNextPageError && feed.error instanceof ApiError && feed.error.status === 400;
  const data = checking || denied ? undefined : feed.data;
  const rows = Array.from(new Map(data?.pages.flatMap(page => page.publications).map(item => [item.slug, item]) ?? []).values());
  const total = data?.pages[0]?.total;
  const updating = text.trim() !== q || feed.isPlaceholderData || (feed.isFetching && !feed.isFetchingNextPage);
  const hasFilters = Boolean(vault || text || status !== "active" || expiry !== "any");

  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => {
    const previous = document.title;
    document.title = "Publications · AKB";
    return () => { document.title = previous; };
  }, []);
  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(null), 2000);
    return () => clearTimeout(timer);
  }, [copied]);

  function change(key: string, value: string, replace = false) {
    const next = new URLSearchParams(params);
    if (!value || (key === "status" && value === "active") || (key === "expiry" && value === "any")) next.delete(key);
    else next.set(key, value);
    setParams(next, { replace });
  }
  function scopeHref(name: string) {
    const next = new URLSearchParams(params);
    next.set("vault", name);
    return `/publications?${next}`;
  }
  async function copy(item: DirectoryPublication) {
    if (checking || !alive.current) return;
    setCopyError("");
    try {
      await navigator.clipboard.writeText(item.share_url);
      if (alive.current) setCopied(item.slug);
    } catch {
      if (alive.current) setCopyError("Could not copy this public link. Try again.");
    }
  }
  async function unpublish() {
    const current = rows.find(item => item.slug === pending?.slug);
    if (!current?.can_unpublish || updating || checking) throw new Error("Refresh the list to verify this link's management permissions.");
    setRevoking(current.slug);
    try {
      await deletePublication(current.vault, current.slug);
      if (alive.current) await client.invalidateQueries({ queryKey: ["publication-directory", user?.user_id] });
    } finally {
      if (alive.current) setRevoking(null);
    }
  }
  const scopeOptions = [{ value: "", label: "All vaults" }, ...vaultDirectory.vaults.map(item => ({ value: item.name, label: item.name }))];
  if (vault && !scopeOptions.some(item => item.value === vault)) scopeOptions.push({ value: vault, label: vaultDirectory.loading ? "Loading vault…" : "Vault unavailable" });

  return <section className="workspace-density-compact @container/publications mx-auto w-full max-w-7xl" aria-labelledby="public-links-title">
    <WorkspacePageHeader variant="page" titleId="public-links-title" icon={Share2} title="Publications"
      context="Manage published documents, tables, and files across your vaults."
      actions={<Button variant="ghost" size="sm" onClick={() => void feed.refetch()} disabled={checking || feed.isFetching} aria-label="Refresh publications" className="workspace-control"><RefreshCw className="h-3.5 w-3.5" aria-hidden />Refresh</Button>} />
    <div className="mb-3 flex min-w-0 flex-wrap items-center gap-2">
      <SelectMenu value={vault} options={scopeOptions} onValueChange={value => change("vault", value)} leadingIcon={<Boxes className="h-3.5 w-3.5" />} aria-label="Publications vault scope" searchable searchPlaceholder="Find a vault…" className="workspace-control h-9 w-auto min-w-40 max-w-full text-ui" disabled={checking || (vaultDirectory.loading && !vaultDirectory.vaults.length)} />
      <label className="relative min-w-0 flex-1 basis-60"><span className="sr-only">Search publications</span><Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-foreground-muted" aria-hidden />
        <Input ref={searchRef} value={text} maxLength={200} onChange={event => change("q", event.target.value, true)} placeholder="Search titles, vaults, or paths…" className="workspace-control h-9 pl-9 text-ui max-sm:text-base" />
      </label>
    </div>
    {vaultDirectory.error && <Alert variant="destructive" className="mb-3">The vault selector could not be updated.<Button variant="link" className="ml-2" onClick={vaultDirectory.refetch}>Retry vaults</Button></Alert>}
    <Panel variant="workspace" flush>
      <div className="flex min-w-0 flex-wrap items-center gap-2 border-b border-border px-3 py-2">
        <div role="group" aria-label="Publication status" className="flex flex-wrap gap-1">
          {([{ value: "active", label: "Published" }, { value: "all", label: "All" }, { value: "unavailable", label: "Unavailable" }] as const).map(item => <Button key={item.value} size="sm" variant="ghost" className={"workspace-control " + (status === item.value ? "bg-surface-selected text-surface-selected-foreground" : "text-foreground-muted")} aria-pressed={status === item.value} onClick={() => change("status", item.value)}>{item.label}</Button>)}
        </div>
        <SelectMenu value={expiry} onValueChange={value => change("expiry", value)} options={[{ value: "any", label: "Any expiry" }, { value: "none", label: "No expiry" }, { value: "soon", label: "Expires within 7 days" }, { value: "long-running", label: "Published for 30+ days" }]} aria-label="Publication expiry" className="workspace-control ml-auto h-8 w-auto max-w-full text-xs" />
      </div>
      <PublicationPolicySummary />
      <div aria-busy={updating || feed.isFetchingNextPage || checking}>
        {copyError && <Alert variant="destructive" className="m-3">{copyError}</Alert>}
        {updating && data && <InlineLoadingState label="Updating publications; previous results shown" className="px-4 py-2" />}
        {feed.isError && (!feed.isFetchNextPageError || denied || pagingNeedsRefresh) && <Alert variant="destructive" className="m-3" title="Publications could not be loaded">{data ? "Previous publications are still shown. " : ""}{feed.error.message}<Button variant="outline" size="sm" className="ml-2 workspace-control" onClick={() => void feed.refetch()}>{pagingNeedsRefresh ? "Refresh list" : "Retry publications"}</Button></Alert>}
        {!data && !feed.isError ? <PublicationSkeleton /> : data && rows.length ? <ol aria-label="Publications" className="divide-y divide-border">
          {rows.map((item, index) => <PublicationRow key={item.slug} compact publication={item} number={index + 1} vault={item.vault} vaultScopeHref={scopeHref(item.vault)} copied={copied === item.slug} revoking={revoking === item.slug}
            unpublishDisabledReason={updating ? "Refreshing link permissions…" : !item.can_unpublish ? item.unpublish_disabled_reason || "Management permission is unavailable." : undefined}
            onCopy={() => void copy(item)} onRevoke={() => { if (!updating && item.can_unpublish) setPending(item); }} />)}
        </ol> : data && <div role="status" className="px-4 py-6"><p className="text-sm font-medium">{hasFilters ? "No publications match these filters" : "Nothing published right now"}</p>
          <p className="mt-1 text-xs text-foreground-muted">{hasFilters ? "Choose another scope or clear the filters." : "Publish a document, table, or file from its page. Expired publications are available under All."}</p>
          <Button variant="link" size="sm" className="mt-2" onClick={() => setParams({ status: "all" })}>Show all publications</Button>
        </div>}
        <AutoLoadMore hasMore={!denied && !pagingNeedsRefresh && Boolean(feed.hasNextPage)} loading={feed.isFetchingNextPage} paused={checking || updating} error={feed.isFetchNextPageError} requestKey={`${vault}:${status}:${expiry}:${q}:${data?.pages.length ?? 0}`} onLoadMore={() => void feed.fetchNextPage()} label="Load more publications" loadingLabel="Loading more publications" className="px-4" />
        {total !== undefined && <p role="status" className="border-t border-border px-4 py-2 text-xs tabular-nums text-foreground-muted">{total.toLocaleString()} matching publication{total === 1 ? "" : "s"}{rows.length < total ? ` · ${rows.length} loaded` : ""}</p>}
      </div>
    </Panel>
    {vault && <Link to={`/vault/${encodeURIComponent(vault)}/publications`} className="mt-3 inline-flex min-h-9 items-center gap-1.5 rounded-[var(--radius-sm)] text-xs text-link hover:text-link-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">Open in vault<ArrowRight className="h-3.5 w-3.5" aria-hidden /></Link>}
    <ConfirmDialog open={pending !== null} onOpenChange={open => { if (!open) setPending(null); }} title={pending ? `Unpublish "${pending.title || pending.slug}"?` : ""}
      description="This public link will stop working immediately. The original resource will remain in its Vault."
      confirmLabel="Unpublish" variant="destructive" onConfirm={unpublish} returnFocusRef={searchRef} />
  </section>;
}
