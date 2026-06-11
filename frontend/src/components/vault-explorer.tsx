import { Link, useLocation, useNavigate } from "react-router-dom";
import { Fragment, memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronDown,
  ChevronRight,
  FilePlus,
  FileText,
  FolderPlus,
  Paperclip,
  Sparkles,
  Table,
  Trash2,
} from "lucide-react";
import { Alert } from "@/components/ui/alert";
import { SkillBadge } from "@/components/ui/skill-badge";
import { useVaultTree, useExpandedPaths, type NodeKind, type TreeNode } from "@/hooks/use-vault-tree";
import { useVaultRefresh } from "@/contexts/vault-refresh-context";
import {
  activePathFromRoute,
  countDocs,
  filterTree,
  flattenVisible,
  leafHref,
  type FlatRow,
} from "@/lib/tree-route";
import { getVaultInfo } from "@/lib/api";
import { recentTone } from "@/lib/recent";
import { CreateCollectionDialog } from "@/components/create-collection-dialog";
import { DeleteCollectionDialog } from "@/components/delete-collection-dialog";

const PAGE_SIZE = 10;
const TYPEAHEAD_TIMEOUT_MS = 500;
/** Soft cap on rendered rows to keep first paint fast on very large
 *  vaults. Users can opt into rendering all rows. */
const TREE_RENDER_CAP = 300;

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
}

