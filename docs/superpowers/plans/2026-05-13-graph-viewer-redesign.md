# Graph Viewer Redesign Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the cytoscape-based `frontend/src/pages/graph.tsx` (951 lines) with a 3-column page built on `react-force-graph-2d` that exposes backend relations / drill-down / provenance APIs through a dedicated right detail panel and a left filter/entry-point sidebar.

**Architecture:** 3-column page (sidebar / canvas / detail) below the existing TitleBar. URL is the canonical view state; `use-graph-data.ts` orchestrates a hybrid fetch (`/graph/{vault}` for full mode, BFS over `/relations/{vault}/{doc_id}` for neighborhood mode, 1-hop merge on click). All visuals are custom-painted on Canvas to keep the AKB mono / [10px] uppercase / border tone.

**Tech Stack:** React 19 + TypeScript + Vite, `react-force-graph-2d` (new), TanStack Query 5 (existing), Tailwind v4 + AKB tokens (existing), Vitest + Testing Library (existing).

**Spec:** `docs/superpowers/specs/2026-05-13-graph-viewer-redesign-design.md`

---

## File map (locked from spec)

| File | Status | Responsibility |
|---|---|---|
| `frontend/src/components/graph/graph-types.ts` | create | `GraphView`, `GraphNode`, `GraphEdge`, `RelationKind`, `NodeKind`, visual-token constants |
| `frontend/src/components/graph/graph-state.ts` | create | `viewToQuery` / `queryToView` codec + default-trimming |
| `frontend/src/hooks/use-graph-history.ts` | create | localStorage recent (max 5) + saved views (max 20, overwrite-on-duplicate) |
| `frontend/src/components/graph/use-graph-data.ts` | create | Full / Neighborhood / click-expand fetch orchestration with React Query |
| `frontend/src/components/graph/GraphCanvas.tsx` | create | `react-force-graph-2d` wrapper + custom Canvas paint + freeze/fit/zoom controls |
| `frontend/src/components/graph/GraphSidebar.tsx` | create | Entry search · depth · type/relation filters · recent · saved views |
| `frontend/src/components/graph/GraphDetailPanel.tsx` | create | Header + summary + tags + relations group + preview + lazy META |
| `frontend/src/pages/graph.tsx` | rewrite | Page shell; URL ↔ state; render 3-col grid |
| `frontend/src/lib/api.ts` | modify | Tighten `getGraph` / `getRelations` types; add `getProvenance`, `drillDown` if missing |
| `frontend/package.json` | modify | Add `react-force-graph-2d`; remove `cytoscape`, `cytoscape-fcose`, `cytoscape-cola` |
| Tests under `frontend/src/**/__tests__/` | create | Per task below |

---

## Reusable code blocks

These helpers are referenced from multiple tasks below. Keep them identical.

**AKB design-token reader (lifted from current `graph.tsx:79`):**

```ts
// frontend/src/components/graph/graph-types.ts (excerpt)
export interface GraphColors {
  background: string;
  surface: string;
  surfaceMuted: string;
  foreground: string;
  foregroundMuted: string;
  accent: string;
  border: string;
}

export function readColors(): GraphColors {
  const root = getComputedStyle(document.documentElement);
  const pick = (name: string) => root.getPropertyValue(name).trim() || "#000";
  return {
    background: pick("--color-background"),
    surface: pick("--color-surface"),
    surfaceMuted: pick("--color-surface-muted"),
    foreground: pick("--color-foreground"),
    foregroundMuted: pick("--color-foreground-muted"),
    accent: pick("--color-accent"),
    border: pick("--color-border"),
  };
}
```

---

## Task 1 — Foundation: types + URL codec + history hook

**Files:**
- Create: `frontend/src/components/graph/graph-types.ts`
- Create: `frontend/src/components/graph/graph-state.ts`
- Create: `frontend/src/hooks/use-graph-history.ts`
- Create: `frontend/src/components/graph/__tests__/graph-state.test.ts`
- Create: `frontend/src/hooks/__tests__/use-graph-history.test.ts`

- [ ] **Step 1.1: Write `graph-types.ts`**

```ts
// frontend/src/components/graph/graph-types.ts
export type NodeKind = "document" | "table" | "file";

export type RelationKind =
  | "depends_on"
  | "implements"
  | "references"
  | "related_to"
  | "attached_to"
  | "derived_from";

export const ALL_NODE_KINDS: ReadonlyArray<NodeKind> = ["document", "table", "file"];
export const ALL_RELATIONS: ReadonlyArray<RelationKind> = [
  "depends_on", "implements", "references", "related_to", "attached_to", "derived_from",
];

export interface GraphNode {
  uri: string;
  name: string;
  kind: NodeKind;
  doc_id?: string;
  doc_type?: string;
  // simulation-owned positional state
  x?: number; y?: number; vx?: number; vy?: number; fx?: number; fy?: number;
}

export interface GraphEdge {
  source: string;
  target: string;
  relation: RelationKind;
}

export interface GraphView {
  entry?: string;
  depth: 1 | 2 | 3;
  types: Set<NodeKind>;
  relations: Set<RelationKind>;
  selected?: string;
}

export const DEFAULT_VIEW: GraphView = {
  depth: 2,
  types: new Set(ALL_NODE_KINDS),
  relations: new Set(ALL_RELATIONS),
};

export interface GraphColors {
  background: string;
  surface: string;
  surfaceMuted: string;
  foreground: string;
  foregroundMuted: string;
  accent: string;
  border: string;
}

export function readColors(): GraphColors {
  const root = getComputedStyle(document.documentElement);
  const pick = (name: string) => root.getPropertyValue(name).trim() || "#000";
  return {
    background: pick("--color-background"),
    surface: pick("--color-surface"),
    surfaceMuted: pick("--color-surface-muted"),
    foreground: pick("--color-foreground"),
    foregroundMuted: pick("--color-foreground-muted"),
    accent: pick("--color-accent"),
    border: pick("--color-border"),
  };
}

export const RELATION_DASH: Record<RelationKind, number[]> = {
  depends_on: [],
  implements: [],
  references: [4, 4],
  related_to: [2, 2],
  attached_to: [],
  derived_from: [6, 2],
};
```

- [ ] **Step 1.2: Write failing tests for `graph-state.ts`**

```ts
// frontend/src/components/graph/__tests__/graph-state.test.ts
import { describe, it, expect } from "vitest";
import { viewToQuery, queryToView } from "../graph-state";
import { DEFAULT_VIEW, ALL_NODE_KINDS, ALL_RELATIONS, type GraphView } from "../graph-types";

describe("graph-state codec", () => {
  it("returns empty query for default view", () => {
    expect(viewToQuery(DEFAULT_VIEW)).toBe("");
  });

  it("roundtrips entry + depth", () => {
    const v: GraphView = { ...DEFAULT_VIEW, entry: "d-94d8657f", depth: 3 };
    const q = viewToQuery(v);
    expect(q).toContain("entry=d-94d8657f");
    expect(q).toContain("depth=3");
    const back = queryToView(new URLSearchParams(q));
    expect(back.entry).toBe("d-94d8657f");
    expect(back.depth).toBe(3);
  });

  it("omits types when all are selected", () => {
    const v: GraphView = { ...DEFAULT_VIEW, types: new Set(ALL_NODE_KINDS) };
    expect(viewToQuery(v)).not.toContain("types=");
  });

  it("encodes a partial types subset", () => {
    const v: GraphView = { ...DEFAULT_VIEW, types: new Set(["document"]) };
    expect(viewToQuery(v)).toContain("types=document");
  });

  it("omits rel when all are selected", () => {
    const v: GraphView = { ...DEFAULT_VIEW, relations: new Set(ALL_RELATIONS) };
    expect(viewToQuery(v)).not.toContain("rel=");
  });

  it("roundtrips a partial relation subset (order-insensitive)", () => {
    const v: GraphView = { ...DEFAULT_VIEW, relations: new Set(["depends_on", "implements"]) };
    const q = viewToQuery(v);
    const back = queryToView(new URLSearchParams(q));
    expect(back.relations).toEqual(new Set(["depends_on", "implements"]));
  });

  it("roundtrips selected", () => {
    const uri = "akb://akb/doc/specs/2026/foo.md";
    const v: GraphView = { ...DEFAULT_VIEW, selected: uri };
    const back = queryToView(new URLSearchParams(viewToQuery(v)));
    expect(back.selected).toBe(uri);
  });

  it("ignores unknown depths and clamps to 2", () => {
    const back = queryToView(new URLSearchParams("depth=7"));
    expect(back.depth).toBe(2);
  });

  it("ignores unknown node kinds in types", () => {
    const back = queryToView(new URLSearchParams("types=document,bogus"));
    expect(back.types).toEqual(new Set(["document"]));
  });
});
```

- [ ] **Step 1.3: Run tests to verify they fail**

```bash
cd frontend && npx vitest run src/components/graph/__tests__/graph-state.test.ts
```
Expected: FAIL — `Cannot find module '../graph-state'`.

- [ ] **Step 1.4: Implement `graph-state.ts`**

```ts
// frontend/src/components/graph/graph-state.ts
import {
  ALL_NODE_KINDS,
  ALL_RELATIONS,
  DEFAULT_VIEW,
  type GraphView,
  type NodeKind,
  type RelationKind,
} from "./graph-types";

const ALL_KIND_SET = new Set<NodeKind>(ALL_NODE_KINDS);
const ALL_REL_SET = new Set<RelationKind>(ALL_RELATIONS);

function setsEqual<T>(a: Set<T>, b: Set<T>): boolean {
  if (a.size !== b.size) return false;
  for (const v of a) if (!b.has(v)) return false;
  return true;
}

export function viewToQuery(v: GraphView): string {
  const p = new URLSearchParams();
  if (v.entry) p.set("entry", v.entry);
  if (v.depth !== DEFAULT_VIEW.depth) p.set("depth", String(v.depth));
  if (!setsEqual(v.types, ALL_KIND_SET)) {
    p.set("types", [...v.types].join(","));
  }
  if (!setsEqual(v.relations, ALL_REL_SET)) {
    p.set("rel", [...v.relations].join(","));
  }
  if (v.selected) p.set("sel", v.selected);
  return p.toString();
}

function parseDepth(raw: string | null): 1 | 2 | 3 {
  if (raw === "1") return 1;
  if (raw === "3") return 3;
  return 2;
}

export function queryToView(q: URLSearchParams): GraphView {
  const types = q.get("types");
  const rel = q.get("rel");
  return {
    entry: q.get("entry") || undefined,
    depth: parseDepth(q.get("depth")),
    types: types
      ? new Set(
          types.split(",").filter((s): s is NodeKind => (ALL_KIND_SET as Set<string>).has(s)),
        )
      : new Set(ALL_NODE_KINDS),
    relations: rel
      ? new Set(
          rel.split(",").filter((s): s is RelationKind => (ALL_REL_SET as Set<string>).has(s)),
        )
      : new Set(ALL_RELATIONS),
    selected: q.get("sel") || undefined,
  };
}
```

