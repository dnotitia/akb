import { useCallback, useEffect, useMemo, useState } from "react";
import { useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import { useAccessVerification, useCurrentUser } from "@/contexts/current-user-context";
import { ApiError, browseVault, type ArchiveScope, type BrowseOptions } from "@/lib/api";
import { isReservedCollection } from "@/lib/skill";
import { parseFileUri } from "@/lib/uri";

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
  /** Canonical `akb://{vault}/{type}/{id}` handle (null for collections,
   *  which aren't URI-addressable). Used as the routing key for tree
   *  nodes — the legacy `file_id` field was removed when the MCP /
   *  REST surface collapsed onto `uri`. */
  uri?: string | null;
  /** Path of the collection this resource lives in, or null for vault root.
   *  Documents encode collection in `path` and leave this null. */
  collection?: string | null;
  [k: string]: any;
}

/** Account/grant-scoped pages. Scroll demand and scoped searches request one
 * bounded page at a time rather than downloading a whole Vault up front. */
export function vaultTreeQueryKey(
  userId: string | undefined,
  revision: number,
  vault: string,
  archiveScope: ArchiveScope = "unarchived",
) {
  return ["vault-tree", userId, revision, vault, archiveScope] as const;
}

export function useVaultTree(
  vault: string | undefined,
  archiveScope: ArchiveScope = "unarchived",
  options: { content_type?: BrowseOptions["content_type"]; collection?: string; depth?: number; name_query?: string; enabled?: boolean } = {},
) {
  const user = useCurrentUser();
  const { checking, revision } = useAccessVerification();
  const query = useInfiniteQuery({
    queryKey: [...vaultTreeQueryKey(user?.user_id, revision, vault ?? "", archiveScope), options.content_type ?? "all", options.collection ?? "", ...(options.depth === undefined ? [] : [options.depth]), ...(options.name_query ? [options.name_query] : [])],
    initialPageParam: undefined as string | undefined,
    queryFn: async (context) => {
      const { pageParam, client, queryKey } = context;
      try {
        const result = await browseVault(vault!, options.collection, options.depth ?? -1, {
          page_size: 100, archive_scope: archiveScope,
          ...(options.content_type ? { content_type: options.content_type } : {}),
          ...(pageParam ? { cursor: pageParam } : {}),
          ...(options.name_query ? { name_query: options.name_query } : {}),
          signal: context.signal,
        });
        if (options.name_query && result.name_query !== options.name_query) throw new Error("Name search is unavailable. Please try again later.");
        return result;
      } catch (error) {
        if (!context.signal.aborted && error instanceof ApiError && [401, 403].includes(error.status)) {
          client.getQueryCache().find({ queryKey, exact: true })?.setState({ data: undefined, dataUpdatedAt: 0 });
        }
        throw error;
      }
    },
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    staleTime: 30_000,
    retry: false,
    enabled: Boolean(vault) && !checking && options.enabled !== false,
    // While another scope of the same Vault loads, keep showing the last one;
    // never show a different Vault's tree.
    placeholderData: (previous, previousQuery) =>
      previousQuery?.queryKey[1] === user?.user_id && previousQuery?.queryKey[2] === revision &&
      previousQuery?.queryKey[3] === vault ? previous : undefined,
  });
  const queryClient = useQueryClient();
  const userId = user?.user_id;
  // A mutation changes every scope of this Vault and every surface sharing it.
  const refetch = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ["vault-tree", userId, revision, vault] });
  }, [queryClient, userId, revision, vault]);

  useEffect(() => {
    if (options.enabled === false) return;
    const refresh = (event: Event) => {
      if ((event as CustomEvent<{ vault?: string }>).detail?.vault === vault) refetch();
    };
    window.addEventListener("akb:document-status-changed", refresh);
    return () => window.removeEventListener("akb:document-status-changed", refresh);
  }, [vault, refetch, options.enabled]);

  const accessDenied = query.error instanceof ApiError && [401, 403].includes(query.error.status);
  const data = vault && !checking && !accessDenied ? query.data : undefined;
  const lastPage = data?.pages.at(-1);
  const showingPreviousScope = query.isPlaceholderData;
  const unsupported = Boolean(
    lastPage && !showingPreviousScope && archiveScope !== "unarchived" && lastPage.archive_scope !== archiveScope,
  );
  const items = useMemo(
    () => {
      if (!data) return null;
      if (unsupported) return [];
      const unique = new Map<string, BrowseItem>();
      for (const page of data.pages) for (const item of page.items as BrowseItem[]) {
        unique.set(item.uri || `${item.type}:${item.path}`, item);
      }
      return [...unique.values()];
    },
    [data, unsupported],
  );
  const error = vault && query.error ? (query.error as Error).message || String(query.error) : "";

  const tree = useMemo<TreeNode[] | null>(() => {
    if (!items) return null;
    const context = lastPage?.context;
    return buildTree(context?.type === "collection"
      ? [{ type: "collection", name: context.name, path: context.path, summary: context.summary }, ...items]
      : items);
  }, [items, lastPage?.context]);

  return {
    tree,
    items,
    path: lastPage?.path,
    accessDenied,
    complete: lastPage?.complete === true,
    paginationUnsupported: Boolean(lastPage && typeof lastPage.complete !== "boolean"),
    hasMore: query.hasNextPage,
    pageCount: data?.pages.length ?? 0,
    // Kind ordering puts the Collection catalog before every resource kind.
    // A following page without Collections has passed that catalog, even if
    // status filtering leaves the resource page empty.
    pastCollectionCatalog: Boolean(lastPage && !lastPage.items.some(item => item.type === "collection")),
    loadingMore: query.isFetchingNextPage,
    loadMore: () => { if (!checking && options.enabled !== false && query.hasNextPage && !query.isFetching) void query.fetchNextPage(); },
    loading: items === null && !error && Boolean(vault),
    refreshing: Boolean(vault) && query.isFetching && !query.isFetchingNextPage && items !== null,
    showingPreviousScope,
    unsupported,
    error,
    refetch,
  };
}

