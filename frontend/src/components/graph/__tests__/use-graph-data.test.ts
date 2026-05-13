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
