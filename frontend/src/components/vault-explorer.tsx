import { Link, useLocation, useNavigate } from "react-router-dom";
import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import {
  ChevronDown,
  ChevronRight,
  FilePlus,
  FileText,
  FolderPlus,
  Info,
  Lock,
  MoreHorizontal,
  Paperclip,
  PanelLeftClose,
  Plus,
  RefreshCw,
  Search,
  Sparkles,
  Table,
  Trash2,
  Upload,
} from "lucide-react";
import { Alert } from "@/components/ui/alert";
import { LoadingState } from "@/components/ui/loading-state";
import { Skeleton } from "@/components/ui/skeleton";
import { SkillBadge } from "@/components/ui/skill-badge";
import { useVaultTree, useExpandedPaths, type NodeKind, type TreeNode } from "@/hooks/use-vault-tree";
import { useVaultRefresh } from "@/contexts/vault-refresh-context";
import { useOpenDocumentCreateDialog } from "@/contexts/document-create-dialog-context";
import {
  activePathFromRoute,
  filterTree,
  filterTreeByKind,
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
} from "@/lib/api";
import { recentTone } from "@/lib/recent";
import { isReservedCollection } from "@/lib/skill";
import { CreateCollectionDialog } from "@/components/create-collection-dialog";
import {
  CollectionDetailsDialog,
  type CollectionResourceCounts,
} from "@/components/collection-details-dialog";
import { DeleteCollectionDialog } from "@/components/delete-collection-dialog";
import { FileUploadDialog } from "@/components/file-upload-dialog";
import { TableCreateDialog } from "@/components/table-create-dialog";
import { SelectMenu } from "@/components/ui/select-menu";
import { parseFileUri } from "@/lib/uri";
import { ResourceActionsMenu } from "@/components/resource-actions-menu";
import {
  ResourceDeleteDialog,
  type DeletableResourceKind,
} from "@/components/resource-delete-dialog";
import { ROLE_RANK, type Role } from "@/lib/roles";
import { documentTitleKey } from "@/lib/document-title-conflict";

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
}

