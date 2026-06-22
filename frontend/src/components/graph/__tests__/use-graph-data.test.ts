import { describe, it, expect, vi } from "vitest";
import {
  mergeGraph,
  bfsExpand,
  applyFilters,
  docIdFromUri,
  endpointUri,
  degreeMap,
  impactCones,
} from "../use-graph-data";
import { docUri } from "@/lib/uri";
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

  it("survives force-graph mutating edge.source/target into node-object references", () => {
    // react-force-graph rewrites edge.source/target from string URI to the
    // resolved node object after the simulation starts. The filter must
    // still match endpoints by URI and emit fresh string-source edges
    // so the next render's force-graph can resolve them again.
    const mutatedEdges = [
      { source: nodes[0], target: nodes[1], relation: "depends_on" as const },
      { source: nodes[1], target: nodes[2], relation: "references" as const },
    ];
    const out = applyFilters({ nodes, edges: mutatedEdges as unknown as GraphEdge[] }, DEFAULT_VIEW);
    expect(out.edges.length).toBe(2);
    for (const e of out.edges) {
      expect(typeof e.source).toBe("string");
      expect(typeof e.target).toBe("string");
    }
    expect(out.edges.map((e) => e.source)).toEqual(["a", "b"]);
    expect(out.edges.map((e) => e.target)).toEqual(["b", "c"]);
  });

  it("returns fresh edge objects so force-graph mutation doesn't leak across renders", () => {
    const out1 = applyFilters({ nodes, edges }, DEFAULT_VIEW);
    // Simulate force-graph mutating the first render's links in place.
    (out1.edges[0] as any).source = nodes[0];
    (out1.edges[0] as any).target = nodes[1];
    // Next render: same base inputs (edges still original strings).
    const out2 = applyFilters({ nodes, edges }, DEFAULT_VIEW);
    expect(out2.edges[0].source).toBe("a");
    expect(out2.edges[0].target).toBe("b");
    // And the freshly-returned edges must be different objects.
    expect(out2.edges[0]).not.toBe(out1.edges[0]);
  });

  it("passes through when filters are at defaults", () => {
    const out = applyFilters({ nodes, edges }, DEFAULT_VIEW);
    expect(out.nodes.length).toBe(3);
    expect(out.edges.length).toBe(2);
  });
});

describe("docIdFromUri", () => {
  // The regression this guards: before the canonical-aware rewrite the
  // regex only matched `akb://V/doc/...`, so collection-scoped docs (the
  // common case) returned null — the detail panel never opened and BFS
  // neighbour expansion silently dropped every collection node.
  it("resolves a collection-scoped doc to its full vault-relative path", () => {
    expect(docIdFromUri("akb://gnu/coll/specs/2026/doc/api.md")).toBe("specs/2026/api.md");
  });

  it("resolves a single-segment collection doc", () => {
    expect(docIdFromUri("akb://v/coll/notes/doc/hello.md")).toBe("notes/hello.md");
  });

  it("resolves a vault-root doc (no collection segment)", () => {
    expect(docIdFromUri("akb://v/doc/readme.md")).toBe("readme.md");
  });

  it("resolves the legacy multi-segment root shape (root regex captures full tail)", () => {
    expect(docIdFromUri("akb://v/doc/specs/legacy/api.md")).toBe("specs/legacy/api.md");
  });

  it("returns the bare identifier for tables and files (collection part dropped)", () => {
    expect(docIdFromUri("akb://v/coll/data/table/metrics")).toBe("metrics");
    expect(docIdFromUri("akb://v/table/metrics")).toBe("metrics");
    expect(docIdFromUri("akb://v/coll/assets/file/8f1c.png")).toBe("8f1c.png");
    expect(docIdFromUri("akb://v/file/8f1c.png")).toBe("8f1c.png");
  });

  it("percent-decodes spaces in collection path and leaf", () => {
    expect(docIdFromUri("akb://v/coll/my%20notes/doc/a%20b.md")).toBe("my notes/a b.md");
  });

  it("returns null for vault-only and collection-only URIs", () => {
    expect(docIdFromUri("akb://v")).toBeNull();
    expect(docIdFromUri("akb://v/coll/specs/2026")).toBeNull();
  });

  it("returns null for unparseable input", () => {
    expect(docIdFromUri("not-a-uri")).toBeNull();
    expect(docIdFromUri("")).toBeNull();
  });

  it("round-trips with the docUri builder for nested collection paths", () => {
    for (const path of ["readme.md", "notes/hello.md", "specs/2026/api.md", "a/b/c/d.md"]) {
      expect(docIdFromUri(docUri("v", path))).toBe(path);
    }
  });
});

