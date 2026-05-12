import { Link, useLocation } from "react-router-dom";
import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronDown,
  ChevronRight,
  File,
  FileText,
  FolderPlus,
  Network,
  Table,
  Trash2,
} from "lucide-react";
import { useVaultTree, useExpandedPaths, type TreeNode } from "@/hooks/use-vault-tree";
import {
  activePathFromRoute,
  countDocs,
  filterTree,
  flattenVisible,
  leafHref,
  type FlatRow,
} from "@/lib/tree-route";
import { getVaultInfo } from "@/lib/api";
import { CreateCollectionDialog } from "@/components/create-collection-dialog";
import { DeleteCollectionDialog } from "@/components/delete-collection-dialog";

const PAGE_SIZE = 10;
const TYPEAHEAD_TIMEOUT_MS = 500;
/** Soft cap on rendered rows per section to keep first paint fast on
 *  very large vaults. Users can opt into rendering all rows; the
 *  cap is per-vault per-section so opting in for one section doesn't
 *  drag the others. */
const SECTION_RENDER_CAP = 300;

type SectionKind = "documents" | "tables" | "files";

const SECTION_LABEL: Record<SectionKind, string> = {
  documents: "DOCUMENTS",
  tables: "TABLES",
  files: "FILES",
};

/**
 * Left-rail explorer. Partitions the browse tree into three kind-based
 * sections (documents / tables / files) so categories are visually distinct
 * rather than interleaved in one flat list.
 *
 * Keyboard nav still operates across sections as a single focusable list:
 * all visible rows are concatenated in section order (documents → tables →
 * files) for arrow/home/end/typeahead purposes.
 */
export interface VaultExplorerProps {
  vault: string;
  /**
   * Optional callback fired after a successful create/delete-collection
   * mutation. Task 12 will plug a `refetchTree` here so the tree
   * auto-invalidates; for now the parent can omit it and the tree will
   * simply show stale state until reload.
   */
  onMutation?: () => void;
}