export function VaultExplorer({
  vault,
  onMutation,
  onRefetchReady,
  onCollapse,
}: VaultExplorerProps) {
  const { tree, loading, refreshing, error, refetch } = useVaultTree(vault);
  const openCreateDocument = useOpenDocumentCreateDialog();
  const refreshCtx = useVaultRefresh();
  // Prefer the explicit prop; otherwise fall back to context. This lets
  // tests render the explorer with no provider and still wire mutation
  // refreshes for production.
  const handleMutation = onMutation ?? refreshCtx.refetchTree;

  // Publish our refetch upward exactly once per change so the parent can
  // forward it to a context provider. `onRefetchReady` should itself be
  // stable; calling it on every render is fine — React deduplicates the
  // setState inside the parent if the function identity matches.
  useEffect(() => {
    onRefetchReady?.(refetch);
  }, [onRefetchReady, refetch]);
  const { expanded, toggle, revealAncestorsOf } = useExpandedPaths(vault);
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const [filter, setFilter] = useState("");
  const [kindFilter, setKindFilter] = useState<ResourceKind | "all">("all");
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
  } | null>(null);
  const [detailsTarget, setDetailsTarget] = useState<{
    path: string;
    summary?: string | null;
    counts: CollectionResourceCounts;
    editable: boolean;
  } | null>(null);
  const [uploadCollection, setUploadCollection] = useState<string | null>(null);
  const [tableCollection, setTableCollection] = useState<string | null>(null);
  const [resourceDeleteTarget, setResourceDeleteTarget] = useState<{
    kind: DeletableResourceKind;
    name: string;
    ref: string;
    rowCount: number;
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

  const activeSig = useMemo(() => activePathFromRoute(pathname, tree), [pathname, tree]);

  useEffect(() => {
    if (activeSig) {
      const path = activeSig.split(":").slice(1).join(":");
      revealAncestorsOf(path);
    }
  }, [activeSig, revealAncestorsOf]);

  const kindFiltered = useMemo(() => {
    if (!tree) return tree;
    return filterTreeByKind(tree, kindFilter);
  }, [tree, kindFilter]);

  const filtered = useMemo(() => {
    if (!kindFiltered) return kindFiltered;
    const q = filter.trim().toLowerCase();
    return q ? filterTree(kindFiltered, q) : kindFiltered;
  }, [kindFiltered, filter]);

  const forceOpen = filter.length > 0;

  /** Total row count (unfiltered) for the empty-state check. */
  const total = useMemo<number>(() => {
    return tree ? countTreeNodes(tree) : 0;
  }, [tree]);

  const resourceCounts = useMemo(
    () => countResourceKinds(tree ?? []),
    [tree],
  );
  const duplicateDocumentPaths = useMemo(
    () => findDuplicateDocumentPaths(tree ?? []),
    [tree],
  );
  const resourceTotal =
    resourceCounts.document + resourceCounts.table + resourceCounts.file;
  const kindOptions = useMemo(
    () => [
      {
        value: "all",
        label: "All",
        hint: `${resourceTotal.toLocaleString()} resources`,
      },
      {
        value: "document",
        label: "Documents",
        hint: `${resourceCounts.document.toLocaleString()} documents`,
      },
      {
        value: "table",
        label: "Tables",
        hint: `${resourceCounts.table.toLocaleString()} tables`,
      },
      {
        value: "file",
        label: "Files",
        hint: `${resourceCounts.file.toLocaleString()} files`,
      },
    ],
    [resourceCounts, resourceTotal],
  );

  /** Full flattened row list (without cap). */
  const fullRows = useMemo<FlatRow[]>(
    () =>
      filtered
          ? flattenVisible(filtered, expanded, forceOpen, {
            collapsedKindGroups,
            kindLimits,
            activeSig,
          })
        : [],
    [filtered, expanded, forceOpen, collapsedKindGroups, kindLimits, activeSig],
  );

  /** Progressive global cap prevents a single interaction from mounting an
   * entire multi-thousand-row tree. Per-kind previews handle the common case;
   * this is the final guard for vaults with many expanded collections. */
  const visibleRows = useMemo<FlatRow[]>(() => {
    return fullRows.slice(0, renderLimit);
  }, [fullRows, renderLimit]);

  useEffect(() => {
    setRenderLimit(TREE_RENDER_PAGE);
    setKindLimits(new Map());
  }, [filter, kindFilter, vault]);

  useEffect(() => {
    setCollapsedKindGroups(new Set());
  }, [vault]);

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
            e.preventDefault();
            return;
          }
          if (row.type !== "node" || row.node.kind !== "collection") return;
          const isOpen = forceOpen || expanded.has(row.node.path);
          if (e.key === "ArrowRight" && !isOpen) toggle(row.node.path);
          if (e.key === "ArrowLeft" && isOpen) toggle(row.node.path);
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

  const headBtn =
    "inline-flex h-7 w-7 items-center justify-center rounded-[var(--radius-sm)] text-foreground-muted hover:bg-surface-hover hover:text-link transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface disabled:cursor-default disabled:opacity-50";

  return (
    <aside
      className="flex flex-col h-full overflow-hidden text-sm bg-surface"
      aria-label={`${vault} collections`}
    >
      <div className="flex h-10 shrink-0 items-center justify-between border-b border-border px-3">
        <span className="coord-ink">Collections</span>
        <div className="flex items-center gap-0.5">
          <button
            type="button"
            onClick={() => refetch()}
            disabled={loading || refreshing}
            aria-label="Refresh collections"
            title="Refresh collections"
            className={headBtn}
          >
            <RefreshCw className={loading || refreshing ? "h-3 w-3 animate-spin" : "h-3 w-3"} aria-hidden />
          </button>
          {canWrite && (
            <RootCreateMenu
              triggerClassName={headBtn}
              onCreateDocument={() => openCreateDocument()}
              onUploadFile={() => openUpload("")}
              onCreateTable={() => openTableCreate("")}
              onCreateCollection={() => openCreate(null)}
            />
          )}
          {onCollapse && (
            <button
              type="button"
              onClick={onCollapse}
              title="Collapse tree (⌘\\)"
              aria-label="Collapse collection tree"
              aria-expanded={true}
              className={headBtn}
            >
              <PanelLeftClose className="h-4 w-4" aria-hidden />
            </button>
          )}
        </div>
      </div>

      {total > 0 && (
        <div className="shrink-0 border-b border-border px-2 py-1.5">
          <div className="grid grid-cols-[minmax(0,1fr)_6.5rem] gap-1.5">
            <div className="relative min-w-0">
              <Search
                className="pointer-events-none absolute left-2 top-1/2 h-3 w-3 -translate-y-1/2 text-foreground-muted"
                aria-hidden
              />
              <input
                type="search"
                placeholder="Filter resources"
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                className="h-8 w-full rounded-[var(--radius-md)] border border-border bg-background pl-6 pr-2 text-xs text-foreground placeholder:text-foreground-muted transition-colors focus:border-primary focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                aria-label="Filter resources"
              />
            </div>
            <SelectMenu
              value={kindFilter}
              onValueChange={(value) =>
                setKindFilter(value as ResourceKind | "all")
              }
              options={kindOptions}
              aria-label="Resource type"
              className="h-8 bg-background px-2 text-xs"
            />
          </div>
        </div>
      )}

      <div
        ref={listRef}
        role="tree"
        aria-label={`${vault} explorer`}
        aria-busy={loading || refreshing || undefined}
        onKeyDown={onKeyDown}
        className="flex-1 overflow-y-auto"
      >
        {loading && <VaultExplorerLoading />}
        {error && <Alert variant="destructive" className="m-2">{error}</Alert>}
        {!loading && total === 0 && (
          <div className="px-3 py-4 text-xs leading-relaxed text-foreground-muted" role="status">
            No collections yet — the tree fills in with your first document.
          </div>
        )}
        {!loading && total > 0 && visibleRows.length === 0 && (
          <div className="coord px-3 py-2" role="status">
            No resources match these filters.
          </div>
        )}

        {!loading &&
          visibleRows.map((row) => {
            if (row.type === "kind-group") {
              return (
                <KindGroupRow
                  key={row.sig}
                  row={row}
                  onToggle={() => toggleKindGroup(row.parentPath, row.kind)}
                />
              );
            }
            if (row.type === "more") {
              return (
                <KindMoreRow
                  key={row.sig}
                  row={row}
                  onShowMore={() => showMoreKind(row.parentPath, row.kind)}
                />
              );
            }
            return (
              <TreeRow
                key={row.sig}
                node={row.node}
                depth={row.depth}
                sig={row.sig}
                isOpen={row.isOpen}
                isActive={row.sig === activeSig}
                vault={vault}
                onToggle={toggle}
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
                    editable: canWrite && !isReservedCollection(node.path),
                  });
                }}
                onDeleteCollection={(node) => {
                  const counts = countCollectionResources(node);
                  setDeleteTarget({
                    path: node.path,
                    docCount: counts.documents,
                    tableCount: counts.tables,
                    fileCount: counts.files,
                    subCollectionCount: countSubCollections(node),
                  });
                }}
                onDeleteResource={(node) => {
                  setResourceDeleteTarget({
                    kind: node.kind as DeletableResourceKind,
                    name: node.name,
                    ref: node.path,
                    rowCount: Number(node.raw?.row_count || 0),
                    active: `${node.kind}:${node.path}` === activeSig,
                  });
                }}
              />
            );
          })}

        {!loading &&
          fullRows.length > visibleRows.length && (
            <button
              type="button"
              onClick={() =>
                setRenderLimit((current) => current + TREE_RENDER_PAGE)
              }
              className="w-full coord px-3 py-2 text-left hover:bg-surface-hover hover:text-link transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background cursor-pointer"
            >
              Show next {Math.min(TREE_RENDER_PAGE, fullRows.length - visibleRows.length)} of{" "}
              {(fullRows.length - visibleRows.length).toLocaleString()}
            </button>
          )}

      </div>

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