- [ ] **Step 1.5: Run tests to verify they pass**

```bash
cd frontend && npx vitest run src/components/graph/__tests__/graph-state.test.ts
```
Expected: PASS — 9/9.

- [ ] **Step 1.6: Write failing tests for `use-graph-history.ts`**

```ts
// frontend/src/hooks/__tests__/use-graph-history.test.ts
import { describe, it, expect, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useGraphHistory } from "../use-graph-history";

beforeEach(() => localStorage.clear());

describe("useGraphHistory · recent", () => {
  it("starts empty", () => {
    const { result } = renderHook(() => useGraphHistory("akb"));
    expect(result.current.recent).toEqual([]);
  });

  it("pushes a recent entry", () => {
    const { result } = renderHook(() => useGraphHistory("akb"));
    act(() => result.current.pushRecent({ doc_id: "d-1", title: "First" }));
    expect(result.current.recent).toEqual([{ doc_id: "d-1", title: "First" }]);
  });

  it("dedupes and moves to front on re-push", () => {
    const { result } = renderHook(() => useGraphHistory("akb"));
    act(() => result.current.pushRecent({ doc_id: "d-1", title: "First" }));
    act(() => result.current.pushRecent({ doc_id: "d-2", title: "Second" }));
    act(() => result.current.pushRecent({ doc_id: "d-1", title: "First again" }));
    expect(result.current.recent.map((r) => r.doc_id)).toEqual(["d-1", "d-2"]);
    expect(result.current.recent[0].title).toBe("First again");
  });

  it("caps at 5 entries (oldest first eviction)", () => {
    const { result } = renderHook(() => useGraphHistory("akb"));
    for (let i = 1; i <= 7; i++) {
      act(() => result.current.pushRecent({ doc_id: `d-${i}`, title: `T${i}` }));
    }
    expect(result.current.recent.length).toBe(5);
    expect(result.current.recent[0].doc_id).toBe("d-7");
    expect(result.current.recent[4].doc_id).toBe("d-3");
  });

  it("clearRecent empties storage", () => {
    const { result } = renderHook(() => useGraphHistory("akb"));
    act(() => result.current.pushRecent({ doc_id: "d-1", title: "x" }));
    act(() => result.current.clearRecent());
    expect(result.current.recent).toEqual([]);
  });
});

describe("useGraphHistory · saved views", () => {
  it("starts empty", () => {
    const { result } = renderHook(() => useGraphHistory("akb"));
    expect(result.current.saved).toEqual([]);
  });

  it("saves a named view", () => {
    const { result } = renderHook(() => useGraphHistory("akb"));
    act(() => result.current.saveView("roadmap", "?entry=d-94d8657f&depth=2"));
    expect(result.current.saved).toEqual([{ name: "roadmap", url: "?entry=d-94d8657f&depth=2" }]);
  });

  it("overwrites a duplicate name", () => {
    const { result } = renderHook(() => useGraphHistory("akb"));
    act(() => result.current.saveView("roadmap", "?entry=d-1"));
    act(() => result.current.saveView("roadmap", "?entry=d-2"));
    expect(result.current.saved.length).toBe(1);
    expect(result.current.saved[0].url).toBe("?entry=d-2");
  });

  it("caps at 20 entries (oldest evicted)", () => {
    const { result } = renderHook(() => useGraphHistory("akb"));
    for (let i = 1; i <= 22; i++) {
      act(() => result.current.saveView(`v${i}`, `?entry=d-${i}`));
    }
    expect(result.current.saved.length).toBe(20);
    expect(result.current.saved[0].name).toBe("v22");
    expect(result.current.saved[19].name).toBe("v3");
  });

  it("deleteView removes by name", () => {
    const { result } = renderHook(() => useGraphHistory("akb"));
    act(() => result.current.saveView("a", "?x=1"));
    act(() => result.current.saveView("b", "?y=2"));
    act(() => result.current.deleteView("a"));
    expect(result.current.saved.map((v) => v.name)).toEqual(["b"]);
  });

  it("scopes storage per vault", () => {
    const { result: a } = renderHook(() => useGraphHistory("vault-a"));
    const { result: b } = renderHook(() => useGraphHistory("vault-b"));
    act(() => a.current.saveView("a-view", "?x=1"));
    expect(b.current.saved).toEqual([]);
  });
});
```

- [ ] **Step 1.7: Run tests to verify they fail**

```bash
cd frontend && npx vitest run src/hooks/__tests__/use-graph-history.test.ts
```
Expected: FAIL — `Cannot find module '../use-graph-history'`.

- [ ] **Step 1.8: Implement `use-graph-history.ts`**

```ts
// frontend/src/hooks/use-graph-history.ts
import { useCallback, useEffect, useState } from "react";

export interface RecentEntry {
  doc_id: string;
  title: string;
}

export interface SavedView {
  name: string;
  url: string;
}

const RECENT_MAX = 5;
const SAVED_MAX = 20;

const recentKey = (vault: string) => `akb-graph-recent:${vault}`;
const savedKey = (vault: string) => `akb-graph-saves:${vault}`;

function readJson<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : fallback;
  } catch {
    return fallback;
  }
}

function writeJson(key: string, value: unknown): boolean {
  try {
    localStorage.setItem(key, JSON.stringify(value));
    return true;
  } catch {
    // Quota exceeded or storage disabled — caller may surface a toast.
    return false;
  }
}

export function useGraphHistory(vault: string) {
  const [recent, setRecent] = useState<RecentEntry[]>(() => readJson(recentKey(vault), []));
  const [saved, setSaved] = useState<SavedView[]>(() => readJson(savedKey(vault), []));

  useEffect(() => {
    setRecent(readJson(recentKey(vault), []));
    setSaved(readJson(savedKey(vault), []));
  }, [vault]);

  const pushRecent = useCallback(
    (entry: RecentEntry) => {
      setRecent((prev) => {
        const filtered = prev.filter((r) => r.doc_id !== entry.doc_id);
        const next = [entry, ...filtered].slice(0, RECENT_MAX);
        writeJson(recentKey(vault), next);
        return next;
      });
    },
    [vault],
  );

  const clearRecent = useCallback(() => {
    writeJson(recentKey(vault), []);
    setRecent([]);
  }, [vault]);

  const saveView = useCallback(
    (name: string, url: string) => {
      setSaved((prev) => {
        const filtered = prev.filter((v) => v.name !== name);
        const next = [{ name, url }, ...filtered].slice(0, SAVED_MAX);
        writeJson(savedKey(vault), next);
        return next;
      });
    },
    [vault],
  );

  const deleteView = useCallback(
    (name: string) => {
      setSaved((prev) => {
        const next = prev.filter((v) => v.name !== name);
        writeJson(savedKey(vault), next);
        return next;
      });
    },
    [vault],
  );

  return { recent, pushRecent, clearRecent, saved, saveView, deleteView };
}
```

- [ ] **Step 1.9: Run tests to verify they pass**

```bash
cd frontend && npx vitest run src/hooks/__tests__/use-graph-history.test.ts
```
Expected: PASS — 10/10.

- [ ] **Step 1.10: Type-check + full vitest run**

```bash
cd frontend && npx tsc --noEmit && npx vitest run
```
Expected: tsc clean; all tests pass (existing + new).

- [ ] **Step 1.11: Commit**

```bash
git add frontend/src/components/graph/graph-types.ts \
        frontend/src/components/graph/graph-state.ts \
        frontend/src/components/graph/__tests__/graph-state.test.ts \
        frontend/src/hooks/use-graph-history.ts \
        frontend/src/hooks/__tests__/use-graph-history.test.ts
git commit -m "feat(graph): foundation — types, URL codec, history hook"
```

---

## Task 2 — Data layer: hybrid fetch (`use-graph-data.ts`)

**Files:**
- Modify: `frontend/src/lib/api.ts` — tighten `getGraph` / `getRelations` return types
- Create: `frontend/src/components/graph/use-graph-data.ts`
- Create: `frontend/src/components/graph/__tests__/use-graph-data.test.ts`

- [ ] **Step 2.1: Tighten api.ts types**

Replace the `any[]` payloads with concrete shapes the data layer relies on. Locate lines 253–270 (the `// ── Graph ──` and `// ── Relations ──` blocks) and replace with:

```ts
// frontend/src/lib/api.ts (around line 253)
// ── Graph ──
export interface GraphApiNode {
  uri: string;
  name?: string;
  resource_type?: string;
}
export interface GraphApiEdge {
  source: string;
  target: string;
  relation?: string;
}
export const getGraph = (vault: string, docId?: string, depth = 2, limit = 50) => {
  const p = new URLSearchParams({ depth: String(depth), limit: String(limit) });
  if (docId) p.set("doc_id", docId);
  return api<{ nodes: GraphApiNode[]; edges: GraphApiEdge[] }>(`/graph/${vault}?${p}`);
};

// ── Relations ──
export interface RelationRow {
  source: string;
  target: string;
  relation: string;
  // backend may include resource_type / display_name on the "other" side
  other_uri?: string;
  other_name?: string;
  other_type?: string;
}
export const getRelations = (vault: string, docId: string) =>
  api<{ doc_id: string; resource_uri: string; relations: RelationRow[] }>(
    `/relations/${vault}/${encodeURIComponent(docId)}`,
  );
```

- [ ] **Step 2.2: Run typecheck**

```bash
cd frontend && npx tsc --noEmit
```
Expected: tsc clean. (Existing call sites use `any` so they should still compile.)

- [ ] **Step 2.3: Write failing tests for `use-graph-data.ts`**

