import { useCallback, useEffect, useMemo, useState } from "react";
import { browseVault } from "@/lib/api";

export type NodeKind = "collection" | "document" | "table" | "file";

export interface TreeNode {
  kind: NodeKind;
  name: string;
  path: string;
  /** children only populated for collections; undefined otherwise */
  children?: TreeNode[];
  /** backend item payload (doc_type, summary, mime_type, ...) */
  raw?: any;
}

interface BrowseItem {
  type: NodeKind;
  name: string;
  path: string;
  file_id?: string;
  [k: string]: any;
}

/**
 * Single depth=2 browse gives us every collection + doc. Tables/files live at
 * vault root from the same call. We fold everything into a nested tree on the
 * client so "overview" under many parents renders as `features/overview`,
 * `prd/overview`, etc. — not 14 lookalike cards.
 *
 * Scaling note: this assumes the vault fits in a single browse response
 * comfortably. The largest real vault today holds ~30 items; when (if) a
 * vault grows past a few thousand, switch the initial call to depth=1 and
 * add a per-collection lazy-load on expand. Not implemented now because it
 * would be unreachable code under current sizes.
 */
export function useVaultTree(vault: string | undefined) {
  const [items, setItems] = useState<BrowseItem[] | null>(null);
  const [error, setError] = useState<string>("");

  useEffect(() => {
    if (!vault) return;
    let alive = true;
    setItems(null);
    setError("");
    browseVault(vault, undefined, 2)
      .then((d) => { if (alive) setItems(d.items as BrowseItem[]); })
      .catch((e) => { if (alive) setError(e.message || String(e)); });
    return () => { alive = false; };
  }, [vault]);

  const tree = useMemo<TreeNode[] | null>(() => {
    if (!items) return null;
    return buildTree(items);
  }, [items]);

  return { tree, loading: items === null && !error, error };
}

export function buildTree(items: BrowseItem[]): TreeNode[] {
  // Root map keyed by first path segment for collections, or by name for tables/files.
  const roots: TreeNode[] = [];
  const colByPath = new Map<string, TreeNode>();

  // Phase 1: register every collection (create intermediate ancestors lazily).
  const collections = items.filter((i) => i.type === "collection");
  // Sort by path depth so parents are registered before children.
  collections.sort((a, b) => a.path.localeCompare(b.path));

  for (const c of collections) {
    ensureCollection(c.path, c, roots, colByPath);
  }

  // Phase 2: attach documents to their collection (or root if none).
  for (const d of items.filter((i) => i.type === "document")) {
    const collectionPath = d.path.includes("/") ? d.path.split("/").slice(0, -1).join("/") : "";
    const node: TreeNode = {
      kind: "document",
      name: d.name,
      path: d.path,
      raw: d,
    };
    if (collectionPath && colByPath.has(collectionPath)) {
      colByPath.get(collectionPath)!.children!.push(node);
    } else if (collectionPath) {
      // Orphan — fabricate missing ancestors so the doc has a home.
      const parent = ensureCollection(collectionPath, null, roots, colByPath);
      parent.children!.push(node);
    } else {
      roots.push(node);
    }
  }

  // Phase 3: tables + files at root.
  for (const t of items.filter((i) => i.type === "table")) {
    roots.push({ kind: "table", name: t.name, path: t.name, raw: t });
  }
  for (const f of items.filter((i) => i.type === "file")) {
    roots.push({ kind: "file", name: f.name, path: f.file_id || f.path, raw: f });
  }

  sortTree(roots);
  return roots;
}

function ensureCollection(
  path: string,
  meta: BrowseItem | null,
  roots: TreeNode[],
  colByPath: Map<string, TreeNode>,
): TreeNode {
  const existing = colByPath.get(path);
  if (existing) {
    // Upgrade with real metadata if we only had a fabricated placeholder.
    if (meta && !existing.raw) existing.raw = meta;
    return existing;
  }
  const segs = path.split("/");
  const name = segs[segs.length - 1];
  const parentPath = segs.slice(0, -1).join("/");
  const node: TreeNode = {
    kind: "collection",
    name,
    path,
    children: [],
    raw: meta ?? undefined,
  };
  colByPath.set(path, node);
  if (parentPath) {
    const parent = ensureCollection(parentPath, null, roots, colByPath);
    parent.children!.push(node);
  } else {
    roots.push(node);
  }
  return node;
}

function sortTree(nodes: TreeNode[]) {
  // Collections first, then documents, then tables, then files — each alpha.
  const order: Record<NodeKind, number> = {
    collection: 0, document: 1, table: 2, file: 3,
  };
  nodes.sort((a, b) => {
    const k = order[a.kind] - order[b.kind];
    if (k !== 0) return k;
    return a.name.localeCompare(b.name);
  });
  for (const n of nodes) if (n.children) sortTree(n.children);
}

/* ── Expand state, persisted per-vault in localStorage ─────────────────────── */

const storageKey = (vault: string) => `akb-explorer-expanded:${vault}`;

export function useExpandedPaths(vault: string | undefined) {
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());

  useEffect(() => {
    if (!vault) return;
    try {
      const raw = localStorage.getItem(storageKey(vault));
      setExpanded(raw ? new Set(JSON.parse(raw)) : new Set());
    } catch {
      setExpanded(new Set());
    }
  }, [vault]);

  // Callbacks stay identity-stable across renders — consumers can pass them to
  // memoized rows or effect deps without re-running on every parent render.
  // Functional setState lets the callbacks avoid depending on `expanded`.
  const mutate = useCallback(
    (fn: (prev: Set<string>) => Set<string> | null) => {
      setExpanded((prev) => {
        const next = fn(prev);
        if (!next) return prev;
        if (vault) localStorage.setItem(storageKey(vault), JSON.stringify([...next]));
        return next;
      });
    },
    [vault],
  );

  const toggle = useCallback(
    (path: string) =>
      mutate((prev) => {
        const next = new Set(prev);
        next.has(path) ? next.delete(path) : next.add(path);
        return next;
      }),
    [mutate],
  );

  const expand = useCallback(
    (path: string) =>
      mutate((prev) => {
        if (prev.has(path)) return null;
        const next = new Set(prev);
        next.add(path);
        return next;
      }),
    [mutate],
  );

  const revealAncestorsOf = useCallback(
    (path: string) =>
      mutate((prev) => {
        const segs = path.split("/");
        if (segs.length <= 1) return null;
        const next = new Set(prev);
        let changed = false;
        for (let i = 1; i < segs.length; i++) {
          const anc = segs.slice(0, i).join("/");
          if (!next.has(anc)) { next.add(anc); changed = true; }
        }
        return changed ? next : null;
      }),
    [mutate],
  );

  return { expanded, toggle, expand, revealAncestorsOf };
}
