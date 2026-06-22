import { describe, it, expect } from "vitest";
import {
  apiToPayload,
  mergeGraph,
  applyFilters,
  docIdFromUri,
  endpointUri,
  degreeMap,
  impactCones,
} from "../use-graph-data";
import { docUri } from "@/lib/uri";
import { ALL_RELATIONS, DEFAULT_VIEW, type GraphNode, type GraphEdge } from "../graph-types";

describe("apiToPayload", () => {
  it("maps backend nodes/edges into the renderer shape", () => {
    const out = apiToPayload({
      nodes: [
        { uri: "akb://v/doc/a.md", name: "A", resource_type: "doc" },
        { uri: "akb://v/table/t", name: "", resource_type: "table" },
      ],
      edges: [{ source: "akb://v/doc/a.md", target: "akb://v/table/t", relation: "depends_on" }],
    });
    expect(out.nodes.map((n) => n.kind)).toEqual(["document", "table"]);
    // empty name falls back to the uri
    expect(out.nodes[1].name).toBe("akb://v/table/t");
    expect(out.edges).toEqual([
      { source: "akb://v/doc/a.md", target: "akb://v/table/t", relation: "depends_on" },
    ]);
  });

  it("drops edges whose relation is not in the renderer allowlist", () => {
    const out = apiToPayload({
      nodes: [{ uri: "a", resource_type: "doc" }, { uri: "b", resource_type: "doc" }],
      // `mentions` is not a RelationKind → that edge must be filtered out;
      // `links_to` (implicit body links) must survive — the exact regression
      // the deleted bfsExpand suite used to guard.
      edges: [
        { source: "a", target: "b", relation: "mentions" },
        { source: "a", target: "b", relation: "links_to" },
      ],
    });
    expect(out.edges).toEqual([{ source: "a", target: "b", relation: "links_to" }]);
  });
});

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

  it("carries the BASE payload's meta through an expansion overlay merge", () => {
    const meta = { nodesTotal: 500, edgesTotal: 900, returned: 200, truncated: true };
    const merged = mergeGraph(
      { nodes: [], edges: [], meta },
      { nodes: [{ uri: "n1", name: "A", kind: "document" }], edges: [] },
    );
    // The overlay (b) describes a neighborhood, not the whole vault, so the
    // base (a) totals must survive — that's what "showing N of M" reads.
    expect(merged.meta).toEqual(meta);
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

  it("preserves the overview meta across a filter pass", () => {
    const meta = { nodesTotal: 42, edgesTotal: 99, returned: 30, truncated: true };
    const v = { ...DEFAULT_VIEW, types: new Set<GraphNode["kind"]>(["document"]) };
    const out = applyFilters({ nodes, edges, meta }, v);
    // Filtering hides nodes locally but the vault totals are unchanged, so the
    // honesty banner must keep reading the same "N of M".
    expect(out.meta).toEqual(meta);
  });

  it("keeps links_to (implicit body-link) edges under the default view", () => {
    // Regression guard: ALL_RELATIONS must include `links_to`, or applyFilters
    // silently drops every body/wikilink edge even when the backend returns it.
    expect(ALL_RELATIONS).toContain("links_to" as GraphEdge["relation"]);
    const linkNodes: GraphNode[] = [
      { uri: "a", name: "A", kind: "document" },
      { uri: "b", name: "B", kind: "document" },
    ];
    const linkEdges: GraphEdge[] = [{ source: "a", target: "b", relation: "links_to" }];
    const out = applyFilters({ nodes: linkNodes, edges: linkEdges }, DEFAULT_VIEW);
    expect(out.edges.map((e) => e.relation)).toEqual(["links_to"]);
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