```ts
// frontend/src/components/graph/__tests__/use-graph-data.test.ts
import { describe, it, expect, vi, beforeEach } from "vitest";
import { mergeGraph, bfsExpand, applyFilters, isDegraded } from "../use-graph-data";
import { DEFAULT_VIEW, type GraphNode, type GraphEdge } from "../graph-types";

describe("mergeGraph", () => {
  it("dedupes nodes by uri", () => {
    const a: GraphNode[] = [{ uri: "n1", name: "A", kind: "document" }];
    const b: GraphNode[] = [
      { uri: "n1", name: "A2", kind: "document" }, // newer copy
      { uri: "n2", name: "B", kind: "document" },
    ];
    const merged = mergeGraph(
      { nodes: a, edges: [] },
      { nodes: b, edges: [] },
    );
    expect(merged.nodes.length).toBe(2);
    // existing wins (preserves simulation x/y), new ones appended
    expect(merged.nodes.find((n) => n.uri === "n1")?.name).toBe("A");
  });

  it("dedupes edges by source+target+relation triple", () => {
    const e1: GraphEdge[] = [{ source: "n1", target: "n2", relation: "depends_on" }];
    const e2: GraphEdge[] = [
      { source: "n1", target: "n2", relation: "depends_on" }, // dup
      { source: "n1", target: "n2", relation: "references" }, // distinct
    ];
    const merged = mergeGraph({ nodes: [], edges: e1 }, { nodes: [], edges: e2 });
    expect(merged.edges.length).toBe(2);
  });
});

describe("applyFilters", () => {
  const nodes: GraphNode[] = [
    { uri: "a", name: "A", kind: "document" },
    { uri: "b", name: "B", kind: "table" },
    { uri: "c", name: "C", kind: "file" },
  ];
  const edges: GraphEdge[] = [
    { source: "a", target: "b", relation: "depends_on" },
    { source: "b", target: "c", relation: "references" },
  ];

  it("drops nodes whose kind is filtered out, and edges that lose an endpoint", () => {
    const v = { ...DEFAULT_VIEW, types: new Set<GraphNode["kind"]>(["document"]) };
    const out = applyFilters({ nodes, edges }, v);
    expect(out.nodes.map((n) => n.uri)).toEqual(["a"]);
    expect(out.edges).toEqual([]);
  });

  it("drops edges whose relation is filtered out", () => {
    const v = { ...DEFAULT_VIEW, relations: new Set<GraphEdge["relation"]>(["depends_on"]) };
    const out = applyFilters({ nodes, edges }, v);
    expect(out.edges.map((e) => e.relation)).toEqual(["depends_on"]);
  });

  it("passes through when filters are at defaults", () => {
    const out = applyFilters({ nodes, edges }, DEFAULT_VIEW);
    expect(out.nodes.length).toBe(3);
    expect(out.edges.length).toBe(2);
  });
});

describe("isDegraded", () => {
  it("flips at > 500 raw nodes (unfiltered count)", () => {
    expect(isDegraded(500)).toBe(false);
    expect(isDegraded(501)).toBe(true);
  });
});

describe("bfsExpand", () => {
  it("walks depth-1 with one fetch and produces seed neighbors", async () => {
    const fetchRelations = vi.fn(async (_v: string, docId: string) => ({
      doc_id: docId,
      resource_uri: `akb://v/doc/${docId}`,
      relations: [
        {
          source: `akb://v/doc/${docId}`,
          target: "akb://v/doc/d-2",
          relation: "depends_on",
          other_uri: "akb://v/doc/d-2",
          other_name: "Second",
          other_type: "document",
        },
      ],
    }));
    const out = await bfsExpand({
      vault: "v",
      entry: "d-1",
      depth: 1,
      fetchRelations,
    });
    expect(fetchRelations).toHaveBeenCalledTimes(1);
    expect(out.nodes.map((n) => n.uri).sort()).toEqual([
      "akb://v/doc/d-1",
      "akb://v/doc/d-2",
    ]);
    expect(out.edges.length).toBe(1);
  });

  it("walks depth-2 by following neighbors discovered in hop 1", async () => {
    const calls: string[] = [];
    const fetchRelations = vi.fn(async (_v: string, docId: string) => {
      calls.push(docId);
      if (docId === "d-1") {
        return {
          doc_id: docId,
          resource_uri: "akb://v/doc/d-1",
          relations: [
            {
              source: "akb://v/doc/d-1",
              target: "akb://v/doc/d-2",
              relation: "depends_on",
              other_uri: "akb://v/doc/d-2",
              other_name: "Second",
              other_type: "document",
            },
          ],
        };
      }
      if (docId === "d-2") {
        return {
          doc_id: docId,
          resource_uri: "akb://v/doc/d-2",
          relations: [
            {
              source: "akb://v/doc/d-2",
              target: "akb://v/doc/d-3",
              relation: "references",
              other_uri: "akb://v/doc/d-3",
              other_name: "Third",
              other_type: "document",
            },
          ],
        };
      }
      return { doc_id: docId, resource_uri: `akb://v/doc/${docId}`, relations: [] };
    });
    const out = await bfsExpand({
      vault: "v",
      entry: "d-1",
      depth: 2,
      fetchRelations,
    });
    expect(calls.sort()).toEqual(["d-1", "d-2"]);
    expect(out.nodes.map((n) => n.uri).sort()).toEqual([
      "akb://v/doc/d-1",
      "akb://v/doc/d-2",
      "akb://v/doc/d-3",
    ]);
  });

  it("does not re-fetch already-visited nodes", async () => {
    const fetchRelations = vi.fn(async (_v: string, docId: string) => ({
      doc_id: docId,
      resource_uri: `akb://v/doc/${docId}`,
      relations: [
        {
          source: `akb://v/doc/${docId}`,
          target: "akb://v/doc/d-1", // cycle back to entry
          relation: "depends_on",
          other_uri: "akb://v/doc/d-1",
          other_name: "First",
          other_type: "document",
        },
      ],
    }));
    await bfsExpand({ vault: "v", entry: "d-1", depth: 3, fetchRelations });
    // d-1 fetched once at hop 0; hop 1 returns d-1 again but it's already visited,
    // so no further fetch.
    expect(fetchRelations).toHaveBeenCalledTimes(1);
  });
});
```

- [ ] **Step 2.4: Run tests to verify they fail**

```bash
cd frontend && npx vitest run src/components/graph/__tests__/use-graph-data.test.ts
```
Expected: FAIL — module missing.

- [ ] **Step 2.5: Implement `use-graph-data.ts`**

```ts
// frontend/src/components/graph/use-graph-data.ts
import { useQuery } from "@tanstack/react-query";
import { getGraph, getRelations, type RelationRow } from "@/lib/api";
import {
  ALL_NODE_KINDS,
  type GraphEdge,
  type GraphNode,
  type GraphView,
  type NodeKind,
  type RelationKind,
} from "./graph-types";

