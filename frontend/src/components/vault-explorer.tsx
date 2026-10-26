import { Link, useLocation, useNavigate } from "react-router-dom";
import { memo, useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import {
  ChevronDown,
  Check,
  Archive,
  ChevronRight,
  FilePlus,
  FileText,
  Folder,
  FolderPlus,
  FolderOpen,
  FolderSearch,
  Info,
  Lock,
  MoreHorizontal,
  Paperclip,
  Pin,
  PinOff,
  Plus,
  RefreshCw,
  Sparkles,
  Table,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { AutoLoadMore } from "@/components/ui/auto-load-more";
import { RailCollapseButton, RailFilterField, RailFilterToggle, RailIdentity, RailManagement, RailSearchToolbar } from "@/components/navigation-rail-controls";
import { OverflowRevealTitle } from "@/components/ui/overflow-reveal-title";
import { CollectionExplorerContext } from "@/components/collection-explorer-context";
import { VaultSidebarBackLink } from "@/components/vault-navigation-controls";
import { InlineLoadingState, LoadingState } from "@/components/ui/loading-state";
import { Skeleton } from "@/components/ui/skeleton";
import { SkillBadge } from "@/components/ui/skill-badge";
import { buildTree, useCollectionProof, useVaultTree, useExpandedPaths, type NodeKind, type TreeNode } from "@/hooks/use-vault-tree";
import { useResourceLocation } from "@/contexts/resource-location-context";
import { useVaultRefresh } from "@/contexts/vault-refresh-context";
import { useOpenDocumentCreateDialog } from "@/contexts/document-create-dialog-context";
import {
  activePathFromRoute,
  filterTreeByKind,
  findResourceGroup,
  flattenVisible,
  kindGroupKey,
  leafHref,
  RESOURCE_PREVIEW_SIZE,
  type ResourceKind,
  type FlatKindGroupRow,
  type FlatMoreRow,
  type FlatRow,
} from "@/lib/tree-route";
import {
  deleteDocument,
  deleteVaultFile,
  deleteVaultTable,
  getVaultInfo,
  type ArchiveScope,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import { recentTone } from "@/lib/recent";
import { isReservedCollection, VAULT_SKILL_PATH } from "@/lib/skill";
import { CreateCollectionDialog } from "@/components/create-collection-dialog";
import {
  CollectionDetailsDialog,
  type CollectionResourceCounts,
} from "@/components/collection-details-dialog";
import { DeleteCollectionDialog } from "@/components/delete-collection-dialog";
import { FileUploadDialog } from "@/components/file-upload-dialog";
import { TableCreateDialog } from "@/components/table-create-dialog";
import { SelectMenu } from "@/components/ui/select-menu";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { docUri, fileUri, tableUri, parseFileUri } from "@/lib/uri";
import { ResourceActionsMenu } from "@/components/resource-actions-menu";
import {
  ResourceDeleteDialog,
  type DeletableResourceKind,
} from "@/components/resource-delete-dialog";
import { ROLE_RANK, type Role } from "@/lib/roles";
import { documentTitleKey } from "@/lib/document-title-conflict";
import { useAccessVerification, useCurrentUser } from "@/contexts/current-user-context";
import { toggleWorkspaceShortcut, useWorkspaceShortcuts, workspaceShortcutKey } from "@/lib/workspace-shortcuts";
import { recordRecentCollection } from "@/lib/recent-collections";
import { useDebounce } from "@/hooks/use-debounce";

const PAGE_SIZE = 10;
const TYPEAHEAD_TIMEOUT_MS = 500;
const TREE_RENDER_PAGE = 300;
const RESOURCE_PAGE_SIZE = 50;

/**
 * Left-rail explorer — single collection-rooted tree. Documents, tables,
 * and files all live as children of their owning collection (or at the
 * vault root). No kind-based section partitioning: the same collection's
 * docs + tables + files appear together so the user sees one cohesive
 * hierarchy rather than three parallel lists.
 *
 * Keyboard nav (arrow/home/end/typeahead) operates over the flattened
 * visible rows in tree order.
 */
export interface VaultExplorerProps {
  vault: string;
  /**
   * Optional callback fired after a successful create/delete-collection
   * mutation. When omitted, the explorer falls back to
   * `useVaultRefresh().refetchTree` so any in-tree mutation invalidates
   * the cached browse response.
   */
  onMutation?: () => void;
  /**
   * Called once with the tree's `refetch` function so a parent (e.g.
   * `VaultShell`) can plumb it into a `VaultRefreshProvider`. The
   * explorer owns the hook (chicken-and-egg with the tree fetch), but
   * the parent needs the handle to share it with siblings.
   */
  onRefetchReady?: (refetch: () => void) => void;
  /** Collapse the collection column from the shared sidebar header. */
  onCollapse?: () => void;
  /** The focused sidebar already owns Vault identity and its collapse control. */
  presentation?: "standalone" | "focused";
}

export function VaultExplorer({
  vault,
  onMutation,
  onRefetchReady,
  onCollapse,
  presentation = "standalone",
}: VaultExplorerProps) {
  const focused = presentation === "focused";
  const user = useCurrentUser();
  const currentResource = useResourceLocation();
  const { checking: checkingAccess } = useAccessVerification();
  const filterOwner = JSON.stringify([vault, user?.user_id ?? null]);
  const [scopeSelection, setScopeSelection] = useState<{ owner: string; scope: ArchiveScope }>({ owner: filterOwner, scope: "unarchived" });
  const archiveScope = scopeSelection.owner === filterOwner ? scopeSelection.scope : "unarchived";
  const openCreateDocument = useOpenDocumentCreateDialog();
  const refreshCtx = useVaultRefresh();
  // Prefer the explicit prop; otherwise fall back to context. This lets
  // tests render the explorer with no provider and still wire mutation
  // refreshes for production.
  const handleMutation = onMutation ?? refreshCtx.refetchTree;

  const { expanded, toggle, revealAncestorsOf } = useExpandedPaths(vault);
  const { pathname, search, key: locationKey } = useLocation();
  const requestedCollection = pathname === `/vault/${encodeURIComponent(vault)}`
    ? new URLSearchParams(search).get("collection") : null;
  const navigate = useNavigate();
  const [resourceFilter, setResourceFilter] = useState({ owner: filterOwner, query: "", collection: "" });
  // Scope is temporary explorer state, never inherited by another Vault/account.
  if (resourceFilter.owner !== filterOwner) {
    setResourceFilter({ owner: filterOwner, query: "", collection: "" });
  }
  const filter = resourceFilter.owner === filterOwner ? resourceFilter.query : "";
  const collectionScope = resourceFilter.owner === filterOwner ? resourceFilter.collection : "";
  const [exploration, setExploration] = useState({ owner: filterOwner, path: "" });
  if (exploration.owner !== filterOwner) setExploration({ owner: filterOwner, path: "" });
  const explorationPath = focused && exploration.owner === filterOwner ? exploration.path : "";
  const explorationBackRef = useRef<HTMLButtonElement>(null);
  const explorationFocusRef = useRef<{ owner: string; previousPath?: string } | null>(null);
  const setFilter = useCallback((query: string) => {
    setResourceFilter(current => ({ ...current, owner: filterOwner, query }));
  }, [filterOwner]);
  const filterRowRef = useRef<HTMLDivElement>(null);
  const filterHelpId = useId();
  const filterFocusFrame = useRef<number | null>(null);
  const cancelFilterFocus = useCallback(() => {
    if (filterFocusFrame.current !== null) cancelAnimationFrame(filterFocusFrame.current);
    filterFocusFrame.current = null;
  }, []);
  useEffect(() => cancelFilterFocus, [filterOwner, locationKey, cancelFilterFocus]);
  const focusResourceFilter = useCallback(() => {
    // Callers may open the disclosure in this same event. Focus after React
    // reveals its input rather than leaving focus on a hidden control.
    const focusInput = () => {
      const input = filterRowRef.current?.querySelector<HTMLInputElement>('input[type="search"]');
      if (input && !input.closest("[hidden]")) input.focus();
    };
    cancelFilterFocus();
    focusInput();
    filterFocusFrame.current = requestAnimationFrame(() => {
      filterFocusFrame.current = null;
      focusInput();
    });
  }, [cancelFilterFocus]);
  const changeCollectionScope = useCallback((collection: string) => {
    setResourceFilter(current => ({ ...current, owner: filterOwner, collection }));
    if (collection) revealAncestorsOf(`${collection}/_`);
  }, [filterOwner, revealAncestorsOf]);
  const exploreCollection = useCallback((path: string, returning = false) => {
    cancelFilterFocus();
    explorationFocusRef.current = { owner: filterOwner, previousPath: returning ? explorationPath : undefined };
    setExploration({ owner: filterOwner, path });
    // Collection search is relative to the new root. Text, kind and state
    // filters survive moving up; an older nested boundary must not trap it.
    setResourceFilter(current => ({ ...current, owner: filterOwner, collection: "" }));
    if (path) revealAncestorsOf(`${path}/_`);
  }, [filterOwner, explorationPath, revealAncestorsOf, cancelFilterFocus]);
  const [kindFilter, setKindFilter] = useState<ResourceKind | "all">("all");
  const nameQuery = useDebounce(filter.trim());
  // Keep the existing expandable hierarchy; bounded pages limit each request.
  const browseDepth = -1;
  const { tree: pageTree, items: pageItems, loading, refreshing, showingPreviousScope, unsupported, error, refetch,
    complete, paginationUnsupported, hasMore, loadingMore, loadMore, pageCount, pastCollectionCatalog } = useVaultTree(vault, archiveScope, {
      collection: collectionScope || explorationPath || undefined,
      content_type: kindFilter === "all" ? "all" : `${kindFilter}s` as "documents" | "tables" | "files",
      depth: browseDepth,
      name_query: nameQuery,
    });
  const searching = filter.trim() !== nameQuery || showingPreviousScope;
  const inventoryComplete = complete && browseDepth === -1 && !nameQuery && kindFilter === "all" && archiveScope === "all";
  const explorationProof = useCollectionProof(vault, explorationPath, focused && !checkingAccess);
  const tree = useMemo(() => {
    if (!pageTree || !pageItems || nameQuery || currentResource?.vault !== vault || !currentResource.resourceRef) return pageTree;
    const kind = { Document: "document", File: "file", Table: "table" }[currentResource.kind] as ResourceKind;
    if (archiveScope === "archived" && (kind !== "document" || currentResource.status !== "archived")) return pageTree;
    if (archiveScope === "unarchived" && currentResource.status === "archived") return pageTree;
    const ref = currentResource.resourceRef;
    const uri = kind === "document" ? docUri(vault, ref) : kind === "file"
      ? fileUri(vault, ref, currentResource.collectionPath) : tableUri(vault, ref, currentResource.collectionPath);
    if (pageItems.some(item => item.uri === uri || (item.type === kind && item.path === ref))) return pageTree;
    // The current reader already verified this identity and access. Include
    // that single row without downloading every preceding directory page.
    const navigation: { type: "collection"; name: string; path: string }[] = [];
    const collectNavigation = (nodes: TreeNode[]) => nodes.forEach(node => {
      if (node.kind !== "collection") return;
      navigation.push({ ...node.raw, type: "collection", name: node.name, path: node.path });
      collectNavigation(node.children ?? []);
    });
    collectNavigation(pageTree);
    return buildTree([...navigation, ...pageItems, { type: kind, name: currentResource.title, path: ref,
      uri, collection: currentResource.collectionPath, status: currentResource.status }]);
  }, [pageTree, pageItems, currentResource, vault, archiveScope, nameQuery]);
  const resourceRevealKey = currentResource?.vault === vault && currentResource.resourceRef
    ? `${filterOwner}:${currentResource.kind}:${currentResource.resourceRef}` : null;
  const revealedResource = useRef<{ key: string; pathname: string } | null>(null);
  useEffect(() => {
    if (!resourceRevealKey || !currentResource) {
      // Leaving this reader permits a later visit to reveal it again. A
      // same-route access proof must not undo the user's manual collapse.
      if (revealedResource.current?.pathname !== pathname) revealedResource.current = null;
      return;
    }
    if (revealedResource.current?.key === resourceRevealKey) return;
    revealedResource.current = { key: resourceRevealKey, pathname };
    setFilter("");
    // Opening a resource reveals its ancestors below; it must not turn the
    // reader's location into a Collection search filter and hide sibling rows.
    setKindFilter("all");
    setScopeSelection({ owner: filterOwner, scope: currentResource.status === "archived" ? "all" : "unarchived" });
  }, [resourceRevealKey, currentResource, vault, pathname, filterOwner, setFilter]);
  useEffect(() => {
    onRefetchReady?.(refetch);
  }, [onRefetchReady, refetch]);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const filtersId = useId();
  const activeFilterCount = Number(Boolean(collectionScope)) + Number(archiveScope !== "unarchived") + Number(archiveScope !== "archived" && kindFilter !== "all") + Number(focused && Boolean(filter.trim()));
  useEffect(() => {
    setKindFilter("all");
    if (focused) setFiltersOpen(false);
  }, [filterOwner, focused]);
  useEffect(() => {
    if (focused && filtersOpen) focusResourceFilter();
  }, [focused, filtersOpen, focusResourceFilter]);
  function changeArchiveScope(value: string) {
    setScopeSelection({ owner: filterOwner, scope: value as ArchiveScope });
    if (value === "archived") setKindFilter("all");
  }
  const [collapsedKindGroups, setCollapsedKindGroups] = useState<Set<string>>(
    () => new Set(),
  );
  const [kindLimits, setKindLimits] = useState<Map<string, number>>(
    () => new Map(),
  );
  const [renderLimit, setRenderLimit] = useState(TREE_RENDER_PAGE);
  const listRef = useRef<HTMLDivElement | null>(null);

  // Role-gated affordances. We fetch the role on mount/vault-change rather
  // than threading it through props because the existing parent
  // (`VaultShell`) doesn't have it cached and adding a redundant fetch in
  // the shell would slow first paint. document.tsx uses the same pattern.
  const [vaultRole, setVaultRole] = useState<string | null>(null);
  const [vaultReadOnly, setVaultReadOnly] = useState(true);
  useEffect(() => {
    let alive = true;
    setVaultRole(null);
    setVaultReadOnly(true);
    getVaultInfo(vault)
      .then((d) => {
        if (alive) {
          setVaultRole(d?.role || null);
          setVaultReadOnly(Boolean(d?.is_archived || d?.is_external_git));
        }
      })
      .catch(() => {
        if (alive) {
          setVaultRole(null);
          setVaultReadOnly(true);
        }
      });
    return () => {
      alive = false;
    };
  }, [vault]);
  const canWrite =
    !vaultReadOnly &&
    (vaultRole === "writer" || vaultRole === "admin" || vaultRole === "owner");
  const canAdmin =
    !vaultReadOnly &&
    (ROLE_RANK[vaultRole as Role] ?? 0) >= ROLE_RANK.admin;

  // Dialog state.
  const [createOpen, setCreateOpen] = useState(false);
  // When non-null, seeds the create-collection dialog's path with this
  // parent prefix + a trailing slash. Null means root create.
  const [createParentPath, setCreateParentPath] = useState<string | null>(null);
  // Stable opener so we can pass it down through memoized row props
  // without recreating identities on every render.
  const openCreate = useCallback((parent: string | null) => {
    setCreateParentPath(parent);
    setCreateOpen(true);
  }, []);
  const [deleteTarget, setDeleteTarget] = useState<{
    path: string;
    docCount: number;
    tableCount: number;
    fileCount: number;
    subCollectionCount: number;
    countsComplete: boolean;
  } | null>(null);
  const [detailsTarget, setDetailsTarget] = useState<{
    path: string;
    summary?: string | null;
    counts: CollectionResourceCounts;
    countsComplete: boolean;
    editable: boolean;
  } | null>(null);
  const [uploadCollection, setUploadCollection] = useState<string | null>(null);
  const [tableCollection, setTableCollection] = useState<string | null>(null);
  const [resourceDeleteTarget, setResourceDeleteTarget] = useState<{
    kind: DeletableResourceKind;
    name: string;
    ref: string;
    rowCount?: number;
    active: boolean;
  } | null>(null);
  const mutationTriggerRef = useRef<HTMLElement | null>(null);

  const rememberMutationTrigger = useCallback(() => {
    if (document.activeElement instanceof HTMLElement) {
      mutationTriggerRef.current = document.activeElement;
    }
  }, []);

  const openUpload = useCallback((collection: string) => {
    rememberMutationTrigger();
    setUploadCollection(collection);
  }, [rememberMutationTrigger]);

  const openTableCreate = useCallback((collection: string) => {
    rememberMutationTrigger();
    setTableCollection(collection);
  }, [rememberMutationTrigger]);

  const activeSig = useMemo(() => requestedCollection ? `collection:${requestedCollection}` : activePathFromRoute(pathname, tree), [pathname, tree, requestedCollection]);
  const activeResourcePath = activeSig?.split(":").slice(1).join(":") ?? "";
  const activeResourceGroup = useMemo(
    () => tree && activeSig ? findResourceGroup(tree, activeSig) : null,
    [tree, activeSig],
  );
  const collectionContext = requestedCollection || activeResourceGroup?.parentPath || "";
  const knownCollection = useMemo(() => {
    const contains = (nodes: TreeNode[]): boolean => nodes.some(node => node.kind === "collection"
      && (node.path === collectionContext || contains(node.children ?? [])));
    return Boolean(collectionContext && tree && contains(tree));
  }, [tree, collectionContext]);
  const lastRecordedCollection = useRef("");
  const userId = user?.user_id;
  const collectionReady = !checkingAccess && !loading && !refreshing && !showingPreviousScope && !unsupported && !error && knownCollection;
  useEffect(() => {
    if (!userId || !collectionReady) return;
    const visit = JSON.stringify([userId, vault, locationKey, collectionContext]);
    if (lastRecordedCollection.current === visit) return;
    lastRecordedCollection.current = visit;
    recordRecentCollection(userId, { vault, path: collectionContext });
  }, [userId, vault, locationKey, collectionContext, collectionReady]);

  useEffect(() => {
    if (pathname.replace(/\/$/, "") !== `/vault/${encodeURIComponent(vault)}`) return;
    setFilter("");
    changeCollectionScope(requestedCollection || "");
    setExploration({ owner: filterOwner, path: "" });
    setKindFilter("all");
    setScopeSelection({ owner: filterOwner, scope: "unarchived" });
    if (requestedCollection) revealAncestorsOf(`${requestedCollection}/_`);
  }, [pathname, requestedCollection, vault, filterOwner, revealAncestorsOf, setFilter, changeCollectionScope]);

  const activeGroupKey = activeResourceGroup
    ? kindGroupKey(activeResourceGroup.parentPath, activeResourceGroup.kind)
    : null;
  const activeRevealPath = activeResourceGroup
    ? activeResourceGroup.parentPath ? `${activeResourceGroup.parentPath}/_` : ""
    : activeResourcePath;

  // Reveal on resource navigation. Stable scalar dependencies let subsequent
  // manual collapses survive re-renders and refreshed tree objects.
  useEffect(() => {
    if (!activeSig) return;
    if (activeRevealPath) revealAncestorsOf(activeRevealPath);
    if (activeGroupKey) {
      setCollapsedKindGroups((current) => {
        if (!current.has(activeGroupKey)) return current;
        const next = new Set(current);
        next.delete(activeGroupKey);
        return next;
      });
    }
  }, [activeSig, activeGroupKey, activeRevealPath, revealAncestorsOf]);

  const explorationTrail = useMemo(() => findCollectionTrail(tree ?? [], explorationPath), [tree, explorationPath]);
  const explorationRoot = explorationProof.verified ? explorationTrail.find(node => node.path === explorationPath) : undefined;
  const explorationTree = useMemo(() => explorationPath ? explorationRoot ? [explorationRoot] : [] : tree,
    [explorationPath, explorationRoot, tree]);

  // Keep the reader's new destination visible if another surface navigates
  // outside this root. Explicit exploration itself never changes the reader.
  const destination = JSON.stringify([filterOwner, pathname, requestedCollection]);
  const previousDestination = useRef(destination);
  useEffect(() => {
    if (previousDestination.current === destination || checkingAccess || loading || !tree || showingPreviousScope || unsupported || error) return;
    const target = requestedCollection && findCollectionTrail(tree, requestedCollection).length
      ? requestedCollection : activeResourceGroup?.parentPath;
    // A new resource may arrive in the browse refresh after its route. Do not
    // consume that destination while its ownership is still unresolved.
    if (target === undefined) return;
    previousDestination.current = destination;
    if (!explorationPath || isInCollection(target, explorationPath)) return;
    const common = sharedCollectionPath(explorationPath, target);
    setExploration({ owner: filterOwner, path: common });
    setResourceFilter(current => ({ ...current, owner: filterOwner, collection: "" }));
  }, [destination, checkingAccess, loading, tree, showingPreviousScope, unsupported, error, requestedCollection, activeResourceGroup, explorationPath, filterOwner]);

  const collectionOptions = useMemo(() => {
    const options = [{ value: "", label: explorationPath ? "Current collection" : "All collections" }];
    const visit = (nodes: TreeNode[]) => nodes.forEach(node => {
      if (node.kind !== "collection") return;
      options.push({ value: node.path, label: node.path });
      visit(node.children ?? []);
    });
    visit(explorationTree ?? []);
    // A changed archive view may omit an empty Collection. Keep the chosen
    // boundary visible instead of implying the query broadened to the Vault.
    if (collectionScope && !options.some(option => option.value === collectionScope)) {
      options.push({ value: collectionScope, label: collectionScope });
    }
    return options;
  }, [explorationTree, explorationPath, collectionScope]);

  const scopedTree = useMemo(() => {
    if (!explorationTree || !collectionScope) return explorationTree;
    const trail = findCollectionTrail(explorationTree, collectionScope);
    const collection = trail.at(-1);
    if (!collection) return [];
    // Keep the bounded scope's ancestor chain available for exploration. A
    // reader reveal must not strand users at a deep Collection with no way up.
    return trail.slice(0, -1).reduceRight<TreeNode[]>((children, node) => [{ ...node, children }], [collection]);
  }, [explorationTree, collectionScope]);

  const kindFiltered = useMemo(() => {
    if (!scopedTree) return scopedTree;
    return filterTreeByKind(scopedTree, archiveScope === "archived" ? "document" : kindFilter);
  }, [scopedTree, kindFilter, archiveScope]);

  // The server searches the entire scope; its verified matches and their
  // ancestry must not be narrowed again to a client-side approximation.
  const filtered = useMemo(() => nameQuery && pageItems?.length === 0 ? [] : kindFiltered, [nameQuery, pageItems, kindFiltered]);

  const forceOpen = filter.length > 0;

  /** Total row count (unfiltered) for the empty-state check. */
  const total = useMemo<number>(() => {
    return tree ? countTreeNodes(tree) : 0;
  }, [tree]);

  const resourceCounts = useMemo(
    () => countResourceKinds(scopedTree ?? []),
    [scopedTree],
  );
  const duplicateDocumentPaths = useMemo(
    () => findDuplicateDocumentPaths(tree ?? []),
    [tree],
  );
  const kindOptions = useMemo(
    () => [
      {
        value: "all",
        label: "All",
        hint: "All resource types",
      },
      {
        value: "document",
        label: "Documents",
        hint: "Browse documents",
      },
      {
        value: "table",
        label: "Tables",
        hint: "Browse tables",
      },
      {
        value: "file",
        label: "Files",
        hint: "Browse files",
      },
    ],
    [],
  );

  /** Full flattened row list (without cap). */
  const fullRows = useMemo<FlatRow[]>(() => {
    if (!filtered) return [];
    const paths = explorationPath ? new Set([...expanded, explorationPath]) : expanded;
    const rows = flattenVisible(filtered, paths, forceOpen, { collapsedKindGroups, kindLimits, activeSig, previewSize: Infinity });
    // Preserve real collection/group signatures for paging and active-resource
    // reveal, but remove the repeated root row already named by the context bar.
    return explorationPath && filtered[0]?.path === explorationPath
      ? rows.filter(row => row.sig !== `collection:${explorationPath}`).map(row => ({ ...row, depth: Math.max(0, row.depth - 1) }))
      : rows;
  }, [filtered, expanded, forceOpen, collapsedKindGroups, kindLimits, activeSig, explorationPath]);

  /** Progressive global cap prevents a single interaction from mounting an
   * entire multi-thousand-row tree. Per-kind previews handle the common case;
   * this is the final guard for vaults with many expanded collections. */
  const visibleRows = useMemo<FlatRow[]>(() => {
    return fullRows.slice(0, renderLimit);
  }, [fullRows, renderLimit]);
  // If an appended resource page only fills closed branches, the sentinel
  // staying visible is not fresh demand. This also covers a short root list
  // containing a README beside closed Collections. Preserve manual paging.
  const progressOwner = JSON.stringify([filterOwner, archiveScope, kindFilter, collectionScope, explorationPath,
    nameQuery, [...expanded].sort(), [...collapsedKindGroups].sort()]);
  const progressPages = loading || searching ? 0 : pageCount;
  const closedBranch = fullRows.some(row => row.type === "node"
    ? row.node.kind === "collection" && !row.isOpen : row.type === "kind-group" && !row.isOpen);
  const [pageProgress, setPageProgress] = useState({ owner: progressOwner, pages: 0, rows: 0, halted: false });
  let autoHalted = pageProgress.halted;
  if (pageProgress.owner !== progressOwner || progressPages < pageProgress.pages) {
    autoHalted = false;
    setPageProgress({ owner: progressOwner, pages: progressPages, rows: fullRows.length, halted: false });
  } else if (progressPages > pageProgress.pages) {
    autoHalted = !nameQuery && pastCollectionCatalog && pageProgress.pages > 0 && closedBranch
      && fullRows.length <= pageProgress.rows;
    setPageProgress({ owner: progressOwner, pages: progressPages, rows: fullRows.length, halted: autoHalted });
  }

  useEffect(() => {
    setRenderLimit(TREE_RENDER_PAGE);
    setKindLimits(new Map());
  }, [filter, kindFilter, vault, collectionScope, explorationPath]);

  useLayoutEffect(() => {
    const request = explorationFocusRef.current;
    if (!request) return;
    if (request.owner !== filterOwner) { explorationFocusRef.current = null; return; }
    // Restore focus for this interaction only, using controls already present.
    // Never defer it until a later browse/proof can steal an editor's focus.
    explorationFocusRef.current = null;
    const previous = request.previousPath;
    const row = previous ? listRef.current?.querySelector<HTMLElement>(`[data-sig="${cssEscape(`collection:${previous}`)}"]`) : null;
    const target = row ?? explorationBackRef.current ?? filterRowRef.current?.querySelector<HTMLInputElement>('input[type="search"]');
    if (!target || target.closest("[hidden], [inert]")) return;
    target.focus({ preventScroll: true });
    if (row) row.scrollIntoView?.({ block: "nearest" });
  }, [explorationPath, fullRows, filterOwner]);

  useEffect(() => {
    setCollapsedKindGroups(new Set());
  }, [vault]);

  const revealedLocation = useRef<string | null>(null);
  useEffect(() => {
    if (!requestedCollection || loading || revealedLocation.current === locationKey) return;
    const index = fullRows.findIndex(row => row.sig === `collection:${requestedCollection}`);
    if (index < 0) return;
    if (index >= renderLimit) { setRenderLimit(index + 1); return; }
    const row = listRef.current?.querySelector<HTMLElement>(`[data-sig="${cssEscape(`collection:${requestedCollection}`)}"]`);
    if (!row) return;
    row.scrollIntoView?.({ block: "nearest" });
    row.focus({ preventScroll: true });
    revealedLocation.current = locationKey;
  }, [fullRows, requestedCollection, locationKey, loading, renderLimit]);

  const toggleKindGroup = useCallback((parentPath: string, kind: ResourceKind) => {
    const key = kindGroupKey(parentPath, kind);
    setCollapsedKindGroups((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  const showMoreKind = useCallback((parentPath: string, kind: ResourceKind) => {
    const key = kindGroupKey(parentPath, kind);
    setKindLimits((current) => {
      const next = new Map(current);
      next.set(
        key,
        (current.get(key) ?? RESOURCE_PREVIEW_SIZE) + RESOURCE_PAGE_SIZE,
      );
      return next;
    });
  }, []);

  const focusAt = useCallback((i: number) => {
    const clamped = Math.max(0, Math.min(i, visibleRows.length - 1));
    const sig = visibleRows[clamped]?.sig;
    if (!sig) return;
    listRef.current
      ?.querySelector<HTMLElement>(`[data-sig="${cssEscape(sig)}"]`)
      ?.focus();
  }, [visibleRows]);

  const typeaheadRef = useRef<{ buffer: string; t: number | null }>({ buffer: "", t: null });

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (visibleRows.length === 0) return;
      const focused = (document.activeElement as HTMLElement | null)?.dataset?.sig ?? null;
      const idx = focused ? visibleRows.findIndex((r) => r.sig === focused) : -1;
      // Row actions and portalled menus own their own keys. The optional
      // navigation shortcuts apply only while a primary row has focus.
      if (!focused || e.nativeEvent.isComposing || e.nativeEvent.keyCode === 229) return;

      switch (e.key) {
        case "ArrowDown": e.preventDefault(); focusAt((idx < 0 ? -1 : idx) + 1); return;
        case "ArrowUp":   e.preventDefault(); focusAt((idx < 0 ? 1 : idx) - 1); return;
        case "Home":      e.preventDefault(); focusAt(0); return;
        case "End":       e.preventDefault(); focusAt(visibleRows.length - 1); return;
        case "PageDown":  e.preventDefault(); focusAt((idx < 0 ? 0 : idx) + PAGE_SIZE); return;
        case "PageUp":    e.preventDefault(); focusAt((idx < 0 ? 0 : idx) - PAGE_SIZE); return;
        case "ArrowRight":
        case "ArrowLeft": {
          if (idx < 0) return;
          const row = visibleRows[idx];
          if (row.type === "kind-group") {
            if (e.key === "ArrowRight" && !row.isOpen) {
              toggleKindGroup(row.parentPath, row.kind);
            }
            if (e.key === "ArrowLeft" && row.isOpen) {
              toggleKindGroup(row.parentPath, row.kind);
            }
            if (e.key === "ArrowRight" && row.isOpen && visibleRows[idx + 1]?.depth > row.depth) focusAt(idx + 1);
            if (e.key === "ArrowLeft" && !row.isOpen) {
              const parent = visibleRows.slice(0, idx).findLastIndex(candidate => candidate.depth < row.depth);
              if (parent >= 0) focusAt(parent);
            }
            e.preventDefault();
            return;
          }
          const isCollection = row.type === "node" && row.node.kind === "collection";
          const isOpen = isCollection && (forceOpen || expanded.has(row.node.path));
          if (e.key === "ArrowRight" && isCollection && !isOpen) toggle(row.node.path);
          else if (e.key === "ArrowRight" && isOpen && visibleRows[idx + 1]?.depth > row.depth) focusAt(idx + 1);
          if (e.key === "ArrowLeft" && isOpen) toggle(row.node.path);
          else if (e.key === "ArrowLeft") {
            const parent = visibleRows.slice(0, idx).findLastIndex(candidate => candidate.depth < row.depth);
            if (parent >= 0) focusAt(parent);
          }
          e.preventDefault();
          return;
        }
      }

      // Typeahead
      if (e.key.length === 1 && !e.metaKey && !e.ctrlKey && !e.altKey) {
        const ta = typeaheadRef.current;
        if (ta.t != null) clearTimeout(ta.t);
        ta.buffer += e.key.toLowerCase();
        ta.t = window.setTimeout(() => { ta.buffer = ""; ta.t = null; }, TYPEAHEAD_TIMEOUT_MS);

        const start = idx < 0 ? 0 : idx;
        const match = findNextByPrefix(visibleRows, start, ta.buffer);
        if (match >= 0) {
          e.preventDefault();
          focusAt(match);
        }
      }
    },
    [
      visibleRows,
      focusAt,
      forceOpen,
      expanded,
      toggle,
      toggleKindGroup,
    ],
  );

  const headBtn = cn(
    "inline-flex shrink-0 items-center justify-center rounded-[var(--radius-sm)] text-foreground-muted hover:bg-surface-hover hover:text-link transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset disabled:cursor-default disabled:opacity-50",
    focused ? "h-9 w-9 max-sm:h-11 max-sm:w-11 pointer-coarse:h-11 pointer-coarse:w-11" : "h-8 w-8",
  );
  const refreshControl = <button type="button" onClick={() => refetch()} disabled={loading || refreshing}
    aria-label={focused ? "Refresh content" : "Refresh collections"} title={focused ? "Refresh content" : "Refresh collections"} className={headBtn}>
    <RefreshCw className={cn(focused ? "h-3.5 w-3.5" : "h-3 w-3", (loading || refreshing) && "animate-spin")} aria-hidden />
  </button>;
  const filterControl = <RailFilterToggle compact={focused} label={focused ? "Filter content" : "Filter collections"} open={filtersOpen} count={activeFilterCount} controls={filtersId} onClick={() => setFiltersOpen(value => !value)} />;
  const creationCollection = explorationPath && explorationRoot && !isReservedCollection(explorationPath) ? explorationPath : "";
  const createControl = canWrite && !checkingAccess && (!explorationPath || (explorationRoot && !isReservedCollection(explorationPath))) && <RootCreateMenu triggerClassName={headBtn}
    onCreateDocument={() => openCreateDocument(creationCollection ? { collection: creationCollection } : undefined)} onUploadFile={() => openUpload(creationCollection)} onCreateTable={() => openTableCreate(creationCollection)} onCreateCollection={() => openCreate(creationCollection || null)} />;

  return (
    <aside
      className={cn("flex min-h-0 flex-col h-full overflow-hidden text-sm", focused ? "bg-navigation" : "bg-surface")}
      aria-label={`${vault} collections`}
    >
      {!focused && <RailIdentity slot="collection-identity-header">
        <span className="flex min-w-0 flex-1 items-center gap-2 px-2 text-sm font-semibold text-foreground" title={collectionContext || "All collections"}>
          <Folder className="h-4 w-4 shrink-0 text-link" aria-hidden />
          <span className="truncate">{collectionContext?.split("/").filter(Boolean).at(-1) || "All collections"}</span>
        </span>
        {onCollapse && <RailCollapseButton collapsed={false} label="Collapse collections" onClick={onCollapse} />}
      </RailIdentity>}
      <RailManagement label="Collections" slot="collection-management-row" workspace={focused} leading={focused ? <VaultSidebarBackLink /> : undefined}>
        {!focused && refreshControl}{filterControl}{createControl}
      </RailManagement>

      <div ref={filterRowRef} data-slot="collection-filter-row" className={cn("shrink-0", !focused && "flex min-h-10 flex-col justify-center gap-1.5 border-b border-border px-2 py-1")}>
        {focused ? <RailSearchToolbar search={<RailFilterField compact label="Filter resources" placeholder="Find content" value={filter} onChange={setFilter} />} />
          : <RailFilterField label="Filter resources" value={filter} onChange={setFilter} />}
        {explorationPath && !checkingAccess && <CollectionExplorerContext path={explorationPath} trail={explorationTrail}
          backRef={explorationBackRef} onNavigate={path => exploreCollection(path, true)} />}
        <div id={filtersId} hidden={!filtersOpen} className={cn("space-y-2 pb-1", focused && "px-2 pb-2")}>
        <div className="space-y-1">
        <span className="text-xs text-foreground-muted">Collection scope</span>
        <div className="flex min-w-0 items-center gap-1">
          <CollectionScopePicker
            key={filterOwner}
            compact={focused}
            value={collectionScope}
            onValueChange={changeCollectionScope}
            options={collectionOptions}
            describedBy={filterHelpId}
          />
          {collectionScope && <button type="button" aria-label="Clear collection scope" title={explorationPath ? "Search this collection" : "Search all collections"} className={headBtn} onClick={() => { changeCollectionScope(""); focusResourceFilter(); }}><X className="h-3.5 w-3.5" aria-hidden /></button>}
        </div>
        <p id={filterHelpId} className={collectionScope ? "px-1 text-xs leading-relaxed text-foreground-muted" : "sr-only"}>Filter names{collectionScope ? " · Includes sub-collections" : explorationPath ? " in this collection and sub-collections" : " in all collections"}</p>
        </div>
        <label className="block space-y-1 text-xs text-foreground-muted"><span>Document state</span>
        <SelectMenu
          value={archiveScope}
          onValueChange={changeArchiveScope}
          aria-label="Collection document state"
          className={cn("bg-background px-2 py-1 text-xs", focused ? "h-9 max-sm:h-11 pointer-coarse:h-11" : "h-8")}
          options={[
            { value: "unarchived", label: "Current documents", hint: "Includes drafts; files and tables remain visible" },
            { value: "archived", label: "Archived documents", hint: "Documents only, in their original Collections" },
            { value: "all", label: "All documents", hint: "Includes archived documents, files, and tables" },
          ]}
        />
        </label>
        <label className="block space-y-1 text-xs text-foreground-muted"><span>Resource type</span>
            <SelectMenu
              value={archiveScope === "archived" ? "document" : kindFilter}
              onValueChange={(value) =>
                setKindFilter(value as ResourceKind | "all")
              }
              options={kindOptions}
              aria-label="Resource type"
              disabled={archiveScope === "archived"}
              className={cn("bg-background px-2 text-xs", focused ? "h-9 max-sm:h-11 pointer-coarse:h-11" : "h-8")}
            />
        </label>
        </div>
        {activeFilterCount > 0 && <div className={cn("flex items-start justify-between gap-2 py-2 text-xs text-foreground-muted", focused && "px-3")}>
          <span className="min-w-0 break-words">{focused
            ? [filter.trim() && `Name: ${filter}`, collectionScope && `Collection: ${collectionScope}`, archiveScope !== "unarchived" && (archiveScope === "archived" ? "Archived documents" : "Includes archived"), archiveScope !== "archived" && kindFilter !== "all" && KIND_LABEL[kindFilter]].filter(Boolean).join(" · ")
            : `${activeFilterCount} active ${activeFilterCount === 1 ? "filter" : "filters"}`}</span>
          <button type="button" onClick={() => {
            changeArchiveScope("unarchived"); setKindFilter("all"); changeCollectionScope("");
            if (focused) setFilter("");
            focusResourceFilter();
          }} className={cn("shrink-0 rounded-[var(--radius-sm)] text-link focus-visible:ring-2 focus-visible:ring-ring", focused && "min-h-9 max-sm:min-h-11 pointer-coarse:min-h-11")}>Reset filters</button>
        </div>}
      </div>

      <div
        ref={listRef}
        role={focused ? "navigation" : "tree"}
        aria-label={`${vault} explorer`}
        aria-busy={loading || refreshing || loadingMore || searching || undefined}
        onKeyDown={onKeyDown}
        className={cn("min-h-0 flex-1 overflow-y-auto rail-scroll", focused && "overscroll-contain px-1.5 pb-3")}
      >
        {loading && <VaultExplorerLoading focused={focused} />}
        {showingPreviousScope && (
            <p role="status" className="px-3 py-2 text-xs text-foreground-muted">Previous view shown{refreshing ? " while updating…" : ". Refresh to try again."}</p>
        )}
        {error && <Alert variant="destructive" className="m-2">{error}</Alert>}
        {paginationUnsupported && <Alert variant="warning" className="m-2">This server did not confirm bounded paging. Update the backend to browse large Vaults in bounded requests.</Alert>}
        {!loading && (searching || (nameQuery && !complete && visibleRows.length === 0)) && <InlineLoadingState label="Searching content" size="sm" className="px-3 py-2" />}
        {unsupported && (
          <Alert variant="warning" className="m-2">
            This server does not support this document state view yet. Update the server or{" "}
            <button type="button" onClick={() => changeArchiveScope("unarchived")} className="underline focus-visible:ring-2 focus-visible:ring-ring">show current documents</button>.
          </Alert>
        )}
        {!loading && !refreshing && !error && !unsupported && explorationPath && !explorationRoot && <div role="status" className="px-3 py-4 text-xs leading-relaxed text-foreground-muted">
          {explorationProof.error ? "Could not verify this collection." : explorationProof.missing ? "This collection is unavailable. Go back to a parent or choose All collections." : "Checking collection…"}
          {explorationProof.error && <Button variant="ghost" size="sm" onClick={explorationProof.refetch}>Retry</Button>}
        </div>}
        {!loading && !refreshing && !searching && !error && !unsupported && complete && (!explorationPath || explorationRoot) && !nameQuery && (total === 0 || (explorationPath && !explorationRoot?.children?.length) || (archiveScope === "archived" && resourceCounts.document === 0)) && (
          <div className="px-3 py-4 text-xs leading-relaxed text-foreground-muted" role="status">
            {archiveScope === "archived" ? `No archived documents in this ${collectionScope || explorationPath ? "Collection" : "Vault"}.` : explorationPath ? "This collection is empty." : "No collections yet — the tree fills in with your first document."}
          </div>
        )}
        {!loading && !refreshing && !searching && !error && !unsupported && complete && (total > 0 || nameQuery) && visibleRows.length === 0 && (nameQuery || !explorationPath || Boolean(explorationRoot?.children?.length)) && (nameQuery || !(archiveScope === "archived" && resourceCounts.document === 0)) && (
          <div className="coord px-3 py-2" role="status">
            No resources match these filters.
            {collectionScope && <button type="button" onClick={() => { changeCollectionScope(""); focusResourceFilter(); }} className="mt-2 block text-link underline focus-visible:ring-2 focus-visible:ring-ring">{explorationPath ? "Search this collection" : "Search all collections"}</button>}
          </div>
        )}

        {!loading && !unsupported &&
          visibleRows.map((row) => {
            if (row.type === "kind-group") {
              return (
                <KindGroupRow
                  key={row.sig}
                  row={row}
                  disclosure={focused}
                  countComplete={complete}
                  onToggle={() => toggleKindGroup(row.parentPath, row.kind)}
                />
              );
            }
            if (row.type === "more") {
              return (
                <KindMoreRow
                  key={row.sig}
                  row={row}
                  disclosure={focused}
                  onShowMore={() => showMoreKind(row.parentPath, row.kind)}
                />
              );
            }
            return (
              <TreeRow
                key={row.sig}
                node={row.node}
                disclosure={focused}
                depth={row.depth}
                sig={row.sig}
                isOpen={row.isOpen}
                isActive={row.sig === activeSig}
                vault={vault}
                onToggle={toggle}
                onSearchCollection={(node) => { changeCollectionScope(node.path); setFiltersOpen(true); focusResourceFilter(); }}
                onExploreCollection={focused && !checkingAccess ? node => exploreCollection(node.path) : undefined}
                canWrite={canWrite}
                canAdmin={canAdmin}
                showDocumentDisambiguator={duplicateDocumentPaths.has(row.node.path)}
                onCreateDoc={(node) =>
                  openCreateDocument({ collection: node.path })
                }
                onUploadFile={(node) => openUpload(node.path)}
                onCreateTable={(node) => openTableCreate(node.path)}
                onCreateSubCollection={(node) => openCreate(node.path)}
                onOpenDetails={(node) => {
                  const counts = countCollectionResources(node);
                  setDetailsTarget({
                    path: node.path,
                    summary: node.raw?.summary,
                    counts,
                    countsComplete: inventoryComplete,
                    editable: canWrite && !isReservedCollection(node.path),
                  });
                }}
                onDeleteCollection={archiveScope === "archived" || showingPreviousScope ? undefined : (node) => {
                  const counts = countCollectionResources(node);
                  setDeleteTarget({
                    path: node.path,
                    docCount: counts.documents,
                    tableCount: counts.tables,
                    fileCount: counts.files,
                    subCollectionCount: countSubCollections(node),
                    countsComplete: inventoryComplete,
                  });
                }}
                onDeleteResource={(node) => {
                  setResourceDeleteTarget({
                    kind: node.kind as DeletableResourceKind,
                    name: node.name,
                    ref: node.path,
                    rowCount: typeof node.raw?.row_count === "number" ? node.raw.row_count : undefined,
                    active: `${node.kind}:${node.path}` === activeSig,
                  });
                }}
              />
            );
          })}

        <AutoLoadMore hasMore={fullRows.length > visibleRows.length || hasMore} loading={loadingMore}
          autoPaused={autoHalted && fullRows.length === visibleRows.length}
          paused={loading || refreshing || searching || unsupported}
          error={Boolean(error)} requestKey={`${filterOwner}:${archiveScope}:${kindFilter}:${collectionScope}:${explorationPath}:${nameQuery}:${renderLimit}:${pageCount}`}
          label="Load more content" loadingLabel={nameQuery && !visibleRows.length ? "" : "Loading more content"}
          className="px-3" onLoadMore={() => fullRows.length > visibleRows.length ? setRenderLimit(current => current + TREE_RENDER_PAGE) : loadMore()} />

      </div>

      {focused && <div data-slot="collection-refresh-footer" className="shrink-0 border-t border-border px-2 py-1">
        <button type="button" aria-label="Refresh content" onClick={() => refetch()} disabled={loading || refreshing}
          className="flex min-h-9 w-full items-center rounded-[var(--radius-sm)] px-2 text-left text-xs text-foreground-muted hover:bg-surface-hover hover:text-link focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring disabled:cursor-default disabled:opacity-60 max-sm:min-h-11 pointer-coarse:min-h-11">
          {loading || refreshing ? "Updating content…" : "Refresh content"}
        </button>
      </div>}

      {/* Mutation dialogs. Mounted unconditionally so portals stay
          stable across renders; visibility is fully driven by the
          `open` prop and the optional `deleteTarget` payload. */}
      <CreateCollectionDialog
        vault={vault}
        open={createOpen}
        onOpenChange={(o) => {
          setCreateOpen(o);
          // Clear the cached parent on close so a subsequent root-create
          // (via the header or bottom-of-section button) doesn't inherit
          // a stale parent prefix.
          if (!o) setCreateParentPath(null);
        }}
        initialPath={createParentPath ?? undefined}
        onCreated={() => {
          handleMutation();
        }}
      />
      <DeleteCollectionDialog
        vault={vault}
        path={deleteTarget?.path ?? ""}
        docCount={deleteTarget?.docCount ?? 0}
        tableCount={deleteTarget?.tableCount ?? 0}
        fileCount={deleteTarget?.fileCount ?? 0}
        subCollectionCount={deleteTarget?.subCollectionCount ?? 0}
        countsComplete={deleteTarget?.countsComplete ?? false}
        open={deleteTarget !== null}
        onOpenChange={(o) => {
          if (!o) setDeleteTarget(null);
        }}
        onDeleted={() => {
          handleMutation();
        }}
      />
      {resourceDeleteTarget && (
        <ResourceDeleteDialog
          open
          onOpenChange={(next) => {
            if (!next) setResourceDeleteTarget(null);
          }}
          kind={resourceDeleteTarget.kind}
          name={resourceDeleteTarget.name}
          rowCount={resourceDeleteTarget.rowCount}
          onConfirm={async () => {
            if (resourceDeleteTarget.kind === "document") {
              await deleteDocument(vault, resourceDeleteTarget.ref);
            } else if (resourceDeleteTarget.kind === "file") {
              await deleteVaultFile(vault, resourceDeleteTarget.ref);
            } else {
              await deleteVaultTable(vault, resourceDeleteTarget.ref);
            }
            handleMutation();
            if (resourceDeleteTarget.active) navigate(`/vault/${vault}`);
          }}
        />
      )}
      <CollectionDetailsDialog
        vault={vault}
        path={detailsTarget?.path ?? ""}
        summary={detailsTarget?.summary}
        counts={detailsTarget?.counts ?? { documents: 0, tables: 0, files: 0 }}
        countsComplete={detailsTarget?.countsComplete ?? false}
        editable={detailsTarget?.editable ?? false}
        open={detailsTarget !== null}
        onOpenChange={(next) => {
          if (!next) setDetailsTarget(null);
        }}
        onUpdated={() => {
          handleMutation();
        }}
      />
      <FileUploadDialog
        open={uploadCollection !== null}
        onOpenChange={(next) => {
          if (!next) setUploadCollection(null);
        }}
        vault={vault}
        initialCollection={uploadCollection ?? ""}
        returnFocusRef={mutationTriggerRef}
        onUploaded={(file) => {
          handleMutation();
          const parsed = parseFileUri(file.uri);
          if (parsed) {
            navigate(`/vault/${vault}/file/${encodeURIComponent(parsed.id)}`);
          }
        }}
      />
      <TableCreateDialog
        open={tableCollection !== null}
        onOpenChange={(next) => {
          if (!next) setTableCollection(null);
        }}
        vault={vault}
        initialCollection={tableCollection ?? ""}
        returnFocusRef={mutationTriggerRef}
        onCreated={(tableName) => {
          handleMutation();
          navigate(`/vault/${vault}/table/${encodeURIComponent(tableName)}`);
        }}
      />
    </aside>
  );
}

function CollectionScopePicker({ value, options, onValueChange, describedBy, compact = false }: {
  value: string;
  options: { value: string; label: string }[];
  onValueChange: (value: string) => void;
  describedBy: string;
  compact?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(-1);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const id = useId();
  const matches = options.filter(option => option.label.toLowerCase().includes(query.trim().toLowerCase()));
  const select = (next: string) => { onValueChange(next); setOpen(false); };
  useEffect(() => {
    listRef.current?.querySelector<HTMLElement>(`[data-option-index="${active}"]`)?.scrollIntoView?.({ block: "nearest" });
  }, [active]);
  return <Popover open={open} onOpenChange={next => { setOpen(next); setQuery(""); setActive(-1); }}>
    <PopoverTrigger asChild><button type="button" aria-label="Collection search scope" aria-describedby={describedBy} title={value || "All collections"}
      className={cn("flex min-w-0 flex-1 items-center gap-2 rounded-[var(--radius-sm)] border border-border bg-surface px-2 text-left text-xs text-foreground hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset", compact ? "h-9 max-sm:h-11 pointer-coarse:h-11" : "h-8")}>
      <FolderSearch className="h-3.5 w-3.5 shrink-0 text-link" aria-hidden />
      <span className="min-w-0 flex-1 truncate">{value || "All collections"}</span>
      <ChevronDown className="h-3.5 w-3.5 shrink-0 text-foreground-muted" aria-hidden />
    </button></PopoverTrigger>
    <PopoverContent align="start" aria-label="Choose a collection" className="w-72 p-1.5" onOpenAutoFocus={event => { event.preventDefault(); inputRef.current?.focus(); }}>
      <Input ref={inputRef} role="combobox" aria-label="Find a collection" aria-expanded={open} aria-controls={id} aria-autocomplete="list" aria-activedescendant={active >= 0 && matches[active] ? `${id}-${active}` : undefined}
        value={query} placeholder="Find a collection…" className={cn("text-sm", compact ? "h-9 max-sm:h-11 max-sm:text-base pointer-coarse:h-11 pointer-coarse:text-base" : "h-8")} onChange={event => { setQuery(event.target.value); setActive(-1); }}
        onKeyDown={event => {
          if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
          if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            setActive(current => matches.length ? event.key === "ArrowDown" ? (current + 1) % matches.length : current <= 0 ? matches.length - 1 : current - 1 : -1);
          } else if (event.key === "Enter") {
            event.preventDefault();
            const option = matches[active < 0 ? 0 : active];
            if (option) select(option.value);
          }
        }} />
      <div ref={listRef} id={id} role="listbox" aria-label="Collection scopes" className="mt-1 max-h-60 overflow-y-auto rail-scroll">
        {matches.map((option, index) => <button key={option.value} type="button" role="option" id={`${id}-${index}`} data-option-index={index} aria-selected={option.value === value} onClick={() => select(option.value)}
          className={cn("flex min-h-9 w-full items-center gap-2 rounded-[var(--radius-sm)] px-2 py-2 text-left text-sm hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset", compact && "max-sm:min-h-11 pointer-coarse:min-h-11", index === active && "bg-surface-hover", option.value === value ? "text-link" : "text-foreground")}>
          <span className="min-w-0 flex-1 break-words">{option.label}</span>{option.value === value && <Check className="h-4 w-4 shrink-0" aria-hidden />}
        </button>)}
      </div>
      {matches.length === 0 && <p role="status" className="px-2 py-3 text-xs text-foreground-muted">No collections match. Try another name.</p>}
    </PopoverContent>
  </Popover>;
}

function VaultExplorerLoading({ focused = false }: { focused?: boolean }) {
  return (
    <LoadingState label="Loading collections" className="space-y-1 px-2 py-2">
      {[0, 1, 2, 3, 4, 5].map((item) => (
        <div key={item} className={cn("flex items-center gap-2 px-1", focused ? "h-9 max-sm:h-11 pointer-coarse:h-11" : "h-8")} style={{ paddingLeft: `${4 + (item % 3) * 12}px` }}>
          <Skeleton className="h-4 w-4 shrink-0 rounded-[var(--radius-sm)]" />
          <Skeleton className={item % 2 === 0 ? "h-3 w-28 rounded-[var(--radius-sm)]" : "h-3 w-20 rounded-[var(--radius-sm)]"} />
        </div>
      ))}
    </LoadingState>
  );
}

/* ── Row renderer (memoized on primitives) ────────────────────────────────── */

interface RowProps {
  node: TreeNode;
  disclosure?: boolean;
  depth: number;
  sig: string;
  isOpen: boolean;
  isActive: boolean;
  vault: string;
  onToggle: (path: string) => void;
  onSearchCollection: (node: TreeNode) => void;
  onExploreCollection?: (node: TreeNode) => void;
  /** Writer+ unlocks per-row destructive affordances (collection rows
   *  only for now). Readers see the row unchanged. */
  canWrite?: boolean;
  /** Admin+ is required for physical table deletion. */
  canAdmin?: boolean;
  /** Show a secondary file identifier only when sibling document titles collide. */
  showDocumentDisambiguator?: boolean;
  /** Fired when the user clicks the trash icon on a collection row.
   *  Parent decides which dialog to open and seeds it with counts. */
  onDeleteCollection?: (node: TreeNode) => void;
  /** Open the matching document/file/table deletion flow. */
  onDeleteResource?: (node: TreeNode) => void;
  /** Open the collection metadata and resource-count inspector. */
  onOpenDetails?: (node: TreeNode) => void;
  /** Fired when the user clicks the `+` (new sub-collection) icon on a
   *  collection row. Parent opens the create dialog with this node's
   *  path prefilled as the parent. */
  onCreateSubCollection?: (node: TreeNode) => void;
  /** Fired when the user clicks the doc icon on a collection row. Parent
   *  routes to the new-document page with this collection prefilled. */
  onCreateDoc?: (node: TreeNode) => void;
  /** Open the file uploader with this collection preselected. */
  onUploadFile?: (node: TreeNode) => void;
  /** Open the table builder with this collection preselected. */
  onCreateTable?: (node: TreeNode) => void;
}

/** Row tools overlay the full-width title surface without taking a flex slot. */
function explorerRowActions(active: boolean) {
  return cn(
    "absolute right-1 top-1/2 z-10 inline-flex h-8 w-8 -translate-y-1/2 items-center justify-center rounded-[var(--radius-sm)] bg-transparent text-foreground-muted opacity-100 pointer-events-auto transition-[opacity,color] hover:bg-transparent hover:text-link focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring max-sm:right-0 max-sm:h-11 max-sm:w-11 pointer-coarse:right-0 pointer-coarse:h-11 pointer-coarse:w-11",
    !active && "sm:pointer-fine:pointer-events-none sm:pointer-fine:opacity-0",
    "group-hover/explorer-row:pointer-events-auto group-hover/explorer-row:opacity-100 group-focus-within/explorer-row:pointer-events-auto group-focus-within/explorer-row:opacity-100 data-[state=open]:pointer-events-auto data-[state=open]:opacity-100 data-[state=open]:bg-transparent data-[state=open]:text-link pointer-coarse:pointer-events-auto pointer-coarse:opacity-100",
  );
}

const TreeRow = memo(function TreeRow({
  node, disclosure, depth, sig, isOpen, isActive, vault, onToggle, onSearchCollection, onExploreCollection, canWrite, canAdmin, showDocumentDisambiguator, onDeleteCollection, onDeleteResource, onOpenDetails, onCreateSubCollection, onCreateDoc, onUploadFile, onCreateTable,
}: RowProps) {
  const indent = { paddingLeft: `${depth * 12 + (disclosure ? 8 : 12)}px` };

  if (node.kind === "collection") {
    const ChevronIcon = isOpen ? ChevronDown : ChevronRight;
    // The reserved `overview` collection holds the vault guide. The backend
    // rejects writes into it, so the tree shows it read-only (locked, no row
    // actions) rather than offering affordances that would 403.
    const isReserved = isReservedCollection(node.path);
    const containsTables = countCollectionResources(node).tables > 0;
    return (
      <div
        role={disclosure ? undefined : "treeitem"}
        aria-expanded={disclosure ? undefined : isOpen}
        aria-level={disclosure ? undefined : depth + 1}
        aria-selected={disclosure ? undefined : isActive}
        aria-current={!disclosure && isActive ? "page" : undefined}
        className={cn("group group/explorer-row relative flex items-stretch max-sm:min-h-11 pointer-coarse:min-h-11",
          disclosure && "rounded-[var(--radius-sm)]",
          isActive ? "bg-surface-selected text-surface-selected-foreground" : "hover:bg-surface-hover focus-within:bg-surface-hover has-[[data-state=open]]:bg-surface-hover")}
      >
        <button
          type="button"
          data-sig={sig}
          aria-expanded={disclosure ? isOpen : undefined}
          aria-current={disclosure && isActive ? "page" : undefined}
          onClick={() => onToggle(node.path)}
          style={indent}
          className={cn("flex min-h-9 min-w-0 flex-1 items-center gap-1.5 py-1.5 text-left transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring cursor-pointer max-sm:min-h-11 pointer-coarse:min-h-11",
            disclosure && "gap-2 rounded-[var(--radius-sm)]",
            onOpenDetails ? "pr-10 max-sm:pr-12 pointer-coarse:pr-12" : "pr-2")}
        >
          <ChevronIcon
            className={cn("shrink-0 text-foreground-muted group-hover:text-link transition-colors", disclosure ? "h-4 w-4" : "h-3 w-3")}
            aria-hidden
          />
          <span className="min-w-0 flex-1">
            <span className="flex min-w-0 items-center gap-1">
              <span
                title={node.raw?.summary ? `${node.name} — ${node.raw.summary}` : node.name}
                className={cn("block min-w-0 flex-1 truncate font-medium", disclosure ? "text-ui" : "tracking-tight text-[13px]")}
              >
                {node.name}
              </span>
              {isReserved && (
                <span
                  title="System collection — managed via vault settings"
                  className="inline-flex shrink-0 items-center"
                  style={{ color: "var(--color-primary)" }}
                >
                  <Lock className="h-3 w-3" aria-hidden />
                  <span className="sr-only">System collection</span>
                </span>
              )}
            </span>
          </span>
        </button>
        {onOpenDetails && (
          <CollectionActionsMenu
            vault={vault}
            node={node}
            editable={Boolean(canWrite && !isReserved)}
            active={isActive}
            onSearch={() => onSearchCollection(node)}
            onExplore={onExploreCollection ? () => onExploreCollection(node) : undefined}
            onOpenDetails={() => onOpenDetails(node)}
            onCreateDoc={
              !isReserved && canWrite && onCreateDoc
                ? () => onCreateDoc(node)
                : undefined
            }
            onUploadFile={
              !isReserved && canWrite && onUploadFile
                ? () => onUploadFile(node)
                : undefined
            }
            onCreateTable={
              !isReserved && canWrite && onCreateTable
                ? () => onCreateTable(node)
                : undefined
            }
            onCreateSubCollection={
              !isReserved && canWrite && onCreateSubCollection
                ? () => onCreateSubCollection(node)
                : undefined
            }
            onDelete={
              !isReserved &&
              canWrite &&
              (canAdmin || !containsTables) &&
              onDeleteCollection
                ? () => onDeleteCollection(node)
                : undefined
            }
          />
        )}
      </div>
    );
  }

  const href = leafHref(vault, node);
  const isSkill = node.kind === "document" && (node.raw?.doc_type === "skill" || node.path === VAULT_SKILL_PATH);
  const LeafIcon = isSkill ? Sparkles : node.kind === "document" ? FileText : node.kind === "table" ? Table : Paperclip;
  // Tint the leaf icon by resource kind from the categorical ramp (the same
  // doc=cat-1 / table=cat-3 / file=cat-4 mapping the Home + overview rows use),
  // so a doc vs a table vs a file is a colour at a glance — not just an icon
  // shape in a flat list. (Was text-accent ORANGE for table/file/skill, which
  // was off-system + spent the one-marquee-orange budget.) Skill = teal.
  const leafTone = isSkill ? "var(--color-primary)" : recentTone(node.kind);
  const canDeleteResource =
    !isSkill &&
    Boolean(onDeleteResource) &&
    (node.kind === "table" ? Boolean(canAdmin) : Boolean(canWrite));
  const documentDisambiguator =
    node.kind === "document" && showDocumentDisambiguator
      ? compactDocumentIdentifier(node.path)
      : null;

  return (
    <div
      role="none"
      className={cn("group group/explorer-row relative flex items-stretch max-sm:min-h-11 pointer-coarse:min-h-11",
        disclosure && "rounded-[var(--radius-sm)]",
        documentDisambiguator ? "min-h-11" : "min-h-9",
        isActive ? "bg-surface-selected text-surface-selected-foreground" : "hover:bg-surface-hover focus-within:bg-surface-hover has-[[data-state=open]]:bg-surface-hover")}
    >
      <Link
        to={href}
        data-sig={sig}
        role={disclosure ? undefined : "treeitem"}
        aria-level={disclosure ? undefined : depth + 1}
        aria-current={isActive ? "page" : undefined}
        aria-selected={disclosure ? undefined : isActive}
        style={indent}
        className={cn("flex min-h-9 min-w-0 flex-1 items-center gap-1.5 py-1.5 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring max-sm:min-h-11 pointer-coarse:min-h-11",
          disclosure && "gap-2 rounded-[var(--radius-sm)]",
          canDeleteResource ? "pr-10 max-sm:pr-12 pointer-coarse:pr-12" : "pr-2",
          isActive && "font-medium")}
      >
        {/* Tinted icon chip — the same kind-swatch grammar Home + the vault
            overview use, so a doc vs table vs file is pre-attentive here too
            (was a bare 12px glyph, the one surface that dropped the chip). */}
        <span
          className="inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-[var(--radius-sm)]"
          style={{
            color: leafTone,
            backgroundColor: `color-mix(in srgb, ${leafTone} 12%, transparent)`,
          }}
          aria-hidden
        >
          <LeafIcon className="h-2.5 w-2.5" aria-hidden />
        </span>
        {/* The chip is aria-hidden, so name kind to assistive tech in words
            (otherwise a SR user hears the bare title with no type). */}
        <span className="sr-only">
          {node.kind === "document"
            ? isSkill
              ? "Skill: "
              : "Document: "
            : node.kind === "table"
              ? "Table: "
              : "File: "}
        </span>
        <span className="min-w-0 flex-1">
          {node.kind === "document"
            ? <OverflowRevealTitle title={node.name} className="text-ui group-hover:text-link" />
            : <span title={node.name} className={cn("block truncate group-hover:text-link", disclosure ? "text-ui" : "text-[13px]")}>{node.name}</span>}
          {documentDisambiguator && (
            <span className="mt-0.5 block truncate font-mono text-xs text-foreground-muted">
              <span className="sr-only">File identifier: </span>
              {documentDisambiguator}
            </span>
          )}
        </span>
        {isSkill && <SkillBadge defined className="ml-auto shrink-0" />}
        {node.kind === "document" && node.raw?.status === "archived" && (
          <span title="Archived" className="shrink-0 text-foreground-muted">
            <Archive className="h-3 w-3" aria-hidden />
            <span className="sr-only">Archived</span>
          </span>
        )}
      </Link>
      {canDeleteResource && (
        <ResourceActionsMenu
          resourceName={node.name}
          deleteLabel={`Delete ${node.kind}`}
          onDelete={() => onDeleteResource?.(node)}
          side="right"
          align="start"
          className={explorerRowActions(isActive)}
        />
      )}
    </div>
  );
});

/* ── Resource-kind disclosure rows ───────────────────────────────────────── */

const KIND_LABEL: Partial<Record<NodeKind, string>> = {
  document: "Documents",
  table: "Tables",
  file: "Files",
};

function KindGroupRow({
  row,
  onToggle,
  disclosure,
  countComplete = true,
}: {
  row: FlatKindGroupRow;
  onToggle: () => void;
  disclosure?: boolean;
  countComplete?: boolean;
}) {
  const label = KIND_LABEL[row.kind] ?? "Resources";
  const ChevronIcon = row.isOpen ? ChevronDown : ChevronRight;
  return (
    <button
      type="button"
      role={disclosure ? undefined : "treeitem"}
      aria-level={disclosure ? undefined : row.depth + 1}
      aria-expanded={row.isOpen}
      aria-label={`${label}, ${countComplete ? "" : "at least "}${row.count.toLocaleString()} ${row.count === 1 ? "item" : "items"}`}
      data-sig={row.sig}
      onClick={onToggle}
      style={{ paddingLeft: `${row.depth * 12 + (disclosure ? 8 : 12)}px` }}
      className={cn("group flex w-full items-center px-2 py-1 text-left text-xs font-medium transition-colors hover:bg-surface-hover hover:text-link focus:bg-surface-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring", disclosure ? "min-h-9 gap-2 rounded-[var(--radius-sm)] text-foreground-muted max-sm:min-h-11 pointer-coarse:min-h-11" : "min-h-8 gap-1.5 border-y border-border bg-surface-2 text-foreground")}
    >
      <ChevronIcon
        className={cn("shrink-0 text-foreground-muted transition-colors group-hover:text-link", disclosure ? "h-4 w-4" : "h-3 w-3")}
        aria-hidden
      />
      <span className="min-w-0 flex-1 truncate">{label}</span>
      <span className="shrink-0 tabular-nums text-foreground-muted">
        {row.count.toLocaleString()}{!countComplete && "+"}
      </span>
    </button>
  );
}

function KindMoreRow({
  row,
  onShowMore,
  disclosure,
}: {
  row: FlatMoreRow;
  onShowMore: () => void;
  disclosure?: boolean;
}) {
  const remaining = row.totalCount - row.visibleCount;
  const next = Math.min(RESOURCE_PAGE_SIZE, remaining);
  const label = KIND_LABEL[row.kind]?.toLowerCase() ?? "resources";
  return (
    <button
      type="button"
      role={disclosure ? undefined : "treeitem"}
      aria-level={disclosure ? undefined : row.depth + 1}
      data-sig={row.sig}
      onClick={onShowMore}
      style={{ paddingLeft: `${row.depth * 12 + (disclosure ? 8 : 12)}px` }}
      className={cn("flex min-h-9 w-full items-center pr-2 py-1.5 text-left text-xs font-medium text-link transition-colors hover:bg-surface-hover hover:text-link-hover focus:bg-surface-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring", disclosure && "rounded-[var(--radius-sm)] max-sm:min-h-11 pointer-coarse:min-h-11")}
    >
      Show {next.toLocaleString()} more {label}
      <span className="ml-auto pl-2 tabular-nums text-foreground-muted">
        {remaining.toLocaleString()} left
      </span>
    </button>
  );
}

/* ── Helpers ──────────────────────────────────────────────────────────────── */

function findCollectionTrail(nodes: TreeNode[], path: string, ancestors: TreeNode[] = []): TreeNode[] {
  if (!path) return [];
  for (const node of nodes) {
    if (node.kind !== "collection") continue;
    const trail = [...ancestors, node];
    if (node.path === path) return trail;
    if (isInCollection(path, node.path)) {
      const match = findCollectionTrail(node.children ?? [], path, trail);
      if (match.length) return match;
    }
  }
  return [];
}

function isInCollection(path: string, root: string) {
  return path === root || path.startsWith(`${root}/`);
}

function sharedCollectionPath(a: string, b: string) {
  const left = a.split("/");
  const right = b.split("/");
  let length = 0;
  while (length < Math.min(left.length, right.length) && left[length] === right[length]) length++;
  return left.slice(0, length).join("/");
}

/** Count every descendant collection node under `node` (excluding the
 *  node itself). Used by the delete dialog's cascade-mode banner so the
 *  user sees how many sub-collections will be removed. The tree client
 *  already nests collections under their parent so this is a pure
 *  local walk — no server round-trip. */
function countSubCollections(node: TreeNode): number {
  if (!node.children) return 0;
  let n = 0;
  for (const c of node.children) {
    if (c.kind === "collection") {
      n += 1 + countSubCollections(c);
    }
  }
  return n;
}

/**
 * Duplicate titles are only ambiguous among document siblings. Keep ordinary
 * rows single-line; reveal a compact technical discriminator only for the
 * small set that cannot otherwise be told apart.
 */
function findDuplicateDocumentPaths(nodes: TreeNode[]): Set<string> {
  const duplicates = new Set<string>();

  const visitSiblings = (siblings: TreeNode[]) => {
    const byTitle = new Map<string, TreeNode[]>();
    for (const node of siblings) {
      if (node.kind !== "document") continue;
      const key = documentTitleKey(node.name);
      const group = byTitle.get(key) ?? [];
      group.push(node);
      byTitle.set(key, group);
    }
    for (const group of byTitle.values()) {
      if (group.length < 2) continue;
      group.forEach((node) => duplicates.add(node.path));
    }
    siblings
      .filter((node) => node.kind === "collection")
      .forEach((node) => visitSiblings(node.children ?? []));
  };

  visitSiblings(nodes);
  return duplicates;
}

function compactDocumentIdentifier(path: string): string {
  const fileName = path.split("/").pop() || path;
  const stem = fileName.replace(/\.md$/i, "");
  const generatedSuffix = stem.match(/^(.*)-([0-9a-f]{8,32})$/i);
  if (!generatedSuffix) return stem;
  return `${generatedSuffix[1]} · ${generatedSuffix[2].slice(0, 4)}`;
}

function countCollectionResources(node: TreeNode): CollectionResourceCounts {
  const counts: CollectionResourceCounts = { documents: 0, tables: 0, files: 0 };
  const visit = (current: TreeNode) => {
    if (current.kind === "document") counts.documents += 1;
    else if (current.kind === "table") counts.tables += 1;
    else if (current.kind === "file") counts.files += 1;
    current.children?.forEach(visit);
  };
  node.children?.forEach(visit);
  return counts;
}

function countTreeNodes(nodes: TreeNode[]): number {
  let total = 0;
  const visit = (node: TreeNode) => {
    total += 1;
    node.children?.forEach(visit);
  };
  nodes.forEach(visit);
  return total;
}

function countResourceKinds(nodes: TreeNode[]): Record<ResourceKind, number> {
  const counts: Record<ResourceKind, number> = {
    document: 0,
    table: 0,
    file: 0,
  };
  const visit = (node: TreeNode) => {
    if (node.kind !== "collection") counts[node.kind] += 1;
    node.children?.forEach(visit);
  };
  nodes.forEach(visit);
  return counts;
}

function CollectionActionsMenu({
  vault,
  node,
  editable,
  active,
  onSearch,
  onExplore,
  onOpenDetails,
  onCreateDoc,
  onUploadFile,
  onCreateTable,
  onCreateSubCollection,
  onDelete,
}: {
  vault: string;
  node: TreeNode;
  editable: boolean;
  active: boolean;
  onSearch: () => void;
  onExplore?: () => void;
  onOpenDetails: () => void;
  onCreateDoc?: () => void;
  onUploadFile?: () => void;
  onCreateTable?: () => void;
  onCreateSubCollection?: () => void;
  onDelete?: () => void;
}) {
  const user = useCurrentUser();
  const triggerId = useId();
  const requestedAction = useRef<"search" | "explore" | null>(null);
  const shortcuts = useWorkspaceShortcuts(user?.user_id);
  const shortcut = { kind: "collection" as const, vault, path: node.path, title: node.name };
  const pinned = shortcuts.some(item => workspaceShortcutKey(item) === workspaceShortcutKey(shortcut));
  const [pinError, setPinError] = useState(false);
  const PinIcon = pinned ? PinOff : Pin;
  const hasWriteActions = Boolean(
    onCreateDoc || onUploadFile || onCreateTable || onCreateSubCollection || onDelete,
  );
  const itemClass =
    "flex cursor-pointer select-none items-center gap-2 rounded-[var(--radius-sm)] px-2.5 py-2 text-sm text-foreground outline-none data-[highlighted]:bg-surface-hover";

  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button
          id={triggerId}
          type="button"
          title={`Collection actions for ${node.path}`}
          aria-label={`Collection actions for ${node.path}`}
          className={explorerRowActions(active)}
        >
          <MoreHorizontal className="h-4 w-4" aria-hidden />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="start"
          side="right"
          sideOffset={4}
          onCloseAutoFocus={event => {
            if (!requestedAction.current) return;
            event.preventDefault();
            const action = requestedAction.current;
            requestedAction.current = null;
            if (action === "explore") onExplore?.();
            else onSearch();
          }}
          className="z-[var(--z-popover)] min-w-48 overflow-hidden rounded-[var(--radius-md)] border border-border bg-surface p-1 shadow-md"
        >
          {onExplore && <DropdownMenu.Item onSelect={() => { requestedAction.current = "explore"; }} className={itemClass}>
            <FolderOpen className="h-4 w-4 text-foreground-muted" aria-hidden />Explore from here
          </DropdownMenu.Item>}
          <DropdownMenu.Item onSelect={() => { requestedAction.current = "search"; }} className={itemClass}>
            <FolderSearch className="h-4 w-4 text-foreground-muted" aria-hidden />
            Search in collection
          </DropdownMenu.Item>
          <DropdownMenu.Separator className="my-1 h-px bg-border" />
          {hasWriteActions && (
            <DropdownMenu.Label className="px-2.5 pb-1 pt-1 text-xs font-medium text-foreground-muted">
              Add to collection
            </DropdownMenu.Label>
          )}
          {onCreateDoc && (
            <DropdownMenu.Item onSelect={onCreateDoc} className={itemClass}>
              <FilePlus className="h-4 w-4 text-foreground-muted" aria-hidden />
              New document
            </DropdownMenu.Item>
          )}
          {onUploadFile && (
            <DropdownMenu.Item onSelect={onUploadFile} className={itemClass}>
              <Upload className="h-4 w-4 text-foreground-muted" aria-hidden />
              Upload file
            </DropdownMenu.Item>
          )}
          {onCreateTable && (
            <DropdownMenu.Item onSelect={onCreateTable} className={itemClass}>
              <Table className="h-4 w-4 text-foreground-muted" aria-hidden />
              New table
            </DropdownMenu.Item>
          )}
          {onCreateSubCollection && (
            <DropdownMenu.Item onSelect={onCreateSubCollection} className={itemClass}>
              <FolderPlus className="h-4 w-4 text-foreground-muted" aria-hidden />
              New sub-collection
            </DropdownMenu.Item>
          )}
          {hasWriteActions && <DropdownMenu.Separator className="my-1 h-px bg-border" />}
          <DropdownMenu.Item onSelect={onOpenDetails} className={itemClass}>
            <Info className="h-4 w-4 text-foreground-muted" aria-hidden />
            {editable ? "View or edit summary" : "View details"}
          </DropdownMenu.Item>
          {user && <DropdownMenu.Item className={itemClass} onSelect={event => {
            if (!toggleWorkspaceShortcut(user.user_id, shortcut)) { event.preventDefault(); setPinError(true); }
            else setPinError(false);
          }}>
            <PinIcon className="h-4 w-4 text-foreground-muted" aria-hidden />
            {pinned ? "Unpin from workspace" : "Pin to workspace"}
          </DropdownMenu.Item>}
          {pinError && <p role="alert" className="max-w-56 px-2.5 py-2 text-xs text-destructive">Could not save. Check browser storage or remove a pin (limit 30).</p>}
          {onDelete && (
            <>
              <DropdownMenu.Separator className="my-1 h-px bg-border" />
              <DropdownMenu.Item
                onSelect={onDelete}
                className={`${itemClass} text-destructive data-[highlighted]:bg-destructive-soft`}
              >
                <Trash2 className="h-4 w-4" aria-hidden />
                Delete collection
              </DropdownMenu.Item>
            </>
          )}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

function RootCreateMenu({
  triggerClassName,
  onCreateDocument,
  onUploadFile,
  onCreateTable,
  onCreateCollection,
}: {
  triggerClassName: string;
  onCreateDocument: () => void;
  onUploadFile: () => void;
  onCreateTable: () => void;
  onCreateCollection: () => void;
}) {
  const trigger = useRef<HTMLButtonElement>(null);
  const launching = useRef(false);
  const launch = (action: () => void) => {
    launching.current = true;
    trigger.current?.focus();
    action();
  };
  const itemClass =
    "flex cursor-pointer select-none items-center gap-2 rounded-[var(--radius-sm)] px-2.5 py-2 text-sm text-foreground outline-none data-[highlighted]:bg-surface-hover";
  return (
    <DropdownMenu.Root modal={false} onOpenChange={open => { if (open) launching.current = false; }}>
      <DropdownMenu.Trigger asChild>
        <Button
          ref={trigger}
          type="button"
          variant="ghost"
          size="icon"
          aria-label="Add content"
          title="Add"
          className={triggerClassName}
        >
          <Plus className="h-3.5 w-3.5" aria-hidden />
        </Button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={4}
          onCloseAutoFocus={event => { if (launching.current) event.preventDefault(); }}
          className="z-[var(--z-popover)] min-w-48 overflow-hidden rounded-[var(--radius-md)] border border-border bg-surface p-1 shadow-md"
        >
          <DropdownMenu.Item onSelect={() => launch(onCreateDocument)} className={itemClass}>
            <FilePlus className="h-4 w-4 text-foreground-muted" aria-hidden />
            New document
          </DropdownMenu.Item>
          <DropdownMenu.Item onSelect={() => launch(onUploadFile)} className={itemClass}>
            <Upload className="h-4 w-4 text-foreground-muted" aria-hidden />
            Upload file
          </DropdownMenu.Item>
          <DropdownMenu.Item onSelect={() => launch(onCreateTable)} className={itemClass}>
            <Table className="h-4 w-4 text-foreground-muted" aria-hidden />
            New table
          </DropdownMenu.Item>
          <DropdownMenu.Item onSelect={() => launch(onCreateCollection)} className={itemClass}>
            <FolderPlus className="h-4 w-4 text-foreground-muted" aria-hidden />
            New collection
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

function findNextByPrefix(rows: FlatRow[], start: number, prefix: string): number {
  const n = rows.length;
  for (let i = 0; i < n; i++) {
    const k = (start + i) % n;
    if (rowLabel(rows[k]).toLowerCase().startsWith(prefix)) return k;
  }
  return -1;
}

function rowLabel(row: FlatRow): string {
  if (row.type === "node") return row.node.name;
  if (row.type === "kind-group") return KIND_LABEL[row.kind] ?? "Resources";
  return `Show more ${KIND_LABEL[row.kind] ?? "resources"}`;
}

function cssEscape(s: string): string {
  if (typeof CSS !== "undefined" && typeof CSS.escape === "function") return CSS.escape(s);
  return s.replace(/([^\w-])/g, "\\$1");
}