describe("bfsExpand", () => {
  it("walks depth-1 with one fetch and produces seed neighbors", async () => {
    const fetchRelations = vi.fn(async (_v: string, docId: string) => ({
      doc_id: docId,
      uri: `akb://v/doc/${docId}`,
      relations: [
        {
          direction: "outgoing" as const,
          relation: "depends_on",
          uri: "akb://v/doc/d-2",
          name: "Second",
          resource_type: "document",
        },
      ],
    }));
    const out = await bfsExpand({
      vault: "v",
      entry: "d-1",
      hops: 1,
      fetchRelations,
    });
    expect(fetchRelations).toHaveBeenCalledTimes(1);
    expect(out.nodes.map((n) => n.uri).sort()).toEqual([
      "akb://v/doc/d-1",
      "akb://v/doc/d-2",
    ]);
    expect(out.edges.length).toBe(1);
  });

  // Regression: `links_to` (body markdown / wikilink edges) was missing
  // from normalizeRelation's allowlist, so every body-link edge was
  // silently dropped — the neighbor node appeared but its edge never did,
  // leaving floating nodes with no lines in the graph.
  it("keeps links_to edges (not just the frontmatter relation kinds)", async () => {
    const fetchRelations = vi.fn(async (_v: string, docId: string) => ({
      doc_id: docId,
      uri: `akb://v/doc/${docId}`,
      relations: [
        {
          direction: "outgoing" as const,
          relation: "links_to",
          uri: "akb://v/doc/d-2",
          name: "Linked",
          resource_type: "document",
        },
      ],
    }));
    const out = await bfsExpand({ vault: "v", entry: "d-1", hops: 1, fetchRelations });
    expect(out.edges).toEqual([
      { source: "akb://v/doc/d-1", target: "akb://v/doc/d-2", relation: "links_to" },
    ]);
  });

  it("DEFAULT_VIEW keeps links_to edges through applyFilters", () => {
    const nodes: GraphNode[] = [
      { uri: "akb://v/doc/a", name: "A", kind: "document" },
      { uri: "akb://v/doc/b", name: "B", kind: "document" },
    ];
    const edges: GraphEdge[] = [{ source: "akb://v/doc/a", target: "akb://v/doc/b", relation: "links_to" }];
    const out = applyFilters({ nodes, edges }, DEFAULT_VIEW);
    expect(out.edges).toEqual(edges);
  });

  it("walks depth-2 by following neighbors discovered in hop 1", async () => {
    const calls: string[] = [];
    const fetchRelations = vi.fn(async (_v: string, docId: string) => {
      calls.push(docId);
      if (docId === "d-1") {
        return {
          doc_id: docId,
          uri: "akb://v/doc/d-1",
          relations: [
            {
              direction: "outgoing" as const,
              relation: "depends_on",
              uri: "akb://v/doc/d-2",
              name: "Second",
              resource_type: "document",
            },
          ],
        };
      }
      if (docId === "d-2") {
        return {
          doc_id: docId,
          uri: "akb://v/doc/d-2",
          relations: [
            {
              direction: "outgoing" as const,
              relation: "references",
              uri: "akb://v/doc/d-3",
              name: "Third",
              resource_type: "document",
            },
          ],
        };
      }
      return { doc_id: docId, uri: `akb://v/doc/${docId}`, relations: [] };
    });
    const out = await bfsExpand({
      vault: "v",
      entry: "d-1",
      hops: 2,
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
      uri: `akb://v/doc/${docId}`,
      relations: [
        {
          direction: "outgoing" as const,
          relation: "depends_on",
          uri: "akb://v/doc/d-1", // cycle back to entry
          name: "First",
          resource_type: "document",
        },
      ],
    }));
    await bfsExpand({ vault: "v", entry: "d-1", hops: 3, fetchRelations });
    // d-1 fetched once at hop 0; hop 1 returns d-1 again but it's already visited,
    // so no further fetch.
    expect(fetchRelations).toHaveBeenCalledTimes(1);
  });

  it("returns only the seed when entry has no neighbors", async () => {
    const fetchRelations = vi.fn(async (_v: string, docId: string) => ({
      doc_id: docId,
      uri: `akb://v/doc/${docId}`,
      relations: [],
    }));
    const out = await bfsExpand({
      vault: "v",
      entry: "d-lonely",
      hops: 3,
      fetchRelations,
    });
    expect(fetchRelations).toHaveBeenCalledTimes(1);
    expect(out.nodes.map((n) => n.uri)).toEqual(["akb://v/doc/d-lonely"]);
    expect(out.edges).toEqual([]);
  });

  it("continues past a mid-hop fetch failure and includes the survivors", async () => {
    const fetchRelations = vi.fn(async (_v: string, docId: string) => {
      if (docId === "d-1") {
        return {
          doc_id: docId,
          uri: "akb://v/doc/d-1",
          relations: [
            { direction: "outgoing" as const, relation: "depends_on",
              uri: "akb://v/doc/d-fail", name: "Fail", resource_type: "document" },
            { direction: "outgoing" as const, relation: "depends_on",
              uri: "akb://v/doc/d-ok", name: "OK", resource_type: "document" },
          ],
        };
      }
      if (docId === "d-fail") throw new Error("network down");
      if (docId === "d-ok") {
        return {
          doc_id: docId,
          uri: "akb://v/doc/d-ok",
          relations: [
            { direction: "outgoing" as const, relation: "references",
              uri: "akb://v/doc/d-deep", name: "Deep", resource_type: "document" },
          ],
        };
      }
      return { doc_id: docId, uri: `akb://v/doc/${docId}`, relations: [] };
    });
    const out = await bfsExpand({
      vault: "v",
      entry: "d-1",
      hops: 2,
      fetchRelations,
    });
    // d-1 fetched at hop 0; hop 1 attempts d-fail (rejects) and d-ok (succeeds).
    // The survivor's neighbors (d-deep) appear; the failed branch only contributes
    // the neighbor node that the seed already announced.
    const uris = new Set(out.nodes.map((n) => n.uri));
    expect(uris.has("akb://v/doc/d-1")).toBe(true);
    expect(uris.has("akb://v/doc/d-ok")).toBe(true);
    expect(uris.has("akb://v/doc/d-fail")).toBe(true); // listed as a neighbor by seed
    expect(uris.has("akb://v/doc/d-deep")).toBe(true); // discovered via d-ok in hop 1
  });

  it("collapses two hop-1 nodes sharing the same hop-2 neighbor into one fetch", async () => {
    const seen: string[] = [];
    const fetchRelations = vi.fn(async (_v: string, docId: string) => {
      seen.push(docId);
      if (docId === "d-1") {
        return {
          doc_id: docId,
          uri: "akb://v/doc/d-1",
          relations: [
            { direction: "outgoing" as const, relation: "depends_on",
              uri: "akb://v/doc/d-a", name: "A", resource_type: "document" },
            { direction: "outgoing" as const, relation: "depends_on",
              uri: "akb://v/doc/d-b", name: "B", resource_type: "document" },
          ],
        };
      }
      if (docId === "d-a" || docId === "d-b") {
        return {
          doc_id: docId,
          uri: `akb://v/doc/${docId}`,
          relations: [
            { direction: "outgoing" as const, relation: "references",
              uri: "akb://v/doc/d-shared", name: "Shared", resource_type: "document" },
          ],
        };
      }
      if (docId === "d-shared") {
        return {
          doc_id: docId,
          uri: "akb://v/doc/d-shared",
          relations: [],
        };
      }
      return { doc_id: docId, uri: `akb://v/doc/${docId}`, relations: [] };
    });
    await bfsExpand({ vault: "v", entry: "d-1", hops: 3, fetchRelations });
    // d-shared is announced by both d-a and d-b in hop 1, but the visited set
    // collapses it to a single fetch in hop 2.
    const sharedCalls = seen.filter((id) => id === "d-shared").length;
    expect(sharedCalls).toBe(1);
  });

  it("breaks a longer cycle without re-fetching nodes already seen", async () => {
    const calls: string[] = [];
    const fetchRelations = vi.fn(async (_v: string, docId: string) => {
      calls.push(docId);
      const next = { "A": "B", "B": "C", "C": "A" }[docId];
      if (!next) return { doc_id: docId, uri: `akb://v/doc/${docId}`, relations: [] };
      return {
        doc_id: docId,
        uri: `akb://v/doc/${docId}`,
        relations: [
          { direction: "outgoing" as const, relation: "depends_on",
            uri: `akb://v/doc/${next}`, name: next, resource_type: "document" },
        ],
      };
    });
    await bfsExpand({ vault: "v", entry: "A", hops: 3, fetchRelations });
    // A → B → C → (A — already visited; cycle breaks here, no re-fetch)
    expect(calls.sort()).toEqual(["A", "B", "C"]);
  });
});