export interface GraphPayload {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

const KIND_SET = new Set<string>(ALL_NODE_KINDS);

function normalizeKind(raw: string | undefined): NodeKind {
  if (raw && KIND_SET.has(raw)) return raw as NodeKind;
  return "document";
}

function normalizeRelation(raw: string | undefined): RelationKind | null {
  switch (raw) {
    case "depends_on":
    case "implements":
    case "references":
    case "related_to":
    case "attached_to":
    case "derived_from":
      return raw;
    default:
      return null;
  }
}

export function mergeGraph(a: GraphPayload, b: GraphPayload): GraphPayload {
  const nodeByUri = new Map<string, GraphNode>();
  for (const n of a.nodes) nodeByUri.set(n.uri, n);
  for (const n of b.nodes) if (!nodeByUri.has(n.uri)) nodeByUri.set(n.uri, n);
  const edgeKey = (e: GraphEdge) => `${e.source}\u0001${e.target}\u0001${e.relation}`;
  const edgeKeys = new Set<string>();
  const edges: GraphEdge[] = [];
  for (const e of [...a.edges, ...b.edges]) {
    const k = edgeKey(e);
    if (edgeKeys.has(k)) continue;
    edgeKeys.add(k);
    edges.push(e);
  }
  return { nodes: [...nodeByUri.values()], edges };
}

export function applyFilters(p: GraphPayload, v: GraphView): GraphPayload {
  const nodes = p.nodes.filter((n) => v.types.has(n.kind));
  const keep = new Set(nodes.map((n) => n.uri));
  const edges = p.edges.filter(
    (e) => v.relations.has(e.relation) && keep.has(e.source) && keep.has(e.target),
  );
  return { nodes, edges };
}

export function isDegraded(rawNodeCount: number): boolean {
  return rawNodeCount > 500;
}

interface BfsExpandArgs {
  vault: string;
  entry: string; // doc_id
  depth: 1 | 2 | 3;
  fetchRelations: (
    vault: string,
    docId: string,
  ) => Promise<{ resource_uri: string; relations: RelationRow[] }>;
}

function rowToEdge(row: RelationRow): GraphEdge | null {
  const relation = normalizeRelation(row.relation);
  if (!relation) return null;
  return { source: row.source, target: row.target, relation };
}

function rowToNeighbor(row: RelationRow): GraphNode | null {
  if (!row.other_uri) return null;
  return {
    uri: row.other_uri,
    name: row.other_name || row.other_uri,
    kind: normalizeKind(row.other_type),
  };
}

// Extract the doc / table / file id from an `akb://{vault}/{kind}/{path}` URI.
// Multi-segment paths (e.g. `specs/2026/foo.md`) are preserved intact —
// `find_by_ref` on the backend matches the metadata `id` or the `path LIKE`
// fallback against this full tail.
export function docIdFromUri(uri: string): string | null {
  const m = uri.match(/^akb:\/\/[^/]+\/(?:doc|table|file)\/(.+)$/);
  return m ? decodeURIComponent(m[1]) : null;
}

export async function bfsExpand(args: BfsExpandArgs): Promise<GraphPayload> {
  const { vault, entry, depth, fetchRelations } = args;
  const visited = new Set<string>();
  visited.add(entry);

  const seedResp = await fetchRelations(vault, entry);
  const seedNode: GraphNode = {
    uri: seedResp.resource_uri,
    name: entry,
    kind: "document",
    doc_id: entry,
  };
  const nodesByUri = new Map<string, GraphNode>([[seedNode.uri, seedNode]]);
  const edgeKeys = new Set<string>();
  const edges: GraphEdge[] = [];

  function ingest(rows: RelationRow[]): string[] {
    const newDocIds: string[] = [];
    for (const row of rows) {
      const edge = rowToEdge(row);
      if (edge) {
        const k = `${edge.source}\u0001${edge.target}\u0001${edge.relation}`;
        if (!edgeKeys.has(k)) {
          edgeKeys.add(k);
          edges.push(edge);
        }
      }
      const neighbor = rowToNeighbor(row);
      if (neighbor && !nodesByUri.has(neighbor.uri)) {
        nodesByUri.set(neighbor.uri, neighbor);
        const id = docIdFromUri(neighbor.uri);
        if (id && !visited.has(id)) newDocIds.push(id);
      }
    }
    return newDocIds;
  }

  // Seed hop populates the first frontier from the entry's neighbors.
  let frontier: string[] = ingest(seedResp.relations);

  for (let hop = 1; hop < depth; hop++) {
    const toFetch = frontier.filter((docId) => {
      if (visited.has(docId)) return false;
      visited.add(docId);
      return true;
    });
    if (toFetch.length === 0) break;
    const responses = await Promise.all(
      toFetch.map((docId) => fetchRelations(vault, docId).catch(() => null)),
    );
    const nextFrontier: string[] = [];
    for (const r of responses) {
      if (!r) continue;
      nextFrontier.push(...ingest(r.relations));
    }
    if (nextFrontier.length === 0) break;
    frontier = nextFrontier;
  }

  return { nodes: [...nodesByUri.values()], edges };
}

/* React Query hooks — the consumers used by `pages/graph.tsx`. */

export function useFullGraph(vault: string, enabled: boolean) {
  return useQuery({
    queryKey: ["graph", vault, "full"],
    enabled,
    queryFn: async (): Promise<GraphPayload> => {
      const resp = await getGraph(vault);
      const nodes: GraphNode[] = resp.nodes.map((n) => ({
        uri: n.uri,
        name: n.name || n.uri,
        kind: normalizeKind(n.resource_type),
      }));
      const edges: GraphEdge[] = resp.edges
        .map((e) => {
          const rel = normalizeRelation(e.relation);
          return rel ? { source: e.source, target: e.target, relation: rel } : null;
        })
        .filter((e): e is GraphEdge => e !== null);
      return { nodes, edges };
    },
  });
}

export function useNeighborhood(
  vault: string,
  entry: string | undefined,
  depth: 1 | 2 | 3,
) {
  return useQuery({
    queryKey: ["graph", vault, "neighborhood", entry, depth],
    enabled: !!entry,
    queryFn: () =>
      bfsExpand({
        vault,
        entry: entry as string,
        depth,
        fetchRelations: async (v, docId) => {
          const r = await getRelations(v, docId);
          return { resource_uri: r.resource_uri, relations: r.relations };
        },
      }),
  });
}
```

- [ ] **Step 2.6: Run tests to verify they pass**

```bash
cd frontend && npx vitest run src/components/graph/__tests__/use-graph-data.test.ts
```
Expected: PASS — all `mergeGraph`, `applyFilters`, `isDegraded`, `bfsExpand` cases green.

- [ ] **Step 2.7: Type-check + full vitest**

```bash
cd frontend && npx tsc --noEmit && npx vitest run
```
Expected: clean.

- [ ] **Step 2.8: Commit**

```bash
git add frontend/src/lib/api.ts \
        frontend/src/components/graph/use-graph-data.ts \
        frontend/src/components/graph/__tests__/use-graph-data.test.ts
git commit -m "feat(graph): data layer — hybrid fetch (full + BFS neighborhood)"
```

---

## Task 3 — Canvas: `react-force-graph-2d` wrapper

**Files:**
- Modify: `frontend/package.json` (add `react-force-graph-2d`)
- Create: `frontend/src/components/graph/GraphCanvas.tsx`

This task is visually-driven; the only automated test is a smoke render. Verify by eye after Task 6 wires the page together.

- [ ] **Step 3.1: Add `react-force-graph-2d`**

```bash
cd frontend && npm install react-force-graph-2d
```
Expected: lockfile updates; no version warnings.

- [ ] **Step 3.2: Implement `GraphCanvas.tsx`**

```tsx
// frontend/src/components/graph/GraphCanvas.tsx
import { forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState } from "react";
import ForceGraph2D, { type ForceGraphMethods } from "react-force-graph-2d";
import { Pause, Play, Maximize2, Minus, Plus } from "lucide-react";
import { useTheme } from "@/hooks/use-theme";
import {
  RELATION_DASH,
  readColors,
  type GraphEdge,
  type GraphNode,
  type GraphColors,
} from "./graph-types";

export interface GraphCanvasHandle {
  centerOnNode: (uri: string) => void;
}

interface Props {
  nodes: GraphNode[];
  edges: GraphEdge[];
  selected?: string;
  pinned: Set<string>;
  hidden: Set<string>;
  degraded: boolean;
  onSelect: (uri: string | undefined) => void;
  onDoubleClick: (node: GraphNode) => void;
  onContextMenu: (
    node: GraphNode,
    screenX: number,
    screenY: number,
  ) => void;
}

interface RenderNode extends GraphNode {
  // react-force-graph mutates these
}
interface RenderEdge extends GraphEdge {
  source: string | RenderNode;
  target: string | RenderNode;
}

const NODE_SIZE = 16;
const LABEL_ZOOM_THRESHOLD = 0.5;

export const GraphCanvas = forwardRef<GraphCanvasHandle, Props>(function GraphCanvas(
  {
    nodes,
    edges,
    selected,
    pinned,
    hidden,
    degraded,
    onSelect,
    onDoubleClick,
    onContextMenu,
  },
  ref,
) {
  const fgRef = useRef<ForceGraphMethods<RenderNode, RenderEdge>>();
  const { theme } = useTheme();
  const [colors, setColors] = useState<GraphColors>(() => readColors());
  const [frozen, setFrozen] = useState(false);

  useImperativeHandle(
    ref,
    (): GraphCanvasHandle => ({
      centerOnNode: (uri) => {
        const n = nodes.find((node) => node.uri === uri);
        if (!n || n.x == null || n.y == null) return;
        fgRef.current?.centerAt(n.x, n.y, 400);
        fgRef.current?.zoom(Math.max(fgRef.current?.zoom() || 1, 1.5), 400);
      },
    }),
    [nodes],
  );

  // Re-read CSS variables on theme change so paint functions use the right palette.
  useEffect(() => {
    setColors(readColors());
  }, [theme]);

  // When degraded, kill simulation entirely.
  useEffect(() => {
    const fg = fgRef.current;
    if (!fg) return;
    if (degraded || frozen) {
      fg.d3Force("center", null);
      fg.pauseAnimation();
    } else {
      fg.resumeAnimation();
    }
  }, [degraded, frozen]);

  const visibleNodes = useMemo(
    () => nodes.filter((n) => !hidden.has(n.uri)),
    [nodes, hidden],
  );
  const visibleEdges = useMemo(
    () => edges.filter((e) => !hidden.has(e.source) && !hidden.has(e.target)),
    [edges, hidden],
  );

  const graphData = useMemo(
    () => ({ nodes: visibleNodes as RenderNode[], links: visibleEdges as RenderEdge[] }),
    [visibleNodes, visibleEdges],
  );

  const paintNode = useCallback(
    (n: RenderNode, ctx: CanvasRenderingContext2D, scale: number) => {
      const x = n.x || 0;
      const y = n.y || 0;
      const isSelected = n.uri === selected;
      const isPinned = pinned.has(n.uri);

      ctx.beginPath();
      ctx.rect(x - NODE_SIZE / 2, y - NODE_SIZE / 2, NODE_SIZE, NODE_SIZE);
      switch (n.kind) {
        case "document":
          ctx.fillStyle = colors.surfaceMuted;
          ctx.strokeStyle = isSelected ? colors.accent : colors.foreground;
          ctx.lineWidth = isSelected ? 2.5 : 1.5;
          ctx.setLineDash([]);
          break;
        case "table":
          ctx.fillStyle = colors.surface;
          ctx.strokeStyle = colors.accent;
          ctx.lineWidth = isSelected ? 2.5 : 1.5;
          ctx.setLineDash([]);
          break;
        case "file":
          ctx.fillStyle = "transparent";
          ctx.strokeStyle = isSelected ? colors.accent : colors.foregroundMuted;
          ctx.lineWidth = isSelected ? 2.5 : 1;
          ctx.setLineDash([3, 2]);
          break;
      }
      ctx.fill();
      ctx.stroke();
      ctx.setLineDash([]);

      if (isPinned) {
        ctx.fillStyle = colors.accent;
        ctx.fillRect(x + NODE_SIZE / 2 - 3, y - NODE_SIZE / 2, 3, 3);
      }

      if (scale > LABEL_ZOOM_THRESHOLD) {
        const label = (n.name || "").slice(0, 16).toUpperCase();
        ctx.font = `10px ui-monospace, SFMono-Regular, monospace`;
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.fillStyle = colors.foregroundMuted;
        ctx.fillText(label, x, y + NODE_SIZE / 2 + 4);
      }
    },
    [colors, selected, pinned],
  );

  const paintLink = useCallback(
    (l: RenderEdge, ctx: CanvasRenderingContext2D) => {
      const src = typeof l.source === "object" ? l.source : null;
      const tgt = typeof l.target === "object" ? l.target : null;
      if (!src || !tgt) return;
      const dash = RELATION_DASH[l.relation];
      const isAccent = l.relation === "implements" || l.relation === "derived_from";
      ctx.beginPath();
      ctx.moveTo(src.x || 0, src.y || 0);
      ctx.lineTo(tgt.x || 0, tgt.y || 0);
      ctx.strokeStyle = isAccent ? colors.accent : colors.foregroundMuted;
      ctx.lineWidth = l.relation === "attached_to" ? 1 : 1.5;
      ctx.setLineDash(dash);
      ctx.stroke();
      ctx.setLineDash([]);
    },
    [colors],
  );

  const handleNodeClick = useCallback(
    (n: RenderNode) => {
      onSelect(n.uri);
    },
    [onSelect],
  );

  const handleBackgroundClick = useCallback(() => {
    onSelect(undefined);
  }, [onSelect]);

  const handleNodeDoubleClick = useCallback(
    (n: RenderNode) => {
      onDoubleClick(n);
    },
    [onDoubleClick],
  );

  const handleNodeRightClick = useCallback(
    (n: RenderNode, ev: MouseEvent) => {
      ev.preventDefault();
      onContextMenu(n, ev.clientX, ev.clientY);
    },
    [onContextMenu],
  );

  const handleNodeDrag = useCallback((n: RenderNode, _t: { x: number; y: number }, ev?: MouseEvent) => {
    if (ev?.shiftKey) {
      n.fx = n.x;
      n.fy = n.y;
    }
  }, []);

  return (
    <div className="absolute inset-0">
      <div className="absolute top-3 left-3 z-10 flex gap-1">
        <CanvasButton
          onClick={() => setFrozen((f) => !f)}
          label={frozen ? "Resume" : "Freeze"}
          icon={frozen ? Play : Pause}
        />
        <CanvasButton
          onClick={() => fgRef.current?.zoomToFit(400, 60)}
          label="Fit"
          icon={Maximize2}
        />
        <CanvasButton
          onClick={() => fgRef.current?.zoom((fgRef.current?.zoom() || 1) * 0.8, 200)}
          label="Zoom out"
          icon={Minus}
        />
        <CanvasButton
          onClick={() => fgRef.current?.zoom((fgRef.current?.zoom() || 1) * 1.25, 200)}
          label="Zoom in"
          icon={Plus}
        />
      </div>
      <ForceGraph2D
        ref={fgRef as never}
        graphData={graphData}
        backgroundColor={colors.background}
        nodeId="uri"
        linkSource="source"
        linkTarget="target"
        nodeCanvasObject={paintNode}
        linkCanvasObject={paintLink}
        linkDirectionalArrowLength={5}
        linkDirectionalArrowRelPos={1}
        linkDirectionalArrowColor={() => colors.foregroundMuted}
        cooldownTicks={degraded ? 0 : 200}
        onNodeClick={handleNodeClick}
        onBackgroundClick={handleBackgroundClick}
        onNodeRightClick={handleNodeRightClick}
        onNodeDrag={handleNodeDrag}
        onNodeDragEnd={(n) => {
          if (!pinned.has((n as RenderNode).uri)) {
            (n as RenderNode).fx = undefined;
            (n as RenderNode).fy = undefined;
          }
        }}
        // double-click is not a built-in prop; emulate with onNodeClick + manual timing
      />
    </div>
  );
});

function CanvasButton({
  onClick,
  label,
  icon: Icon,
}: {
  onClick: () => void;
  label: string;
  icon: React.ComponentType<{ className?: string; "aria-hidden"?: boolean }>;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={label}
      aria-label={label}
      className="inline-flex items-center justify-center h-6 w-6 bg-surface border border-border text-foreground-muted hover:text-foreground hover:bg-surface-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background transition-colors cursor-pointer"
    >
      <Icon className="h-3 w-3" aria-hidden />
    </button>
  );
}
```

Note: the `react-force-graph-2d` API does not expose `onNodeDoubleClick` directly; the page shell (Task 6) will emulate double-click by tracking the time between two single-clicks on the same `uri`. The handler shape (`onDoubleClick`) stays so call sites are clean.

- [ ] **Step 3.3: Smoke render test**

```tsx
// frontend/src/components/graph/__tests__/GraphCanvas.smoke.test.tsx
import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import { GraphCanvas } from "../GraphCanvas";

describe("GraphCanvas smoke", () => {
  it("renders without throwing on empty input", () => {
    const { container } = render(
      <GraphCanvas
        nodes={[]}
        edges={[]}
        pinned={new Set()}
        hidden={new Set()}
        degraded={false}
        onSelect={() => {}}
        onDoubleClick={() => {}}
        onContextMenu={() => {}}
      />,
    );
    expect(container).toBeTruthy();
  });
});
```

- [ ] **Step 3.4: Run test**

```bash
cd frontend && npx vitest run src/components/graph/__tests__/GraphCanvas.smoke.test.tsx
```
Expected: PASS. (jsdom does not run Canvas paint; the test only proves the component mounts.)

- [ ] **Step 3.5: Typecheck**

```bash
cd frontend && npx tsc --noEmit
```
Expected: clean.

- [ ] **Step 3.6: Commit**

```bash
git add frontend/package.json frontend/package-lock.json \
        frontend/src/components/graph/GraphCanvas.tsx \
        frontend/src/components/graph/__tests__/GraphCanvas.smoke.test.tsx
git commit -m "feat(graph): canvas wrapper on react-force-graph-2d with AKB paint"
```

---

## Task 4 — Left sidebar (`GraphSidebar.tsx`)

**Files:**
- Create: `frontend/src/components/graph/GraphSidebar.tsx`
- Create: `frontend/src/components/graph/__tests__/GraphSidebar.test.tsx`

- [ ] **Step 4.1: Write failing tests**

```tsx
// frontend/src/components/graph/__tests__/GraphSidebar.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { GraphSidebar } from "../GraphSidebar";
import { DEFAULT_VIEW, type GraphView } from "../graph-types";

beforeEach(() => localStorage.clear());

function setup(view: Partial<GraphView> = {}) {
  const onChange = vi.fn();
  const onNavigate = vi.fn();
  render(
    <GraphSidebar
      vault="akb"
      view={{ ...DEFAULT_VIEW, ...view }}
      currentUrl="?"
      onChange={onChange}
      onNavigate={onNavigate}
    />,
  );
  return { onChange, onNavigate };
}

describe("GraphSidebar · types", () => {
  it("toggles a node type off", async () => {
    const u = userEvent.setup();
    const { onChange } = setup();
    await u.click(screen.getByRole("button", { name: /toggle document/i }));
    expect(onChange).toHaveBeenCalledTimes(1);
    const next: GraphView = onChange.mock.calls[0][0];
    expect(next.types.has("document")).toBe(false);
    expect(next.types.has("table")).toBe(true);
  });
});

describe("GraphSidebar · depth", () => {
  it("is disabled when no entry is set", () => {
    setup();
    const radio = screen.getByRole("radio", { name: /depth 3/i });
    expect(radio).toBeDisabled();
  });

  it("emits depth change when entry is set", async () => {
    const u = userEvent.setup();
    const { onChange } = setup({ entry: "d-1", depth: 2 });
    await u.click(screen.getByRole("radio", { name: /depth 3/i }));
    expect(onChange.mock.calls[0][0].depth).toBe(3);
  });
});

describe("GraphSidebar · saved + recent", () => {
  it("saves a named view and lists it", async () => {
    const u = userEvent.setup();
    setup({ entry: "d-1" });
    await u.click(screen.getByRole("button", { name: /save view/i }));
    const input = await screen.findByPlaceholderText(/name this view/i);
    await u.type(input, "roadmap{Enter}");
    expect(screen.getByText("roadmap")).toBeTruthy();
  });

  it("navigates when a saved view is clicked", async () => {
    const u = userEvent.setup();
    const { onNavigate } = setup({ entry: "d-1" });
    await u.click(screen.getByRole("button", { name: /save view/i }));
    const input = await screen.findByPlaceholderText(/name this view/i);
    await u.type(input, "roadmap{Enter}");
    await u.click(screen.getByText("roadmap"));
    expect(onNavigate).toHaveBeenCalledWith(expect.stringContaining("entry=d-1"));
  });

  it("renders recent entries from localStorage", () => {
    localStorage.setItem(
      "akb-graph-recent:akb",
      JSON.stringify([{ doc_id: "d-9", title: "Niner" }]),
    );
    setup();
    expect(screen.getByText("Niner")).toBeTruthy();
  });
});
```

- [ ] **Step 4.2: Run tests to verify they fail**

```bash
cd frontend && npx vitest run src/components/graph/__tests__/GraphSidebar.test.tsx
```
Expected: FAIL (module missing).

- [ ] **Step 4.3: Implement `GraphSidebar.tsx`**

```tsx
// frontend/src/components/graph/GraphSidebar.tsx
import { useEffect, useMemo, useRef, useState } from "react";
import { Search as SearchIcon, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { searchDocs } from "@/lib/api";
import { useGraphHistory } from "@/hooks/use-graph-history";
import {
  ALL_NODE_KINDS,
  ALL_RELATIONS,
  type GraphView,
  type NodeKind,
  type RelationKind,
} from "./graph-types";
import { cn } from "@/lib/utils";

interface Props {
  vault: string;
  view: GraphView;
  currentUrl: string; // e.g. "?entry=d-1&depth=2" — used when saving a view
  onChange: (next: GraphView) => void;
  onNavigate: (queryString: string) => void;
}

interface SearchHit {
  doc_id: string;
  title: string;
  type: NodeKind;
}

export function GraphSidebar({ vault, view, currentUrl, onChange, onNavigate }: Props) {
  const { recent, pushRecent, clearRecent, saved, saveView, deleteView } =
    useGraphHistory(vault);
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [savingName, setSavingName] = useState<string | null>(null);
  const saveNameRef = useRef<HTMLInputElement>(null);

  // Debounced search → /search
  useEffect(() => {
    if (!query.trim()) {
      setHits([]);
      return;
    }
    const handle = setTimeout(async () => {
      try {
        // searchDocs(query, vault?, limit) — order: query first, vault second
        const resp = await searchDocs(query.trim(), vault, 8);
        const rows = (resp.results || []).slice(0, 8).map((r: any) => ({
          doc_id: r.doc_id || r.id,
          title: r.title || r.name || r.doc_id || "(untitled)",
          type: (r.resource_type || r.type || "document") as NodeKind,
        }));
        setHits(rows);
      } catch {
        setHits([]);
      }
    }, 300);
    return () => clearTimeout(handle);
  }, [query, vault]);

  function commitEntry(hit: SearchHit) {
    pushRecent({ doc_id: hit.doc_id, title: hit.title });
    onChange({ ...view, entry: hit.doc_id });
    setQuery("");
    setHits([]);
  }

  function toggleType(k: NodeKind) {
    const next = new Set(view.types);
    next.has(k) ? next.delete(k) : next.add(k);
    onChange({ ...view, types: next });
  }
  function toggleRelation(r: RelationKind) {
    const next = new Set(view.relations);
    next.has(r) ? next.delete(r) : next.add(r);
    onChange({ ...view, relations: next });
  }

  function beginSave() {
    setSavingName("");
    setTimeout(() => saveNameRef.current?.focus(), 0);
  }
  function commitSave(name: string) {
    const trimmed = name.trim();
    if (!trimmed) {
      setSavingName(null);
      return;
    }
    saveView(trimmed, currentUrl);
    setSavingName(null);
  }

  return (
    <aside
      className="flex flex-col h-full overflow-y-auto border-r border-border bg-surface"
      aria-label="Graph controls"
    >
      <Section label="ENTRY POINT">
        <div className="relative">
          <SearchIcon className="absolute left-2 top-1/2 -translate-y-1/2 h-3 w-3 text-foreground-muted pointer-events-none" />
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search documents"
            aria-label="Search documents"
            className="w-full h-9 pl-6 pr-2 bg-background border border-border text-[11px] focus:outline-none focus:border-accent"
          />
        </div>
        {hits.length > 0 && (
          <ul className="mt-1 flex flex-col gap-px">
            {hits.map((h) => (
              <li key={h.doc_id}>
                <button
                  type="button"
                  onClick={() => commitEntry(h)}
                  className="w-full flex items-center justify-between gap-2 px-2 h-7 text-left text-[11px] hover:bg-surface-muted"
                >
                  <span className="truncate">{h.title}</span>
                  <span className="coord">{h.type}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
        {view.entry && hits.length === 0 && (
          <div className="mt-1 flex items-center justify-between gap-2 px-2 h-7 text-[11px] bg-surface-muted">
            <span className="truncate">entry: {view.entry}</span>
            <button
              type="button"
              onClick={() => onChange({ ...view, entry: undefined })}
              aria-label="Clear entry"
              className="text-foreground-muted hover:text-foreground"
            >
              <X className="h-3 w-3" />
            </button>
          </div>
        )}
      </Section>

      <Section label="DEPTH">
        <div className="flex items-center gap-3">
          {[1, 2, 3].map((d) => (
            <label key={d} className="inline-flex items-center gap-1 text-[11px] cursor-pointer">
              <input
                type="radio"
                name="depth"
                checked={view.depth === d}
                disabled={!view.entry}
                onChange={() => onChange({ ...view, depth: d as 1 | 2 | 3 })}
                aria-label={`Depth ${d}`}
              />
              {d}
            </label>
          ))}
        </div>
      </Section>

      <Section label="TYPES">
        <div className="flex flex-wrap gap-1">
          {ALL_NODE_KINDS.map((k) => (
            <button
              key={k}
              type="button"
              onClick={() => toggleType(k)}
              aria-label={`Toggle ${k}`}
              aria-pressed={view.types.has(k)}
              className={cn(
                "px-1.5 py-0.5 border font-mono text-[10px] uppercase tracking-[0.12em]",
                view.types.has(k)
                  ? "border-foreground text-foreground"
                  : "border-border text-foreground-muted opacity-50",
              )}
            >
              {k}
            </button>
          ))}
        </div>
      </Section>

      <Section label="RELATIONS">
        <div className="grid grid-cols-2 gap-1">
          {ALL_RELATIONS.map((r) => (
            <button
              key={r}
              type="button"
              onClick={() => toggleRelation(r)}
              aria-label={`Toggle ${r}`}
              aria-pressed={view.relations.has(r)}
              className={cn(
                "px-1.5 py-0.5 border font-mono text-[10px] uppercase tracking-[0.12em] text-left",
                view.relations.has(r)
                  ? "border-foreground text-foreground"
                  : "border-border text-foreground-muted opacity-50",
              )}
            >
              {r}
            </button>
          ))}
        </div>
      </Section>

      <Section
        label="RECENT"
        rightAction={
          recent.length > 0 ? (
            <button type="button" onClick={clearRecent} className="coord hover:text-foreground">
              clear
            </button>
          ) : null
        }
      >
        {recent.length === 0 ? (
          <p className="coord text-foreground-muted">none</p>
        ) : (
          <ul className="flex flex-col gap-px">
            {recent.map((r) => (
              <li key={r.doc_id}>
                <button
                  type="button"
                  onClick={() => onChange({ ...view, entry: r.doc_id })}
                  className="w-full text-left px-2 h-7 text-[11px] hover:bg-surface-muted truncate"
                >
                  ‹ {r.title}
                </button>
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section
        label="SAVED VIEWS"
        rightAction={
          <button
            type="button"
            onClick={beginSave}
            aria-label="Save view"
            className="coord hover:text-foreground"
          >
            + save
          </button>
        }
      >
        {savingName !== null && (
          <input
            ref={saveNameRef}
            value={savingName}
            onChange={(e) => setSavingName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") commitSave(savingName);
              if (e.key === "Escape") setSavingName(null);
            }}
            onBlur={() => commitSave(savingName)}
            placeholder="Name this view"
            aria-label="View name"
            className="w-full h-7 px-2 bg-background border border-accent text-[11px] focus:outline-none mb-1"
          />
        )}
        {saved.length === 0 && savingName === null ? (
          <p className="coord text-foreground-muted">none</p>
        ) : (
          <ul className="flex flex-col gap-px">
            {saved.map((s) => (
              <li key={s.name} className="flex items-center gap-1">
                <button
                  type="button"
                  onClick={() => onNavigate(s.url)}
                  className="flex-1 text-left px-2 h-7 text-[11px] hover:bg-surface-muted truncate"
                >
                  ★ {s.name}
                </button>
                <button
                  type="button"
                  onClick={() => deleteView(s.name)}
                  aria-label={`Delete ${s.name}`}
                  className="text-foreground-muted hover:text-destructive px-1"
                >
                  <X className="h-3 w-3" />
                </button>
              </li>
            ))}
          </ul>
        )}
      </Section>
    </aside>
  );
}

function Section({
  label,
  rightAction,
  children,
}: {
  label: string;
  rightAction?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="border-b border-border px-2 py-3">
      <div className="flex items-center justify-between mb-2">
        <span className="coord">§ {label}</span>
        {rightAction}
      </div>
      {children}
    </section>
  );
}
```

- [ ] **Step 4.4: Confirm `searchDocs` signature is unchanged**

```bash
cd frontend && grep -nA2 "export const searchDocs" src/lib/api.ts
```
Expected: `searchDocs = (query: string, vault?: string, limit = 10)`. If the signature has drifted, adjust the call site in `GraphSidebar.tsx`.

- [ ] **Step 4.5: Run tests to verify they pass**

```bash
cd frontend && npx vitest run src/components/graph/__tests__/GraphSidebar.test.tsx
```
Expected: PASS — 5/5.

- [ ] **Step 4.6: Commit**

```bash
git add frontend/src/components/graph/GraphSidebar.tsx \
        frontend/src/components/graph/__tests__/GraphSidebar.test.tsx
git commit -m "feat(graph): left sidebar — entry search, depth, filters, recent, saved"
```

---

## Task 5 — Right detail panel (`GraphDetailPanel.tsx`)

**Files:**
- Modify: `frontend/src/lib/api.ts` — add `getProvenance`, `drillDown` if absent
- Create: `frontend/src/components/graph/GraphDetailPanel.tsx`
- Create: `frontend/src/components/graph/__tests__/GraphDetailPanel.test.tsx`

- [ ] **Step 5.1: Ensure api.ts has `getProvenance` + `drillDown`**

Grep for them:

```bash
cd frontend && grep -nE "getProvenance|drillDown" src/lib/api.ts
```

If missing, append to `frontend/src/lib/api.ts`:

```ts
// ── Provenance / drill-down ──
export const getProvenance = (docId: string) =>
  api<{ provenance: any }>(`/provenance/${encodeURIComponent(docId)}`);

export const drillDown = (vault: string, docId: string, section?: string) => {
  const p = section ? `?section=${encodeURIComponent(section)}` : "";
  return api<{ doc_id: string; vault: string; sections: any[] }>(
    `/drill-down/${vault}/${encodeURIComponent(docId)}${p}`,
  );
};
```

- [ ] **Step 5.2: Write failing tests**

```tsx
// frontend/src/components/graph/__tests__/GraphDetailPanel.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { GraphDetailPanel } from "../GraphDetailPanel";

const getDocument = vi.fn();
const getRelations = vi.fn();
const getProvenance = vi.fn();
const drillDown = vi.fn();

vi.mock("@/lib/api", () => ({
  getDocument: (...args: unknown[]) => getDocument(...args),
  getRelations: (...args: unknown[]) => getRelations(...args),
  getProvenance: (...args: unknown[]) => getProvenance(...args),
  drillDown: (...args: unknown[]) => drillDown(...args),
}));

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>;
}

beforeEach(() => {
  getDocument.mockReset();
  getRelations.mockReset();
  getProvenance.mockReset();
  drillDown.mockReset();
});

describe("GraphDetailPanel · document node", () => {
  it("renders title, summary, preview, and relations after fetch", async () => {
    getDocument.mockResolvedValue({
      doc_id: "d-1",
      title: "Hello",
      summary: "A nice doc",
      tags: ["alpha", "beta"],
      content: "line1\nline2\nline3",
      type: "document",
    });
    getRelations.mockResolvedValue({
      doc_id: "d-1",
      resource_uri: "akb://akb/doc/x",
      relations: [
        {
          source: "akb://akb/doc/x",
          target: "akb://akb/doc/y",
          relation: "depends_on",
          other_name: "Y",
          other_type: "document",
        },
      ],
    });

    render(
      wrap(
        <GraphDetailPanel
          vault="akb"
          docId="d-1"
          kind="document"
          uri="akb://akb/doc/x"
          onSelectUri={() => {}}
          onFitToNode={() => {}}
          onClose={() => {}}
        />,
      ),
    );

    expect(await screen.findByText("Hello")).toBeTruthy();
    expect(screen.getByText("A nice doc")).toBeTruthy();
    expect(screen.getByText("alpha")).toBeTruthy();
    expect(screen.getByText(/depends_on/i)).toBeTruthy();
    expect(screen.getByText(/line1/)).toBeTruthy();
  });

  it("defers META fetches until the section expands", async () => {
    getDocument.mockResolvedValue({ doc_id: "d-1", title: "x", content: "" });
    getRelations.mockResolvedValue({
      doc_id: "d-1",
      resource_uri: "u",
      relations: [],
    });
    getProvenance.mockResolvedValue({ provenance: { source: "manual" } });

    const u = userEvent.setup();
    render(
      wrap(
        <GraphDetailPanel
          vault="akb"
          docId="d-1"
          kind="document"
          uri="u"
          onSelectUri={() => {}}
          onFitToNode={() => {}}
          onClose={() => {}}
        />,
      ),
    );
    await screen.findByText("x");
    expect(getProvenance).not.toHaveBeenCalled();
    await u.click(screen.getByRole("button", { name: /toggle meta/i }));
    await waitFor(() => expect(getProvenance).toHaveBeenCalledWith("d-1"));
  });
});

describe("GraphDetailPanel · table node", () => {
  it("does not show preview for tables", async () => {
    getDocument.mockResolvedValue({
      doc_id: "t-1",
      title: "Things",
      columns: ["a", "b"],
      type: "table",
    });
    getRelations.mockResolvedValue({ doc_id: "t-1", resource_uri: "u", relations: [] });
    render(
      wrap(
        <GraphDetailPanel
          vault="akb"
          docId="t-1"
          kind="table"
          uri="u"
          onSelectUri={() => {}}
          onFitToNode={() => {}}
          onClose={() => {}}
        />,
      ),
    );
    await screen.findByText("Things");
    expect(screen.queryByText(/§ PREVIEW/i)).toBeNull();
  });
});
```

- [ ] **Step 5.3: Run tests to verify they fail**

```bash
cd frontend && npx vitest run src/components/graph/__tests__/GraphDetailPanel.test.tsx
```
Expected: FAIL.

- [ ] **Step 5.4: Implement `GraphDetailPanel.tsx`**

```tsx
// frontend/src/components/graph/GraphDetailPanel.tsx
import { useState } from "react";
import { ChevronDown, ChevronRight, ExternalLink, Pin, X } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { getDocument, getRelations, getProvenance, drillDown } from "@/lib/api";
import { type NodeKind, type RelationKind } from "./graph-types";
import { cn } from "@/lib/utils";

interface Props {
  vault: string;
  docId: string;
  kind: NodeKind;
  uri: string;
  onSelectUri: (uri: string) => void;
  onFitToNode: (uri: string) => void;
  onClose: () => void;
  onTogglePin?: () => void;
  pinned?: boolean;
}

interface DocResponse {
  doc_id: string;
  title?: string;
  summary?: string;
  tags?: string[];
  content?: string;
  type?: string;
  columns?: string[];
  mime_type?: string;
  size_bytes?: number;
  author?: string;
  created_at?: string;
  updated_at?: string;
}

const PREVIEW_LINES = 40;

export function GraphDetailPanel({
  vault,
  docId,
  kind,
  uri,
  onSelectUri,
  onFitToNode,
  onClose,
  onTogglePin,
  pinned,
}: Props) {
  const [metaOpen, setMetaOpen] = useState(false);
  const [sectionsOpen, setSectionsOpen] = useState(false);

  const docQuery = useQuery<DocResponse>({
    queryKey: ["document", vault, docId],
    queryFn: () => getDocument(vault, docId) as Promise<DocResponse>,
  });
  const relQuery = useQuery({
    queryKey: ["relations", vault, docId],
    queryFn: () => getRelations(vault, docId),
  });
  const provQuery = useQuery({
    queryKey: ["provenance", docId],
    queryFn: () => getProvenance(docId),
    enabled: metaOpen,
  });
  const secQuery = useQuery({
    queryKey: ["drill", vault, docId],
    queryFn: () => drillDown(vault, docId),
    enabled: sectionsOpen,
  });

  const doc = docQuery.data;
  const preview = (doc?.content || "").split("\n").slice(0, PREVIEW_LINES).join("\n");

  const groupedRels = groupRelations(relQuery.data?.relations || [], uri);
  const totalRels =
    groupedRels.incoming.reduce((s, g) => s + g.rows.length, 0) +
    groupedRels.outgoing.reduce((s, g) => s + g.rows.length, 0);

  function openDoc() {
    const path = doc?.title ? `/vault/${vault}/${kind === "table" ? "table" : kind === "file" ? "file" : "doc"}/${encodeURIComponent(docId)}` : "#";
    window.location.assign(path);
  }

  return (
    <aside className="flex flex-col h-full overflow-y-auto border-l border-border bg-surface">
      <header className="flex items-center justify-between px-3 py-2 border-b border-border">
        <span className="coord">{(kind || "document").toUpperCase()} · {docId.toUpperCase()}</span>
        <button onClick={onClose} aria-label="Close detail" className="text-foreground-muted hover:text-foreground">
          <X className="h-3 w-3" />
        </button>
      </header>

      <div className="px-3 py-3 border-b border-border">
        <h2 className="font-serif text-2xl leading-tight mb-1">{doc?.title || "…"}</h2>
        <p className="coord text-foreground-muted truncate">{uri}</p>
        <div className="flex flex-wrap gap-1 mt-3">
          <Button size="sm" variant="accent" onClick={openDoc}>
            <ExternalLink className="h-3 w-3" /> Open
          </Button>
          <Button size="sm" variant="outline" onClick={() => navigator.clipboard.writeText(uri)}>
            Copy URI
          </Button>
          {onTogglePin && (
            <Button size="sm" variant={pinned ? "accent" : "outline"} onClick={onTogglePin}>
              <Pin className="h-3 w-3" /> {pinned ? "Pinned" : "Pin"}
            </Button>
          )}
        </div>
      </div>

      {doc?.summary && (
        <Section label="SUMMARY">
          <p className="text-[12px] leading-relaxed text-foreground">{doc.summary}</p>
        </Section>
      )}

      {kind === "table" && doc?.columns && (
        <Section label="COLUMNS">
          <ul className="flex flex-wrap gap-1">
            {doc.columns.map((c) => (
              <li key={c}>
                <Badge variant="outline">{c}</Badge>
              </li>
            ))}
          </ul>
        </Section>
      )}

      {kind === "file" && (
        <Section label="FILE">
          <p className="coord">
            {doc?.mime_type || "—"} · {doc?.size_bytes ? `${doc.size_bytes} bytes` : "—"}
          </p>
        </Section>
      )}

      {doc?.tags && doc.tags.length > 0 && (
        <Section label="TAGS">
          <div className="flex flex-wrap gap-1">
            {doc.tags.map((t) => (
              <Badge key={t} variant="outline">{t}</Badge>
            ))}
          </div>
        </Section>
      )}

      <Section label={`RELATIONS [${totalRels}]`}>
        {totalRels === 0 ? (
          <p className="coord text-foreground-muted">none</p>
        ) : (
          <div className="flex flex-col gap-2">
            {groupedRels.outgoing.map((g) => (
              <RelGroup
                key={`out-${g.relation}`}
                relation={g.relation}
                direction="out"
                rows={g.rows}
                onSelectUri={onSelectUri}
                onFitToNode={onFitToNode}
              />
            ))}
            {groupedRels.incoming.map((g) => (
              <RelGroup
                key={`in-${g.relation}`}
                relation={g.relation}
                direction="in"
                rows={g.rows}
                onSelectUri={onSelectUri}
                onFitToNode={onFitToNode}
              />
            ))}
          </div>
        )}
      </Section>

      {kind === "document" && (
        <Section label="PREVIEW">
          <button
            type="button"
            onClick={() => setSectionsOpen((v) => !v)}
            className="coord hover:text-foreground inline-flex items-center gap-1 mb-2"
            aria-expanded={sectionsOpen}
          >
            {sectionsOpen ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
            show sections
          </button>
          {sectionsOpen && (
            <ul className="mb-2 flex flex-col gap-px">
              {(secQuery.data?.sections || []).map((s: any, i) => (
                <li key={i} className="coord truncate">‣ {s.heading || s.title || `section ${i}`}</li>
              ))}
            </ul>
          )}
          <pre className="font-mono text-[11px] leading-snug whitespace-pre-wrap text-foreground bg-background border border-border p-2 max-h-64 overflow-auto">
            {preview || "(empty)"}
          </pre>
        </Section>
      )}

      <Section
        label="META"
        rightAction={
          <button
            type="button"
            onClick={() => setMetaOpen((v) => !v)}
            aria-label="Toggle meta"
            aria-expanded={metaOpen}
            className="coord hover:text-foreground inline-flex items-center gap-1"
          >
            {metaOpen ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
          </button>
        }
      >
        {metaOpen && (
          <div className="flex flex-col gap-1 text-[11px]">
            <p>
              <span className="coord">author</span> · {doc?.author || "—"}
            </p>
            <p>
              <span className="coord">created</span> · {doc?.created_at || "—"}
            </p>
            <p>
              <span className="coord">updated</span> · {doc?.updated_at || "—"}
            </p>
            <p className="break-all">
              <span className="coord">provenance</span> · {JSON.stringify(provQuery.data?.provenance || "—")}
            </p>
          </div>
        )}
      </Section>
    </aside>
  );
}

function Section({
  label,
  rightAction,
  children,
}: {
  label: string;
  rightAction?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="border-b border-border px-3 py-3">
      <div className="flex items-center justify-between mb-2">
        <span className="coord">§ {label}</span>
        {rightAction}
      </div>
      {children}
    </section>
  );
}

interface GroupedRel {
  relation: RelationKind;
  rows: Array<{
    other_uri: string;
    other_name: string;
    other_type: NodeKind;
    direction: "in" | "out";
  }>;
}

function groupRelations(
  rows: Array<{ source: string; target: string; relation: string; other_uri?: string; other_name?: string; other_type?: string }>,
  selfUri: string,
): { incoming: GroupedRel[]; outgoing: GroupedRel[] } {
  const out: Map<string, GroupedRel> = new Map();
  const inc: Map<string, GroupedRel> = new Map();
  for (const r of rows) {
    const rel = r.relation as RelationKind;
    const isOut = r.source === selfUri;
    const map = isOut ? out : inc;
    if (!map.has(rel)) map.set(rel, { relation: rel, rows: [] });
    map.get(rel)!.rows.push({
      other_uri: r.other_uri || (isOut ? r.target : r.source),
      other_name: r.other_name || "(unnamed)",
      other_type: ((r.other_type as NodeKind) || "document"),
      direction: isOut ? "out" : "in",
    });
  }
  return { outgoing: [...out.values()], incoming: [...inc.values()] };
}

function RelGroup({
  relation,
  direction,
  rows,
  onSelectUri,
  onFitToNode,
}: {
  relation: RelationKind;
  direction: "in" | "out";
  rows: GroupedRel["rows"];
  onSelectUri: (uri: string) => void;
  onFitToNode: (uri: string) => void;
}) {
  return (
    <div>
      <p className="coord mb-1">{direction === "out" ? "→" : "←"} {relation} ({rows.length})</p>
      <ul className="flex flex-col gap-px pl-2">
        {rows.map((r) => (
          <li key={r.other_uri} className="flex items-center gap-1">
            <button
              type="button"
              onClick={() => onSelectUri(r.other_uri)}
              className="flex-1 text-left text-[11px] hover:text-accent truncate"
            >
              {r.other_name}
            </button>
            <button
              type="button"
              onClick={() => onFitToNode(r.other_uri)}
              aria-label="Center on node"
              className="text-foreground-muted hover:text-foreground"
            >
              ⌖
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
```

- [ ] **Step 5.5: Run tests to verify they pass**

```bash
cd frontend && npx vitest run src/components/graph/__tests__/GraphDetailPanel.test.tsx
```
Expected: PASS — 3/3.

- [ ] **Step 5.6: Commit**

```bash
git add frontend/src/lib/api.ts \
        frontend/src/components/graph/GraphDetailPanel.tsx \
        frontend/src/components/graph/__tests__/GraphDetailPanel.test.tsx
git commit -m "feat(graph): detail panel — meta, relations, preview, lazy provenance/drill"
```

---

## Task 6 — Page integration (`pages/graph.tsx` rewrite)

**Files:**
- Rewrite: `frontend/src/pages/graph.tsx`

- [ ] **Step 6.1: Replace the page**

```tsx
// frontend/src/pages/graph.tsx
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { PanelLeftClose, PanelLeftOpen, PanelRightClose, PanelRightOpen } from "lucide-react";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/empty-state";
import { GraphCanvas, type GraphCanvasHandle } from "@/components/graph/GraphCanvas";
import { GraphSidebar } from "@/components/graph/GraphSidebar";
import { GraphDetailPanel } from "@/components/graph/GraphDetailPanel";
import {
  useFullGraph,
  useNeighborhood,
  applyFilters,
  mergeGraph,
  isDegraded,
  docIdFromUri,
} from "@/components/graph/use-graph-data";
import { viewToQuery, queryToView } from "@/components/graph/graph-state";
import { type GraphEdge, type GraphNode, type GraphView } from "@/components/graph/graph-types";

const DOUBLECLICK_MS = 250;

export default function GraphPage() {
  const { name: vault } = useParams<{ name: string }>();
  const [search, setSearch] = useSearchParams();
  const navigate = useNavigate();

  const view: GraphView = useMemo(() => queryToView(search), [search]);

  const setView = useCallback(
    (next: GraphView) => {
      setSearch(new URLSearchParams(viewToQuery(next)), { replace: true });
    },
    [setSearch],
  );

  // Hybrid fetch
  const fullQuery = useFullGraph(vault!, !view.entry);
  const neighborQuery = useNeighborhood(vault!, view.entry, view.depth);
  const base = view.entry ? neighborQuery.data : fullQuery.data;
  const loading = view.entry ? neighborQuery.isLoading : fullQuery.isLoading;
  const error = view.entry ? neighborQuery.error : fullQuery.error;
  const rawNodeCount = base?.nodes.length || 0;
  const degraded = isDegraded(rawNodeCount);

  // Click-expand merges into a session-scoped overlay so URL state stays clean.
  const [overlay, setOverlay] = useState<{ nodes: GraphNode[]; edges: GraphEdge[] }>({
    nodes: [],
    edges: [],
  });
  // Reset overlay when the base shape (mode/entry/depth) changes:
  useEffect(() => {
    setOverlay({ nodes: [], edges: [] });
  }, [view.entry, view.depth, vault]);

  const merged = useMemo(() => {
    const m = mergeGraph(base || { nodes: [], edges: [] }, overlay);
    return applyFilters(m, view);
  }, [base, overlay, view]);

  const canvasRef = useRef<GraphCanvasHandle>(null);

  // UI-only state (not URL)
  const [pinned, setPinned] = useState<Set<string>>(new Set());
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const lastClickRef = useRef<{ uri: string; at: number } | null>(null);

  // Double-click emulation: two clicks on the same URI within DOUBLECLICK_MS
  // navigate to the doc; otherwise the first click is treated as select.
  function handleSelect(uri: string | undefined) {
    if (uri && lastClickRef.current && lastClickRef.current.uri === uri && Date.now() - lastClickRef.current.at < DOUBLECLICK_MS) {
      const node = merged.nodes.find((n) => n.uri === uri);
      if (node) handleDoubleClick(node);
      lastClickRef.current = null;
      return;
    }
    lastClickRef.current = uri ? { uri, at: Date.now() } : null;
    setView({ ...view, selected: uri });
  }

  function handleDoubleClick(node: GraphNode) {
    const id = node.doc_id || docIdFromUri(node.uri);
    if (!id) return;
    const segment = node.kind === "table" ? "table" : node.kind === "file" ? "file" : "doc";
    navigate(`/vault/${vault}/${segment}/${encodeURIComponent(id)}`);
  }

  function handleContextMenu(_n: GraphNode, _x: number, _y: number) {
    // Context menu wiring deferred — fall back to plain select for now.
    // TODO(graph-context-menu): floating menu with Pin/Unpin/Hide/Copy/Open-new-tab.
  }

  const selectedNode = view.selected
    ? merged.nodes.find((n) => n.uri === view.selected)
    : undefined;
  const selectedDocId = selectedNode ? (selectedNode.doc_id || docIdFromUri(selectedNode.uri)) : null;
  const detailOpen = !!selectedNode && !!selectedDocId;

  const gridCols = `${sidebarOpen ? "240px" : "40px"} 1fr ${detailOpen ? "320px" : "0px"}`;

  return (
    <div
      className="grid grid-cols-[var(--gcols)] h-full min-h-0"
      style={{ ["--gcols" as any]: gridCols }}
    >
      {sidebarOpen ? (
        <GraphSidebar
          vault={vault!}
          view={view}
          currentUrl={"?" + viewToQuery(view)}
          onChange={setView}
          onNavigate={(qs) => {
            navigate({ search: qs.startsWith("?") ? qs : `?${qs}` }, { replace: true });
          }}
        />
      ) : (
        <button
          type="button"
          onClick={() => setSidebarOpen(true)}
          aria-label="Open sidebar"
          className="h-9 w-9 m-2 inline-flex items-center justify-center border border-border bg-surface text-foreground-muted hover:text-foreground"
        >
          <PanelLeftOpen className="h-4 w-4" />
        </button>
      )}

      <div className="relative bg-background overflow-hidden">
        <button
          type="button"
          onClick={() => setSidebarOpen((v) => !v)}
          aria-label={sidebarOpen ? "Hide sidebar" : "Show sidebar"}
          className="absolute top-3 right-3 z-10 h-6 w-6 inline-flex items-center justify-center bg-surface border border-border text-foreground-muted hover:text-foreground"
        >
          {sidebarOpen ? <PanelLeftClose className="h-3 w-3" /> : <PanelLeftOpen className="h-3 w-3" />}
        </button>

        {degraded && (
          <div className="absolute top-3 left-1/2 -translate-x-1/2 z-10 bg-warning/10 border border-warning text-warning px-3 py-1 font-mono text-[10px] uppercase">
            {rawNodeCount} nodes — pick an entry point to explore
          </div>
        )}

        {loading ? (
          <div className="p-8"><Skeleton className="h-64 w-full" /></div>
        ) : error ? (
          <EmptyState title="Failed to load graph" description={String(error)} />
        ) : merged.nodes.length === 0 ? (
          <EmptyState title="Empty graph" description="No relations match the current filters." />
        ) : (
          <GraphCanvas
            ref={canvasRef}
            nodes={merged.nodes}
            edges={merged.edges}
            selected={view.selected}
            pinned={pinned}
            hidden={hidden}
            degraded={degraded}
            onSelect={handleSelect}
            onDoubleClick={handleDoubleClick}
            onContextMenu={handleContextMenu}
          />
        )}
      </div>

      {detailOpen && selectedNode && selectedDocId ? (
        <GraphDetailPanel
          vault={vault!}
          docId={selectedDocId}
          kind={selectedNode.kind}
          uri={selectedNode.uri}
          onSelectUri={(uri) => setView({ ...view, selected: uri })}
          onFitToNode={(uri) => canvasRef.current?.centerOnNode(uri)}
          onClose={() => setView({ ...view, selected: undefined })}
          onTogglePin={() => {
            setPinned((prev) => {
              const next = new Set(prev);
              next.has(selectedNode.uri) ? next.delete(selectedNode.uri) : next.add(selectedNode.uri);
              return next;
            });
          }}
          pinned={pinned.has(selectedNode.uri)}
        />
      ) : null}
    </div>
  );
}
```

- [ ] **Step 6.2: Typecheck**

```bash
cd frontend && npx tsc --noEmit
```
Expected: clean. (`getDocument` is confirmed exported from `frontend/src/lib/api.ts:209`; if any import resolves wrong it's a typo in the new files, not a renamed API.)

- [ ] **Step 6.3: Run full Vitest suite**

```bash
cd frontend && npx vitest run
```
Expected: all green. The new page has no dedicated test (covered by component tests + manual integration).

- [ ] **Step 6.4: Manual smoke**

Start dev server and visit two routes:

```bash
cd frontend && npm run dev
```

- `http://localhost:5173/vault/gnu-weekly/graph` → small vault, Full mode renders, click a node, detail panel opens, double-click navigates, sidebar filters work, save view roundtrips.
- `http://localhost:5173/vault/seahorse-kb/graph` → large vault, expect degrade banner; pick an entry from search → mode flips, BFS expands.

Confirm: theme toggle still updates canvas colors; pin (Shift+drag or Pin button) keeps a node in place; clear filter combinations don't desync the URL.

- [ ] **Step 6.5: Commit**

```bash
git add frontend/src/pages/graph.tsx
git commit -m "feat(graph): page integration — 3-col shell, hybrid fetch, dblclick emu"
```

---

## Task 7 — Cleanup: remove cytoscape

**Files:**
- Modify: `frontend/package.json` — drop cytoscape deps
- Verify: no remaining cytoscape imports

- [ ] **Step 7.1: Verify no remaining cytoscape imports**

```bash
cd frontend && grep -rn "cytoscape" src/
```
Expected: zero matches. (If any survive, fix them before continuing.)

- [ ] **Step 7.2: Remove deps**

```bash
cd frontend && npm uninstall cytoscape cytoscape-fcose cytoscape-cola
```
Expected: `package.json` and `package-lock.json` shrink.

- [ ] **Step 7.3: Build to confirm no broken imports**

```bash
cd frontend && npx vite build
```
Expected: build succeeds. Bundle size should be smaller than before; no missing-module errors.

- [ ] **Step 7.4: Full type + test sweep**

```bash
cd frontend && npx tsc --noEmit && npx vitest run
```
Expected: clean.

- [ ] **Step 7.5: Commit**

```bash
git add frontend/package.json frontend/package-lock.json
git commit -m "chore(graph): drop cytoscape + fcose + cola (replaced by force-graph-2d)"
```

---

## Out of scope (do not implement)

- Backend-stored shared `saved_graph_views` — URL representation is forward-compatible.
- Floating right-click context menu — page emits the hook (`onContextMenu`) but the menu UI is a follow-up. The `TODO(graph-context-menu)` is intentional.
- Per-document version history — backend lacks a REST endpoint today.
- Path-finding / cluster detection / annotations.

## Skills referenced

- @superpowers:test-driven-development — every task above follows red → green → commit.
- @superpowers:subagent-driven-development — the recommended way to execute this plan.

## Final integration checklist

After all 7 tasks merge, verify on the deployed cluster:

- [ ] `/vault/{small-vault}/graph` renders Full mode without degrade banner
- [ ] `/vault/{large-vault}/graph` renders degrade banner; entry search recovers
- [ ] URL roundtrips: copy URL with filters → paste in new tab → identical view
- [ ] Saved view appears across reloads of the same browser
- [ ] Theme toggle (light ↔ dark) updates the canvas palette live
- [ ] `cytoscape` is gone from `package-lock.json`