export function VaultExplorer({
  vault,
  onMutation,
  onRefetchReady,
}: VaultExplorerProps) {
  const { tree, loading, error, refetch } = useVaultTree(vault);
  const navigate = useNavigate();
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
  const [filter, setFilter] = useState("");
  const [uncapped, setUncapped] = useState(false);
  const listRef = useRef<HTMLDivElement | null>(null);

  // Role-gated affordances. We fetch the role on mount/vault-change rather
  // than threading it through props because the existing parent
  // (`VaultShell`) doesn't have it cached and adding a redundant fetch in
  // the shell would slow first paint. document.tsx uses the same pattern.
  const [vaultRole, setVaultRole] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    getVaultInfo(vault)
      .then((d) => {
        if (alive) setVaultRole(d?.role || null);
      })
      .catch(() => {
        if (alive) setVaultRole(null);
      });
    return () => {
      alive = false;
    };
  }, [vault]);
  const canWrite =
    vaultRole === "writer" || vaultRole === "admin" || vaultRole === "owner";

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
    fileCount: number;
    subCollectionCount: number;
  } | null>(null);

  const activeSig = useMemo(() => activePathFromRoute(pathname, tree), [pathname, tree]);

  useEffect(() => {
    if (activeSig) {
      const path = activeSig.split(":").slice(1).join(":");
      revealAncestorsOf(path);
    }
  }, [activeSig, revealAncestorsOf]);

  const filtered = useMemo(() => {
    if (!tree) return tree;
    const q = filter.trim().toLowerCase();
    return q ? filterTree(tree, q) : tree;
  }, [tree, filter]);

  const forceOpen = filter.length > 0;

  /** Total row count (unfiltered) for the empty-state check. */
  const total = useMemo<number>(() => {
    if (!tree) return 0;
    let c = 0;
    for (const n of tree) {
      if (n.kind === "collection") c += countDocs(n);
      else c += 1;
    }
    return c;
  }, [tree]);

  /** Full flattened row list (without cap). */
  const fullRows = useMemo<FlatRow[]>(
    () => (filtered ? flattenVisible(filtered, expanded, forceOpen) : []),
    [filtered, expanded, forceOpen],
  );

  /** Capped rows — soft cap applied unless the user opts in, or unless
   *  a filter is active (the user is hunting for a specific match and a
   *  cap could hide it). */
  const visibleRows = useMemo<FlatRow[]>(() => {
    if (filter || uncapped) return fullRows;
    return fullRows.length > TREE_RENDER_CAP
      ? fullRows.slice(0, TREE_RENDER_CAP)
      : fullRows;
  }, [fullRows, uncapped, filter]);

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
          if (row.node.kind !== "collection") return;
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
    [visibleRows, focusAt, forceOpen, expanded, toggle],
  );

  return (
    <aside
      className="flex flex-col h-full overflow-hidden text-sm bg-background"
      aria-label={`${vault} collections`}
    >
      <div className="border-b border-border px-2 py-1.5 shrink-0">
        <input
          type="search"
          placeholder="Filter in vault…"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          className="w-full h-9 px-2.5 rounded-[var(--radius-md)] bg-surface border border-border text-xs text-foreground placeholder:text-foreground-muted focus:outline-none focus:border-primary focus-visible:ring-2 focus-visible:ring-ring transition-colors"
          aria-label="Filter tree"
        />
      </div>

      <div
        ref={listRef}
        role="tree"
        aria-label={`${vault} explorer`}
        onKeyDown={onKeyDown}
        className="flex-1 overflow-y-auto"
      >
        {loading && <div className="coord px-3 py-3" role="status" aria-live="polite">— Loading —</div>}
        {error && <Alert variant="destructive" className="m-2">{error}</Alert>}
        {!loading && !error && total === 0 && (
          <div className="coord px-3 py-3" role="status">— Empty —</div>
        )}
        {!loading && !error && total > 0 && visibleRows.length === 0 && filter && (
          <div className="coord px-3 py-1.5" role="status">— No matches —</div>
        )}

        {!loading && !error &&
          visibleRows.map((r) => (
            <Fragment key={r.sig}>
              {r.kindHeader && <KindGroupLabel kind={r.kindHeader} depth={r.depth} />}
              <TreeRow
              node={r.node}
              depth={r.depth}
              sig={r.sig}
              isOpen={r.isOpen}
              isActive={r.sig === activeSig}
              vault={vault}
              onToggle={toggle}
              canWrite={canWrite}
              onCreateDoc={(node) =>
                navigate(
                  `/vault/${vault}/doc/new?collection=${encodeURIComponent(node.path)}`,
                )
              }
              onCreateSubCollection={(node) => openCreate(node.path)}
              onDeleteCollection={(node) =>
                setDeleteTarget({
                  path: node.path,
                  docCount: countDocs(node),
                  // TODO: the in-memory tree currently flattens files
                  // to vault root (see use-vault-tree.ts buildTree),
                  // so a collection's true file count isn't available
                  // client-side. The server's 409 response will
                  // surface the real count if the user picks
                  // empty-mode on a collection that secretly has files.
                  fileCount: 0,
                  // The tree already nests sub-collections under their
                  // parent, so this is a pure local walk. Critical for
                  // the nested-parent case: when the user clicks trash
                  // on a synthesized parent, this drives the dialog into
                  // cascade mode and shows the strengthened banner.
                  subCollectionCount: countSubCollections(node),
                })
              }
              />
            </Fragment>
          ))}

        {!loading && !error && !filter && !uncapped &&
          fullRows.length > visibleRows.length && (
            <button
              type="button"
              onClick={() => setUncapped(true)}
              className="w-full coord px-3 py-2 text-left hover:bg-surface-hover hover:text-link transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background cursor-pointer"
            >
              ↓ Show {fullRows.length - visibleRows.length} more
            </button>
          )}

        {/* Always-visible "+ NEW COLLECTION" affordance at the bottom of
            the tree. Pairs the discoverable bottom-of-list button with
            the row-hover sub-collection `+`. Writer+ only. */}
        {!loading && !error && canWrite && (
          <button
            type="button"
            onClick={() => openCreate(null)}
            className="w-full inline-flex items-center gap-1.5 px-3 py-1.5 text-left text-foreground-muted hover:bg-surface-hover hover:text-link transition-colors cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            <FolderPlus className="h-3 w-3" aria-hidden />
            <span className="coord">+ New collection</span>
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
    </aside>
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
  /** Fired when the user clicks the trash icon on a collection row.
   *  Parent decides which dialog to open and seeds it with counts. */
  onDeleteCollection?: (node: TreeNode) => void;
  /** Fired when the user clicks the `+` (new sub-collection) icon on a
   *  collection row. Parent opens the create dialog with this node's
   *  path prefilled as the parent. */
  onCreateSubCollection?: (node: TreeNode) => void;
  /** Fired when the user clicks the doc icon on a collection row. Parent
   *  routes to the new-document page with this collection prefilled. */
  onCreateDoc?: (node: TreeNode) => void;
}

const TreeRow = memo(function TreeRow({
  node, depth, sig, isOpen, isActive, vault, onToggle, canWrite, onDeleteCollection, onCreateSubCollection, onCreateDoc,
}: RowProps) {
  const indent = { paddingLeft: `${depth * 12 + 12}px` };

  if (node.kind === "collection") {
    const count = countDocs(node);
    const ChevronIcon = isOpen ? ChevronDown : ChevronRight;
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
          className={`flex-1 min-w-0 flex items-center gap-1.5 pr-2 py-1.5 min-h-9 text-left transition-colors hover:bg-surface-hover focus:bg-surface-hover focus:outline-none cursor-pointer ${
            isActive ? "bg-surface-selected text-surface-selected-foreground" : ""
          }`}
        >
          <ChevronIcon
            className="h-3 w-3 shrink-0 text-foreground-muted group-hover:text-link transition-colors"
            aria-hidden
          />
          <span title={node.name} className="truncate font-medium tracking-tight text-[13px]">{node.name}</span>
          {count > 0 && <span className="coord ml-auto shrink-0 tabular-nums">{count}</span>}
        </button>
        {canWrite && onCreateDoc && (
          <button
            type="button"
            onClick={(e) => {
              // Stop the row's expand-toggle from also firing.
              e.stopPropagation();
              onCreateDoc(node);
            }}
            title={`New document in ${node.path}`}
            aria-label={`Create document in ${node.path}`}
            className="shrink-0 px-2 inline-flex items-center justify-center text-foreground-muted hover:text-link transition-colors cursor-pointer opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 focus:opacity-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            <FilePlus className="h-3 w-3" aria-hidden />
          </button>
        )}
        {canWrite && onCreateSubCollection && (
          <button
            type="button"
            onClick={(e) => {
              // Stop the row's expand-toggle from also firing.
              e.stopPropagation();
              onCreateSubCollection(node);
            }}
            title={`New sub-collection in ${node.path}`}
            aria-label={`Create sub-collection in ${node.path}`}
            className="shrink-0 px-2 inline-flex items-center justify-center text-foreground-muted hover:text-link transition-colors cursor-pointer opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 focus:opacity-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            <FolderPlus className="h-3 w-3" aria-hidden />
          </button>
        )}
        {canWrite && onDeleteCollection && (
          <button
            type="button"
            onClick={(e) => {
              // Stop the row's expand-toggle from also firing.
              e.stopPropagation();
              onDeleteCollection(node);
            }}
            title={`Delete ${node.path}`}
            aria-label={`Delete collection ${node.path}`}
            className="shrink-0 px-2 inline-flex items-center justify-center text-foreground-muted hover:text-destructive transition-colors cursor-pointer opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 focus:opacity-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            <Trash2 className="h-3 w-3" aria-hidden />
          </button>
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

  return (
    <Link
      to={href}
      data-sig={sig}
      role="treeitem"
      aria-level={depth + 1}
      aria-current={isActive ? "page" : undefined}
      aria-selected={isActive}
      style={indent}
      className={`flex items-center gap-1.5 pr-2 py-1.5 min-h-9 group transition-colors hover:bg-surface-hover focus:bg-surface-hover focus:outline-none ${
        isActive ? "bg-surface-selected text-surface-selected-foreground border-l-2 border-primary -ml-[2px]" : ""
      }`}
    >
      {/* Tinted icon chip — the same kind-swatch grammar Home + the vault
          overview use, so a doc vs table vs file is pre-attentive here too
          (was a bare 12px glyph, the one surface that dropped the chip). */}
      <span
        className="inline-flex h-4 w-4 items-center justify-center rounded-[var(--radius-sm)] shrink-0"
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
      <span title={node.name} className="truncate min-w-0 text-[13px] group-hover:text-link">{node.name}</span>
      {isSkill && <SkillBadge defined className="ml-auto shrink-0" />}
    </Link>
  );
});

/* ── Kind-group label (Documents / Tables / Files) ────────────────────────── */

const KIND_LABEL: Partial<Record<NodeKind, string>> = {
  document: "Documents",
  table: "Tables",
  file: "Files",
};

/**
 * A muted, non-interactive group heading rendered above the first row of each
 * leaf-kind run when a collection mixes kinds (Sentence case per the design
 * system). `role="presentation"` + no `data-sig` keeps it out of the tree's
 * roving keyboard nav and the aria-tree row set — it's purely a visual divider.
 */
function KindGroupLabel({ kind, depth }: { kind: NodeKind; depth: number }) {
  const label = KIND_LABEL[kind];
  if (!label) return null;
  return (
    <div
      role="presentation"
      style={{ paddingLeft: `${depth * 12 + 12}px` }}
      className="coord px-2 pt-2 pb-0.5 select-none"
    >
      {label}
    </div>
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

function findNextByPrefix(rows: FlatRow[], start: number, prefix: string): number {
  const n = rows.length;
  for (let i = 0; i < n; i++) {
    const k = (start + i) % n;
    if (rows[k].node.name.toLowerCase().startsWith(prefix)) return k;
  }
  return -1;
}

function cssEscape(s: string): string {
  if (typeof CSS !== "undefined" && typeof CSS.escape === "function") return CSS.escape(s);
  return s.replace(/([^\w-])/g, "\\$1");
}

