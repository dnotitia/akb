import { matchPath } from "react-router-dom";
import type { TreeNode } from "@/hooks/use-vault-tree";

/**
 * Resolve which tree node (if any) the current URL refers to. Returns a
 * stable `kind:path` signature so call sites can compare without keeping
 * node identity across renders.
 *
 * Callers must ensure document URLs are in canonical `path` form (not the
 * `d-XX` hash id). DocumentPage does this rewrite on fetch.
 */
export function activePathFromRoute(pathname: string, tree: TreeNode[] | null): string | null {
  if (!tree) return null;

  const doc = matchPath("/vault/:name/doc/:id", pathname);
  if (doc) {
    const ref = decodeURIComponent(doc.params.id ?? "");
    const hit = findDoc(tree, ref);
    return hit ? signatureOf(hit) : null;
  }

  const table = matchPath("/vault/:name/table/:table", pathname);
  if (table) return `table:${decodeURIComponent(table.params.table ?? "")}`;

  const file = matchPath("/vault/:name/file/:id", pathname);
  if (file) return `file:${decodeURIComponent(file.params.id ?? "")}`;

  return null;
}

export function signatureOf(n: TreeNode): string {
  return `${n.kind}:${n.path}`;
}

/**
 * Walk the tree looking for a document whose `path`, `raw.id`, or `raw.uri`
 * matches the given ref. We deliberately *don't* fall back on partial-path
 * matching anymore — DocumentPage canonicalizes URLs, so a clean path is
 * the only input this function needs to handle.
 */
export function findDoc(nodes: TreeNode[], ref: string): TreeNode | null {
  for (const n of nodes) {
    if (n.kind === "document") {
      const raw = n.raw || {};
      if (n.path === ref || raw.id === ref || raw.uri === ref || raw.doc_id === ref) return n;
    } else if (n.children) {
      const hit = findDoc(n.children, ref);
      if (hit) return hit;
    }
  }
  return null;
}

export function countDocs(n: TreeNode): number {
  if (n.kind === "document") return 1;
  if (!n.children) return 0;
  return n.children.reduce((s, c) => s + countDocs(c), 0);
}

export function leafHref(vault: string, node: TreeNode): string {
  switch (node.kind) {
    case "document": return `/vault/${vault}/doc/${encodeURIComponent(node.path)}`;
    case "table":    return `/vault/${vault}/table/${encodeURIComponent(node.path)}`;
    case "file":     return `/vault/${vault}/file/${encodeURIComponent(node.path)}`;
    default:         return `/vault/${vault}`;
  }
}

/**
 * Return a subset of the tree whose names/paths contain `q` (lowercased).
 * Collections are retained if any descendant matches.
 */
export function filterTree(nodes: TreeNode[], q: string): TreeNode[] {
  const out: TreeNode[] = [];
  for (const n of nodes) {
    if (n.kind === "collection") {
      const kept = n.children ? filterTree(n.children, q) : [];
      if (kept.length > 0 || n.name.toLowerCase().includes(q)) {
        out.push({ ...n, children: kept });
      }
    } else if (n.name.toLowerCase().includes(q) || n.path.toLowerCase().includes(q)) {
      out.push(n);
    }
  }
  return out;
}

export interface FlatRow {
  node: TreeNode;
  depth: number;
  sig: string;
  /** True iff this collection is currently expanded. `false` for leaves. */
  isOpen: boolean;
}

/**
 * Flatten the tree into the exact DFS order the explorer renders, annotating
 * each row with depth / signature / open state. `forceOpen` reflects the
 * "filter active — expand all matches" mode.
 *
 * Flat rendering with primitive props is what makes React.memo on the row
 * component effective — a recursive render with a Set prop would fail
 * shallow-equal on every toggle.
 */
export function flattenVisible(
  nodes: TreeNode[],
  expanded: Set<string>,
  forceOpen: boolean,
): FlatRow[] {
  const out: FlatRow[] = [];
  const visit = (list: TreeNode[], depth: number) => {
    for (const n of list) {
      const isOpen = n.kind === "collection" && (forceOpen || expanded.has(n.path));
      out.push({ node: n, depth, sig: signatureOf(n), isOpen });
      if (isOpen && n.children) visit(n.children, depth + 1);
    }
  };
  visit(nodes, 0);
  return out;
}