/** A browse context describes a prefix, even when that collection was deleted.
 * Verify the actual row through its bounded parent catalog before offering it. */
export function useCollectionProof(vault: string, path: string, enabled = true) {
  const parent = path.split("/").slice(0, -1).join("/");
  const query = useVaultTree(vault, "unarchived", {
    collection: parent || undefined, content_type: "collections", depth: 0,
    enabled: enabled && Boolean(path),
  });
  const row = !query.showingPreviousScope && !query.accessDenied && !query.error
    ? query.items?.find(item => item.type === "collection" && item.path === path) : undefined;
  const node: TreeNode | undefined = row ? { kind: "collection", name: row.name, path: row.path, raw: row } : undefined;
  useEffect(() => {
    // Verification is a lookup, not a directory the user should have to page.
    if (enabled && path && !row && query.hasMore && !query.loadingMore && !query.refreshing && !query.error && !query.showingPreviousScope) query.loadMore();
  }, [enabled, path, row, query.hasMore, query.loadingMore, query.refreshing, query.error, query.showingPreviousScope, query.pageCount, query.loadMore]);
  return { ...query, verified: Boolean(row),
    missing: enabled && Boolean(path) && !query.showingPreviousScope && !query.error && query.complete && !query.refreshing && !row,
    node };
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

  // Phase 3: tables + files under their collection (or root if none).
  // Backend now sends `collection` on every table/file item — the
  // unified-collection refactor put doc/table/file siblings under the
  // same `collections.id` FK. NULL collection => vault root.
  const attachToCollection = (
    node: TreeNode,
    collectionPath: string | null | undefined,
  ) => {
    if (collectionPath && colByPath.has(collectionPath)) {
      colByPath.get(collectionPath)!.children!.push(node);
    } else if (collectionPath) {
      const parent = ensureCollection(collectionPath, null, roots, colByPath);
      parent.children!.push(node);
    } else {
      roots.push(node);
    }
  };

  for (const t of items.filter((i) => i.type === "table")) {
    attachToCollection(
      { kind: "table", name: t.name, path: t.name, raw: t },
      t.collection,
    );
  }
  for (const f of items.filter((i) => i.type === "file")) {
    // File path uses the URI tail (the file UUID) — the legacy
    // `file_id` browse field is gone after the URI cutover. Fall back
    // to `path` for any defensive case where uri is missing.
    const fileTail = parseFileUri(f.uri)?.id ?? null;
    attachToCollection(
      { kind: "file", name: f.name, path: fileTail || f.path, raw: f },
      f.collection,
    );
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

/**
 * Sort a flat list of items skill-first (doc_type === "skill" floats to the
 * top), then alphabetically by name. Pure function — no side effects.
 * Exported so tests (and future direct consumers) can exercise the logic
 * in isolation.
 */
export function sortCollectionItems<T extends { name: string; raw?: any }>(items: T[]): T[] {
  return [...items].sort((a, b) => {
    const aSkill = (a.raw?.doc_type ?? a.raw?.type) === "skill" ? 0 : 1;
    const bSkill = (b.raw?.doc_type ?? b.raw?.type) === "skill" ? 0 : 1;
    if (aSkill !== bSkill) return aSkill - bSkill;
    return a.name.localeCompare(b.name);
  });
}

function sortTree(nodes: TreeNode[]) {
  // Collections first, then documents, then tables, then files — each alpha.
  // Within the collection kind, the reserved system collection is pinned to
  // the top; within the document kind, skill docs are pinned to the top.
  const order: Record<NodeKind, number> = {
    collection: 0, document: 1, table: 2, file: 3,
  };
  nodes.sort((a, b) => {
    const k = order[a.kind] - order[b.kind];
    if (k !== 0) return k;
    // Within collection kind, pin the reserved system collection to the top.
    if (a.kind === "collection" && b.kind === "collection") {
      const aRes = isReservedCollection(a.path) ? 0 : 1;
      const bRes = isReservedCollection(b.path) ? 0 : 1;
      if (aRes !== bRes) return aRes - bRes;
    }
    // Within document kind, pin skill docs to the top.
    if (a.kind === "document" && b.kind === "document") {
      const aSkill = (a.raw?.doc_type ?? a.raw?.type) === "skill" ? 0 : 1;
      const bSkill = (b.raw?.doc_type ?? b.raw?.type) === "skill" ? 0 : 1;
      if (aSkill !== bSkill) return aSkill - bSkill;
    }
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
        if (next.has(path)) next.delete(path);
        else next.add(path);
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