export function VaultExplorer({ vault, onMutation }: VaultExplorerProps) {
  const { tree, loading, error } = useVaultTree(vault);
  const { expanded, toggle, revealAncestorsOf } = useExpandedPaths(vault);
  const { pathname } = useLocation();
  const [filter, setFilter] = useState("");
  const [sectionOpen, setSectionOpen] = useSectionCollapseState(vault);
  const [uncapped, setUncapped] = useState<Record<SectionKind, boolean>>({
    documents: false,
    tables: false,
    files: false,
  });
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
  const [deleteTarget, setDeleteTarget] = useState<{
    path: string;
    docCount: number;
    fileCount: number;
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

  /** Partition root-level nodes by kind. Collections + docs → documents;
   *  tables/files become their own flat sections. */
  const sections = useMemo<Record<SectionKind, TreeNode[]>>(() => {
    const s: Record<SectionKind, TreeNode[]> = {
      documents: [],
      tables: [],
      files: [],
    };
    if (!filtered) return s;
    for (const n of filtered) {
      if (n.kind === "collection" || n.kind === "document") s.documents.push(n);
      else if (n.kind === "table") s.tables.push(n);
      else if (n.kind === "file") s.files.push(n);
    }
    return s;
  }, [filtered]);

  /** Cached counts from the unfiltered tree so section headers show the
   *  true size of each kind, not the filtered subset. */
  const counts = useMemo<Record<SectionKind, number>>(() => {
    const c = { documents: 0, tables: 0, files: 0 };
    if (!tree) return c;
    for (const n of tree) {
      if (n.kind === "collection") c.documents += countDocs(n);
      else if (n.kind === "document") c.documents += 1;
      else if (n.kind === "table") c.tables += 1;
      else if (n.kind === "file") c.files += 1;
    }
    return c;
  }, [tree]);

  const fullSectionRows = useMemo<Record<SectionKind, FlatRow[]>>(() => ({
    documents: flattenVisible(sections.documents, expanded, forceOpen),
    tables: flattenVisible(sections.tables, expanded, forceOpen),
    files: flattenVisible(sections.files, expanded, forceOpen),
  }), [sections, expanded, forceOpen]);

  /** Apply the soft cap unless the user has explicitly expanded a
   *  section. We never cap during an active filter — the user is
   *  hunting for a specific match and a cap could hide it. */
  const sectionRows = useMemo<Record<SectionKind, FlatRow[]>>(() => {
    const out: Record<SectionKind, FlatRow[]> = {
      documents: fullSectionRows.documents,
      tables: fullSectionRows.tables,
      files: fullSectionRows.files,
    };
    if (filter) return out;
    for (const k of ["documents", "tables", "files"] as SectionKind[]) {
      if (!uncapped[k] && out[k].length > SECTION_RENDER_CAP) {
        out[k] = out[k].slice(0, SECTION_RENDER_CAP);
      }
    }
    return out;
  }, [fullSectionRows, uncapped, filter]);

  // Concatenated visible rows for keyboard nav / signature matching.
  // Rows in a collapsed section are excluded so keyboard nav skips them.
  // We use the *capped* rows so keyboard nav can't focus a hidden row.
  const visibleRows = useMemo<FlatRow[]>(
    () =>
      (["documents", "tables", "files"] as SectionKind[]).flatMap((k) =>
        sectionOpen[k] ? sectionRows[k] : [],
      ),
    [sectionRows, sectionOpen],
  );

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
    <aside className="flex flex-col h-full overflow-hidden border-r border-border text-sm bg-background">
      <header className="border-b border-border px-3 py-2 flex items-center justify-between gap-2 shrink-0">
        <Link
          to={`/vault/${vault}`}
          className="font-mono text-sm font-semibold truncate text-foreground hover:text-accent focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
        >
          {vault}
        </Link>
        <div className="flex items-center gap-2 shrink-0">
          {canWrite && (
            <button
              type="button"
              onClick={() => setCreateOpen(true)}
              title="New collection"
              aria-label="New collection"
              className="inline-flex items-center gap-1 coord hover:text-accent transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background cursor-pointer"
            >
              <FolderPlus className="h-3 w-3" aria-hidden />
              + COLL
            </button>
          )}
          <Link
            to={`/vault/${vault}/graph`}
            title="Knowledge graph"
            aria-label="Knowledge graph"
            className="inline-flex items-center gap-1 coord hover:text-accent transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            <Network className="h-3 w-3" aria-hidden />
            GRAPH
          </Link>
        </div>
      </header>

      <div className="border-b border-border px-2 py-1.5 shrink-0">
        <input
          type="search"
          placeholder="Filter in vault…"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          className="w-full h-9 px-2 bg-surface border border-border text-xs text-foreground placeholder:text-foreground-muted focus:outline-none focus:border-accent focus-visible:ring-0 transition-colors"
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
        {loading && <div className="coord px-3 py-3">— LOADING —</div>}
        {error && <div className="coord-spark px-3 py-3">⚠ {error}</div>}
        {/* Only show the top-level empty row when the vault has NO items at all
            (distinct from 'everything collapsed' or 'filter matches nothing' —
            those are handled per-section below). */}
        {!loading && !error &&
          counts.documents + counts.tables + counts.files === 0 && (
            <div className="coord px-3 py-3">— EMPTY —</div>
          )}

        {!loading && !error && (["documents", "tables", "files"] as SectionKind[]).map((kind) => {
          const rows = sectionRows[kind];
          const total = counts[kind];
          const open = sectionOpen[kind];
          return (
            <div key={kind}>
              <SectionHeader
                label={SECTION_LABEL[kind]}
                count={total}
                open={open}
                onToggle={() =>
                  setSectionOpen({ ...sectionOpen, [kind]: !open })
                }
              />
              {open && rows.length === 0 && filter && total > 0 && (
                <div className="coord px-3 py-1.5 opacity-60">— NO MATCHES —</div>
              )}
              {open &&
                rows.map((r) => (
                  <TreeRow
                    key={r.sig}
                    node={r.node}
                    depth={r.depth}
                    sig={r.sig}
                    isOpen={r.isOpen}
                    isActive={r.sig === activeSig}
                    vault={vault}
                    onToggle={toggle}
                    canWrite={canWrite}
                    onDeleteCollection={(node) =>
                      setDeleteTarget({
                        path: node.path,
                        docCount: countDocs(node),
                        // TODO: the in-memory tree currently flattens files
                        // to vault root (see use-vault-tree.ts buildTree),
                        // so a collection's true file count isn't available
                        // client-side. The server's 409 response will
                        // surface the real count if the user picks
                        // empty-mode on a collection that secretly has
                        // files. Revisit when the tree carries
                        // file→collection relationships.
                        fileCount: 0,
                      })
                    }
                  />
                ))}
              {open &&
                !filter &&
                !uncapped[kind] &&
                fullSectionRows[kind].length > rows.length && (
                  <button
                    type="button"
                    onClick={() => setUncapped({ ...uncapped, [kind]: true })}
                    className="w-full coord px-3 py-2 text-left hover:bg-surface-muted hover:text-accent transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background cursor-pointer"
                  >
                    ↓ SHOW {fullSectionRows[kind].length - rows.length} MORE
                  </button>
                )}
            </div>
          );
        })}
      </div>

      {/* Mutation dialogs. Mounted unconditionally so portals stay
          stable across renders; visibility is fully driven by the
          `open` prop and the optional `deleteTarget` payload. */}
      <CreateCollectionDialog
        vault={vault}
        open={createOpen}
        onOpenChange={setCreateOpen}
        onCreated={() => {
          onMutation?.();
        }}
      />
      <DeleteCollectionDialog
        vault={vault}
        path={deleteTarget?.path ?? ""}
        docCount={deleteTarget?.docCount ?? 0}
        fileCount={deleteTarget?.fileCount ?? 0}
        open={deleteTarget !== null}
        onOpenChange={(o) => {
          if (!o) setDeleteTarget(null);
        }}
        onDeleted={() => {
          onMutation?.();
        }}
      />
    </aside>
  );
}