function VaultExplorerLoading() {
  return (
    <LoadingState label="Loading collections" className="space-y-1 px-2 py-2">
      {[0, 1, 2, 3, 4, 5].map((item) => (
        <div key={item} className="flex h-8 items-center gap-2 px-1" style={{ paddingLeft: `${4 + (item % 3) * 12}px` }}>
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
  depth: number;
  sig: string;
  isOpen: boolean;
  isActive: boolean;
  vault: string;
  onToggle: (path: string) => void;
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

const TreeRow = memo(function TreeRow({
  node, depth, sig, isOpen, isActive, vault, onToggle, canWrite, canAdmin, showDocumentDisambiguator, onDeleteCollection, onDeleteResource, onOpenDetails, onCreateSubCollection, onCreateDoc, onUploadFile, onCreateTable,
}: RowProps) {
  const indent = { paddingLeft: `${depth * 12 + 12}px` };

  if (node.kind === "collection") {
    const ChevronIcon = isOpen ? ChevronDown : ChevronRight;
    // The reserved `overview` collection holds the vault guide. The backend
    // rejects writes into it, so the tree shows it read-only (locked, no row
    // actions) rather than offering affordances that would 403.
    const isReserved = isReservedCollection(node.path);
    const containsTables = countCollectionResources(node).tables > 0;
    return (
      <div
        role="treeitem"
        aria-expanded={isOpen}
        aria-level={depth + 1}
        aria-selected={isActive}
        aria-current={isActive ? "page" : undefined}
        className="group relative flex items-stretch focus-within:bg-surface-hover"
      >
        <button
          data-sig={sig}
          onClick={() => onToggle(node.path)}
          style={indent}
          className={`flex min-h-11 min-w-0 flex-1 items-center gap-1.5 py-1.5 pr-1 text-left transition-colors hover:bg-surface-hover focus:bg-surface-hover focus:outline-none cursor-pointer ${
            isActive ? "bg-surface-selected text-surface-selected-foreground" : ""
          }`}
        >
          <ChevronIcon
            className="h-3 w-3 shrink-0 text-foreground-muted group-hover:text-link transition-colors"
            aria-hidden
          />
          <span className="min-w-0 flex-1">
            <span className="flex min-w-0 items-center gap-1">
              <span
                title={node.raw?.summary ? `${node.name} — ${node.raw.summary}` : node.name}
                className="block min-w-0 flex-1 truncate font-medium tracking-tight text-[13px]"
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
            node={node}
            editable={Boolean(canWrite && !isReserved)}
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
  const isSkill = node.kind === "document" && node.raw?.doc_type === "skill";
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
      className={`group flex items-stretch focus-within:bg-surface-hover ${documentDisambiguator ? "min-h-11" : "min-h-9"}`}
    >
      <Link
        to={href}
        data-sig={sig}
        role="treeitem"
        aria-level={depth + 1}
        aria-current={isActive ? "page" : undefined}
        aria-selected={isActive}
        style={indent}
        className={`flex min-h-9 min-w-0 flex-1 items-center gap-1.5 py-1.5 pr-1 transition-colors hover:bg-surface-hover focus:bg-surface-hover focus:outline-none ${
          isActive ? "-ml-[2px] border-l-2 border-primary bg-surface-selected text-surface-selected-foreground" : ""
        }`}
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
          <span title={node.name} className="block truncate text-[13px] group-hover:text-link">
            {node.name}
          </span>
          {documentDisambiguator && (
            <span className="mt-0.5 block truncate font-mono text-xs text-foreground-muted">
              <span className="sr-only">File identifier: </span>
              {documentDisambiguator}
            </span>
          )}
        </span>
        {isSkill && <SkillBadge defined className="ml-auto shrink-0" />}
      </Link>
      {canDeleteResource && (
        <ResourceActionsMenu
          resourceName={node.name}
          deleteLabel={`Delete ${node.kind}`}
          onDelete={() => onDeleteResource?.(node)}
          side="right"
          align="start"
          className="h-9 w-9 rounded-none opacity-100 transition-opacity sm:opacity-0 sm:group-hover:opacity-100 sm:group-focus-within:opacity-100"
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
}: {
  row: FlatKindGroupRow;
  onToggle: () => void;
}) {
  const label = KIND_LABEL[row.kind] ?? "Resources";
  const ChevronIcon = row.isOpen ? ChevronDown : ChevronRight;
  return (
    <button
      type="button"
      role="treeitem"
      aria-level={row.depth + 1}
      aria-expanded={row.isOpen}
      aria-label={`${label}, ${row.count.toLocaleString()} ${row.count === 1 ? "item" : "items"}`}
      data-sig={row.sig}
      onClick={onToggle}
      style={{ paddingLeft: `${row.depth * 12 + 12}px` }}
      className="group flex min-h-8 w-full items-center gap-1.5 border-y border-border bg-surface-2 px-2 py-1 text-left text-xs font-medium text-foreground transition-colors hover:bg-surface-hover hover:text-link focus:bg-surface-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
    >
      <ChevronIcon
        className="h-3 w-3 shrink-0 text-foreground-muted transition-colors group-hover:text-link"
        aria-hidden
      />
      <span className="min-w-0 flex-1 truncate">{label}</span>
      <span className="shrink-0 tabular-nums text-foreground-muted">
        {row.count.toLocaleString()}
      </span>
    </button>
  );
}

function KindMoreRow({
  row,
  onShowMore,
}: {
  row: FlatMoreRow;
  onShowMore: () => void;
}) {
  const remaining = row.totalCount - row.visibleCount;
  const next = Math.min(RESOURCE_PAGE_SIZE, remaining);
  const label = KIND_LABEL[row.kind]?.toLowerCase() ?? "resources";
  return (
    <button
      type="button"
      role="treeitem"
      aria-level={row.depth + 1}
      data-sig={row.sig}
      onClick={onShowMore}
      style={{ paddingLeft: `${row.depth * 12 + 12}px` }}
      className="flex min-h-9 w-full items-center pr-2 py-1.5 text-left text-xs font-medium text-link transition-colors hover:bg-surface-hover hover:text-link-hover focus:bg-surface-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
    >
      Show {next.toLocaleString()} more {label}
      <span className="ml-auto pl-2 tabular-nums text-foreground-muted">
        {remaining.toLocaleString()} left
      </span>
    </button>
  );
}

/* ── Helpers ──────────────────────────────────────────────────────────────── */

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
  node,
  editable,
  onOpenDetails,
  onCreateDoc,
  onUploadFile,
  onCreateTable,
  onCreateSubCollection,
  onDelete,
}: {
  node: TreeNode;
  editable: boolean;
  onOpenDetails: () => void;
  onCreateDoc?: () => void;
  onUploadFile?: () => void;
  onCreateTable?: () => void;
  onCreateSubCollection?: () => void;
  onDelete?: () => void;
}) {
  const hasWriteActions = Boolean(
    onCreateDoc || onUploadFile || onCreateTable || onCreateSubCollection || onDelete,
  );
  const itemClass =
    "flex cursor-pointer select-none items-center gap-2 rounded-[var(--radius-sm)] px-2.5 py-2 text-sm text-foreground outline-none data-[highlighted]:bg-surface-hover";

  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button
          type="button"
          title={`Collection actions for ${node.path}`}
          aria-label={`Collection actions for ${node.path}`}
          className="inline-flex min-h-11 w-8 shrink-0 items-center justify-center text-foreground-muted transition-colors hover:bg-surface-hover hover:text-link focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
        >
          <MoreHorizontal className="h-4 w-4" aria-hidden />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="start"
          side="right"
          sideOffset={4}
          className="z-[var(--z-popover)] min-w-48 overflow-hidden rounded-[var(--radius-md)] border border-border bg-surface p-1 shadow-md"
        >
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
  const itemClass =
    "flex cursor-pointer select-none items-center gap-2 rounded-[var(--radius-sm)] px-2.5 py-2 text-sm text-foreground outline-none data-[highlighted]:bg-surface-hover";
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button
          type="button"
          aria-label="Create in vault"
          title="Create in vault"
          className={triggerClassName}
        >
          <Plus className="h-3.5 w-3.5" aria-hidden />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={4}
          className="z-[var(--z-popover)] min-w-48 overflow-hidden rounded-[var(--radius-md)] border border-border bg-surface p-1 shadow-md"
        >
          <DropdownMenu.Label className="px-2.5 pb-1 pt-1 text-xs font-medium text-foreground-muted">
            Create at Vault root
          </DropdownMenu.Label>
          <DropdownMenu.Item onSelect={onCreateDocument} className={itemClass}>
            <FilePlus className="h-4 w-4 text-foreground-muted" aria-hidden />
            New document
          </DropdownMenu.Item>
          <DropdownMenu.Item onSelect={onUploadFile} className={itemClass}>
            <Upload className="h-4 w-4 text-foreground-muted" aria-hidden />
            Upload file
          </DropdownMenu.Item>
          <DropdownMenu.Item onSelect={onCreateTable} className={itemClass}>
            <Table className="h-4 w-4 text-foreground-muted" aria-hidden />
            New table
          </DropdownMenu.Item>
          <DropdownMenu.Item onSelect={onCreateCollection} className={itemClass}>
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
