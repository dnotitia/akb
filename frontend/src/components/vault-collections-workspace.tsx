import { useMemo, useRef, useState, type MouseEvent, type ReactNode } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { ArrowLeft, ChevronRight, FileText, Files, LayoutGrid, LibraryBig, List, Search, Table2, X } from "lucide-react";
import { useCollectionProof, useVaultTree, type TreeNode } from "@/hooks/use-vault-tree";
import { useDebounce } from "@/hooks/use-debounce";
import { useAccessVerification } from "@/contexts/current-user-context";
import { useResourceNavigation } from "@/contexts/resource-navigation-context";
import { isReservedCollection } from "@/lib/skill";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { AutoLoadMore } from "@/components/ui/auto-load-more";
import { Input } from "@/components/ui/input";
import { LoadingState, InlineLoadingState } from "@/components/ui/loading-state";
import { Panel } from "@/components/ui/panel";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

const focus = "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring";
const PAGE = 24;
const CONTENT_KINDS = [
  { value: "collections", label: "Collections", count: "collection_count" },
  { value: "documents", label: "Documents", count: "document_count" },
  { value: "tables", label: "Tables", count: "table_count" },
  { value: "files", label: "Files", count: "file_count" },
] as const;
type ContentKind = typeof CONTENT_KINDS[number]["value"];
export type VaultInventory = Partial<Record<typeof CONTENT_KINDS[number]["count"], number>>;
function knownTotal(value: number | undefined): value is number { return typeof value === "number" && Number.isFinite(value) && value >= 0; }


function collectionRows(tree: TreeNode[]): TreeNode[] {
  return tree.flatMap(node => node.kind === "collection" ? [node, ...collectionRows(node.children ?? [])] : []);
}

function resourceRows(tree: TreeNode[]): TreeNode[] {
  return tree.flatMap(node => node.kind === "collection" ? resourceRows(node.children ?? []) : [node]);
}