describe("endpointUri", () => {
  it("passes a string endpoint through", () => {
    expect(endpointUri("akb://v/doc/a.md")).toBe("akb://v/doc/a.md");
  });
  it("extracts .uri once the sim has mutated the endpoint to a node object", () => {
    expect(endpointUri({ uri: "akb://v/doc/a.md", x: 1, y: 2 })).toBe("akb://v/doc/a.md");
  });
  it("returns '' for a malformed endpoint", () => {
    expect(endpointUri(null)).toBe("");
    expect(endpointUri(42)).toBe("");
  });
});

describe("degreeMap", () => {
  it("counts both endpoints of every edge (undirected degree)", () => {
    const edges: GraphEdge[] = [
      { source: "a", target: "b", relation: "depends_on" },
      { source: "a", target: "c", relation: "references" },
    ];
    const d = degreeMap(edges);
    expect(d.get("a")).toBe(2);
    expect(d.get("b")).toBe(1);
    expect(d.get("c")).toBe(1);
  });
  it("is correct whether endpoints are strings or sim-mutated node objects", () => {
    const edges = [
      { source: { uri: "a" }, target: "b", relation: "depends_on" },
    ] as unknown as GraphEdge[];
    const d = degreeMap(edges);
    expect(d.get("a")).toBe(1);
    expect(d.get("b")).toBe(1);
  });
});