function SectionHeader({
  label,
  count,
  open,
  onToggle,
}: {
  label: string;
  count: number;
  open: boolean;
  onToggle: () => void;
}) {
  const empty = count === 0;
  const disabled = empty; // no content to show/hide
  const Chevron = open ? ChevronDown : ChevronRight;
  return (
    <button
      type="button"
      onClick={disabled ? undefined : onToggle}
      disabled={disabled}
      aria-expanded={open}
      aria-controls={`section-${label.toLowerCase()}`}
      className={`w-full flex items-center justify-between px-3 py-1.5 border-t border-border first:border-t-0 bg-surface-muted transition-colors ${
        disabled ? "cursor-default" : "cursor-pointer hover:bg-surface-muted/60"
      } ${empty ? "opacity-60" : ""} focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-surface`}
    >
      <span className="flex items-center gap-1.5">
        {!disabled && (
          <Chevron
            className="h-3 w-3 text-foreground-muted shrink-0"
            aria-hidden
          />
        )}
        <span className={empty ? "coord" : "coord-ink"}>{label}</span>
      </span>
      <span className="coord tabular-nums">[{count}]</span>
    </button>
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
}

const TreeRow = memo(function TreeRow({
  node, depth, sig, isOpen, isActive, vault, onToggle, canWrite, onDeleteCollection,
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
        className="group relative flex items-stretch focus-within:bg-surface-muted/40"
      >
        <button
          data-sig={sig}
          onClick={() => onToggle(node.path)}
          style={indent}
          className={`flex-1 min-w-0 flex items-center gap-1.5 pr-2 py-1 text-left transition-colors hover:bg-surface-muted focus:bg-surface-muted focus:outline-none cursor-pointer ${
            isActive ? "bg-accent/10" : ""
          }`}
        >
          <ChevronIcon
            className="h-3 w-3 shrink-0 text-foreground-muted group-hover:text-accent transition-colors"
            aria-hidden
          />
          <span className="truncate font-medium tracking-tight text-[13px] text-foreground">{node.name}</span>
          {count > 0 && <span className="coord ml-auto shrink-0">{count}</span>}
        </button>
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
            className="shrink-0 px-2 inline-flex items-center justify-center text-foreground-muted hover:text-destructive transition-colors cursor-pointer opacity-0 group-hover:opacity-100 focus:opacity-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            <Trash2 className="h-3 w-3" aria-hidden />
          </button>
        )}
      </div>
    );
  }

  const href = leafHref(vault, node);
  const LeafIcon = node.kind === "document" ? FileText : node.kind === "table" ? Table : File;
  const leafIconColor = node.kind === "document" ? "text-foreground-muted" : "text-accent";

  return (
    <Link
      to={href}
      data-sig={sig}
      role="treeitem"
      aria-level={depth + 1}
      aria-current={isActive ? "page" : undefined}
      style={indent}
      className={`flex items-center gap-1.5 pr-2 py-1 group transition-colors hover:bg-surface-muted focus:bg-surface-muted focus:outline-none ${
        isActive ? "bg-accent/15 border-l-2 border-accent -ml-[2px]" : ""
      }`}
    >
      <LeafIcon
        className={`h-3 w-3 shrink-0 ${leafIconColor} group-hover:text-accent transition-colors`}
        aria-hidden
      />
      <span className="truncate text-[13px] text-foreground group-hover:text-accent">{node.name}</span>
    </Link>
  );
});

/* ── Helpers ──────────────────────────────────────────────────────────────── */

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

/* ── Section collapse state (per-vault, persisted) ────────────────────────── */

type SectionState = Record<SectionKind, boolean>;
const DEFAULT_SECTION_STATE: SectionState = {
  documents: true,
  tables: true,
  files: true,
};

function sectionStorageKey(vault: string) {
  return `akb-explorer-sections:${vault}`;
}

function useSectionCollapseState(
  vault: string,
): [SectionState, (next: SectionState) => void] {
  const [state, setState] = useState<SectionState>(DEFAULT_SECTION_STATE);

  useEffect(() => {
    try {
      const raw = localStorage.getItem(sectionStorageKey(vault));
      if (raw) {
        const parsed = JSON.parse(raw);
        setState({ ...DEFAULT_SECTION_STATE, ...parsed });
      } else {
        setState(DEFAULT_SECTION_STATE);
      }
    } catch {
      setState(DEFAULT_SECTION_STATE);
    }
  }, [vault]);

  const update = (next: SectionState) => {
    setState(next);
    try {
      localStorage.setItem(sectionStorageKey(vault), JSON.stringify(next));
    } catch {}
  };

  return [state, update];
}