/** Collection index and its selected contents share the explorer's verified cache. */
export function VaultCollectionsWorkspace({ vault, enabled, inventory, context, onboarding, navigation }: {
  vault: string; enabled: boolean; inventory?: VaultInventory; context?: ReactNode; onboarding?: ReactNode; navigation?: ReactNode;
}) {
  const { checking } = useAccessVerification();
  const { requestNavigation } = useResourceNavigation();
  const location = useLocation();
  const navigate = useNavigate();
  const locationKey = useRef(location.key);
  const pendingKind = useRef<string | null>(null);
  if (locationKey.current !== location.key) pendingKind.current = null;
  locationKey.current = location.key;
  const kindParam = new URLSearchParams(location.search).get("type");
  const kind: ContentKind = CONTENT_KINDS.find(item => item.value === kindParam)?.value ?? "collections";
  const [view, setView] = useState<"grid" | "list">(() => {
    try { return localStorage.getItem("akb.collectionDirectoryView.v1") === "list" ? "list" : "grid"; } catch { return "grid"; }
  });
  function changeView(next: "grid" | "list") {
    setView(next);
    try { localStorage.setItem("akb.collectionDirectoryView.v1", next); } catch { /* Appearance remains usable without storage. */ }
  }
  function changeKind(next: string) {
    if (next === kind) return;
    const params = new URLSearchParams(location.search);
    if (next === "collections") params.delete("type"); else params.set("type", next);
    const target = `${location.pathname}${params.size ? `?${params}` : ""}${location.hash}`;
    if (pendingKind.current === target) return;
    if (requestNavigation(target)) { pendingKind.current = target; setVisible(PAGE); setVisibleResources(PAGE); navigate(target); }
  }
  const requested = new URLSearchParams(location.search).get("collection") || "";
  const [filter, setFilter] = useState("");
  const [visible, setVisible] = useState(PAGE);
  const [visibleResources, setVisibleResources] = useState(PAGE);
  const [selection, setSelection] = useState(requested);
  // Picker, explorer and browser history can change the destination without
  // using this directory's links. Search and pagination belong to that view.
  if (selection !== requested) {
    setSelection(requested);
    setFilter("");
    setVisible(PAGE);
    setVisibleResources(PAGE);
  }
  const queryText = filter.trim();
  const nameQuery = useDebounce(queryText);
  // Name search always crosses the whole Vault, independently of the active tab
  // and Collection. Normal resource tabs request only their own kind, bounded.
  const showCollections = kind === "collections" || Boolean(queryText);
  const browseCollections = showCollections || Boolean(requested);
  const showResources = kind !== "collections" || Boolean(requested) || Boolean(queryText);
  const options = { collection: nameQuery ? undefined : requested || undefined,
    name_query: nameQuery, enabled: enabled && !checking };
  const query = useVaultTree(vault, "unarchived", { ...options, depth: nameQuery ? -1 : 0,
    enabled: options.enabled && browseCollections, content_type: "collections" });
  const contents = useVaultTree(vault, "unarchived", { ...options,
    depth: nameQuery || kind !== "collections" ? -1 : 0,
    enabled: options.enabled && showResources,
    content_type: nameQuery || kind === "collections" ? "resources" : kind });
  const proof = useCollectionProof(vault, requested, enabled && !checking);
  const denied = (browseCollections && query.accessDenied) || (showResources && contents.accessDenied) || Boolean(requested && proof.accessDenied);
  const tree = enabled && !checking && !denied && (!query.showingPreviousScope || query.path === requested) ? query.tree : null;
  const error = (showCollections ? query.error : "") || (requested ? proof.error : "");
  const searching = nameQuery !== queryText || (showCollections && query.showingPreviousScope) || (showResources && contents.showingPreviousScope);
  const collections = useMemo(() => tree ? collectionRows(tree) : [], [tree]);
  const selected = requested && proof.verified ? collections.find(node => node.path === requested) ?? proof.node : undefined;
  const ancestors = selected ? selected.path.split("/").slice(0, -1).map((_part, index, parts) =>
    collections.find(node => node.path === parts.slice(0, index + 1).join("/")))
    .filter((node): node is TreeNode => node !== undefined && !isReservedCollection(node.path)) : [];
  const parent = ancestors.at(-1);
  const direct = selected ? selected.children ?? [] : tree ?? [];
  const cards = (queryText ? collections : direct.filter(node => node.kind === "collection"))
    .filter(node => node.path !== requested && !isReservedCollection(node.path) && (!queryText || query.items?.some(item => item.type === "collection" && item.path === node.path)));
  const resources = enabled && !checking && !denied && (!contents.showingPreviousScope || contents.path === requested)
    ? resourceRows(contents.tree ?? []).filter(node => queryText || kind === "collections" || `${node.kind}s` === kind) : [];

  function href(path: string) { return path ? `/vault/${encodeURIComponent(vault)}?collection=${encodeURIComponent(path)}` : `/vault/${encodeURIComponent(vault)}`; }
  function navigationState(target: string) {
    return target === href("") || target.startsWith(href("") + "?collection=")
      ? { vaultPageNavigation: target, collectionView: "main" }
      : { vaultPageNavigation: target };
  }
  function follow(event: MouseEvent<HTMLAnchorElement>, target: string) {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    if (!requestNavigation(target, { state: navigationState(target) })) event.preventDefault();
    else if (target.includes("?collection=") || target === href("")) { setFilter(""); setVisible(PAGE); setVisibleResources(PAGE); }
  }
  function collectionLink(node: TreeNode) {
    const target = href(node.path);
    const parent = node.path.split("/").slice(0, -1).join(" / ");
    return <li key={node.path} className="min-w-0"><Panel variant="workspace" flush inset={false} className="vault-collection-card relative h-full min-w-0">
      <Link to={target} state={{ vaultPageNavigation: target, collectionView: "main" }} onClick={event => follow(event, target)} aria-label={`Open collection ${node.path}`}
        className={`vault-collection-link group ${focus}`}>
        <span className="vault-collection-mark"><LibraryBig className="h-5 w-5" aria-hidden /></span>
        <span className="min-w-0 flex-1"><span className="block break-words text-ui font-medium leading-5 text-foreground group-hover:text-link [overflow-wrap:anywhere]">{node.name}</span>
          {(node.raw?.summary || parent) && <span className="vault-collection-description">{node.raw?.summary || parent}</span>}
          {knownTotal(node.raw?.doc_count) && <span className="vault-collection-count">{node.raw.doc_count.toLocaleString()} document{node.raw.doc_count === 1 ? "" : "s"}</span>}
        </span>
        <ChevronRight className="h-4 w-4 shrink-0 text-foreground-muted" aria-hidden />
      </Link>
    </Panel></li>;
  }
  function resourceHref(node: TreeNode) {
    const kind = node.kind === "document" ? "doc" : node.kind;
    return `/vault/${encodeURIComponent(vault)}/${kind}/${encodeURIComponent(node.path)}`;
  }

  return <section aria-label="Collection workspace" className="@container/collections min-w-0" aria-busy={searching || (showCollections && (query.loading || query.refreshing || query.loadingMore)) || (showResources && (contents.loading || contents.refreshing || contents.loadingMore)) || Boolean(requested && proof.loading)}>
    <Tabs value={kind} onValueChange={changeKind} activationMode="manual">
      <section aria-label="Contents" className="vault-content-navigation">
        <TabsList aria-label="Resource type" className="vault-content-tabs">
          {CONTENT_KINDS.map(item => <TabsTrigger key={item.value} value={item.value} onClick={() => changeKind(item.value)}>
            {item.label}<span className="vault-content-total" aria-label={knownTotal(inventory?.[item.count]) ? `${inventory![item.count]!.toLocaleString()} total in this Vault` : "Total unavailable"}>
              {knownTotal(inventory?.[item.count]) ? inventory![item.count]!.toLocaleString() : "—"}
            </span>
          </TabsTrigger>)}
        </TabsList>
        {navigation}
      </section>
      <TabsContent value={kind} className="vault-content-panel">
      <div className={context ? "vault-overview-body" : "vault-overview-body vault-overview-body-empty"}>
      <div className="vault-overview-main">
      {onboarding}
      <div className="vault-directory-search-row">
        <div className="vault-collection-search relative min-w-0">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-foreground-muted" aria-hidden />
          <Input aria-label="Search collections and content" placeholder="Search this Vault" value={filter} onChange={event => { setFilter(event.target.value); setVisible(PAGE); setVisibleResources(PAGE); }} className="h-10 pl-9 text-sm max-sm:h-11 max-sm:text-base pointer-coarse:h-11" />
        </div>
        {showCollections && <div role="group" aria-label="Collection view" className="vault-collection-view">
          <Button variant="ghost" size="icon" aria-label="Grid view" aria-pressed={view === "grid"} onClick={() => changeView("grid")}><LayoutGrid className="h-4 w-4" aria-hidden /></Button>
          <Button variant="ghost" size="icon" aria-label="List view" aria-pressed={view === "list"} onClick={() => changeView("list")}><List className="h-4 w-4" aria-hidden /></Button>
        </div>}
      </div>
    <div className={requested || queryText ? "vault-collection-toolbar" : "sr-only"}>
      <div className="min-w-0">
        {requested && !queryText ? <nav aria-label="Collection location">
          <ol className="flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-1 text-xs">
            <li className="min-w-0"><Link to={href("")} state={navigationState(href(""))} onClick={event => follow(event, href(""))}
              className={`inline-flex min-h-9 items-center gap-1.5 rounded-[var(--radius-sm)] text-link max-sm:min-h-11 pointer-coarse:min-h-11 ${focus}`}>
              {!parent && <ArrowLeft className="h-3.5 w-3.5 shrink-0" aria-hidden />}All collections
            </Link></li>
            {ancestors.map(node => <li key={node.path} className="flex min-w-0 items-center gap-1.5">
              <span aria-hidden className="text-foreground-muted">/</span>
              <Link to={href(node.path)} state={navigationState(href(node.path))} onClick={event => follow(event, href(node.path))}
                aria-label={node === parent ? `Back to collection ${node.path}` : `Open collection ${node.path}`}
                className={`inline-flex min-h-9 min-w-0 items-center gap-1.5 rounded-[var(--radius-sm)] text-link max-sm:min-h-11 pointer-coarse:min-h-11 ${focus}`}>
                {node === parent && <ArrowLeft className="h-3.5 w-3.5 shrink-0" aria-hidden />}<span className="break-words [overflow-wrap:anywhere]">{node.name}</span>
              </Link>
            </li>)}
            {selected && <li className="flex min-w-0 items-center gap-1.5" aria-current="page"><span aria-hidden className="text-foreground-muted">/</span><h2 className="min-w-0 break-words text-sm font-semibold [overflow-wrap:anywhere]">{selected.name}</h2></li>}
          </ol>
        </nav> : <h2 className="text-sm font-semibold">{queryText ? "Search results" : CONTENT_KINDS.find(item => item.value === kind)!.label}</h2>}
      </div>
      {queryText && <Button variant="ghost" size="sm" className="vault-clear-search" onClick={() => setFilter("")}><X className="h-3.5 w-3.5" aria-hidden />Clear search</Button>}
    </div>
    {error && <Alert variant="destructive" className="mb-3" title="Collections could not be loaded">
      {tree ? "Previous collections remain visible. " : "Try loading the collection directory again. "}<Button variant="outline" size="sm" className="max-sm:min-h-11 pointer-coarse:min-h-11" loading={query.refreshing || proof.refreshing} onClick={query.refetch}>Retry collections</Button>
    </Alert>}
    {((showCollections ? !tree : contents.loading) || (requested && proof.loading)) && !error ? <LoadingState label={showCollections ? "Loading collections" : "Loading content"}><div className={showCollections ? "vault-collection-grid" : "space-y-2"} aria-hidden>{Array.from({ length: 4 }, (_, index) => <Skeleton key={index} className={showCollections ? "h-19 w-full" : "h-12 w-full"} />)}</div></LoadingState> : (showCollections ? tree : !contents.loading) && <>
      {((showCollections && query.refreshing) || (showResources && contents.refreshing) || searching) && <InlineLoadingState label={queryText ? "Searching" : showCollections ? "Updating collections" : "Updating content"} className="mb-3" />}
      {requested && !selected ? <div role="status" className="py-6 text-ui text-foreground-muted">{proof.missing ? "This collection is unavailable. Choose another collection or return to all collections." : "Checking collection…"}
      </div> : <>
        {showCollections && (cards.length > 0 ? <ul aria-label={queryText ? "Matching collections" : "Collections"} className={`vault-collection-grid${view === "list" ? " vault-collection-list" : ""}`}>{cards.slice(0, visible).map(node => collectionLink(node))}</ul>
          : !searching && query.complete && (queryText || !selected || resources.length === 0) && <p role="status" className="py-5 text-ui text-foreground-muted">{queryText ? "No collections match this name." : selected ? "No subcollections." : "No collections yet. Add a collection from the New menu."}</p>)}
        {showCollections && <AutoLoadMore hasMore={cards.length > visible || query.hasMore} loading={query.loadingMore}
          paused={searching || query.loading || query.refreshing} error={Boolean(query.error)}
          requestKey={`${vault}:${requested}:${nameQuery}:collections:${visible}:${query.pageCount}`}
          label="Load more collections" loadingLabel="Loading more collections"
          onLoadMore={() => cards.length > visible ? setVisible(count => count + PAGE) : query.loadMore()} />}
        {showResources && (resources.length > 0 || contents.loading || contents.hasMore || contents.error) && <section aria-label="Collection resources" className="mt-5">
          {showCollections && <h3 className="mb-2 text-xs font-medium text-foreground-muted">{queryText ? "Matching content" : "Contents"}</h3>}
          {contents.loading && <InlineLoadingState label="Loading content" />}
          {queryText && !resources.length && contents.hasMore && !contents.error && <InlineLoadingState label="Searching content" size="sm" />}
          {contents.error && <Alert variant="destructive" className="mb-3" title="Content could not be loaded"><Button variant="outline" size="sm" onClick={contents.refetch}>Retry content</Button></Alert>}
          <Panel variant="workspace" flush className="vault-resources"><ul className="divide-y divide-border">{resources.slice(0, visibleResources).map(node => {
            const Icon = node.kind === "document" ? FileText : node.kind === "table" ? Table2 : Files;
            const target = resourceHref(node);
            return <li key={`${node.kind}:${node.path}`}><Link to={target} state={{ vaultPageNavigation: target }} onClick={event => follow(event, target)} className={`flex min-h-11 min-w-0 items-center gap-2 px-3 py-2 text-ui hover:bg-surface-hover ${focus}`}><Icon className="h-4 w-4 shrink-0 text-foreground-muted" aria-hidden /><span className="min-w-0 flex-1 break-words [overflow-wrap:anywhere]">{node.name}</span><span className="text-xs text-foreground-muted">{node.kind}</span></Link></li>;
          })}</ul></Panel>
          <AutoLoadMore hasMore={resources.length > visibleResources || contents.hasMore} loading={contents.loadingMore}
            paused={searching || contents.loading || contents.refreshing} error={Boolean(contents.error)}
            requestKey={`${vault}:${requested}:${nameQuery}:content:${visibleResources}:${contents.pageCount}`}
            label="Load more content" loadingLabel={queryText && !resources.length ? "" : "Loading more content"}
            onLoadMore={() => resources.length > visibleResources ? setVisibleResources(count => count + PAGE) : contents.loadMore()} />
        </section>}
        {showResources && !searching && contents.complete && !resources.length && <p role="status" className="py-3 text-ui text-foreground-muted">{queryText ? "No content matches this name." : "No content of this type yet."}</p>}
      </>}
    </>}
      </div>
      {context}
      </div>
      </TabsContent>
    </Tabs>
  </section>;
}