describe("impactCones", () => {
  // a → b → c (depends_on, structural); d → a (structural); b ~ e (references, associative)
  const edges: GraphEdge[] = [
    { source: "a", target: "b", relation: "depends_on" },
    { source: "b", target: "c", relation: "implements" },
    { source: "d", target: "a", relation: "depends_on" },
    { source: "b", target: "e", relation: "references" },
  ];

  it("follows source→target for the dependency (out) cone, transitively", () => {
    const { out } = impactCones(edges, "a");
    expect(out).toEqual(new Set(["b", "c"])); // a depends on b, b depends on c
  });

  it("follows target→source for the dependent (in) cone, transitively", () => {
    const { in: dependents } = impactCones(edges, "c");
    // c ← b ← a ← d : everything whose depends_on chain reaches c.
    expect(dependents).toEqual(new Set(["b", "a", "d"]));
  });

  it("excludes associative (non-structural) edges from the cones", () => {
    const { out } = impactCones(edges, "b");
    expect(out.has("e")).toBe(false); // b → e is `references` (associative)
    expect(out.has("c")).toBe(true); // b → c is structural
  });

  it("excludes the root and terminates on a cycle", () => {
    const cyclic: GraphEdge[] = [
      { source: "x", target: "y", relation: "depends_on" },
      { source: "y", target: "x", relation: "depends_on" },
    ];
    const { out } = impactCones(cyclic, "x");
    expect(out.has("x")).toBe(false); // root excluded
    expect(out).toEqual(new Set(["y"])); // and it returns (no infinite loop)
  });
});
